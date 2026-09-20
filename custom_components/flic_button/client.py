"""Flic-local connection workaround for pyflic-ble 0.2.5.

The upstream connection helper imposes a 20-second service-discovery timeout.
Use the same Bleak transport with an explicit timeout, without changing global
Bluetooth settings. Protocol handling remains in the pinned upstream library.

Connection initialization adapted from pyflic-ble (Shortcut Labs), Apache-2.0.
See LICENSE.pyflic-ble. Local changes: bounded connection stages, cleanup on
failure/cancellation, and suppression of reconnects during initial pairing.
"""

import asyncio
import logging
from time import monotonic
from typing import Any

from bleak import BleakError
from bleak_retry_connector import (
    BleakClientWithServiceCache,
    close_stale_connections_by_address,
)
from pyflic_ble import FlicClient as BaseFlicClient
from pyflic_ble import FlicProtocolError
from pyflic_ble.client import SessionState

_LOGGER = logging.getLogger(__name__)
CONNECT_TIMEOUT = 60
NOTIFY_TIMEOUT = 15
CLEANUP_TIMEOUT = 10


class FlicClient(BaseFlicClient):
    """Keep initial pairing separate from automatic session reconnection."""

    def __init__(self, *args: Any, pairing_only: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pairing_only = pairing_only
        self._connecting = False
        self._connect_lock = asyncio.Lock()

    def _schedule_reconnect(self) -> None:
        if self._pairing_only or self._connecting or self._stopped:
            return
        super()._schedule_reconnect()

    async def connect(self) -> None:
        """Connect and discover services with enough time for slower adapters."""
        async with self._connect_lock:
            if self._stopped:
                raise FlicProtocolError("Flic client has been stopped")
            if (
                self._firmware_update_active
                or self.address in BaseFlicClient._firmware_update_addresses
            ):
                raise FlicProtocolError("Firmware update in progress")
            if self._client and self._client.is_connected:
                return
            if self.ble_device is None:
                raise FlicProtocolError(f"No BLE device available for {self.address}")

            self._connecting = True
            started = monotonic()
            stage = "connection cleanup"
            ready = False
            try:
                async with asyncio.timeout(CLEANUP_TIMEOUT):
                    await self.disconnect()
                    await close_stale_connections_by_address(self.address)
                self._intentional_disconnect = False
                if self.address in BaseFlicClient._firmware_update_addresses:
                    raise FlicProtocolError("Firmware update started during cleanup")
                while not self._response_queue.empty():
                    self._response_queue.get_nowait()
                self._fragment_buffer = bytearray()
                self._expecting_fragment = False

                stage = "BLE connection and service discovery"
                _LOGGER.info(
                    "%s: %s (timeout %ss)", self.address, stage, CONNECT_TIMEOUT
                )
                # Retains Home Assistant's Bleak backend/adapter selection. Avoid
                # establish_connection(), which overrides the timeout to 20s.
                self._client = BleakClientWithServiceCache(
                    self.ble_device,
                    disconnected_callback=self._handle_disconnected,
                    timeout=CONNECT_TIMEOUT,
                )
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    await self._client.connect(timeout=CONNECT_TIMEOUT)
                _LOGGER.info(
                    "%s: services discovered after %.1fs",
                    self.address,
                    monotonic() - started,
                )

                if self.address in BaseFlicClient._firmware_update_addresses:
                    raise FlicProtocolError("Firmware update started during connection")
                stage = "notification subscription"
                async with asyncio.timeout(NOTIFY_TIMEOUT):
                    await self._request_connection_parameters()
                    await self._client.start_notify(
                        self._handler.notify_char_uuid, self._notification_handler
                    )
                self._state = SessionState.CONNECTED
                self._connection_id = 0
                self._handler.connection_id = 0
                self._packet_counter_to_button = 0
                self._packet_counter_from_button = 0
                self._handler.bind_transport(
                    write_gatt=self._write_gatt,
                    write_packet=self._write_packet,
                    wait_for_opcode=self._wait_for_handler_opcode,
                    wait_for_opcodes=self._wait_for_handler_opcodes,
                )
                await asyncio.sleep(0.5)
                if not self._client.is_connected:
                    raise FlicProtocolError("Disconnected before authentication")
                ready = True
                _LOGGER.info(
                    "%s: ready for authentication after %.1fs",
                    self.address,
                    monotonic() - started,
                )
            except (TimeoutError, BleakError) as err:
                raise FlicProtocolError(
                    f"{stage} failed after {monotonic() - started:.1f}s: "
                    f"{type(err).__name__}: {err}"
                ) from err
            finally:
                try:
                    if not ready:
                        async with asyncio.timeout(CLEANUP_TIMEOUT):
                            await self.disconnect()
                except (TimeoutError, BleakError) as err:
                    _LOGGER.warning(
                        "%s: connection cleanup failed: %s", self.address, err
                    )
                finally:
                    self._connecting = False

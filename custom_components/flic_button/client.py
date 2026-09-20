"""Flic-local connection workaround for pyflic-ble 0.2.5.

The upstream connection helper imposes a 20-second service-discovery timeout.
Use the same Bleak transport with an explicit timeout, without changing global
Bluetooth settings. Protocol handling remains in the pinned upstream library,
apart from correcting its Flic 2/Duo auto-disconnect field before signing.

Connection initialization adapted from pyflic-ble (Shortcut Labs), Apache-2.0.
See LICENSE.pyflic-ble. Local changes: bounded connection stages, cleanup on
failure/cancellation, and suppression of reconnects during initial pairing.
The init-event packet correction is local to this integration, not a global patch.
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
from pyflic_ble import DeviceType, FlicProtocolError
from pyflic_ble import FlicClient as BaseFlicClient
from pyflic_ble.client import SessionState
from pyflic_ble.const import (
    OPCODE_INIT_BUTTON_EVENTS_DUO_REQUEST,
    OPCODE_INIT_BUTTON_EVENTS_REQUEST,
)

_LOGGER = logging.getLogger(__name__)
CONNECT_TIMEOUT = 60
NOTIFY_TIMEOUT = 15
CLEANUP_TIMEOUT = 10
# Pinned pyflic-ble 0.2.5 layouts: opcode and byte offset of the packed field.
# Includes the one-byte frame header. Twist has a different protocol.
_INIT_EVENT_LAYOUTS = {
    DeviceType.FLIC2: (OPCODE_INIT_BUTTON_EVENTS_REQUEST, 10),
    DeviceType.DUO: (OPCODE_INIT_BUTTON_EVENTS_DUO_REQUEST, 14),
}


class FlicClient(BaseFlicClient):
    """Keep initial pairing separate from automatic session reconnection."""

    def __init__(self, *args: Any, pairing_only: bool = False, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._pairing_only = pairing_only
        self._connecting = False
        self._connect_lock = asyncio.Lock()

    async def _write_packet(self, data: bytes, authenticated: bool = True) -> None:
        """Disable idle disconnect in Flic 2/Duo init packets before MAC signing.

        pyflic-ble 0.2.5 sends zero, but the protocol specifies 511 (all nine
        bits set) for disabled auto-disconnect. Preserve all adjacent fields,
        other commands, pairing packets and the entire Twist protocol.
        https://github.com/50ButtonsEach/flic2-documentation/wiki/
        Flic-2-Protocol-Specification#init-button-events
        """
        layout = _INIT_EVENT_LAYOUTS.get(self.device_type)
        if authenticated and layout is not None and len(data) >= 2:
            opcode, offset = layout
            if data[1] == opcode:
                if len(data) != offset + 8:
                    raise FlicProtocolError("Unexpected init-event packet layout")
                packet = bytearray(data)
                packet[offset] = 0xFF
                packet[offset + 1] |= 0x01
                data = bytes(packet)
        await super()._write_packet(data, authenticated)

    def _schedule_reconnect(self) -> None:
        if self._pairing_only or self._connecting or self._stopped:
            return
        super()._schedule_reconnect()

    async def _start_inner(self) -> None:
        """Leave no physical connection behind after an incomplete session.

        Upstream's retry loop checks the BLE link, not session authentication.
        A quick-verify timeout otherwise leaves is_connected true and stops the
        loop while all event entities are still unavailable.
        """
        try:
            await super()._start_inner()
        except BaseException:
            self._flic_state.connected = False
            try:
                async with asyncio.timeout(CLEANUP_TIMEOUT):
                    await self.disconnect()
            except (TimeoutError, BleakError) as err:
                _LOGGER.warning("%s: session cleanup failed: %s", self.address, err)
            finally:
                self._notify_state_callbacks()
            raise

    async def stop(self) -> None:
        """Wait for the background attempt to finish before releasing the client."""
        self._stopped = True
        task = self._reconnect_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._reconnect_task = None
        await super().stop()

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

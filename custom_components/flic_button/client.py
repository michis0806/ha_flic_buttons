"""Flic-local connection workaround for pyflic-ble 0.2.5.

The upstream connection helper imposes a 20-second service-discovery timeout.
Use the same Bleak transport with an explicit timeout, without changing global
Bluetooth settings. Protocol handling uses the pinned upstream library with local Flic 2/Duo
fixes for idle disconnect, session controls and reconnect advertising.

Connection initialization adapted from pyflic-ble (Shortcut Labs), Apache-2.0.
See LICENSE.pyflic-ble. Local changes: bounded connection stages, cleanup on
failure/cancellation, and suppression of reconnects during initial pairing.
The init-event packet correction is local to this integration, not a global patch.
"""

import asyncio
import logging
import struct
from collections.abc import Coroutine
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
    FRAME_HEADER_CONN_ID_MASK,
    FRAME_HEADER_FRAGMENT_FLAG,
    OPCODE_INIT_BUTTON_EVENTS_DUO_REQUEST,
    OPCODE_INIT_BUTTON_EVENTS_DUO_RESPONSE_WITH_BOOT_ID,
    OPCODE_INIT_BUTTON_EVENTS_DUO_RESPONSE_WITHOUT_BOOT_ID,
    OPCODE_INIT_BUTTON_EVENTS_REQUEST,
    OPCODE_INIT_BUTTON_EVENTS_RESPONSE_WITH_BOOT_ID,
    OPCODE_INIT_BUTTON_EVENTS_RESPONSE_WITHOUT_BOOT_ID,
)

from .connection_diagnostics import ConnectionDiagnostics

_LOGGER = logging.getLogger(__name__)
CONNECT_TIMEOUT = 60
NOTIFY_TIMEOUT = 15
CLEANUP_TIMEOUT = 10
# Flic 2 protocol directions have separate opcode namespaces.
_SET_ADV_PARAMETERS_REQUEST = 27
# The Android SDK uses response opcode 25; the older wiki lists 27.
_SET_ADV_PARAMETERS_RESPONSE = 25
_PING_REQUEST = 15
_PING_RESPONSE = 14
_DISCONNECTED_VERIFIED_LINK = 9
_INIT_RESPONSES = {
    OPCODE_INIT_BUTTON_EVENTS_RESPONSE_WITH_BOOT_ID: 16,
    OPCODE_INIT_BUTTON_EVENTS_RESPONSE_WITHOUT_BOOT_ID: 12,
    OPCODE_INIT_BUTTON_EVENTS_DUO_RESPONSE_WITH_BOOT_ID: 20,
    OPCODE_INIT_BUTTON_EVENTS_DUO_RESPONSE_WITHOUT_BOOT_ID: 16,
}
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
        self._write_lock = asyncio.Lock()
        self._session_tasks: set[asyncio.Task[None]] = set()
        self._session_generation = 0
        self._init_response_received = False
        self._boot_id: int | None = None
        self._advertising_boot_id: int | None = None

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
        # A ping response must not interleave a fragmented protocol write.
        async with self._write_lock:
            if authenticated and self._state != SessionState.SESSION_ESTABLISHED:
                raise FlicProtocolError("Flic session is no longer established")
            await super()._write_packet(data, authenticated)

    def _schedule_reconnect(self) -> None:
        if self._pairing_only or self._connecting or self._stopped:
            return
        super()._schedule_reconnect()

    async def _start_inner(self) -> None:
        """Publish availability only after a complete, live Flic session."""
        self._starting = True
        try:
            await self.connect()
            await self.quick_verify()
            await self.init_button_events()
            await self._send_connection_parameters()
            # These diagnostics are optional, as in the upstream client.
            for getter, attribute in (
                (self.get_battery_voltage, "battery_voltage"),
                (self.get_firmware_version, "firmware_version"),
            ):
                try:
                    setattr(self._flic_state, attribute, await getter())
                except Exception:  # noqa: BLE001 - optional upstream diagnostics
                    _LOGGER.warning("%s: could not read %s", self.address, attribute)
            await self._configure_reconnect_advertising()
            try:
                name, _ = await self.get_name()
                self._flic_state.device_name = name or None
            except Exception:  # noqa: BLE001 - optional upstream diagnostics
                _LOGGER.debug("%s: could not read device name", self.address)
            if not self.is_connected or self._state != SessionState.SESSION_ESTABLISHED:
                raise FlicProtocolError("Flic session ended during initialization")
            self._flic_state.connected = True
            self._notify_state_callbacks()
            _LOGGER.info("Successfully started session with %s", self.address)
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
        finally:
            self._starting = False

    async def init_button_events(self) -> None:
        """Require an acknowledgement even if upstream's outer timeout wins."""
        self._init_response_received = False
        await super().init_button_events()
        if self.device_type in _INIT_EVENT_LAYOUTS and not self._init_response_received:
            raise FlicProtocolError("Flic event initialization was not acknowledged")

    async def _configure_reconnect_advertising(self) -> None:
        """Use the SDK's 40 ms / 1022.5 ms, 24 hour reconnect window.

        Configure only our pairing, on LE 1M PHY. Firmware before version 7
        does not support this request. Reapply after a button or HA reboot.
        See the official Flic 2 protocol's Changing Advertising Parameters.
        """
        if self.device_type not in _INIT_EVENT_LAYOUTS:
            return
        firmware = self._flic_state.firmware_version
        if firmware is None or firmware < 7:
            _LOGGER.debug(
                "%s: advertising settings unavailable for firmware %s",
                self.address,
                firmware,
            )
            return
        if self._boot_id is not None and self._advertising_boot_id == self._boot_id:
            return
        request = struct.pack(
            "<BBBBBBHHI",
            self._connection_id,
            _SET_ADV_PARAMETERS_REQUEST,
            1,
            0,
            1,
            0,
            64,
            1636,
            86400,
        )
        await self._write_packet(request)
        await self._wait_for_handler_opcode(_SET_ADV_PARAMETERS_RESPONSE)
        self._advertising_boot_id = self._boot_id
        _LOGGER.info("%s: reconnect advertising configured", self.address)

    async def _wait_for_handler_opcodes(self, opcodes: list[int]) -> bytes:
        """Do not let upstream swallow an event-initialization timeout."""
        try:
            response = await super()._wait_for_handler_opcodes(opcodes)
        except TimeoutError as err:
            if (
                self.device_type in _INIT_EVENT_LAYOUTS
                and set(opcodes) & _INIT_RESPONSES.keys()
            ):
                raise FlicProtocolError("Flic event initialization timed out") from err
            raise
        if self.device_type in _INIT_EVENT_LAYOUTS and len(response) >= 2:
            opcode = response[1]
            if opcode in _INIT_RESPONSES:
                if len(response) < _INIT_RESPONSES[opcode]:
                    raise FlicProtocolError(
                        "Truncated Flic event initialization response"
                    )
                self._init_response_received = True
                if opcode in (
                    OPCODE_INIT_BUTTON_EVENTS_RESPONSE_WITH_BOOT_ID,
                    OPCODE_INIT_BUTTON_EVENTS_DUO_RESPONSE_WITH_BOOT_ID,
                ):
                    offset = _INIT_RESPONSES[opcode] - 4
                    self._boot_id = struct.unpack_from("<I", response, offset)[0]
        return response

    def _handle_framed_notification(self, data: bytearray) -> None:
        """Consume session controls after reassembly, before upstream queueing.

        Pairing/quick-verify and normal events still use the pinned upstream
        parser. Like that parser, this retains its incoming-signature policy.
        """
        if self._state != SessionState.SESSION_ESTABLISHED:
            if self._state != SessionState.FAILED:
                super()._handle_framed_notification(data)
            return
        if not data or data[0] & FRAME_HEADER_CONN_ID_MASK != self._connection_id:
            return
        # The upstream parser uses this same buffer for fragmented frames.
        if data[0] & FRAME_HEADER_FRAGMENT_FLAG:
            self._fragment_buffer.extend(data[1:])
            self._expecting_fragment = True
            return
        if self._expecting_fragment:
            data = bytearray([data[0]]) + self._fragment_buffer + data[1:]
            self._fragment_buffer = bytearray()
            self._expecting_fragment = False
        if len(data) < 7:  # header, opcode, five signature bytes
            return
        opcode = data[1]
        if opcode == _PING_REQUEST:
            self._create_session_task(self._answer_ping(self._session_generation))
        elif opcode == _DISCONNECTED_VERIFIED_LINK:
            if len(data) < 8:
                return
            _LOGGER.warning(
                "%s: Flic session terminated (reason=%d)", self.address, data[2]
            )
            self._fail_session()
        else:
            super()._handle_framed_notification(data)

    def _create_session_task(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._session_tasks.add(task)
        task.add_done_callback(self._session_tasks.discard)

    async def _answer_ping(self, generation: int) -> None:
        if generation != self._session_generation or self._stopped:
            return
        try:
            async with asyncio.timeout(NOTIFY_TIMEOUT):
                await self._write_packet(bytes([self._connection_id, _PING_RESPONSE]))
        except (TimeoutError, BleakError, FlicProtocolError):
            if generation == self._session_generation and not self._stopped:
                _LOGGER.warning("%s: Flic ping response failed", self.address)
                self._fail_session()

    def _fail_session(self) -> None:
        if self._state == SessionState.FAILED or self._stopped:
            return
        self._state = SessionState.FAILED
        self._flic_state.connected = False
        self._notify_state_callbacks()
        self._create_session_task(self._recover_session(self._session_generation))

    async def _recover_session(self, generation: int) -> None:
        # A startup already in progress owns its cleanup and retry. Waiting
        # on its lock also prevents recovery from disconnecting a new session.
        async with self._reconnect_lock:
            if generation != self._session_generation or self._stopped:
                return
            try:
                async with asyncio.timeout(CLEANUP_TIMEOUT):
                    await self.disconnect()
            except (TimeoutError, BleakError):
                _LOGGER.warning("%s: failed to close terminated session", self.address)
            self._schedule_reconnect()

    async def disconnect(self) -> None:
        """Cancel control tasks before releasing or replacing the transport."""
        self._session_generation += 1
        tasks = [
            task for task in self._session_tasks if task is not asyncio.current_task()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await super().disconnect()

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
            diagnostics = None
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
                # Opt-in INFO observations only. No protocol DEBUG logging,
                # scanning, adapter changes or extra connection attempts.
                if _LOGGER.isEnabledFor(logging.INFO):
                    diagnostics = ConnectionDiagnostics(
                        self.address, self._client, _LOGGER
                    )
                    diagnostics.start()
                async with asyncio.timeout(CONNECT_TIMEOUT):
                    await self._client.connect(timeout=CONNECT_TIMEOUT)
                _LOGGER.info(
                    "%s: services discovered after %.1fs",
                    self.address,
                    monotonic() - started,
                )

                if self.address in BaseFlicClient._firmware_update_addresses:
                    raise FlicProtocolError("Firmware update started during connection")
                stage = "connection parameter request"
                async with asyncio.timeout(NOTIFY_TIMEOUT):
                    await self._request_connection_parameters()
                    stage = "notification subscription"
                    _LOGGER.info("%s: subscribing to notifications", self.address)
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
                observation = f"; {diagnostics.summary}" if diagnostics else ""
                raise FlicProtocolError(
                    f"{stage} failed after {monotonic() - started:.1f}s: "
                    f"{type(err).__name__}: {err}{observation}"
                ) from err
            finally:
                if diagnostics is not None:
                    await diagnostics.stop()
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

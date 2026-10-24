"""Read-only, opt-in observations of a pending Bleak connection.

Only connection flags are logged, never device details, keys or GATT payloads.
BlueZ observations use a separate D-Bus connection, not Bleak's property cache.
"""

import asyncio
import logging
import re
import sys
from time import monotonic
from typing import Any

SAMPLE_INTERVAL = 1
READ_TIMEOUT = 1


def _backend_path(transport: Any, address: str) -> tuple[str, str | None]:
    """Inspect pinned Bleak/HA wrapper metadata without changing the backend.

    HA can select a different adapter from the original BLEDevice, so never
    assume that the original device's path is the actual connection target.
    If backend internals change or a remote proxy is selected, report unknown.
    """
    backend = transport
    seen = set()
    for _ in range(4):
        if id(backend) in seen:
            break
        seen.add(id(backend))
        child = getattr(backend, "_backend", None)
        if child is None:
            break
        backend = child
    name = type(backend).__name__
    path = getattr(backend, "_device_path", None)
    expected = rf"/org/bluez/hci[0-9]+/dev_{re.escape(address.replace(':', '_'))}"
    if isinstance(path, str) and re.fullmatch(expected, path, re.IGNORECASE):
        return name, path
    return name, None


class ConnectionDiagnostics:
    """Observe a single attempt; failures must never affect the real connection."""

    def __init__(self, address: str, transport: Any, logger: logging.Logger) -> None:
        self.address = address
        self.transport = transport
        self.logger = logger
        self._task: asyncio.Task | None = None
        self._bus: Any = None
        self._started = monotonic()
        self._last_snapshot: str | None = None
        self._link_seen = False
        self._services_seen = False

    @property
    def summary(self) -> str:
        """Describe observations, not a definitive cause or current link state."""
        return (
            f"connection observations: {self._last_snapshot or 'no sample collected'}; "
            f"link_seen={self._link_seen}, services_resolved_seen={self._services_seen}"
        )

    def start(self) -> None:
        self._task = asyncio.create_task(
            self._run(), name=f"flic-connect-diagnostics-{self.address}"
        )

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def _read_bluez(self, path: str) -> tuple[bool | None, bool | None]:
        # dbus-fast is supplied by HA's Bluetooth dependency on Linux. Import
        # lazily so non-BlueZ/remote backends need no system-bus access at all.
        from dbus_fast import BusType, Message, MessageType
        from dbus_fast.aio import MessageBus

        if self._bus is None:
            self._bus = MessageBus(bus_type=BusType.SYSTEM)
            await self._bus.connect()
        reply = await self._bus.call(
            Message(
                destination="org.bluez",
                path=path,
                interface="org.freedesktop.DBus.Properties",
                member="GetAll",
                signature="s",
                body=["org.bluez.Device1"],
            )
        )
        if reply is None or reply.message_type != MessageType.METHOD_RETURN:
            raise RuntimeError("BlueZ properties unavailable")
        props = reply.body[0]
        values = []
        for key in ("Connected", "ServicesResolved"):
            value = props[key].value if key in props else None
            values.append(value if isinstance(value, bool) else None)
        return values[0], values[1]

    async def _sample(self) -> str:
        backend, path = _backend_path(self.transport, self.address)
        bleak_connected = bool(self.transport.is_connected)
        connected = resolved = None
        status = "not a local BlueZ target"
        if path is not None and sys.platform == "linux":
            try:
                async with asyncio.timeout(READ_TIMEOUT):
                    connected, resolved = await self._read_bluez(path)
                status = "read"
            except Exception as err:  # noqa: BLE001 - diagnostics must be non-fatal
                # Do not log exception bodies: they may contain device data.
                status = type(err).__name__
        self._link_seen |= bleak_connected or connected is True
        self._services_seen |= resolved is True
        return (
            f"backend={backend}, path={path or 'unknown'}, "
            f"bleak_connected={bleak_connected}, bluez_connected={connected}, "
            f"services_resolved={resolved}, bluez_status={status}"
        )

    async def _run(self) -> None:
        try:
            while True:
                snapshot = await self._sample()
                if snapshot != self._last_snapshot:
                    self._last_snapshot = snapshot
                    self.logger.info(
                        "%s: connection observation after %.1fs: %s",
                        self.address, monotonic() - self._started, snapshot,
                    )
                await asyncio.sleep(SAMPLE_INTERVAL)
        except Exception as err:  # noqa: BLE001 - never affect the real connection
            self.logger.info(
                "%s: connection observations unavailable (%s)",
                self.address, type(err).__name__,
            )
        finally:
            if self._bus is not None:
                try:
                    self._bus.disconnect()
                except Exception as err:  # noqa: BLE001 - best-effort cleanup
                    self.logger.info(
                        "%s: diagnostic bus cleanup failed (%s)",
                        self.address, type(err).__name__,
                    )
                self._bus = None

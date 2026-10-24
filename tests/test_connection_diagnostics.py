"""Connection observations are read-only, bounded and safe to log."""

import asyncio
import importlib.util
import logging
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from dbus_fast import MessageType, Variant

PATH = "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF"
ADDRESS = "AA:BB:CC:DD:EE:FF"
SOURCE = (
    Path(__file__).resolve().parents[1]
    / "custom_components/flic_button/connection_diagnostics.py"
)
spec = importlib.util.spec_from_file_location("flic_diagnostics_test", SOURCE)
diagnostics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diagnostics)


def observer(path=PATH):
    backend = types.SimpleNamespace(_device_path=path)
    transport = types.SimpleNamespace(_backend=backend, is_connected=False)
    return diagnostics.ConnectionDiagnostics(
        ADDRESS, transport, logging.getLogger("flic_diagnostics_test")
    )


@pytest.fixture
def bus(monkeypatch):
    import dbus_fast.aio

    instance = types.SimpleNamespace(
        connect=AsyncMock(),
        disconnect=Mock(),
        call=AsyncMock(return_value=types.SimpleNamespace(
            message_type=MessageType.METHOD_RETURN,
            body=[{
                "Connected": Variant("b", False),
                "ServicesResolved": Variant("b", False),
                "ManufacturerData": Variant("s", "NEVER_LOG_THIS_PAYLOAD"),
            }],
        )),
    )
    monkeypatch.setattr(dbus_fast.aio, "MessageBus", Mock(return_value=instance))
    monkeypatch.setattr(diagnostics.sys, "platform", "linux")
    monkeypatch.setattr(diagnostics, "SAMPLE_INTERVAL", 0.001)
    return instance


@pytest.mark.asyncio
async def test_reads_only_target_flags_and_closes_bus(bus, caplog):
    caplog.set_level(logging.INFO, logger="flic_diagnostics_test")
    diag = observer()
    diag.start()
    try:
        async with asyncio.timeout(1):
            while diag._last_snapshot is None:
                await asyncio.sleep(0)
    finally:
        await diag.stop()
    bus.disconnect.assert_called_once()
    for call in bus.call.call_args_list:
        message = call.args[0]
        assert message.member == "GetAll"
        assert message.path == PATH
        assert message.interface == "org.freedesktop.DBus.Properties"
        assert message.body == ["org.bluez.Device1"]
    assert "bluez_connected=False" in caplog.text
    assert "services_resolved=False" in caplog.text
    assert "NEVER_LOG_THIS_PAYLOAD" not in caplog.text
    assert diag._task is None


@pytest.mark.asyncio
async def test_records_link_then_services_even_if_link_later_drops(monkeypatch):
    diag = observer()
    monkeypatch.setattr(diagnostics.sys, "platform", "linux")
    diag._read_bluez = AsyncMock(side_effect=[
        (False, False), (True, False), (True, True), (False, False),
    ])
    assert "bluez_connected=False" in await diag._sample()
    assert "link_seen=False" in diag.summary
    assert "services_resolved=False" in await diag._sample()
    assert "link_seen=True" in diag.summary
    assert "services_resolved_seen=False" in diag.summary
    assert "services_resolved=True" in await diag._sample()
    await diag._sample()
    assert "link_seen=True" in diag.summary
    assert "services_resolved_seen=True" in diag.summary


@pytest.mark.asyncio
@pytest.mark.parametrize("path", [
    None,
    "/org/bluez/hci0/dev_11_22_33_44_55_66",
    "/arbitrary/path/NEVER_LOG_THIS_PAYLOAD",
])
async def test_unknown_or_wrong_backend_path_never_reads_bus(path):
    diag = observer(path)
    diag._read_bluez = AsyncMock()
    snapshot = await diag._sample()
    diag._read_bluez.assert_not_awaited()
    assert "path=unknown" in snapshot
    assert "bluez_connected=None" in snapshot
    assert "NEVER_LOG_THIS_PAYLOAD" not in snapshot


def test_nested_backend_uses_selected_adapter_not_original_device():
    selected = types.SimpleNamespace(
        _device_path="/org/bluez/hci2/dev_AA_BB_CC_DD_EE_FF"
    )
    transport = types.SimpleNamespace(_backend=types.SimpleNamespace(_backend=selected))
    assert diagnostics._backend_path(transport, ADDRESS)[1] == selected._device_path
    selected._backend = transport  # Defensive against cyclic wrapper metadata.
    diagnostics._backend_path(transport, ADDRESS)


@pytest.mark.asyncio
async def test_non_linux_backend_remains_unknown_without_bus(monkeypatch):
    diag = observer()
    diag._read_bluez = AsyncMock()
    monkeypatch.setattr(diagnostics.sys, "platform", "darwin")
    assert "bluez_connected=None" in await diag._sample()
    diag._read_bluez.assert_not_awaited()


@pytest.mark.asyncio
async def test_reader_error_does_not_leak_exception_body(bus, caplog):
    caplog.set_level(logging.INFO, logger="flic_diagnostics_test")
    bus.call.side_effect = RuntimeError("SECRET_PAIRING_KEY")
    diag = observer()
    diag.start()
    try:
        async with asyncio.timeout(1):
            while diag._last_snapshot is None:
                await asyncio.sleep(0)
    finally:
        await diag.stop()
    assert "bluez_status=RuntimeError" in caplog.text
    assert "SECRET_PAIRING_KEY" not in caplog.text
    bus.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_bus_read_timeout_is_bounded_and_cleanup_cancels_observer(bus, monkeypatch):
    monkeypatch.setattr(diagnostics, "READ_TIMEOUT", 0.01)

    async def hang(*_args):
        await asyncio.Event().wait()

    bus.call.side_effect = hang
    diag = observer()
    diag.start()
    task = diag._task
    try:
        async with asyncio.timeout(1):
            while diag._last_snapshot is None:
                await asyncio.sleep(0)
        assert "bluez_status=TimeoutError" in diag.summary
    finally:
        await diag.stop()
    assert task.done()
    bus.disconnect.assert_called_once()


@pytest.mark.asyncio
async def test_stopping_during_bus_connect_releases_bus(bus):
    entered = asyncio.Event()

    async def hang():
        entered.set()
        await asyncio.Event().wait()

    bus.connect.side_effect = hang
    diag = observer()
    diag.start()
    await asyncio.wait_for(entered.wait(), 1)
    await asyncio.wait_for(diag.stop(), 1)
    bus.disconnect.assert_called_once()
    bus.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_backend_observation_error_is_contained(caplog):
    caplog.set_level(logging.INFO, logger="flic_diagnostics_test")
    diag = observer()
    diag._sample = AsyncMock(side_effect=RuntimeError("SECRET_PAIRING_KEY"))
    diag.start()
    await asyncio.wait_for(diag._task, 1)
    await diag.stop()
    assert "observations unavailable (RuntimeError)" in caplog.text
    assert "SECRET_PAIRING_KEY" not in caplog.text

"""Startup/reconnect lifecycle with real pyflic and fake Home Assistant/BLE I/O."""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from bleak import BleakError
from bleak.backends.device import BLEDevice
from pyflic_ble import DeviceType

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "flic_button"
ADDRESS = "AA:BB:CC:DD:EE:FF"


@pytest.fixture
def runtime(monkeypatch):
    def module(name, **values):
        mod = types.ModuleType(name)
        mod.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    module("homeassistant")
    module("homeassistant.components")
    bluetooth = module(
        "homeassistant.components.bluetooth",
        BluetoothServiceInfoBleak=object,
        BluetoothChange=object,
        BluetoothScanningMode=types.SimpleNamespace(ACTIVE="active"),
        async_ble_device_from_address=Mock(return_value=None),
        async_register_callback=Mock(return_value=Mock()),
    )
    module("homeassistant.components.bluetooth.match", BluetoothCallbackMatcher=dict)
    module("homeassistant.config_entries", ConfigEntry=object)
    module(
        "homeassistant.const",
        CONF_ADDRESS="address",
        Platform=types.SimpleNamespace(EVENT="event", SENSOR="sensor"),
    )
    module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    module("homeassistant.helpers")
    registry = types.SimpleNamespace(
        async_get_device_by_identifier=Mock(return_value=None),
        async_update_device=Mock(),
    )
    module("homeassistant.helpers.device_registry", async_get=lambda hass: registry)
    for name in ("flic_runtime_test.client", "flic_runtime_test.const"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(
        "flic_runtime_test", ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, mod)
    spec.loader.exec_module(mod)
    return mod, bluetooth


@pytest.fixture
def setup_data():
    removers = []
    entry = types.SimpleNamespace(
        data={
            "address": ADDRESS.lower(),
            "pairing_id": 123,
            "pairing_key": "00" * 16,
            "device_type": DeviceType.FLIC2.value,
            "serial_number": "BA00-A00000",
        },
        options={},
        entry_id="test_entry",
        async_on_unload=removers.append,
        add_update_listener=Mock(return_value=Mock()),
    )
    hass = types.SimpleNamespace(
        config_entries=types.SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
            async_reload=AsyncMock(),
        )
    )
    return hass, entry, removers


def advertisement(connectable=True):
    return types.SimpleNamespace(
        device=BLEDevice(ADDRESS, "Test Flic", {}), connectable=connectable
    )


def callback_for(bluetooth):
    return bluetooth.async_register_callback.call_args.args[1]


@pytest.mark.asyncio
async def test_cached_device_never_blocks_setup(runtime, setup_data, monkeypatch):
    mod, bluetooth = runtime
    hass, entry, _ = setup_data
    bluetooth.async_ble_device_from_address.return_value = advertisement().device
    entered = asyncio.Event()

    async def hang(client):
        hass.config_entries.async_forward_entry_setups.assert_awaited_once()
        bluetooth.async_register_callback.assert_called_once()
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(mod.FlicClient, "_start_inner", hang)
    assert await asyncio.wait_for(mod.async_setup_entry(hass, entry), 1)
    client = entry.runtime_data.client
    await asyncio.wait_for(entered.wait(), 1)
    assert not client.state.connected
    assert not client._reconnect_task.done()
    assert bluetooth.async_register_callback.call_args.args[2] == {
        "address": ADDRESS,
        "connectable": True,
    }
    await mod.async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_offline_setup_then_fresh_advertisement_connects(runtime, setup_data):
    mod, bluetooth = runtime
    hass, entry, _ = setup_data
    assert await mod.async_setup_entry(hass, entry)
    client = entry.runtime_data.client
    assert client._reconnect_task is None
    assert not client.state.connected

    async def connect_session():
        client._flic_state.connected = True
        client._notify_state_callbacks()

    client._start_inner = AsyncMock(side_effect=connect_session)
    callback_for(bluetooth)(advertisement(), None)
    await asyncio.wait_for(client._reconnect_task, 1)
    assert client.state.connected
    client._start_inner.assert_awaited_once()
    await mod.async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_passive_receiver_cannot_start_connection(runtime, setup_data):
    mod, bluetooth = runtime
    hass, entry, _ = setup_data
    await mod.async_setup_entry(hass, entry)
    callback_for(bluetooth)(advertisement(connectable=False), None)
    assert entry.runtime_data.client._reconnect_task is None
    await mod.async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_failed_start_recovers_on_advertisement_without_reload(
    runtime, setup_data
):
    mod, bluetooth = runtime
    hass, entry, _ = setup_data
    await mod.async_setup_entry(hass, entry)
    client = entry.runtime_data.client
    failed = asyncio.Event()

    async def connect():
        if client.connect.await_count <= 2:
            if client.connect.await_count == 2:
                failed.set()
            raise BleakError("out of range")
        client._client = types.SimpleNamespace(
            is_connected=True, disconnect=AsyncMock()
        )

    client.connect = AsyncMock(side_effect=connect)
    client.quick_verify = AsyncMock()
    client.init_button_events = AsyncMock()
    client._send_connection_parameters = AsyncMock()
    client.get_battery_voltage = AsyncMock(return_value=3.1)
    client.get_firmware_version = AsyncMock(return_value=11)
    client.get_name = AsyncMock(return_value=("Flic", 0))
    callback_for(bluetooth)(advertisement(), None)
    task = client._reconnect_task
    await asyncio.wait_for(failed.wait(), 1)
    await asyncio.sleep(0)
    callback_for(bluetooth)(advertisement(), None)
    assert client._reconnect_task is task
    await asyncio.wait_for(task, 1)
    assert client.state.connected
    assert client.state.battery_voltage == 3.1
    assert client.connect.await_count == 3
    hass.config_entries.async_forward_entry_setups.assert_awaited_once()
    await mod.async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_duplicate_signals_and_unload_do_not_leak_tasks(runtime, setup_data):
    mod, bluetooth = runtime
    hass, entry, removers = setup_data
    await mod.async_setup_entry(hass, entry)
    client = entry.runtime_data.client
    entered = asyncio.Event()
    cleaned = asyncio.Event()

    async def hang():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            cleaned.set()

    client._start_inner = AsyncMock(side_effect=hang)
    signal = callback_for(bluetooth)
    signal(advertisement(), None)
    task = client._reconnect_task
    await asyncio.wait_for(entered.wait(), 1)
    signal(advertisement(), None)
    assert client._reconnect_task is task
    assert await mod.async_unload_entry(hass, entry)
    assert task.done()
    assert cleaned.is_set()
    assert client._stopped
    for remove in removers:
        remove()
    bluetooth.async_register_callback.return_value.assert_called_once()
    signal(advertisement(), None)  # A queued late callback cannot resurrect it.
    await asyncio.sleep(0)
    client._start_inner.assert_awaited_once()
    assert client._reconnect_task is None


@pytest.mark.asyncio
async def test_platform_failure_does_not_start_ble(runtime, setup_data, monkeypatch):
    mod, bluetooth = runtime
    hass, entry, _ = setup_data
    bluetooth.async_ble_device_from_address.return_value = advertisement().device
    hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError(
        "platform"
    )
    start = AsyncMock()
    monkeypatch.setattr(mod.FlicClient, "_start_inner", start)
    with pytest.raises(RuntimeError, match="platform"):
        await mod.async_setup_entry(hass, entry)
    assert entry.runtime_data.client._stopped
    assert entry.runtime_data.client._reconnect_task is None
    bluetooth.async_register_callback.assert_not_called()
    start.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_unload_keeps_client_usable(runtime, setup_data):
    mod, _ = runtime
    hass, entry, _ = setup_data
    await mod.async_setup_entry(hass, entry)
    hass.config_entries.async_unload_platforms.return_value = False
    assert not await mod.async_unload_entry(hass, entry)
    assert not entry.runtime_data.client._stopped
    await entry.runtime_data.client.stop()


@pytest.mark.asyncio
async def test_firmware_metadata_updates_after_background_start(runtime, setup_data):
    mod, _ = runtime
    hass, entry, _ = setup_data
    await mod.async_setup_entry(hass, entry)
    registry = mod.dr.async_get(hass)
    registry.async_get_device_by_identifier.return_value = types.SimpleNamespace(
        id="device", sw_version=None
    )
    client = entry.runtime_data.client
    client._notify_state_callbacks()
    registry.async_update_device.assert_not_called()
    client.state.firmware_version = 11
    client._notify_state_callbacks()
    registry.async_get_device_by_identifier.assert_called_with(
        ("flic_button", ADDRESS), entry.entry_id
    )
    registry.async_update_device.assert_called_once_with("device", sw_version="11")
    await mod.async_unload_entry(hass, entry)

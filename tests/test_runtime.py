"""Startup/reconnect lifecycle with real pyflic and fake Home Assistant/BLE I/O."""

import asyncio
import importlib.util
import sys
import types
from pathlib import Path
from time import monotonic
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
        async_last_service_info=Mock(return_value=None),
    )
    module("homeassistant.components.bluetooth.match", BluetoothCallbackMatcher=dict)
    module("homeassistant.config_entries", ConfigEntry=object)
    module(
        "homeassistant.const",
        CONF_ADDRESS="address",
        Platform=types.SimpleNamespace(EVENT="event", SENSOR="sensor"),
    )
    module(
        "homeassistant.core",
        HomeAssistant=object,
        callback=lambda f: f,
    )
    module("homeassistant.helpers")
    registry = types.SimpleNamespace(
        async_get_device_by_identifier=Mock(return_value=None),
        async_update_device=Mock(),
    )
    module("homeassistant.helpers.device_registry", async_get=lambda hass: registry)
    module(
        "homeassistant.helpers.event",
        async_track_time_interval=Mock(return_value=Mock()),
    )
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


def advertisement(connectable=True, *, received=None, address=ADDRESS):
    return types.SimpleNamespace(
        device=BLEDevice(address, "Test Flic", {}),
        address=address,
        connectable=connectable,
        time=monotonic() if received is None else received,
    )


def callback_for(bluetooth):
    return bluetooth.async_register_callback.call_args.args[1]


def poll_for(mod):
    return mod.async_track_time_interval.call_args.args[1]


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
    async def authenticated():
        client._state = mod.client.SessionState.SESSION_ESTABLISHED

    client.quick_verify = AsyncMock(side_effect=authenticated)
    client._configure_reconnect_advertising = AsyncMock()
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
    mod.async_track_time_interval.return_value.assert_called_once()
    poll_for(mod)(None)  # A queued timer after unload must not touch the cache.
    bluetooth.async_last_service_info.assert_not_called()
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
    mod.async_track_time_interval.assert_not_called()
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


@pytest.mark.asyncio
async def test_fresh_cached_advertisement_connects_without_callback(
    runtime, setup_data, monkeypatch
):
    mod, bluetooth = runtime
    hass, entry, _ = setup_data
    clock = Mock(return_value=100)
    monkeypatch.setattr(mod, "monotonic", clock)
    await mod.async_setup_entry(hass, entry)
    client = entry.runtime_data.client

    async def connect_session():
        client.state.connected = True

    client._start_inner = AsyncMock(side_effect=connect_session)
    # HA refreshed the cache but did not dispatch the unchanged payload.
    bluetooth.async_last_service_info.return_value = advertisement(received=101)
    clock.return_value = 102
    poll_for(mod)(None)
    await asyncio.wait_for(client._reconnect_task, 1)
    client._start_inner.assert_awaited_once()
    assert client.state.connected
    bluetooth.async_last_service_info.assert_called_once_with(
        hass, ADDRESS, connectable=True
    )
    assert mod.async_track_time_interval.call_args.args[2].total_seconds() == 5
    await mod.async_unload_entry(hass, entry)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "info,now",
    [
        (None, 102),
        (advertisement(received=99), 102),  # Before this entry was set up.
        (advertisement(received=100), 102),  # Not newer than the watermark.
        (advertisement(received=101), 117),  # More than 15 seconds old.
        (advertisement(received=103), 102),  # Invalid future timestamp.
        (advertisement(False, received=101), 102),  # Passive-only receiver.
        (advertisement(received=101, address="11:22:33:44:55:66"), 102),
    ],
)
async def test_poll_ignores_missing_old_or_unusable_receptions(
    runtime, setup_data, monkeypatch, info, now
):
    mod, bluetooth = runtime
    hass, entry, _ = setup_data
    clock = Mock(return_value=100)
    monkeypatch.setattr(mod, "monotonic", clock)
    await mod.async_setup_entry(hass, entry)
    client = entry.runtime_data.client
    client.set_ble_device = Mock(wraps=client.set_ble_device)
    bluetooth.async_last_service_info.return_value = info
    clock.return_value = now
    poll_for(mod)(None)
    client.set_ble_device.assert_not_called()
    assert client._reconnect_task is None
    await mod.async_unload_entry(hass, entry)


@pytest.mark.asyncio
@pytest.mark.parametrize("physical_link", [False, True])
async def test_poll_does_not_touch_connected_or_authenticating_buttons(
    runtime, setup_data, physical_link
):
    mod, bluetooth = runtime
    hass, entry, _ = setup_data
    await mod.async_setup_entry(hass, entry)
    client = entry.runtime_data.client
    if physical_link:
        client._client = types.SimpleNamespace(
            is_connected=True, disconnect=AsyncMock()
        )
    else:
        client.state.connected = True
    poll_for(mod)(None)
    bluetooth.async_last_service_info.assert_not_called()
    assert client._reconnect_task is None
    await mod.async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_callback_and_poll_do_not_wake_twice_for_same_reception(
    runtime, setup_data, monkeypatch
):
    mod, bluetooth = runtime
    hass, entry, _ = setup_data
    clock = Mock(return_value=100)
    monkeypatch.setattr(mod, "monotonic", clock)
    await mod.async_setup_entry(hass, entry)
    client = entry.runtime_data.client
    client.set_ble_device = Mock()
    info = advertisement(received=101)
    bluetooth.async_last_service_info.return_value = info
    clock.return_value = 102
    callback_for(bluetooth)(info, None)
    poll_for(mod)(None)
    poll_for(mod)(None)
    client.set_ble_device.assert_called_once_with(info.device)

    # The very same payload/device with a newer receive time wakes once more.
    info.time = 102
    clock.return_value = 103
    poll_for(mod)(None)
    poll_for(mod)(None)
    assert client.set_ble_device.call_count == 2
    await mod.async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_cache_poll_wakes_existing_retry_without_reload_or_parallel_task(
    runtime, setup_data, monkeypatch
):
    mod, bluetooth = runtime
    hass, entry, _ = setup_data
    clock = Mock(return_value=100)
    monkeypatch.setattr(mod, "monotonic", clock)
    await mod.async_setup_entry(hass, entry)
    client = entry.runtime_data.client
    failed = asyncio.Event()

    async def connect_session():
        if client._start_inner.await_count <= 2:
            if client._start_inner.await_count == 2:
                failed.set()
            raise BleakError("out of range")
        client.state.connected = True

    client._start_inner = AsyncMock(side_effect=connect_session)
    callback_for(bluetooth)(advertisement(received=101), None)
    task = client._reconnect_task
    await asyncio.wait_for(failed.wait(), 1)
    await asyncio.sleep(0)
    # No callback is delivered: only the cached receive time changes.
    bluetooth.async_last_service_info.return_value = advertisement(received=102)
    clock.return_value = 103
    poll_for(mod)(None)
    poll_for(mod)(None)
    assert client._reconnect_task is task
    await asyncio.wait_for(task, 1)
    assert client.state.connected
    assert client._start_inner.await_count == 3
    hass.config_entries.async_reload.assert_not_awaited()
    await mod.async_unload_entry(hass, entry)


@pytest.mark.asyncio
async def test_polling_is_isolated_per_config_entry(runtime, setup_data, monkeypatch):
    mod, bluetooth = runtime
    hass, entry, _ = setup_data
    clock = Mock(return_value=100)
    monkeypatch.setattr(mod, "monotonic", clock)
    await mod.async_setup_entry(hass, entry)
    poll_first = poll_for(mod)
    second_removers = []
    second = types.SimpleNamespace(
        **{
            **vars(entry),
            "data": {**entry.data, "address": "11:22:33:44:55:66"},
            "entry_id": "second_entry",
            "async_on_unload": second_removers.append,
        }
    )
    await mod.async_setup_entry(hass, second)
    poll_second = poll_for(mod)
    first_client, second_client = entry.runtime_data.client, second.runtime_data.client
    first_client.set_ble_device = Mock()
    second_client.set_ble_device = Mock()
    second_client.state.connected = True
    bluetooth.async_last_service_info.return_value = advertisement(received=101)
    clock.return_value = 102
    poll_first(None)
    poll_second(None)
    first_client.set_ble_device.assert_called_once()
    second_client.set_ble_device.assert_not_called()
    bluetooth.async_last_service_info.assert_called_once_with(
        hass, ADDRESS, connectable=True
    )
    await mod.async_unload_entry(hass, entry)
    await mod.async_unload_entry(hass, second)

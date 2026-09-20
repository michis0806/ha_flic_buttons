"""Offline regressions against the real pyflic-ble 0.2.5 protocol client.

Only BLE I/O and the Home Assistant form API are replaced by test doubles.
Run with Python 3.14 and the repository's requirements-test.txt.
"""

import asyncio
import importlib
import sys
import types
from pathlib import Path
from time import monotonic
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest
from bleak import BleakError
from bleak.backends.device import BLEDevice
from pyflic_ble import DeviceType, FlicAuthenticationError, FlicPairingError
from pyflic_ble.client import FlicProtocolError, SessionState

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "flic_button"
package = types.ModuleType("flic_under_test")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
client_module = importlib.import_module("flic_under_test.client")


@pytest.fixture
def transport(monkeypatch):
    class Transport:
        instances: ClassVar[list] = []
        connect_effect = None
        notify_error = None

        def __init__(self, device, *, disconnected_callback, timeout):
            self.device = device
            self.callback = disconnected_callback
            self.timeout = timeout
            self.is_connected = False
            self.disconnects = 0
            self.notifications = []
            self.instances.append(self)

        async def connect(self, *, timeout):
            assert timeout == self.timeout
            if type(self).connect_effect:
                await type(self).connect_effect(self)
            self.is_connected = True

        async def start_notify(self, uuid, handler):
            if self.notify_error:
                raise self.notify_error
            self.notifications.append((uuid, handler))

        async def disconnect(self):
            self.is_connected = False
            self.disconnects += 1
            self.callback(self)

    monkeypatch.setattr(client_module, "BleakClientWithServiceCache", Transport)
    monkeypatch.setattr(
        client_module, "close_stale_connections_by_address", AsyncMock()
    )
    return Transport


def make_client(**kwargs):
    return client_module.FlicClient(
        address="AA:BB:CC:DD:EE:FF",
        ble_device=BLEDevice("AA:BB:CC:DD:EE:FF", "Test Flic", {}),
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("device_type", list(DeviceType))
async def test_connection_prepares_real_protocol_handler(transport, device_type):
    client = make_client(pairing_only=True, device_type=device_type)
    await client.connect()
    link = transport.instances[0]
    assert link.timeout == 60
    assert client._state == SessionState.CONNECTED
    assert link.notifications[0][0] == client.handler.notify_char_uuid
    assert client._reconnect_task is None
    await client.stop()
    assert not link.is_connected


@pytest.mark.asyncio
async def test_pairing_disconnect_does_not_spawn_competing_connection(transport):
    async def lose_connection(link):
        link.callback(link)
        await asyncio.sleep(0)
        raise BleakError("lost during discovery")

    transport.connect_effect = lose_connection
    client = make_client(pairing_only=True)
    with pytest.raises(FlicProtocolError, match="service discovery failed"):
        await client.connect()
    assert len(transport.instances) == 1
    assert client._reconnect_task is None
    assert transport.instances[0].disconnects == 1
    assert client._client is None


@pytest.mark.asyncio
async def test_authentication_disconnect_also_suppresses_reconnect(transport):
    client = make_client(pairing_only=True)
    await client.connect()
    transport.instances[0].callback(transport.instances[0])
    await asyncio.sleep(0)
    assert client._reconnect_task is None
    await client.stop()


@pytest.mark.asyncio
async def test_normal_runtime_reconnect_still_runs(transport):
    client = make_client()
    await client.connect()
    client._async_reconnect_loop = AsyncMock()
    link = transport.instances[0]
    link.is_connected = False
    link.callback(link)
    await asyncio.sleep(0)
    client._async_reconnect_loop.assert_awaited_once()
    await client.stop()
    link.callback(link)
    await asyncio.sleep(0)
    client._async_reconnect_loop.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_is_bounded_and_disconnects_partial_transport(
    transport, monkeypatch
):
    monkeypatch.setattr(client_module, "CONNECT_TIMEOUT", 0.01)

    async def hang(link):
        link.is_connected = True
        await asyncio.Event().wait()

    transport.connect_effect = hang
    client = make_client(pairing_only=True)
    with pytest.raises(FlicProtocolError, match="service discovery.*TimeoutError"):
        await client.connect()
    assert not transport.instances[0].is_connected
    assert client._client is None
    assert client._reconnect_task is None


@pytest.mark.asyncio
async def test_notification_error_cleans_up_and_identifies_stage(transport):
    transport.notify_error = BleakError("notify failed")
    client = make_client(pairing_only=True)
    with pytest.raises(FlicProtocolError, match="notification subscription"):
        await client.connect()
    assert transport.instances[0].disconnects == 1
    assert client._client is None


@pytest.mark.asyncio
async def test_cancelled_connection_does_not_leak(transport):
    entered = asyncio.Event()

    async def hang(link):
        link.is_connected = True
        entered.set()
        await asyncio.Event().wait()

    transport.connect_effect = hang
    client = make_client(pairing_only=True)
    task = asyncio.create_task(client.connect())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert transport.instances[0].disconnects == 1
    assert client._client is None
    assert not client._connecting
    assert client._reconnect_task is None


@pytest.mark.asyncio
async def test_parallel_connect_calls_use_one_transport(transport):
    client = make_client(pairing_only=True)
    await asyncio.gather(client.connect(), client.connect())
    assert len(transport.instances) == 1
    await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stage", ["quick_verify", "init_button_events", "_send_connection_parameters"]
)
async def test_incomplete_runtime_session_disconnects_and_can_retry(transport, stage):
    client = make_client()
    client.quick_verify = AsyncMock()
    client.init_button_events = AsyncMock()
    client._send_connection_parameters = AsyncMock()
    client.get_battery_voltage = AsyncMock(return_value=3.1)
    client.get_firmware_version = AsyncMock(return_value=11)
    client.get_name = AsyncMock(return_value=("Flic", 0))
    error = (
        FlicAuthenticationError("quick verify timeout")
        if stage == "quick_verify"
        else FlicProtocolError("session failed")
    )
    getattr(client, stage).side_effect = error
    with pytest.raises(type(error)):
        await client.start()
    assert not client.state.connected
    assert not client.is_connected
    assert transport.instances[0].disconnects == 1
    getattr(client, stage).side_effect = None
    client.set_ble_device(client.ble_device)
    await asyncio.wait_for(client._reconnect_task, 2)
    assert client.state.connected
    assert client.state.battery_voltage == 3.1
    assert len(transport.instances) == 2
    await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["connect", "authenticate"])
async def test_stop_waits_for_runtime_transport_cleanup(transport, stage):
    client = make_client()
    entered = asyncio.Event()

    async def hang(*args):
        entered.set()
        await asyncio.Event().wait()

    if stage == "connect":
        transport.connect_effect = hang
    else:
        client.quick_verify = AsyncMock(side_effect=hang)
    client.set_ble_device(client.ble_device)
    task = client._reconnect_task
    await asyncio.wait_for(entered.wait(), 2)
    await asyncio.wait_for(client.stop(), 2)
    assert task.done()
    assert client._reconnect_task is None
    assert client._client is None
    assert not client.state.connected
    assert transport.instances[0].disconnects == 1


@pytest.mark.asyncio
async def test_stop_before_background_attempt_runs(transport):
    client = make_client()
    client.set_ble_device(client.ble_device)
    task = client._reconnect_task
    await client.stop()
    assert task.done()
    assert client._reconnect_task is None
    assert not transport.instances


@pytest.mark.asyncio
async def test_session_cleanup_timeout_preserves_cancellation(transport, monkeypatch):
    monkeypatch.setattr(client_module, "CLEANUP_TIMEOUT", 0.01)
    client = make_client()
    entered = asyncio.Event()

    async def hang():
        entered.set()
        await asyncio.Event().wait()

    client.quick_verify = AsyncMock(side_effect=hang)
    task = asyncio.create_task(client.start())
    await asyncio.wait_for(entered.wait(), 2)
    transport.instances[0].disconnect = AsyncMock(side_effect=hang)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 1)
    assert client._client is None
    assert not client.state.connected
    await client.stop()


@pytest.fixture
def flow_module(monkeypatch):
    class ConfigFlow:
        def __init_subclass__(cls, **kwargs):
            pass

        def async_show_form(self, **kwargs):
            return {"type": "form", **kwargs}

        def async_create_entry(self, **kwargs):
            return {"type": "create_entry", **kwargs}

        def async_abort(self, **kwargs):
            return {"type": "abort", **kwargs}

        def async_show_progress(self, **kwargs):
            return {"type": "progress", **kwargs}

        def async_show_progress_done(self, **kwargs):
            return {"type": "progress_done", **kwargs}

        def _async_current_ids(self, **kwargs):
            return set()

        def _abort_if_unique_id_configured(self):
            pass

        async def async_set_unique_id(self, unique_id, **kwargs):
            self._unique_id = unique_id

    def module(name, **values):
        result = types.ModuleType(name)
        result.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, result)

    module("homeassistant")
    module("homeassistant.components")
    module(
        "homeassistant.components.bluetooth",
        BluetoothServiceInfoBleak=object,
        async_discovered_service_info=lambda _: [],
        async_request_active_scan=AsyncMock(),
    )
    module(
        "homeassistant.config_entries",
        ConfigFlow=ConfigFlow,
        ConfigFlowResult=dict,
        OptionsFlow=ConfigFlow,
    )
    module("homeassistant.const", CONF_ADDRESS="address")
    module("homeassistant.core", callback=lambda f: f)
    module("homeassistant.helpers")
    module(
        "homeassistant.helpers.selector",
        SelectSelector=object,
        SelectSelectorConfig=object,
        SelectSelectorMode=object,
    )
    monkeypatch.delitem(sys.modules, "flic_under_test.config_flow", raising=False)
    return importlib.import_module("flic_under_test.config_flow")


def make_flow(flow_module):
    flow = flow_module.FlicButtonConfigFlow()
    flow._discovery_info = types.SimpleNamespace(
        name="Test Flic",
        address="AA:BB:CC:DD:EE:FF",
        device=BLEDevice("AA:BB:CC:DD:EE:FF", "Test Flic", {}),
        service_uuids=[flow_module.FLIC_SERVICE_UUID],
        manufacturer_data={},
        connectable=True,
        time=monotonic(),
    )
    flow.hass = types.SimpleNamespace(
        async_create_task=lambda coro, **kwargs: asyncio.create_task(coro),
        async_create_background_task=lambda coro, **kwargs: asyncio.create_task(coro),
    )
    flow.context = {}
    flow._async_wait_for_flic_device = AsyncMock(return_value=flow._discovery_info)
    return flow


async def finish_flow(flow):
    result = await flow.async_step_pair({})
    while result["type"] == "progress":
        await result["progress_task"]
        result = await flow.async_step_pair()
    assert result == {"type": "progress_done", "next_step_id": "pair_finish"}
    return await flow.async_step_pair_finish()


@pytest.mark.asyncio
async def test_flow_success_stores_credentials_and_stops_client(
    flow_module, transport, monkeypatch
):
    credentials = (123, b"\x11" * 16, "F123", 3000, 0, b"\x22" * 16, 1)
    monkeypatch.setattr(
        client_module.FlicClient,
        "full_verify_pairing",
        AsyncMock(return_value=credentials),
    )
    flow = make_flow(flow_module)
    result = await finish_flow(flow)
    assert result["type"] == "create_entry"
    assert result["data"]["pairing_key"] == "11" * 16
    assert result["data"]["device_type"] == DeviceType.FLIC2.value
    assert not transport.instances[0].is_connected
    assert flow._client is None
    assert flow._pairing_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,expected",
    [
        (FlicPairingError("no pairing mode"), "pairing_failed"),
        (FlicAuthenticationError("bad signature"), "invalid_signature"),
        (FlicProtocolError("connection lost"), "cannot_connect"),
    ],
)
async def test_flow_authentication_errors_allow_retry(
    flow_module, transport, monkeypatch, error, expected
):
    monkeypatch.setattr(
        client_module.FlicClient, "full_verify_pairing", AsyncMock(side_effect=error)
    )
    flow = make_flow(flow_module)
    result = await finish_flow(flow)
    assert result["errors"]["base"] == expected
    assert not flow._pairing_started
    assert flow._client is None
    assert not transport.instances[0].is_connected


@pytest.mark.asyncio
async def test_duplicate_form_and_flow_removal_cancel_owned_connection(
    flow_module, transport
):
    entered = asyncio.Event()

    async def hang(link):
        entered.set()
        await asyncio.Event().wait()

    transport.connect_effect = hang
    flow = make_flow(flow_module)
    result = await flow.async_step_pair({})
    await result["progress_task"]
    result = await flow.async_step_pair()
    task = result["progress_task"]
    await entered.wait()
    await flow.async_step_pair(None)
    await flow.async_step_pair({})
    assert len(transport.instances) == 1
    assert flow._pairing_started
    flow.async_remove()
    with pytest.raises(asyncio.CancelledError):
        await task
    await flow._cleanup_task
    assert not flow._pairing_started
    assert flow._client is None
    assert flow._pairing_task is None
    assert transport.instances[0].disconnects == 1


@pytest.mark.parametrize(
    "uuids,connectable,manufacturer,expected",
    [
        ([], True, {}, False),
        (["unrelated"], True, {}, False),
        (["flic"], False, {}, False),
        (["flic"], True, {}, True),
        (["flic"], True, {0x030F: b"\x02\x00\x00\x00\x00"}, True),
        (["flic"], True, {0x030F: b"\x02\x00\x00\x00\x01"}, True),
        (["flic"], True, {0x030F: b"\x02\x00\x00\x00\x02"}, False),
        (["flic"], True, {0x030F: b"\x02\x00\x00\x00\x03"}, False),
        (["flic"], True, {0x030F: b"\x02"}, True),
        (["twist"], True, {0x030F: b"\x02\x00\x00\x00\x02"}, True),
    ],
)
def test_pairing_advertisement_semantics(
    flow_module, uuids, connectable, manufacturer, expected
):
    names = {
        "flic": flow_module.FLIC_SERVICE_UUID.upper(),
        "twist": flow_module.TWIST_SERVICE_UUID,
    }
    info = types.SimpleNamespace(
        service_uuids=[names.get(uuid, uuid) for uuid in uuids],
        connectable=connectable,
        manufacturer_data=manufacturer,
    )
    assert flow_module.FlicButtonConfigFlow._is_pairing_advertisement(info) is expected


@pytest.mark.asyncio
async def test_visible_progress_tracks_real_phases(flow_module, transport, monkeypatch):
    flow = make_flow(flow_module)
    auth_entered = asyncio.Event()
    auth_finish = asyncio.Event()

    async def authenticate():
        auth_entered.set()
        await auth_finish.wait()
        return (123, b"\x11" * 16, "F123", 3000, 0, b"\x22" * 16, 1)

    monkeypatch.setattr(
        client_module.FlicClient,
        "full_verify_pairing",
        AsyncMock(side_effect=authenticate),
    )
    assert (await flow.async_step_pair())["type"] == "form"
    waiting = await flow.async_step_pair({})
    assert waiting["progress_action"] == "wait_for_pairing"
    assert not transport.instances
    assert (await flow.async_step_pair({}))["progress_task"] is waiting["progress_task"]
    await waiting["progress_task"]
    connecting = await flow.async_step_pair()
    assert connecting["progress_action"] == "connect"
    await connecting["progress_task"]
    verifying = await flow.async_step_pair()
    assert verifying["progress_action"] == "authenticate"
    await auth_entered.wait()
    assert flow._pairing_data is None
    assert transport.instances[0].is_connected
    assert (await flow.async_step_pair({}))["progress_task"] is verifying[
        "progress_task"
    ]
    auth_finish.set()
    await verifying["progress_task"]
    assert not transport.instances[0].is_connected
    assert (await flow.async_step_pair())["type"] == "progress_done"
    assert (await flow.async_step_pair_finish())["type"] == "create_entry"


@pytest.mark.asyncio
async def test_freshness_and_address_filter_and_scan_cleanup(flow_module, monkeypatch):
    flow = make_flow(flow_module)
    del flow._async_wait_for_flic_device
    wanted = flow._discovery_info
    other = types.SimpleNamespace(**vars(wanted))
    other.address = "11:22:33:44:55:66"
    scan_started = asyncio.Event()
    scan_stopped = asyncio.Event()

    async def scan(*args):
        scan_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            scan_stopped.set()

    monkeypatch.setattr(flow_module, "async_request_active_scan", scan)
    monkeypatch.setattr(
        flow_module, "async_discovered_service_info", lambda _: [other, wanted]
    )
    task = asyncio.create_task(flow._async_wait_for_flic_device(wanted.address))
    await scan_started.wait()
    other.time = monotonic()
    await asyncio.sleep(0.3)
    assert not task.done()  # old selected device and fresh wrong device rejected
    wanted.time = monotonic()
    assert await asyncio.wait_for(task, 1) is wanted
    assert scan_stopped.is_set()


@pytest.mark.asyncio
async def test_scan_is_cancelled_on_timeout_and_flow_removal(flow_module, monkeypatch):
    flow = make_flow(flow_module)
    del flow._async_wait_for_flic_device
    stopped = asyncio.Event()

    async def scan(*args):
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setattr(flow_module, "async_request_active_scan", scan)
    monkeypatch.setattr(flow_module, "PAIRING_TIMEOUT", 0.02)
    result = await finish_flow(flow)
    assert result["errors"]["base"] == "pairing_mode_timeout"
    assert stopped.is_set()
    stopped.clear()
    monkeypatch.setattr(flow_module, "PAIRING_TIMEOUT", 60)
    result = await flow.async_step_pair({})
    await asyncio.sleep(0.01)
    flow.async_remove()
    await flow._cleanup_task
    assert result["progress_task"].cancelled()
    assert stopped.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("start_auth", [False, True])
async def test_removal_between_stages_cleans_live_connection(
    flow_module, transport, start_auth
):
    flow = make_flow(flow_module)
    waiting = await flow.async_step_pair({})
    await waiting["progress_task"]
    connecting = await flow.async_step_pair()
    await connecting["progress_task"]
    assert flow._client is not None
    if start_auth:
        # Cancel before the authentication coroutine has had a chance to start.
        await flow.async_step_pair()
    flow.async_remove()
    await flow._cleanup_task
    assert flow._client is None
    assert not transport.instances[0].is_connected
    assert (await flow.async_step_pair())["type"] == "abort"


@pytest.mark.asyncio
async def test_cancel_during_authentication_closes_client(
    flow_module, transport, monkeypatch
):
    entered = asyncio.Event()

    async def authenticate():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(
        client_module.FlicClient,
        "full_verify_pairing",
        AsyncMock(side_effect=authenticate),
    )
    flow = make_flow(flow_module)
    waiting = await flow.async_step_pair({})
    await waiting["progress_task"]
    connecting = await flow.async_step_pair()
    await connecting["progress_task"]
    await flow.async_step_pair()
    await entered.wait()
    flow.async_remove()
    await flow._cleanup_task
    assert flow._client is None
    assert flow._pairing_data is None
    assert not transport.instances[0].is_connected


@pytest.mark.asyncio
async def test_readiness_timeout_retries_without_connecting(flow_module, transport):
    flow = make_flow(flow_module)
    flow._async_wait_for_flic_device.side_effect = TimeoutError
    result = await finish_flow(flow)
    assert result["errors"]["base"] == "pairing_mode_timeout"
    assert not transport.instances
    flow._async_wait_for_flic_device.side_effect = None
    waiting = await flow.async_step_pair({})
    assert waiting["progress_action"] == "wait_for_pairing"
    flow.async_remove()
    await flow._cleanup_task


@pytest.mark.asyncio
async def test_twist_progress_does_not_claim_flic2_public_mode(flow_module):
    flow = make_flow(flow_module)
    flow._device_type = DeviceType.TWIST
    waiting = await flow.async_step_pair({})
    assert waiting["progress_action"] == "wait_for_twist"
    await waiting["progress_task"]
    connecting = await flow.async_step_pair()
    assert connecting["progress_action"] == "connect_twist"
    flow.async_remove()
    await flow._cleanup_task


@pytest.mark.asyncio
async def test_connection_failure_is_reported_and_retry_succeeds(
    flow_module, transport, monkeypatch
):
    async def fail(link):
        raise BleakError("unreachable")

    transport.connect_effect = fail
    flow = make_flow(flow_module)
    result = await finish_flow(flow)
    assert result["errors"]["base"] == "cannot_connect"
    assert flow._client is None
    transport.connect_effect = None
    monkeypatch.setattr(
        client_module.FlicClient,
        "full_verify_pairing",
        AsyncMock(return_value=(123, b"\x11" * 16, "F123", 3000, 0, b"\x22" * 16, 1)),
    )
    result = await finish_flow(flow)
    assert result["type"] == "create_entry"
    assert len(transport.instances) == 2


@pytest.mark.asyncio
async def test_selected_button_is_not_replaced_when_cache_expires(flow_module):
    flow = make_flow(flow_module)
    selected = flow._discovery_info
    result = await flow.async_step_bluetooth_confirm({})
    assert result["type"] == "form"
    assert flow._discovery_info is selected
    waiting = await flow.async_step_pair({})
    await waiting["progress_task"]
    flow._async_wait_for_flic_device.assert_awaited_once_with(selected.address)
    flow.async_remove()
    await flow._cleanup_task


@pytest.mark.asyncio
async def test_manual_discovery_advances_to_pairing_progress(flow_module):
    flow = make_flow(flow_module)
    flow._discovery_info = None
    searching = await flow.async_step_user()
    assert searching["progress_action"] == "wait_for_discovery"
    await searching["progress_task"]
    result = await flow.async_step_user()
    assert result["next_step_id"] == "discovery_done"
    result = await flow.async_step_discovery_done()
    assert result["progress_action"] == "wait_for_pairing"
    flow.async_remove()
    await flow._cleanup_task


def test_configured_device_is_not_selected_again(flow_module):
    flow = make_flow(flow_module)
    flow._async_current_ids = lambda **kwargs: {flow._discovery_info.address}
    assert not flow._is_unconfigured_flic_device(flow._discovery_info)

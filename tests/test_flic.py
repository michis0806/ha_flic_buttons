"""Offline regressions against the real pyflic-ble 0.2.6 protocol client.

Only BLE I/O and the Home Assistant form API are replaced by test doubles.
Run with Python 3.14 and the repository's requirements-test.txt.
"""

import asyncio
import importlib
import logging
import struct
import sys
import types
from pathlib import Path
from time import monotonic
from typing import ClassVar
from unittest.mock import AsyncMock, Mock

import pytest
from bleak import BleakError
from bleak.backends.device import BLEDevice
from pyflic_ble import DeviceType, FlicAuthenticationError, FlicPairingError
from pyflic_ble.client import FlicProtocolError, SessionState
from pyflic_ble.protocol import InitButtonEventsDuoRequest, InitButtonEventsRequest
from pyflic_ble.security import chaskey_generate_subkeys, chaskey_with_dir_and_counter

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


def make_packet_client(device_type):
    """Real handlers/serialization/signatures; only Bluetooth I/O is mocked."""
    client = make_client(device_type=device_type)
    client._client = types.SimpleNamespace(write_gatt_char=AsyncMock())
    client._state = SessionState.SESSION_ESTABLISHED
    client._connection_id = 3
    client._chaskey_keys = chaskey_generate_subkeys(bytes(range(16)))

    async def init_response(opcodes):
        opcode = opcodes[0]
        response = bytearray(client_module._INIT_RESPONSES[opcode])
        response[0:2] = bytes([3, opcode])
        client._response_queue.put_nowait(bytes(response))
        return await client._wait_for_handler_opcodes(opcodes)

    client.handler.bind_transport(
        write_gatt=AsyncMock(),
        write_packet=client._write_packet,
        wait_for_opcode=AsyncMock(return_value=b"\x00\x00"),
        wait_for_opcodes=init_response,
    )
    return client


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "device_type,offset", [(DeviceType.FLIC2, 10), (DeviceType.DUO, 14)]
)
async def test_upstream_init_handlers_disable_idle_disconnect_before_signing(
    device_type, offset
):
    client = make_packet_client(device_type)
    # Every new session uses the same init path, including reconnections.
    await client.init_button_events()
    await client.init_button_events()
    writes = client._client.write_gatt_char.await_args_list
    init_packets = []
    for counter, call in enumerate(writes):
        packet = bytes(call.args[1])
        body, signature = packet[:-5], packet[-5:]
        assert signature == chaskey_with_dir_and_counter(
            client._chaskey_keys, direction=1, counter=counter, data=body[1:]
        )
        if body[1] in (23, 35):
            bits = struct.unpack_from("<Q", body, offset)[0]
            assert bits & 0x1FF == 511
            assert (bits >> 9) & 31 == 30
            assert (bits >> 14) & 0xFFFFF == 60
            assert body[0] == 3
            init_packets.append(body)
    assert len(init_packets) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "device_type,offset,request_type",
    [
        (DeviceType.FLIC2, 10, InitButtonEventsRequest),
        (DeviceType.DUO, 14, InitButtonEventsDuoRequest),
    ],
)
@pytest.mark.parametrize("original_timeout", [0, 40, 511])
async def test_packet_writes_preserve_upstream_init_fields(
    device_type, offset, request_type, original_timeout
):
    client = make_packet_client(device_type)
    original = request_type(
        connection_id=3,
        boot_id=0x12345678,
        auto_disconnect_time=original_timeout,
        max_queued_packets=31,
        max_queued_packets_age=0xABCDE,
    ).to_bytes()
    await client._write_packet(original)
    packet = bytes(client._client.write_gatt_char.await_args.args[1])
    actual = packet[:-5]
    assert actual == original
    assert len(actual) == len(original)
    # There is no mutation of the caller's buffer or the upstream request class.
    assert struct.unpack_from("<Q", original, offset)[0] & 0x1FF == original_timeout
    assert request_type(connection_id=0).auto_disconnect_time == 511


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "device_type,packet,authenticated",
    [
        (DeviceType.TWIST, InitButtonEventsRequest(connection_id=0).to_bytes(), True),
        (
            DeviceType.TWIST,
            InitButtonEventsDuoRequest(connection_id=0).to_bytes(),
            True,
        ),
        (DeviceType.TWIST, b"\x0c\x00\x01\x02", True),
        (
            DeviceType.FLIC2,
            InitButtonEventsDuoRequest(connection_id=0).to_bytes(),
            True,
        ),
        (DeviceType.DUO, InitButtonEventsRequest(connection_id=0).to_bytes(), True),
        (DeviceType.FLIC2, b"\x03\x0c\x01\x02\x03\x04", True),
        (DeviceType.DUO, b"\x03\x14", True),
        (DeviceType.FLIC2, InitButtonEventsRequest(connection_id=0).to_bytes(), False),
        (DeviceType.DUO, InitButtonEventsDuoRequest(connection_id=0).to_bytes(), False),
    ],
)
async def test_packet_writes_leave_other_commands_and_unsigned_packets_unchanged(
    device_type, packet, authenticated
):
    client = make_packet_client(device_type)
    await client._write_packet(packet, authenticated)
    actual = bytes(client._client.write_gatt_char.await_args.args[1])
    assert (actual[:-5] if authenticated else actual) == packet


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
async def test_connection_observations_are_opt_in(transport, monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=client_module.__name__)
    diagnostics = Mock()
    monkeypatch.setattr(client_module, "ConnectionDiagnostics", diagnostics)
    client = make_client(pairing_only=True)
    await client.connect()
    diagnostics.assert_not_called()
    await client.stop()


@pytest.mark.asyncio
async def test_successful_connect_also_stops_diagnostic_task(
    transport, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger=client_module.__name__)
    observations = []
    observer_type = client_module.ConnectionDiagnostics

    def create_observer(*args):
        result = observer_type(*args)
        observations.append(result)
        return result

    monkeypatch.setattr(client_module, "ConnectionDiagnostics", create_observer)
    client = make_client(pairing_only=True)
    await asyncio.gather(client.connect(), client.connect())
    assert len(observations) == 1
    assert observations[0]._task is None
    assert client.is_connected
    await client.stop()


@pytest.mark.asyncio
async def test_connection_timeout_includes_observed_link_and_stops_task(
    transport, monkeypatch, caplog
):
    caplog.set_level(logging.INFO, logger=client_module.__name__)
    monkeypatch.setattr(client_module, "CONNECT_TIMEOUT", 0.02)
    observations = []
    observer_type = client_module.ConnectionDiagnostics

    def create_observer(*args):
        result = observer_type(*args)
        observations.append(result)
        return result

    monkeypatch.setattr(client_module, "ConnectionDiagnostics", create_observer)

    async def hang(link):
        link.is_connected = True
        await asyncio.Event().wait()

    transport.connect_effect = hang
    client = make_client(pairing_only=True)
    with pytest.raises(FlicProtocolError, match="link_seen=True") as error:
        await client.connect()
    assert "bluez_connected=None" in str(error.value)
    assert observations[0]._task is None
    assert client._client is None


@pytest.mark.asyncio
async def test_cancelling_connect_stops_diagnostic_task(transport, monkeypatch, caplog):
    caplog.set_level(logging.INFO, logger=client_module.__name__)
    observations = []
    observer_type = client_module.ConnectionDiagnostics

    def create_observer(*args):
        result = observer_type(*args)
        observations.append(result)
        return result

    monkeypatch.setattr(client_module, "ConnectionDiagnostics", create_observer)
    entered = asyncio.Event()

    async def hang(_link):
        entered.set()
        await asyncio.Event().wait()

    transport.connect_effect = hang
    client = make_client(pairing_only=True)
    task = asyncio.create_task(client.connect())
    await asyncio.wait_for(entered.wait(), 1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert observations[0]._task is None
    assert client._client is None


@pytest.mark.asyncio
async def test_connection_parameter_error_identifies_separate_stage(transport):
    client = make_client(pairing_only=True)
    client._request_connection_parameters = AsyncMock(side_effect=BleakError("params"))
    with pytest.raises(FlicProtocolError, match="connection parameter request"):
        await client.connect()
    assert transport.instances[0].disconnects == 1


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

    async def authenticated():
        client._state = SessionState.SESSION_ESTABLISHED

    client.quick_verify.side_effect = authenticated
    client._configure_reconnect_advertising = AsyncMock()
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


@pytest.fixture
def session_peer(request):
    """Exercise real startup, handlers and MAC signing against a fake BLE peer."""
    client = make_client(
        pairing_id=123,
        pairing_key=bytes(range(16)),
        device_type=getattr(request, "param", DeviceType.FLIC2),
    )
    peer = types.SimpleNamespace(
        is_connected=False,
        firmware=11,
        boot_id=1234,
        ignored=set(),
        writes=[],
        event_count=0,
        timestamp_ticks=120 * 32768,
    )

    async def disconnect():
        peer.is_connected = False

    async def connect():
        peer.is_connected = True
        client._client = peer
        client._state = SessionState.CONNECTED
        client._intentional_disconnect = False
        client.handler.bind_transport(
            write_gatt=client._write_gatt,
            write_packet=client._write_packet,
            wait_for_opcode=client._wait_for_handler_opcode,
            wait_for_opcodes=client._wait_for_handler_opcodes,
        )

    async def verify():
        client._state = SessionState.SESSION_ESTABLISHED
        client._connection_id = 3
        client.handler.connection_id = 3
        client._chaskey_keys = chaskey_generate_subkeys(bytes(range(16)))
        client._packet_counter_to_button = 0

    def notify(opcode, payload=b"", header=3):
        # Upstream 0.2.6 strips incoming MACs without checking them. Outbound
        # MACs are asserted separately; these are deliberately dummy tails.
        client._notification_handler(
            None, bytearray([header, opcode]) + payload + bytes(5)
        )

    async def write(_uuid, packet):
        packet = bytes(packet)
        peer.writes.append(packet)
        opcode = packet[1]
        if opcode in peer.ignored:
            return
        if opcode in (23, 35):
            counts = (
                struct.pack("<II", peer.event_count, 0)
                if opcode == 35
                else struct.pack("<I", peer.event_count)
            )
            payload = (peer.timestamp_ticks << 1).to_bytes(6, "little") + counts
            notify(
                30 if opcode == 35 else 10, payload + struct.pack("<I", peer.boot_id)
            )
        elif opcode == 20:
            notify(20, struct.pack("<H", 900))
        elif opcode == 8:
            notify(5, struct.pack("<I", peer.firmware))
        elif opcode == 11:
            notify(16, bytes(6) + b"Test Flic")
        elif opcode == 27:
            # Official SDK RxPacket.SET_ADV_PARAMETERS_RESPONSE (wiki typo: 27).
            notify(25)

    peer.disconnect = AsyncMock(side_effect=disconnect)
    peer.write_gatt_char = AsyncMock(side_effect=write)
    peer.notify = notify
    client.connect = AsyncMock(side_effect=connect)
    client.quick_verify = AsyncMock(side_effect=verify)
    client.handler.bind_transport(
        write_gatt=client._write_gatt,
        write_packet=client._write_packet,
        wait_for_opcode=client._wait_for_handler_opcode,
        wait_for_opcodes=client._wait_for_handler_opcodes,
    )
    return client, peer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_peer", [DeviceType.FLIC2, DeviceType.DUO], indirect=True
)
async def test_sdk_advertising_is_signed_acknowledged_and_reapplied_after_reboot(
    session_peer,
):
    client, peer = session_peer
    states = []
    client.register_state_callback(lambda state: states.append(state.connected))
    await client.start()
    assert client.state.connected
    assert client._boot_id == 1234
    assert states == [True]
    advertising = [packet for packet in peer.writes if packet[1] == 27]
    assert len(advertising) == 1
    body, signature = advertising[0][:-5], advertising[0][-5:]
    assert body == bytes.fromhex("031b010001004000640680510100")
    counter = peer.writes.index(advertising[0])
    assert signature == chaskey_with_dir_and_counter(
        client._chaskey_keys, direction=1, counter=counter, data=body[1:]
    )
    await client.disconnect()
    await client.start()
    assert len([packet for packet in peer.writes if packet[1] == 27]) == 1
    peer.boot_id += 1
    await client.disconnect()
    await client.start()
    assert len([packet for packet in peer.writes if packet[1] == 27]) == 2
    await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("firmware", [0, 6])
async def test_older_firmware_starts_without_advertising_request(
    session_peer, firmware
):
    client, peer = session_peer
    peer.firmware = firmware
    await client.start()
    assert client.state.connected
    assert all(packet[1] != 27 for packet in peer.writes)
    await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("fragmented", [False, True])
async def test_ping_gets_signed_response_without_polluting_command_queue(
    session_peer, fragmented
):
    client, peer = session_peer
    await client.start()
    counter = client._packet_counter_to_button
    if fragmented:
        client._notification_handler(None, bytearray([0x83, 15, 0, 0]))
        # An unrelated session cannot contribute bytes to our fragment buffer.
        peer.notify(15, header=4)
        client._notification_handler(None, bytearray([3, 0, 0, 0]))
    else:
        peer.notify(15)
    await asyncio.wait_for(asyncio.gather(*client._session_tasks), 1)
    packet = peer.writes[-1]
    assert packet[:2] == b"\x03\x0e"
    assert packet[2:] == chaskey_with_dir_and_counter(
        client._chaskey_keys, direction=1, counter=counter, data=b"\x0e"
    )
    assert client._response_queue.empty()
    assert client.state.connected
    await client.stop()


@pytest.mark.asyncio
async def test_controls_for_other_sessions_and_truncated_controls_are_ignored(
    session_peer,
):
    client, peer = session_peer
    await client.start()
    before = len(peer.writes)
    peer.notify(15, header=0)
    peer.notify(9, b"\x00", header=4)
    peer.notify(9)  # No reason field.
    client._notification_handler(None, bytearray([3, 15]))  # No MAC.
    await asyncio.sleep(0)
    assert not client._session_tasks
    assert len(peer.writes) == before
    assert client.state.connected
    await client.stop()


@pytest.mark.asyncio
async def test_terminated_session_reconnects_even_when_ble_link_stays_open(
    session_peer,
):
    client, peer = session_peer
    await client.start()
    peer.notify(9, b"\x00")  # PING_TIMEOUT
    peer.notify(9, b"\x00")  # Repeated packet must not spawn another recovery.
    assert peer.is_connected
    assert not client.state.connected
    assert len(client._session_tasks) == 1
    await asyncio.wait_for(asyncio.gather(*client._session_tasks), 1)
    await asyncio.wait_for(client._reconnect_task, 1)
    assert peer.disconnect.await_count == 1
    assert client.quick_verify.await_count == 2
    assert client.state.connected
    assert client._response_queue.empty()
    await client.stop()


@pytest.mark.asyncio
async def test_stop_cancels_pending_ping_before_new_transport_can_be_used(session_peer):
    client, peer = session_peer
    await client.start()
    entered = asyncio.Event()

    async def blocked_write(*args):
        entered.set()
        await asyncio.Event().wait()

    peer.write_gatt_char.side_effect = blocked_write
    peer.notify(15)
    await asyncio.wait_for(entered.wait(), 1)
    tasks = tuple(client._session_tasks)
    await asyncio.wait_for(client.stop(), 1)
    assert all(task.done() for task in tasks)
    assert not client._session_tasks
    assert not peer.is_connected
    assert client._reconnect_task is None


@pytest.mark.asyncio
@pytest.mark.parametrize("opcode", [23, 27])
async def test_missing_initialization_or_advertising_ack_never_marks_ready(
    session_peer, monkeypatch, opcode
):
    import pyflic_ble.client as upstream_client
    import pyflic_ble.handlers.flic2 as upstream_handler

    monkeypatch.setattr(upstream_client, "COMMAND_TIMEOUT", 0.01)
    monkeypatch.setattr(upstream_handler, "COMMAND_TIMEOUT", 0.01)
    client, peer = session_peer
    peer.ignored.add(opcode)
    states = []
    client.register_state_callback(lambda state: states.append(state.connected))
    with pytest.raises((FlicProtocolError, TimeoutError)):
        await client.start()
    assert not client.is_connected
    assert not client.state.connected
    assert True not in states
    peer.ignored.clear()
    await client.start()
    assert client.state.connected
    await client.stop()


@pytest.mark.asyncio
async def test_truncated_initialization_response_fails_instead_of_becoming_available(
    session_peer,
):
    client, peer = session_peer
    original = peer.write_gatt_char.side_effect

    async def write(uuid, packet):
        if packet[1] == 23:
            peer.notify(10, b"\x00")
        else:
            await original(uuid, packet)

    peer.write_gatt_char.side_effect = write
    with pytest.raises(FlicProtocolError, match="Truncated"):
        await client.start()
    assert not client.state.connected
    assert not peer.is_connected
    await client.stop()


@pytest.mark.asyncio
async def test_ping_write_failure_recovers_session(session_peer):
    client, peer = session_peer
    await client.start()
    original = peer.write_gatt_char.side_effect

    async def write(uuid, packet):
        if packet[1] == 14:
            raise BleakError("link write failed")
        await original(uuid, packet)

    peer.write_gatt_char.side_effect = write
    peer.notify(15)
    await asyncio.wait_for(asyncio.gather(*client._session_tasks), 1)
    # The ping task creates the recovery task on failure.
    if client._session_tasks:
        await asyncio.wait_for(asyncio.gather(*client._session_tasks), 1)
    await asyncio.wait_for(client._reconnect_task, 1)
    assert client.state.connected
    assert client.quick_verify.await_count == 2
    await client.stop()


@pytest.mark.asyncio
async def test_stale_recovery_does_not_disconnect_replacement_session(session_peer):
    client, peer = session_peer
    await client.start()
    old_generation = client._session_generation
    await client.disconnect()
    await client.start()
    before = peer.disconnect.await_count
    await client._recover_session(old_generation)
    assert peer.disconnect.await_count == before
    assert client.state.connected
    await client.stop()


@pytest.mark.asyncio
async def test_session_loss_during_startup_cleans_up_without_recovery_deadlock(
    session_peer, monkeypatch
):
    import pyflic_ble.client as upstream_client
    import pyflic_ble.handlers.flic2 as upstream_handler

    monkeypatch.setattr(upstream_client, "COMMAND_TIMEOUT", 0.01)
    monkeypatch.setattr(upstream_handler, "COMMAND_TIMEOUT", 0.01)
    client, peer = session_peer
    original = peer.write_gatt_char.side_effect

    async def write(uuid, packet):
        if packet[1] == 11:
            peer.notify(9, b"\x01")
        else:
            await original(uuid, packet)

    peer.write_gatt_char.side_effect = write
    with pytest.raises(FlicProtocolError, match="ended during initialization"):
        await asyncio.wait_for(client.start(), 1)
    assert not client.state.connected
    assert not peer.is_connected
    assert not client._session_tasks
    assert client._reconnect_task is None
    await client.stop()


@pytest.mark.asyncio
async def test_upstream_click_ack_and_event_resume_survive_local_client(session_peer):
    client, peer = session_peer
    await client.start()
    events = []
    client.register_button_event_callback(lambda kind, data: events.append(kind))
    counter = client._packet_counter_to_button
    # Flic 2 SINGLE_CLICK_TIMEOUT, count 42, fresh and not queued.
    payload = struct.pack("<I", 42) + (120 * 32768).to_bytes(6, "little") + b"\x02"
    peer.notify(12, payload)
    await asyncio.wait_for(asyncio.gather(*client._session_tasks), 1)
    acknowledgements = [packet for packet in peer.writes if packet[1] == 16]
    assert len(acknowledgements) == 1
    body = struct.pack("<BBI", 3, 16, 42)
    assert acknowledgements[0] == body + chaskey_with_dir_and_counter(
        client._chaskey_keys, direction=1, counter=counter, data=body[1:]
    )
    assert events == ["click"]
    peer.event_count = 42
    await client.disconnect()
    await client.start()
    init = [packet for packet in peer.writes if packet[1] == 23][-1]
    assert struct.unpack_from("<II", init, 2) == (42, 1234)
    await client.stop()


@pytest.mark.asyncio
async def test_upstream_filters_old_queued_clicks_after_ha_restart(session_peer):
    client, peer = session_peer
    await client.start()  # Unknown boot ID; device's clock is at 120 seconds.
    events = []
    client.register_button_event_callback(lambda kind, data: events.append(kind))
    for count, seconds in [(1, 100), (2, 119)]:
        # Both are queued; only the event less than 15 seconds old is delivered.
        payload = (
            struct.pack("<I", count) + (seconds * 32768).to_bytes(6, "little") + b"\x12"
        )
        peer.notify(12, payload)
    await asyncio.wait_for(asyncio.gather(*client._session_tasks), 1)
    assert events == ["click"]
    assert len([packet for packet in peer.writes if packet[1] == 16]) == 2
    await client.stop()


@pytest.mark.asyncio
async def test_disconnect_cancels_upstream_ack_write(session_peer):
    client, peer = session_peer
    await client.start()
    entered = asyncio.Event()
    original = peer.write_gatt_char.side_effect

    async def write(uuid, packet):
        if packet[1] == 16:
            entered.set()
            await asyncio.Event().wait()
        else:
            await original(uuid, packet)

    peer.write_gatt_char.side_effect = write
    peer.notify(12, struct.pack("<I", 1) + bytes(6) + b"\x02")
    await asyncio.wait_for(entered.wait(), 1)
    tasks = tuple(client._session_tasks)
    await asyncio.wait_for(client.disconnect(), 1)
    assert all(task.done() for task in tasks)
    assert not client._session_tasks
    assert not client._background_tasks
    assert not peer.is_connected
    await client.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("opcode,length", [(30, 16), (30, 20), (31, 16), (31, 20)])
async def test_duo_init_boot_id_is_detected_by_length_not_opcode(opcode, length):
    client = make_packet_client(DeviceType.DUO)
    response = bytearray(length)
    response[0:2] = bytes([3, opcode])
    if length == 20:
        struct.pack_into("<I", response, 16, 1234)
    client._response_queue.put_nowait(bytes(response))
    await client._wait_for_handler_opcodes([opcode])
    assert client._init_response_received
    assert client._boot_id == (1234 if length == 20 else None)

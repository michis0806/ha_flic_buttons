"""Offline regressions against the real pyflic-ble 0.2.5 protocol client.

Only BLE I/O and the Home Assistant form API are replaced by test doubles.
Run with Python 3.14 and the repository's requirements-test.txt.
"""

import asyncio
import importlib
import sys
import types
from pathlib import Path
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

    def module(name, **values):
        result = types.ModuleType(name)
        result.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, result)

    module("homeassistant")
    module("homeassistant.components")
    module(
        "homeassistant.components.bluetooth",
        BluetoothScanningMode=object,
        BluetoothServiceInfoBleak=object,
        async_discovered_service_info=lambda _: [],
        async_process_advertisements=AsyncMock(),
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
    )
    return flow


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
    result = await flow.async_step_pair({})
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
    result = await flow.async_step_pair({})
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
    task = asyncio.create_task(flow.async_step_pair({}))
    await entered.wait()
    await flow.async_step_pair(None)
    await flow.async_step_pair({})
    assert len(transport.instances) == 1
    assert flow._pairing_started
    flow.async_remove()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not flow._pairing_started
    assert flow._client is None
    assert flow._pairing_task is None
    assert transport.instances[0].disconnects == 1

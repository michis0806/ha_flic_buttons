"""Sensor logic tests with HA API doubles, no live Bluetooth or HA writes."""

import ast
import importlib
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import Mock

import pytest
from pyflic_ble import DeviceType, FlicState

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "flic_button"


@pytest.fixture
def sensors(monkeypatch):
    def module(name, **values):
        mod = types.ModuleType(name)
        mod.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, mod)
        return mod

    class Entity:
        def async_on_remove(self, remove):
            self.removers.append(remove)

        async def async_added_to_hass(self):
            self.removers = []
            self.writes = 0

        def async_write_ha_state(self):
            self.writes += 1

    class SensorEntity(Entity):
        pass

    @dataclass
    class SensorEntityDescription:
        key: str
        translation_key: str
        device_class: str
        native_unit_of_measurement: str
        state_class: str
        entity_category: str
        suggested_display_precision: int

    module("homeassistant")
    module("homeassistant.components")
    bluetooth = module(
        "homeassistant.components.bluetooth", async_last_service_info=Mock()
    )
    module(
        "homeassistant.components.sensor",
        SensorDeviceClass=types.SimpleNamespace(
            VOLTAGE="voltage", SIGNAL_STRENGTH="signal_strength", BATTERY="battery"
        ),
        SensorEntity=SensorEntity,
        SensorEntityDescription=SensorEntityDescription,
        SensorStateClass=types.SimpleNamespace(MEASUREMENT="measurement"),
    )
    module(
        "homeassistant.const",
        PERCENTAGE="%",
        SIGNAL_STRENGTH_DECIBELS_MILLIWATT="dBm",
        UnitOfElectricPotential=types.SimpleNamespace(VOLT="V"),
    )
    module("homeassistant.core", HomeAssistant=object, callback=lambda f: f)
    module("homeassistant.helpers")
    module(
        "homeassistant.helpers.entity",
        Entity=Entity,
        EntityCategory=types.SimpleNamespace(DIAGNOSTIC="diagnostic"),
    )
    module(
        "homeassistant.helpers.device_registry",
        CONNECTION_BLUETOOTH="bluetooth",
        DeviceInfo=dict,
    )
    module(
        "homeassistant.helpers.entity_platform",
        AddConfigEntryEntitiesCallback=object,
    )
    package = module(
        "flic_sensor_test", FlicButtonData=object, FlicButtonConfigEntry=object
    )
    package.__path__ = [str(ROOT)]
    for name in ("const", "entity", "sensor"):
        monkeypatch.delitem(sys.modules, f"flic_sensor_test.{name}", raising=False)
    return importlib.import_module("flic_sensor_test.sensor"), bluetooth


@pytest.fixture
def data():
    callbacks = []

    def register(cb):
        callbacks.append(cb)
        return lambda: callbacks.remove(cb)

    client = types.SimpleNamespace(
        address="AA:BB:CC:DD:EE:FF",
        device_type=DeviceType.FLIC2,
        state=FlicState(
            connected=True,
            battery_voltage=3.08,
            firmware_version=11,
            device_name="Flic",
        ),
        register_state_callback=register,
        callbacks=callbacks,
    )
    return types.SimpleNamespace(client=client, serial_number="BA00-A00000")


@pytest.mark.asyncio
async def test_setup_creates_distinct_diagnostics_on_same_device(sensors, data):
    mod, _ = sensors
    add = Mock()
    await mod.async_setup_entry(object(), types.SimpleNamespace(runtime_data=data), add)
    battery, signal, level = add.call_args.args[0]
    assert battery._attr_unique_id == "AA:BB:CC:DD:EE:FF-battery_voltage"
    assert signal._attr_unique_id == "AA:BB:CC:DD:EE:FF-signal_strength"
    assert level._attr_unique_id == "AA:BB:CC:DD:EE:FF-battery_level"
    assert level._attr_device_info == battery._attr_device_info
    assert level.entity_description.native_unit_of_measurement == "%"
    assert level.entity_description.device_class == "battery"
    assert level._attr_extra_state_attributes == {"estimated": True}
    assert battery._attr_device_info == signal._attr_device_info
    assert battery._attr_device_info["identifiers"] == {
        ("flic_button", data.client.address)
    }
    assert battery.entity_description.native_unit_of_measurement == "V"
    assert signal.entity_description.native_unit_of_measurement == "dBm"
    for sensor in (battery, signal, level):
        assert sensor.entity_description.entity_category == "diagnostic"
        assert sensor.entity_description.state_class == "measurement"
    assert not battery._attr_should_poll
    assert not level._attr_should_poll
    assert signal._attr_should_poll
    assert mod.SCAN_INTERVAL.total_seconds() == 60


@pytest.mark.asyncio
async def test_battery_uses_real_client_state_and_cleans_up_callback(sensors, data):
    mod, _ = sensors
    sensor = mod.FlicBatteryVoltageSensor(data)
    await sensor.async_added_to_hass()
    assert sensor.native_value == 3.08
    data.client.state.battery_voltage = 2.93
    data.client.callbacks[0](data.client.state)
    assert sensor.native_value == 2.93
    assert sensor.writes == 1
    data.client.state.connected = False
    data.client.callbacks[0](data.client.state)
    assert sensor.available
    assert sensor.native_value == 2.93
    for remove in sensor.removers:
        remove()
    assert not data.client.callbacks


def test_missing_battery_stays_unknown(sensors, data):
    mod, _ = sensors
    data.client.state.battery_voltage = None
    data.client.state.connected = False
    sensor = mod.FlicBatteryVoltageSensor(data)
    assert sensor.available
    assert sensor.native_value is None


@pytest.mark.asyncio
async def test_signal_initial_cache_poll_and_receiver_change(sensors, data):
    mod, bluetooth = sensors
    sensor = mod.FlicSignalStrengthSensor(data)
    sensor.hass = object()
    bluetooth.async_last_service_info.return_value = types.SimpleNamespace(
        rssi=-67, source="hci0"
    )
    await sensor.async_added_to_hass()
    bluetooth.async_last_service_info.assert_called_once_with(
        sensor.hass, data.client.address, connectable=False
    )
    assert sensor.native_value == -67
    assert sensor.extra_state_attributes == {
        "source": "hci0",
        "measurement": "last_advertisement",
    }
    bluetooth.async_last_service_info.return_value = types.SimpleNamespace(
        rssi=-53, source="shelly-passive"
    )
    await sensor.async_update()
    assert sensor.native_value == -53
    assert sensor.extra_state_attributes["source"] == "shelly-passive"
    data.client.state.connected = False
    assert sensor.available
    bluetooth.async_last_service_info.return_value = None
    await sensor.async_update()
    assert sensor.native_value == -53
    for remove in sensor.removers:
        remove()
    assert not data.client.callbacks


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "info", [None, types.SimpleNamespace(rssi=127), types.SimpleNamespace(rssi=None)]
)
async def test_missing_or_invalid_rssi_is_unknown_not_zero(sensors, data, info):
    mod, bluetooth = sensors
    sensor = mod.FlicSignalStrengthSensor(data)
    sensor.hass = object()
    bluetooth.async_last_service_info.return_value = info
    await sensor.async_update()
    assert sensor.native_value is None


def test_sensor_platform_and_translations():
    tree = ast.parse((ROOT / "__init__.py").read_text())
    platforms = next(
        node.value
        for node in tree.body
        if isinstance(node, ast.AnnAssign) and node.target.id == "PLATFORMS"
    )
    assert [node.attr for node in platforms.elts] == ["EVENT", "SENSOR"]
    for filename in ("strings.json", "translations/en.json", "translations/de.json"):
        entities = json.loads((ROOT / filename).read_text())["entity"]
        assert entities["sensor"]["battery_voltage"]["name"]
        assert entities["sensor"]["battery_level"]["name"]
        assert entities["sensor"]["signal_strength"]["name"]
        assert "button" in entities["event"]


@pytest.mark.parametrize(
    "voltage,expected",
    [
        (None, None),
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
        (0, 0),
        (2.0, 0),
        (2.1, 0),
        (2.27, 3),
        (2.44, 6),
        (2.59, 12),
        (2.74, 18),
        (2.82, 30),
        (2.9, 42),
        (2.95, 71),
        (3.0, 100),
        (3.2203125, 100),
    ],
)
def test_manufacturer_battery_curve(sensors, voltage, expected):
    mod, _ = sensors
    assert mod.estimate_flic2_battery_percentage(voltage) == expected


def test_battery_estimate_is_monotonic_and_bounded(sensors):
    mod, _ = sensors
    values = [
        mod.estimate_flic2_battery_percentage(mv / 1000) for mv in range(1800, 3601)
    ]
    assert values == sorted(values)
    assert min(values) == 0
    assert max(values) == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("device_type", [DeviceType.DUO, DeviceType.TWIST])
async def test_flic2_battery_curve_not_used_for_other_models(
    sensors, data, device_type
):
    mod, _ = sensors
    data.client.device_type = device_type
    add = Mock()
    await mod.async_setup_entry(object(), types.SimpleNamespace(runtime_data=data), add)
    assert len(add.call_args.args[0]) == 2


@pytest.mark.asyncio
async def test_battery_percentage_follows_client_updates_without_io(sensors, data):
    mod, _ = sensors
    sensor = mod.FlicBatteryLevelSensor(data)
    await sensor.async_added_to_hass()
    assert sensor.native_value == 100
    data.client.state.battery_voltage = 2.9
    data.client.callbacks[0](data.client.state)
    assert sensor.native_value == 42
    assert sensor.writes == 1
    data.client.state.battery_voltage = None
    assert sensor.native_value is None
    for remove in sensor.removers:
        remove()
    assert not data.client.callbacks


@pytest.fixture
def events(sensors, monkeypatch):
    entity = sys.modules["homeassistant.helpers.entity"].Entity

    class EventEntity(entity):
        pass

    @dataclass
    class EventEntityDescription:
        key: str
        translation_key: str
        event_types: list[str]
        device_class: str

    mod = types.ModuleType("homeassistant.components.event")
    mod.EventEntity = EventEntity
    mod.EventEntityDescription = EventEntityDescription
    mod.EventDeviceClass = types.SimpleNamespace(BUTTON="button")
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    monkeypatch.delitem(sys.modules, "flic_sensor_test.event", raising=False)
    return importlib.import_module("flic_sensor_test.event")


def test_press_event_types_are_only_click_double_and_hold(events):
    assert events.EVENT_DESCRIPTION.event_types == ["click", "double_click", "hold"]
    for description in (
        events.DUO_BIG_BUTTON_DESCRIPTION,
        events.DUO_SMALL_BUTTON_DESCRIPTION,
        events.TWIST_SELECTOR_BUTTON_DESCRIPTION,
        events.TWIST_DEFAULT_BUTTON_DESCRIPTION,
    ):
        assert "up" not in description.event_types
        assert "down" not in description.event_types
        assert {"click", "double_click", "hold"} <= set(description.event_types)
    assert "swipe_up" in events.DUO_BIG_BUTTON_DESCRIPTION.event_types
    assert "rotate_clockwise" in events.TWIST_SELECTOR_BUTTON_DESCRIPTION.event_types


def test_raw_edges_do_not_write_ha_state_and_repeated_clicks_survive(events, data):
    sensor = events.FlicButtonEventEntity(data, events.EVENT_DESCRIPTION)
    sensor._trigger_event = Mock()
    sensor.async_write_ha_state = Mock()
    for event_type in ("down", "up"):
        sensor._async_handle_event(event_type, {})
    sensor._trigger_event.assert_not_called()
    sensor.async_write_ha_state.assert_not_called()
    for event_type in ("click", "click", "double_click", "hold", "up"):
        sensor._async_handle_event(event_type, {})
    assert [call.args[0] for call in sensor._trigger_event.call_args_list] == [
        "click",
        "click",
        "double_click",
        "hold",
    ]
    assert sensor.async_write_ha_state.call_count == 4


def test_duo_button_index_filter_is_preserved(events, data):
    sensor = events.FlicButtonEventEntity(
        data, events.DUO_SMALL_BUTTON_DESCRIPTION, button_index=1
    )
    sensor._trigger_event = Mock()
    sensor.async_write_ha_state = Mock()
    sensor._async_handle_event("click", {"button_index": 0})
    sensor._trigger_event.assert_not_called()
    sensor._async_handle_event("click", {"button_index": 1})
    sensor._trigger_event.assert_called_once_with("click", {"button_index": 1})

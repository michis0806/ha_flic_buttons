"""Cached battery voltage and Bluetooth reception diagnostics for Flic."""

from datetime import timedelta
from itertools import pairwise
from math import isfinite
from typing import Any, override

from homeassistant.components import bluetooth
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    UnitOfElectricPotential,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from pyflic_ble import DeviceType, FlicState

from . import FlicButtonConfigEntry, FlicButtonData
from .entity import FlicButtonEntity

# Poll only HA's in-memory advertisement cache, never the button or adapter.
SCAN_INTERVAL = timedelta(minutes=1)
PARALLEL_UPDATES = 0

BATTERY_LEVEL = SensorEntityDescription(
    key="battery_level",
    translation_key="battery_level",
    device_class=SensorDeviceClass.BATTERY,
    native_unit_of_measurement=PERCENTAGE,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    suggested_display_precision=0,
)

# Voltage/percentage points from Shortcut Labs' Flic 2 Android SDK:
# https://github.com/50ButtonsEach/flic2lib-android/blob/master/
# flic2lib-android/src/main/java/io/flic/flic2libandroid/BatteryLevel.java
# This is an estimate, especially imprecise between 50 and 100 percent.
_FLIC2_BATTERY_CURVE = ((2100, 0), (2440, 6), (2740, 18), (2900, 42), (3000, 100))


def estimate_flic2_battery_percentage(voltage: float | None) -> int | None:
    """Interpolate the manufacturer's millivolt curve using integer arithmetic."""
    if voltage is None or not isfinite(voltage):
        return None
    millivolts = int(voltage * 1000)
    if millivolts <= _FLIC2_BATTERY_CURVE[0][0]:
        return 0
    for (low_mv, low_pct), (high_mv, high_pct) in pairwise(_FLIC2_BATTERY_CURVE):
        if millivolts < high_mv:
            return low_pct + (millivolts - low_mv) * (high_pct - low_pct) // (
                high_mv - low_mv
            )
    return 100


BATTERY_VOLTAGE = SensorEntityDescription(
    key="battery_voltage",
    translation_key="battery_voltage",
    device_class=SensorDeviceClass.VOLTAGE,
    native_unit_of_measurement=UnitOfElectricPotential.VOLT,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    suggested_display_precision=2,
)

SIGNAL_STRENGTH = SensorEntityDescription(
    key="signal_strength",
    translation_key="signal_strength",
    device_class=SensorDeviceClass.SIGNAL_STRENGTH,
    native_unit_of_measurement=SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    suggested_display_precision=0,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: FlicButtonConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add diagnostics without changing existing voltage entity statistics."""
    data = entry.runtime_data
    entities: list[FlicDiagnosticSensor] = [
        FlicBatteryVoltageSensor(data),
        FlicSignalStrengthSensor(data),
    ]
    # Do not apply a Flic 2 cell curve to other models/battery chemistries.
    if data.client.device_type == DeviceType.FLIC2:
        entities.append(FlicBatteryLevelSensor(data))
    async_add_entities(entities)


class FlicDiagnosticSensor(FlicButtonEntity, SensorEntity):
    """Keep last-known measurements visible even while the button is asleep."""

    def __init__(
        self, data: FlicButtonData, description: SensorEntityDescription
    ) -> None:
        super().__init__(data)
        self.entity_description = description
        self._attr_unique_id = f"{self._client.address}-{description.key}"

    @property
    @override
    def available(self) -> bool:
        """A missing measurement is unknown, not a connection failure."""
        return True

    @callback
    @override
    def _handle_state_update(self, state: FlicState) -> None:
        """Publish cached values without treating disconnection as data loss."""
        self.async_write_ha_state()


class FlicBatteryLevelSensor(FlicDiagnosticSensor):
    """Estimated remaining Flic 2 battery capacity, not a measured percentage."""

    def __init__(self, data: FlicButtonData) -> None:
        super().__init__(data, BATTERY_LEVEL)
        self._attr_extra_state_attributes = {"estimated": True}

    @property
    @override
    def native_value(self) -> int | None:
        return estimate_flic2_battery_percentage(self._client.state.battery_voltage)


class FlicBatteryVoltageSensor(FlicDiagnosticSensor):
    """Voltage last read by the existing client at startup or reconnection."""

    def __init__(self, data: FlicButtonData) -> None:
        super().__init__(data, BATTERY_VOLTAGE)

    @property
    @override
    def native_value(self) -> float | None:
        return self._client.state.battery_voltage


class FlicSignalStrengthSensor(FlicDiagnosticSensor):
    """Last-known advertising RSSI; not live GATT connection strength."""

    _attr_should_poll = True

    def __init__(self, data: FlicButtonData) -> None:
        super().__init__(data, SIGNAL_STRENGTH)
        self._rssi: int | None = None
        self._source: str | None = None

    @property
    @override
    def native_value(self) -> int | None:
        return self._rssi

    @property
    @override
    def extra_state_attributes(self) -> dict[str, Any]:
        return {"source": self._source, "measurement": "last_advertisement"}

    @override
    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        await self.async_update()

    @override
    async def async_update(self) -> None:
        # Include passive receivers (e.g. Shelly); they need not support GATT.
        # HA selects the best receiver. Expose its source to avoid implying that
        # this is necessarily the adapter holding the active Flic connection.
        info = bluetooth.async_last_service_info(
            self.hass, self._client.address, connectable=False
        )
        if info is not None and info.rssi is not None and info.rssi != 127:
            self._rssi = info.rssi
            self._source = info.source
        # No new advertisement: retain the previous value, never invent zero.

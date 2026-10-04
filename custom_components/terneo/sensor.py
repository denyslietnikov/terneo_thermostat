"""Sensor platform for Terneo/Welrok thermostat."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfTemperature, UnitOfPower, UnitOfTime, UnitOfEnergy
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER, SENSOR_TYPES
from .coordinator import TerneoConfigEntry, TerneoCoordinator
from .thermostat import TerneoThermostat

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class TerneoSensorEntityDescription(SensorEntityDescription):
    """Describes Terneo sensor entity."""
    
    value_fn: Callable[[TerneoThermostat], float | int | str | datetime | None]
    available_fn: Callable[[TerneoThermostat], bool] = lambda t: True
    new_version_only: bool = False
    requires_settings: bool = False


def get_sensor_type_name(thermostat: TerneoThermostat) -> str | None:
    """Get sensor type name."""
    sensor_type = thermostat.sensor_type
    if sensor_type is not None:
        return SENSOR_TYPES.get(sensor_type, f"Unknown ({sensor_type})")
    return None


SENSOR_DESCRIPTIONS: tuple[TerneoSensorEntityDescription, ...] = (
    TerneoSensorEntityDescription(
        key="floor_temperature",
        translation_key="floor_temperature",
        name="Floor Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda t: t.floor_temperature,
    ),
    TerneoSensorEntityDescription(
        key="air_temperature",
        translation_key="air_temperature",
        name="Air Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda t: t.air_temperature,
        new_version_only=True,
    ),
    TerneoSensorEntityDescription(
        key="setpoint",
        translation_key="setpoint",
        name="Target Temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda t: t.setpoint,
    ),
    TerneoSensorEntityDescription(
        key="relay_on_time_limit",
        requires_settings=True,
        translation_key="relay_on_time_limit",
        name="Continuous Heating Limit",
        icon="mdi:timer-alert",
        native_unit_of_measurement=UnitOfTime.HOURS,
        value_fn=lambda t: t.relay_on_time_limit,
        entity_registry_enabled_default=False,
    ),
    TerneoSensorEntityDescription(
        key="sensor_type",
        requires_settings=True,
        translation_key="sensor_type_display",
        name="Sensor Type",
        icon="mdi:thermometer",
        value_fn=get_sensor_type_name,
        entity_registry_enabled_default=False,
    ),
    TerneoSensorEntityDescription(
        key="ble_sensor_connected",
        requires_settings=True,
        translation_key="ble_sensor_connected",
        name="Wireless Sensor",
        icon="mdi:bluetooth-connect",
        value_fn=lambda t: "Connected" if t.ble_sensor_bind else "Not connected",
        new_version_only=True,
        entity_registry_enabled_default=False,
    ),
    TerneoSensorEntityDescription(
        key="manual_floor_temp",
        requires_settings=True,
        translation_key="manual_floor_temp",
        name="Manual Floor Setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda t: t.manual_floor_temperature,
        entity_registry_enabled_default=False,
    ),
    TerneoSensorEntityDescription(
        key="manual_air_temp",
        requires_settings=True,
        translation_key="manual_air_temp",
        name="Manual Air Setpoint",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        value_fn=lambda t: t.manual_air_temperature,
        new_version_only=True,
        entity_registry_enabled_default=False,
    ),
    TerneoSensorEntityDescription(
        key="heating_energy",
        translation_key="heating_energy",
        name="Heating Energy",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        icon="mdi:lightning-bolt",
        value_fn=lambda t: t.heating_energy_kwh,
        available_fn=lambda t: t.power_watts is not None and t.power_watts > 0,
    ),
    TerneoSensorEntityDescription(
        key="heating_time",
        translation_key="heating_time",
        name="Heating Time",
        device_class=SensorDeviceClass.DURATION,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfTime.HOURS,
        icon="mdi:timer",
        value_fn=lambda t: t.heating_time_hours,
    ),
    TerneoSensorEntityDescription(
        key="current_power",
        requires_settings=True,
        translation_key="current_power",
        name="Current Power",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        icon="mdi:flash",
        value_fn=lambda t: t.power_watts if t.relay_state else 0,
        available_fn=lambda t: t.power_watts is not None and t.power_watts > 0,
    ),
    TerneoSensorEntityDescription(
        key="heating_active",
        translation_key="heating_active",
        name="Heating Active",
        icon="mdi:radiator",
        value_fn=lambda t: "On" if t.relay_state else "Off",
    ),
)


CONNECTION_SENSOR_DESCRIPTIONS: tuple[TerneoSensorEntityDescription, ...] = (
    TerneoSensorEntityDescription(
        key="last_successful_update",
        translation_key="last_successful_update",
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda t: t.last_successful_update,
    ),
    TerneoSensorEntityDescription(
        key="cached_state_age",
        translation_key="cached_state_age",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda t: round(t.cached_state_age, 1) if t.cached_state_age is not None else None,
    ),
    TerneoSensorEntityDescription(
        key="consecutive_update_failures",
        translation_key="consecutive_update_failures",
        icon="mdi:lan-disconnect",
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda t: t.consecutive_update_failures,
    ),
    TerneoSensorEntityDescription(
        key="last_request_duration",
        translation_key="last_request_duration",
        device_class=SensorDeviceClass.DURATION,
        native_unit_of_measurement=UnitOfTime.SECONDS,
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda t: round(t.last_request_duration, 3) if t.last_request_duration is not None else None,
    ),
    TerneoSensorEntityDescription(
        key="last_update_error",
        translation_key="last_update_error",
        device_class=SensorDeviceClass.ENUM,
        options=["none", "timeout", "transport", "http", "json", "protocol"],
        entity_category=EntityCategory.DIAGNOSTIC,
        entity_registry_enabled_default=False,
        value_fn=lambda t: t.last_refresh_error_category or "none",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TerneoConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Terneo sensor entities from a config entry."""
    coordinator = entry.runtime_data
    thermostat = coordinator.thermostat

    entities = []
    for description in (*SENSOR_DESCRIPTIONS, *CONNECTION_SENSOR_DESCRIPTIONS):
        # Skip new version only sensors for old devices
        if description.new_version_only and not thermostat.is_new_version:
            continue
        
        entities.append(TerneoSensorEntity(coordinator, thermostat, entry, description))

    async_add_entities(entities)


class TerneoSensorEntity(CoordinatorEntity[TerneoCoordinator], SensorEntity):
    """Terneo sensor entity."""

    _attr_has_entity_name = True
    entity_description: TerneoSensorEntityDescription

    def __init__(
        self,
        coordinator: TerneoCoordinator,
        thermostat: TerneoThermostat,
        entry: TerneoConfigEntry,
        description: TerneoSensorEntityDescription,
    ) -> None:
        """Initialize the sensor entity."""
        super().__init__(coordinator)
        self._thermostat = thermostat
        self._entry = entry
        self.entity_description = description
        self._attr_entity_registry_enabled_default = (
            description.entity_registry_enabled_default
            or (
                description.entity_category is not EntityCategory.DIAGNOSTIC
                and entry.options.get("show_advanced_sensors", False)
            )
        )
        
        self._attr_unique_id = f"{thermostat.sn}_{description.key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, thermostat.sn)},
            "name": entry.title,
            "manufacturer": MANUFACTURER,
            "model": "OZ" if thermostat.is_new_version else "OZ (Legacy)",
            "serial_number": thermostat.sn,
        }

    @property
    def native_value(self) -> float | int | str | datetime | None:
        """Return the sensor value."""
        return self.entity_description.value_fn(self._thermostat)

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        if self.entity_description.entity_category is EntityCategory.DIAGNOSTIC:
            return True
        if not super().available or not self._thermostat.available:
            return False
        if self.entity_description.requires_settings and not self._thermostat.settings_available:
            return False
        return self.entity_description.available_fn(self._thermostat)

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self.async_write_ha_state()

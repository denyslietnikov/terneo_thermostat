"""Climate platform for Terneo/Welrok thermostat."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import ControlType, OperationMode
from .coordinator import TerneoConfigEntry, TerneoCoordinator
from .entity import TerneoEntity
from .thermostat import TerneoThermostat

_LOGGER = logging.getLogger(__name__)

SUPPORT_FLAGS = (
    ClimateEntityFeature.TARGET_TEMPERATURE
    | ClimateEntityFeature.TURN_ON
    | ClimateEntityFeature.TURN_OFF
    | ClimateEntityFeature.PRESET_MODE
)

HVAC_MODES = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]

PRESET_SCHEDULE = "schedule"
PRESET_MANUAL = "manual"
PRESET_MODES = [PRESET_SCHEDULE, PRESET_MANUAL]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TerneoConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Terneo climate entity from a config entry."""
    coordinator = entry.runtime_data
    thermostat = coordinator.thermostat

    async_add_entities([TerneoClimateEntity(coordinator, thermostat, entry)])


class TerneoClimateEntity(TerneoEntity, ClimateEntity):
    """Terneo climate entity."""

    _attr_name = None
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 1
    _attr_supported_features = SUPPORT_FLAGS
    _attr_hvac_modes = HVAC_MODES
    _attr_preset_modes = PRESET_MODES

    def __init__(
        self,
        coordinator: TerneoCoordinator,
        thermostat: TerneoThermostat,
        entry: TerneoConfigEntry,
    ) -> None:
        """Initialize the climate entity."""
        super().__init__(coordinator, thermostat, entry, "climate")

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        # Return air temperature for new version if control type is air
        if self._thermostat.is_new_version:
            control_type = self._thermostat.control_type
            if control_type in [ControlType.AIR, ControlType.AIR_WITH_FLOOR_LIMIT]:
                return self._thermostat.air_temperature
        return self._thermostat.floor_temperature

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        return self._thermostat.setpoint

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        control_type = self._thermostat.control_type or ControlType.FLOOR

        if self._thermostat.is_new_version and control_type != ControlType.FLOOR:
            limit = self._thermostat.lower_air_limit
            return float(limit) if limit is not None else 5.0

        limit = self._thermostat.lower_limit
        return float(limit) if limit is not None else 5.0

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        control_type = self._thermostat.control_type or ControlType.FLOOR

        if self._thermostat.is_new_version and control_type != ControlType.FLOOR:
            limit = self._thermostat.upper_air_limit
            return float(limit) if limit is not None else 35.0

        limit = self._thermostat.upper_limit
        return float(limit) if limit is not None else 45.0

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return current HVAC mode."""
        mode = self._thermostat.mode

        if mode == -1 or self._thermostat.power_on is False:
            return HVACMode.OFF
        if mode is None or self._thermostat.power_on is None:
            return None

        # Check if in cooling mode
        if self._thermostat.cooling_mode:
            return HVACMode.COOL

        # Schedule mode = AUTO, Manual mode = HEAT
        if mode == OperationMode.SCHEDULE:
            return HVACMode.AUTO

        return HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return current HVAC action."""
        if self._thermostat.power_on is False or self._thermostat.mode == -1:
            return HVACAction.OFF
        if self._thermostat.power_on is None or self._thermostat.relay_state is None:
            return None

        if self._thermostat.relay_state:
            if self._thermostat.cooling_mode:
                return HVACAction.COOLING
            return HVACAction.HEATING

        return HVACAction.IDLE

    @property
    def preset_mode(self) -> str | None:
        """Return current preset mode."""
        mode = self._thermostat.mode

        if mode is None:
            return None
        if mode == OperationMode.SCHEDULE:
            return PRESET_SCHEDULE
        return PRESET_MANUAL

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return extra state attributes."""
        attrs = {
            "serial_number": self._thermostat.sn,
            "device_type": "new" if self._thermostat.is_new_version else "old",
            "control_type": self._get_control_type_name(),
            "relay_state": self._thermostat.relay_state,
            "floor_temperature": self._thermostat.floor_temperature,
            "settings_confirmed": self._thermostat.settings_available,
        }

        if self._thermostat.is_new_version:
            attrs["air_temperature"] = self._thermostat.air_temperature

        if (
            self._thermostat.settings_available
            and self._thermostat.hysteresis is not None
        ):
            attrs["hysteresis"] = self._thermostat.hysteresis

        if (
            self._thermostat.settings_available
            and self._thermostat.power_watts is not None
        ):
            attrs["power_watts"] = self._thermostat.power_watts

        return attrs

    def _get_control_type_name(self) -> str:
        """Get human-readable control type name."""
        control_type = self._thermostat.control_type
        if control_type == ControlType.FLOOR:
            return "floor"
        elif control_type == ControlType.AIR:
            return "air"
        elif control_type == ControlType.AIR_WITH_FLOOR_LIMIT:
            return "air_with_floor_limit"
        return "unknown"

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set new target temperature."""
        if (temperature := kwargs.get(ATTR_TEMPERATURE)) is None:
            return

        await self.coordinator.async_execute_command(
            self._thermostat.set_setpoint, temperature
        )
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set new HVAC mode."""
        if hvac_mode not in HVAC_MODES:
            raise ServiceValidationError(f"Unsupported HVAC mode: {hvac_mode}")
        await self.coordinator.async_execute_command(
            self._thermostat.set_hvac_mode, hvac_mode, refresh_on_failure=True
        )
        await self.coordinator.async_request_refresh()

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """Set new preset mode."""
        if preset_mode == PRESET_SCHEDULE:
            await self.coordinator.async_execute_command(
                self._thermostat.set_mode, OperationMode.SCHEDULE
            )
        elif preset_mode == PRESET_MANUAL:
            await self.coordinator.async_execute_command(
                self._thermostat.set_mode, OperationMode.MANUAL
            )
        else:
            raise ServiceValidationError(f"Unsupported preset: {preset_mode}")

        await self.coordinator.async_request_refresh()

    async def async_turn_on(self) -> None:
        """Turn on the thermostat."""
        await self.coordinator.async_execute_command(self._thermostat.turn_on)
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self) -> None:
        """Turn off the thermostat."""
        await self.coordinator.async_execute_command(self._thermostat.turn_off)
        await self.coordinator.async_request_refresh()

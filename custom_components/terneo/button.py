"""Button platform for Terneo/Welrok thermostat."""

from __future__ import annotations

import logging

from homeassistant.components.button import ButtonDeviceClass, ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .coordinator import TerneoConfigEntry, TerneoCoordinator
from .entity import TerneoEntity
from .thermostat import TerneoThermostat

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TerneoConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Terneo button entities from a config entry."""
    coordinator = entry.runtime_data
    thermostat = coordinator.thermostat

    async_add_entities([TerneoRestartButton(coordinator, thermostat, entry)])


class TerneoRestartButton(TerneoEntity, ButtonEntity):
    """Terneo restart button entity."""

    _attr_translation_key = "restart"
    _attr_icon = "mdi:restart"
    _attr_device_class = ButtonDeviceClass.RESTART

    def __init__(
        self,
        coordinator: TerneoCoordinator,
        thermostat: TerneoThermostat,
        entry: TerneoConfigEntry,
    ) -> None:
        """Initialize the button entity."""
        super().__init__(coordinator, thermostat, entry, "restart")

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self._thermostat.available

    async def async_press(self) -> None:
        """Handle the button press."""
        await self.coordinator.async_execute_command(self._thermostat.restart)

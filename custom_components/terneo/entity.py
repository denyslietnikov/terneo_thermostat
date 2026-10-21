"""Shared identity and coordinator behavior for Terneo entities."""

from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import TerneoConfigEntry, TerneoCoordinator
from .thermostat import TerneoThermostat


class TerneoEntity(CoordinatorEntity[TerneoCoordinator]):
    """Keep device metadata and entity identities consistent across platforms."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: TerneoCoordinator,
        thermostat: TerneoThermostat,
        entry: TerneoConfigEntry,
        key: str,
    ) -> None:
        super().__init__(coordinator)
        self._thermostat = thermostat
        self._entry = entry
        self._attr_unique_id = f"{thermostat.sn}_{key}"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, thermostat.sn)},
            "name": entry.title,
            "manufacturer": MANUFACTURER,
            "model": "OZ" if thermostat.is_new_version else "OZ (Legacy)",
            "serial_number": thermostat.sn,
        }

    @property
    def available(self) -> bool:
        return super().available and self._thermostat.available

    @callback
    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()

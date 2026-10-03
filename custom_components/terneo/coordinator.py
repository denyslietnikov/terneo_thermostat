"""Coordinate polling and commands for one Terneo thermostat."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL
from .thermostat import TerneoThermostat

_LOGGER = logging.getLogger(__name__)


class TerneoCoordinator(DataUpdateCoordinator[TerneoThermostat]):
    """Serialize polling and commands to avoid overlapping HTTP requests."""

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, thermostat: TerneoThermostat
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"Terneo {thermostat.sn}",
            update_interval=timedelta(
                seconds=entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.thermostat = thermostat
        self._request_lock = asyncio.Lock()

    async def _async_update_data(self) -> TerneoThermostat:
        async with self._request_lock:
            success = await self.hass.async_add_executor_job(self.thermostat.update)
        if success or (self.thermostat.available and self.thermostat.has_state):
            return self.thermostat
        raise UpdateFailed(
            self.thermostat.last_update_error or "Failed to update thermostat data"
        )

    async def async_execute_command(
        self, command: Callable[..., Any], *args: Any
    ) -> None:
        """Expose failed commands to the UI and automation traces."""
        async with self._request_lock:
            try:
                success = await self.hass.async_add_executor_job(command, *args)
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
            if not success:
                raise HomeAssistantError(
                    self.thermostat.last_update_error or "Thermostat command failed"
                )


TerneoConfigEntry = ConfigEntry[TerneoCoordinator]

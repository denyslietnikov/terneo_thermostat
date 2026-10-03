"""Coordinate polling and commands for one Terneo thermostat."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import datetime, timedelta
from hashlib import sha256
import logging
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryError, HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .thermostat import TerneoThermostat

_LOGGER = logging.getLogger(__name__)
ENERGY_SAVE_INTERVAL = timedelta(minutes=5)
ENERGY_STORAGE_VERSION = 1


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
        identity = sha256(thermostat.sn.encode()).hexdigest()
        self._energy_store: Store[dict[str, float]] = Store(
            hass, ENERGY_STORAGE_VERSION, f"{DOMAIN}.energy.{identity}",
            private=True, atomic_writes=True,
        )
        self._energy_restored = False
        self._energy_shutdown_complete = False
        self._unsub_energy_save: CALLBACK_TYPE | None = None
        self._unsub_energy_stop: CALLBACK_TYPE | None = None

    async def async_restore_energy_counters(self) -> None:
        """Restore before the first poll and before platform entities are created."""
        if self._energy_restored:
            return
        data = await self._energy_store.async_load()
        if data is not None:
            try:
                self.thermostat.restore_energy_counters(data)
            except ValueError as err:
                # Do not silently overwrite invalid nonempty storage with zeros.
                raise ConfigEntryError("Invalid stored heating counters") from err
        self._energy_restored = True

    @callback
    def async_start_energy_persistence(self) -> None:
        """Bound disk writes independently of poll frequency, including idle periods."""
        if (
            not self._energy_restored or self._energy_shutdown_complete
            or self._unsub_energy_save is not None
        ):
            return

        async def save_periodically(_now: datetime) -> None:
            await self.async_save_energy_counters()

        async def save_on_stop(_event: Event) -> None:
            self._unsub_energy_stop = None
            await self.async_shutdown()

        self._unsub_energy_save = async_track_time_interval(
            self.hass, save_periodically, ENERGY_SAVE_INTERVAL
        )
        self._unsub_energy_stop = self.hass.bus.async_listen_once(
            EVENT_HOMEASSISTANT_STOP, save_on_stop
        )

    async def async_save_energy_counters(self) -> None:
        """Persist only verified totals, with the executor excluded by the device lock."""
        async with self._request_lock:
            if not self._energy_restored or self._energy_shutdown_complete:
                return
            await self._energy_store.async_save(self.thermostat.energy_counters)

    async def async_shutdown(self) -> None:
        """Stop all timers and flush confirmed counters without estimating a tail."""
        if self._unsub_energy_save is not None:
            self._unsub_energy_save()
            self._unsub_energy_save = None
        if self._unsub_energy_stop is not None:
            self._unsub_energy_stop()
            self._unsub_energy_stop = None
        await super().async_shutdown()
        await self.async_save_energy_counters()
        self._energy_shutdown_complete = True

    async def _async_execute_request(
        self, command: Callable[..., Any], *args: Any
    ) -> Any:
        """Keep the caller's lock until executor work finishes, even on cancellation."""
        request = asyncio.ensure_future(self.hass.async_add_executor_job(command, *args))
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(request)
            except asyncio.CancelledError:
                if request.cancelled():
                    raise
                cancelled = True
            except Exception:
                if cancelled:
                    raise asyncio.CancelledError from None
                raise
            else:
                break
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _async_update_data(self) -> TerneoThermostat:
        async with self._request_lock:
            try:
                success = await self._async_execute_request(self.thermostat.update)
            except Exception:
                self.thermostat.break_heating_interval()
                raise
        if success or (self.thermostat.available and self.thermostat.has_state):
            return self.thermostat
        raise UpdateFailed(
            self.thermostat.last_update_error or "Failed to update thermostat data"
        )

    async def async_execute_command(
        self, command: Callable[..., Any], *args: Any, refresh_on_failure: bool = False
    ) -> None:
        """Expose failed commands to the UI and automation traces."""
        error = None
        async with self._request_lock:
            try:
                success = await self._async_execute_request(command, *args)
            except ValueError as err:
                raise ServiceValidationError(str(err)) from err
            if not success:
                error = HomeAssistantError(
                    self.thermostat.last_update_error or "Thermostat command failed"
                )
        if error is not None:
            # Refresh outside the lock: polling acquires the same per-device lock.
            if refresh_on_failure:
                try:
                    await self.async_request_refresh()
                except HomeAssistantError:
                    _LOGGER.debug("Unable to refresh thermostat after a failed command")
            raise error

    async def async_get_diagnostics(self) -> dict[str, Any]:
        """Snapshot metrics after any active executor operation, without polling."""
        async with self._request_lock:
            return {
                "coordinator": {
                    "last_update_success": self.last_update_success,
                    "last_exception_type": (
                        type(self.last_exception).__name__
                        if self.last_exception is not None else None
                    ),
                    "update_interval_seconds": (
                        self.update_interval.total_seconds()
                        if self.update_interval is not None else None
                    ),
                },
                "connection": self.thermostat.connection_diagnostics,
            }


TerneoConfigEntry = ConfigEntry[TerneoCoordinator]

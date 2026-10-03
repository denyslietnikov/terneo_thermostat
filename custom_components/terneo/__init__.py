"""The Terneo/Welrok thermostat integration."""
from __future__ import annotations

from typing import cast

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er, service
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DEVICE_TYPE,
    CONF_SERIAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_TIMEOUT,
    DEVICE_TYPE_OLD,
    DOMAIN,
)
from .coordinator import TerneoConfigEntry, TerneoCoordinator
from .thermostat import TerneoThermostat

try:
    from homeassistant.helpers import target as target_helpers
except ImportError:  # Home Assistant before the target helper was split out.
    target_helpers = None

PLATFORMS = [
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.BUTTON,
]

SERVICE_SET_FLOOR_LIMITS = "set_floor_limits"
SERVICE_SET_AIR_LIMITS = "set_air_limits"
SERVICE_RESTART = "restart"

SERVICE_FLOOR_LIMITS_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Required("lower"): vol.All(vol.Coerce(int), vol.Range(min=5, max=40)),
        vol.Required("upper"): vol.All(vol.Coerce(int), vol.Range(min=10, max=45)),
    }
)
SERVICE_AIR_LIMITS_SCHEMA = cv.make_entity_service_schema(
    {
        vol.Required("lower"): vol.All(vol.Coerce(int), vol.Range(min=5, max=30)),
        vol.Required("upper"): vol.All(vol.Coerce(int), vol.Range(min=10, max=35)),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register actions even when the thermostat is offline."""
    async def selected_coordinators(call: ServiceCall) -> list[TerneoCoordinator]:
        registry = er.async_get(hass)
        if target_helpers is None:
            selected = service.async_extract_referenced_entity_ids(hass, call)
        else:
            selected = target_helpers.async_extract_referenced_entity_ids(
                hass, target_helpers.TargetSelection(call.data)
            )
        coordinators: dict[str, TerneoCoordinator] = {}
        for entity_id in selected.referenced | selected.indirectly_referenced:
            entity = registry.async_get(entity_id)
            if (
                entity is None
                or entity.platform != DOMAIN
                or not entity_id.startswith("climate.")
            ):
                if entity_id in selected.referenced:
                    raise ServiceValidationError(
                        f"{entity_id} is not a Terneo climate entity"
                    )
                continue
            entry = hass.config_entries.async_get_entry(entity.config_entry_id)
            if entry is None or entry.state is not ConfigEntryState.LOADED:
                raise ServiceValidationError(f"{entity_id} is not loaded")
            coordinators[entry.entry_id] = cast(TerneoConfigEntry, entry).runtime_data
        if not coordinators:
            raise ServiceValidationError("Select at least one Terneo thermostat")
        return list(coordinators.values())

    async def handle_limits(call: ServiceCall) -> None:
        lower, upper = call.data["lower"], call.data["upper"]
        if lower > upper:
            raise ServiceValidationError("Minimum temperature must not exceed maximum")
        coordinators = await selected_coordinators(call)
        air = call.service == SERVICE_SET_AIR_LIMITS
        if air and any(not c.thermostat.is_new_version for c in coordinators):
            raise ServiceValidationError("Air limits require a thermostat with an air sensor")
        for coordinator in coordinators:
            method = (
                coordinator.thermostat.set_air_limits
                if air
                else coordinator.thermostat.set_floor_limits
            )
            await coordinator.async_execute_command(method, lower, upper)
            await coordinator.async_request_refresh()

    async def handle_restart(call: ServiceCall) -> None:
        for coordinator in await selected_coordinators(call):
            await coordinator.async_execute_command(coordinator.thermostat.restart)

    hass.services.async_register(
        DOMAIN, SERVICE_SET_FLOOR_LIMITS, handle_limits,
        schema=SERVICE_FLOOR_LIMITS_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_SET_AIR_LIMITS, handle_limits,
        schema=SERVICE_AIR_LIMITS_SCHEMA,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_RESTART, handle_restart,
        schema=cv.make_entity_service_schema({}),
    )
    return True


async def async_setup_entry(hass: HomeAssistant, entry: TerneoConfigEntry) -> bool:
    """Set up a thermostat; first refresh retries setup if it is offline."""
    thermostat = TerneoThermostat(
        serial_number=entry.data[CONF_SERIAL],
        host=entry.data[CONF_HOST],
        device_type=entry.data.get(CONF_DEVICE_TYPE, DEVICE_TYPE_OLD),
        timeout=entry.options.get("timeout", DEFAULT_TIMEOUT),
        max_heating_interval=max(
            300, 2 * entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)
        ),
    )
    coordinator = TerneoCoordinator(hass, entry, thermostat)
    await coordinator.async_restore_energy_counters()
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.async_start_energy_persistence()
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    return True


async def async_update_options(hass: HomeAssistant, entry: TerneoConfigEntry) -> None:
    """Reload when options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: TerneoConfigEntry) -> bool:
    """Unload platforms and stop polling."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.async_shutdown()
    return True

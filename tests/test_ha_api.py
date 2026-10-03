"""Regression tests using real Home Assistant APIs and mocked device HTTP."""
from __future__ import annotations

import asyncio
import json
from tempfile import TemporaryDirectory
from types import MappingProxyType
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
import voluptuous as vol
import requests

from custom_components.terneo import async_setup, async_setup_entry, async_unload_entry
from custom_components.terneo.button import TerneoRestartButton
from custom_components.terneo.climate import TerneoClimateEntity
from custom_components.terneo.const import DOMAIN
from custom_components.terneo.coordinator import TerneoCoordinator
from custom_components.terneo.number import NUMBER_DESCRIPTIONS, TerneoNumberEntity
from custom_components.terneo.select import SELECT_DESCRIPTIONS, TerneoSelectEntity
from custom_components.terneo.sensor import SENSOR_DESCRIPTIONS, TerneoSensorEntity
from custom_components.terneo.switch import SWITCH_DESCRIPTIONS, TerneoSwitchEntity
from custom_components.terneo.thermostat import TerneoThermostat

PARAMS = {"par": [[125, 7, "0"], [23, 2, "2"], [17, 4, "100"]]}
STATUS = {"t.1": "368", "t.5": "400", "m.1": "3", "f.0": "1"}


def response(data):
    result = requests.Response()
    result.status_code = 200
    result._content = json.dumps(data).encode()
    return result


def entry(serial="058009000543474239343620000159", state=ConfigEntryState.LOADED):
    return ConfigEntry(
        version=1, minor_version=1, domain=DOMAIN,
        title=serial, data={"host": "192.0.2.1", "serial": serial},
        options={}, source="user", unique_id=serial,
        discovery_keys=MappingProxyType({}), subentries_data=[], state=state,
    )


class ThermostatTests(unittest.TestCase):
    def setUp(self):
        self.thermostat = TerneoThermostat("test", "192.0.2.1")
        self.sleep = patch("custom_components.terneo.thermostat.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def poll(self, params=PARAMS, status=STATUS):
        with patch("requests.post", side_effect=[response(params), response(status)]):
            return self.thermostat.update()

    def test_failures_retain_state_then_unavailable_and_recover(self):
        self.assertTrue(self.poll())
        with patch("requests.post", side_effect=requests.Timeout("offline")):
            for expected in (True, True, False):
                self.assertFalse(self.thermostat.update())
                self.assertEqual(self.thermostat.available, expected)
                self.assertEqual(self.thermostat.floor_temperature, 23)
        self.assertTrue(self.poll())
        self.assertTrue(self.thermostat.available)
        self.assertIsNone(self.thermostat.last_update_error)

    def test_invalid_status_does_not_publish_partial_parameters(self):
        self.assertTrue(self.poll())
        changed = {"par": [[125, 7, "1"], [23, 2, "9"]]}
        self.assertFalse(self.poll(changed, {"t.1": "invalid"}))
        self.assertEqual(self.thermostat.brightness, 2)
        self.assertTrue(self.thermostat.power_on)

    def test_non_object_json_is_counted_as_failure(self):
        with patch("requests.post", return_value=response([])):
            self.assertFalse(self.thermostat.update())
        self.assertFalse(self.thermostat.available)
        self.assertFalse(self.thermostat.has_state)

    def test_truncated_json_is_counted_as_failure(self):
        result = response({})
        result._content = b'{"par": [[125, 7, "'
        with patch("requests.post", return_value=result):
            self.assertFalse(self.thermostat.update())
        self.assertIn("JSON", self.thermostat.last_update_error)

    def test_malformed_parameters_are_counted_as_failure(self):
        with patch("requests.post", return_value=response({"par": [[23]]})):
            self.assertFalse(self.thermostat.update())
        self.assertIn("parameter", self.thermostat.last_update_error)

    def test_serial_mismatch_is_rejected(self):
        with patch("requests.post", return_value=response({"sn": "other", **PARAMS})):
            self.assertFalse(self.thermostat.update())
        self.assertIn("serial", self.thermostat.last_update_error)


class HomeAssistantTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.hass.config_entries = MagicMock()
        self.entries = {}
        self.hass.config_entries.async_get_entry.side_effect = self.entries.get
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await ar.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        self.sleep = patch("custom_components.terneo.thermostat.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)
        self.coordinators = []
        await async_setup(self.hass, {})

    async def asyncTearDown(self):
        for coordinator in self.coordinators:
            await coordinator.async_shutdown()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    def make_coordinator(self, serial="058009000543474239343620000159"):
        config = entry(serial)
        thermostat = TerneoThermostat(serial, "192.0.2.1")
        coordinator = TerneoCoordinator(self.hass, config, thermostat)
        config.runtime_data = coordinator
        self.entries[config.entry_id] = config
        self.coordinators.append(coordinator)
        return config, thermostat, coordinator

    def register_climate(self, config):
        return er.async_get(self.hass).async_get_or_create(
            "climate", DOMAIN, config.unique_id, config_entry=config,
        ).entity_id

    async def test_service_only_controls_selected_thermostat(self):
        first, thermostat, coordinator = self.make_coordinator()
        second, other, _ = self.make_coordinator("other")
        entity_id = self.register_climate(first)
        self.register_climate(second)
        coordinator.async_request_refresh = AsyncMock()
        with patch.object(thermostat, "set_floor_limits", return_value=True) as command:
            with patch.object(other, "set_floor_limits", return_value=True) as untouched:
                await self.hass.services.async_call(
                    DOMAIN, "set_floor_limits",
                    {"entity_id": entity_id, "lower": 5, "upper": 35},
                    blocking=True,
                )
        command.assert_called_once_with(5, 35)
        untouched.assert_not_called()
        coordinator.async_request_refresh.assert_awaited_once()

    async def test_restart_respects_device_target(self):
        first, thermostat, coordinator = self.make_coordinator()
        second, other, _ = self.make_coordinator("other")
        device = dr.async_get(self.hass).async_get_or_create(
            config_entry_id=first.entry_id, identifiers={(DOMAIN, thermostat.sn)}
        )
        er.async_get(self.hass).async_get_or_create(
            "climate", DOMAIN, first.unique_id,
            config_entry=first, device_id=device.id,
        )
        self.register_climate(second)
        with patch.object(thermostat, "restart", return_value=True) as command:
            with patch.object(other, "restart", return_value=True) as untouched:
                await self.hass.services.async_call(
                    DOMAIN, "restart", {"device_id": device.id}, blocking=True,
                )
        command.assert_called_once()
        untouched.assert_not_called()

    async def test_missing_target_does_not_broadcast(self):
        _, thermostat, _ = self.make_coordinator()
        with patch.object(thermostat, "set_floor_limits") as command:
            with self.assertRaises(vol.Invalid):
                await self.hass.services.async_call(
                    DOMAIN, "set_floor_limits", {"lower": 5, "upper": 35},
                    blocking=True,
                )
        command.assert_not_called()

    async def test_invalid_limits_do_not_send_command(self):
        config, thermostat, _ = self.make_coordinator()
        entity_id = self.register_climate(config)
        with patch.object(thermostat, "set_floor_limits") as command:
            with self.assertRaises(ServiceValidationError):
                await self.hass.services.async_call(
                    DOMAIN, "set_floor_limits",
                    {"entity_id": entity_id, "lower": 40, "upper": 10},
                    blocking=True,
                )
        command.assert_not_called()

    async def test_offline_initial_setup_requests_retry(self):
        config = entry(state=ConfigEntryState.SETUP_IN_PROGRESS)
        with patch("requests.post", side_effect=requests.Timeout("offline")):
            with self.assertRaises(ConfigEntryNotReady):
                await async_setup_entry(self.hass, config)

    async def test_failed_commands_raise_for_all_platforms(self):
        config, thermostat, coordinator = self.make_coordinator()
        entities = [
            (TerneoClimateEntity(coordinator, thermostat, config), "async_turn_off", ()),
            (TerneoSwitchEntity(coordinator, thermostat, config, SWITCH_DESCRIPTIONS[0]), "async_turn_off", ()),
            (TerneoNumberEntity(coordinator, thermostat, config, NUMBER_DESCRIPTIONS[0]), "async_set_native_value", (2,)),
            (TerneoSelectEntity(coordinator, thermostat, config, SELECT_DESCRIPTIONS[1]), "async_select_option", ("10k",)),
            (TerneoRestartButton(coordinator, thermostat, config), "async_press", ()),
        ]
        with patch("requests.post", side_effect=requests.Timeout("offline")):
            for entity, method, args in entities:
                with self.subTest(platform=type(entity).__name__):
                    with self.assertRaises(HomeAssistantError):
                        await getattr(entity, method)(*args)

    async def test_entities_follow_coordinator_failure(self):
        config, thermostat, coordinator = self.make_coordinator()
        with patch("requests.post", side_effect=[response(PARAMS), response(STATUS)]):
            await coordinator.async_refresh()
        entities = [
            TerneoClimateEntity(coordinator, thermostat, config),
            TerneoSwitchEntity(coordinator, thermostat, config, SWITCH_DESCRIPTIONS[0]),
            TerneoNumberEntity(coordinator, thermostat, config, NUMBER_DESCRIPTIONS[0]),
            TerneoSelectEntity(coordinator, thermostat, config, SELECT_DESCRIPTIONS[0]),
            TerneoRestartButton(coordinator, thermostat, config),
            TerneoSensorEntity(coordinator, thermostat, config, SENSOR_DESCRIPTIONS[0]),
        ]
        self.assertTrue(all(entity.available for entity in entities))
        with patch.object(thermostat, "update", side_effect=RuntimeError("unexpected")):
            await coordinator.async_refresh()
        self.assertTrue(thermostat.available)
        self.assertFalse(any(entity.available for entity in entities))

    async def test_commands_do_not_overlap_polling(self):
        _, thermostat, coordinator = self.make_coordinator()
        polling_started = asyncio.Event()
        release_polling = asyncio.Event()
        async def executor(command, *args):
            if command == thermostat.update:
                polling_started.set()
                await release_polling.wait()
                return True
            self.assertTrue(release_polling.is_set())
            return True
        with patch.object(self.hass, "async_add_executor_job", side_effect=executor):
            polling = asyncio.create_task(coordinator._async_update_data())
            await polling_started.wait()
            command = asyncio.create_task(coordinator.async_execute_command(thermostat.turn_off))
            await asyncio.sleep(0)
            self.assertFalse(command.done())
            release_polling.set()
            await asyncio.gather(polling, command)


    async def test_coordinator_retains_state_until_failure_threshold(self):
        config, thermostat, coordinator = self.make_coordinator()
        with patch("requests.post", side_effect=[response(PARAMS), response(STATUS)]):
            await coordinator.async_refresh()
        entity = TerneoClimateEntity(coordinator, thermostat, config)
        with patch("requests.post", side_effect=requests.Timeout("offline")):
            for expected in (True, True, False):
                await coordinator.async_refresh()
                self.assertEqual(entity.available, expected)
        with patch("requests.post", side_effect=[response(PARAMS), response(STATUS)]):
            await coordinator.async_refresh()
        self.assertTrue(entity.available)

    async def test_successful_setup_stores_runtime_data(self):
        config = entry(state=ConfigEntryState.SETUP_IN_PROGRESS)
        self.hass.config_entries.async_forward_entry_setups = AsyncMock()
        with patch("requests.post", side_effect=[response(PARAMS), response(STATUS)]):
            self.assertTrue(await async_setup_entry(self.hass, config))
        self.coordinators.append(config.runtime_data)
        self.assertIsInstance(config.runtime_data, TerneoCoordinator)
        self.assertTrue(config.runtime_data.thermostat.has_state)

    async def test_unload_stops_coordinator_after_platforms_unload(self):
        config, _, coordinator = self.make_coordinator()
        self.hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        with patch.object(coordinator, "async_shutdown", new_callable=AsyncMock) as shutdown:
            self.assertTrue(await async_unload_entry(self.hass, config))
            shutdown.assert_awaited_once()
        self.hass.config_entries.async_unload_platforms.return_value = False
        with patch.object(coordinator, "async_shutdown", new_callable=AsyncMock) as shutdown:
            self.assertFalse(await async_unload_entry(self.hass, config))
            shutdown.assert_not_called()


if __name__ == "__main__":
    unittest.main()

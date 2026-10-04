"""Regression tests using real Home Assistant APIs and mocked device HTTP."""

from __future__ import annotations

import asyncio
import json
import unittest
from tempfile import TemporaryDirectory
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import voluptuous as vol
from homeassistant.components.climate import HVACMode
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.terneo import async_setup, async_setup_entry, async_unload_entry
from custom_components.terneo.button import TerneoRestartButton
from custom_components.terneo.climate import (
    PRESET_MANUAL,
    PRESET_SCHEDULE,
    TerneoClimateEntity,
)
from custom_components.terneo.const import DEVICE_TYPE_NEW, DEVICE_TYPE_OLD, DOMAIN
from custom_components.terneo.coordinator import TerneoCoordinator
from custom_components.terneo.number import NUMBER_DESCRIPTIONS, TerneoNumberEntity
from custom_components.terneo.select import SELECT_DESCRIPTIONS, TerneoSelectEntity
from custom_components.terneo.sensor import SENSOR_DESCRIPTIONS, TerneoSensorEntity
from custom_components.terneo.switch import SWITCH_DESCRIPTIONS, TerneoSwitchEntity
from custom_components.terneo.thermostat import TerneoThermostat
from tests.http import FakeSession, Response

PARAMS = {"par": [[125, 7, "0"], [23, 2, "2"], [17, 4, "100"]]}
STATUS = {"t.1": "368", "t.5": "400", "m.1": "3", "f.0": "1"}


def response(data):
    result = Response()
    result.status_code = 200
    result._content = json.dumps(data).encode()
    return result


def entry(serial="058009000543474239343620000159", state=ConfigEntryState.LOADED):
    return ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title=serial,
        data={"host": "192.0.2.1", "serial": serial},
        options={},
        source="user",
        unique_id=serial,
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
        state=state,
    )


class ThermostatTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.thermostat = TerneoThermostat("test", "192.0.2.1", session=FakeSession())
        self.sleep = patch("custom_components.terneo.thermostat.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    async def poll(self, params=PARAMS, status=STATUS):
        with patch("tests.http.post", side_effect=[response(params), response(status)]):
            return await self.thermostat.update()

    async def test_failures_retain_state_then_unavailable_and_recover(self):
        self.assertTrue(await self.poll())
        with patch("tests.http.post", side_effect=TimeoutError("offline")):
            for expected in (True, True, False):
                self.assertFalse(await self.thermostat.update())
                self.assertEqual(self.thermostat.available, expected)
                self.assertEqual(self.thermostat.floor_temperature, 23)
        self.assertTrue(await self.poll())
        self.assertTrue(self.thermostat.available)
        self.assertIsNone(self.thermostat.last_update_error)

    async def test_invalid_status_does_not_publish_partial_parameters(self):
        self.assertTrue(await self.poll())
        changed = {"par": [[125, 7, "1"], [23, 2, "9"]]}
        self.assertFalse(await self.poll(changed, {"t.1": "invalid"}))
        self.assertEqual(self.thermostat.brightness, 2)
        self.assertTrue(self.thermostat.power_on)

    async def test_non_object_json_is_counted_as_failure(self):
        with patch("tests.http.post", return_value=response([])):
            self.assertFalse(await self.thermostat.update())
        self.assertFalse(self.thermostat.available)
        self.assertFalse(self.thermostat.has_state)

    async def test_truncated_json_is_counted_as_failure(self):
        result = response({})
        result._content = b'{"par": [[125, 7, "'
        with patch("tests.http.post", return_value=result):
            self.assertFalse(await self.thermostat.update())
        self.assertIn("JSON", self.thermostat.last_update_error)

    async def test_malformed_parameters_are_counted_as_failure(self):
        with patch("tests.http.post", return_value=response({"par": [[23]]})):
            self.assertFalse(await self.thermostat.update())
        self.assertIn("parameter", self.thermostat.last_update_error)

    async def test_serial_mismatch_is_rejected(self):
        with patch("tests.http.post", return_value=response({"sn": "other", **PARAMS})):
            self.assertFalse(await self.thermostat.update())
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
        self.sleep = patch("custom_components.terneo.thermostat.sleep")
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

    def make_coordinator(
        self, serial="058009000543474239343620000159", device_type=DEVICE_TYPE_OLD
    ):
        config = entry(serial)
        thermostat = TerneoThermostat(
            serial, "192.0.2.1", device_type, session=FakeSession()
        )
        coordinator = TerneoCoordinator(self.hass, config, thermostat)
        config.runtime_data = coordinator
        self.entries[config.entry_id] = config
        self.coordinators.append(coordinator)
        return config, thermostat, coordinator

    def register_climate(self, config):
        return (
            er.async_get(self.hass)
            .async_get_or_create(
                "climate",
                DOMAIN,
                config.unique_id,
                config_entry=config,
            )
            .entity_id
        )

    async def test_service_only_controls_selected_thermostat(self):
        first, thermostat, coordinator = self.make_coordinator()
        second, other, _ = self.make_coordinator("other")
        entity_id = self.register_climate(first)
        self.register_climate(second)
        coordinator.async_request_refresh = AsyncMock()
        with patch.object(thermostat, "set_floor_limits", return_value=True) as command:
            with patch.object(
                other, "set_floor_limits", return_value=True
            ) as untouched:
                await self.hass.services.async_call(
                    DOMAIN,
                    "set_floor_limits",
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
            "climate",
            DOMAIN,
            first.unique_id,
            config_entry=first,
            device_id=device.id,
        )
        self.register_climate(second)
        with patch.object(thermostat, "restart", return_value=True) as command:
            with patch.object(other, "restart", return_value=True) as untouched:
                await self.hass.services.async_call(
                    DOMAIN,
                    "restart",
                    {"device_id": device.id},
                    blocking=True,
                )
        command.assert_called_once()
        untouched.assert_not_called()

    async def test_missing_target_does_not_broadcast(self):
        _, thermostat, _ = self.make_coordinator()
        with patch.object(thermostat, "set_floor_limits") as command:
            with self.assertRaises(vol.Invalid):
                await self.hass.services.async_call(
                    DOMAIN,
                    "set_floor_limits",
                    {"lower": 5, "upper": 35},
                    blocking=True,
                )
        command.assert_not_called()

    async def test_invalid_limits_do_not_send_command(self):
        config, thermostat, _ = self.make_coordinator()
        entity_id = self.register_climate(config)
        with patch.object(thermostat, "set_floor_limits") as command:
            with self.assertRaises(ServiceValidationError):
                await self.hass.services.async_call(
                    DOMAIN,
                    "set_floor_limits",
                    {"entity_id": entity_id, "lower": 40, "upper": 10},
                    blocking=True,
                )
        command.assert_not_called()

    async def test_offline_initial_setup_requests_retry(self):
        config = entry(state=ConfigEntryState.SETUP_IN_PROGRESS)
        with patch("tests.http.post", side_effect=TimeoutError("offline")):
            with self.assertRaises(ConfigEntryNotReady):
                await async_setup_entry(self.hass, config)

    async def test_failed_commands_raise_for_all_platforms(self):
        config, thermostat, coordinator = self.make_coordinator()
        entities = [
            (
                TerneoClimateEntity(coordinator, thermostat, config),
                "async_turn_off",
                (),
            ),
            (
                TerneoSwitchEntity(
                    coordinator, thermostat, config, SWITCH_DESCRIPTIONS[0]
                ),
                "async_turn_off",
                (),
            ),
            (
                TerneoNumberEntity(
                    coordinator, thermostat, config, NUMBER_DESCRIPTIONS[0]
                ),
                "async_set_native_value",
                (2,),
            ),
            (
                TerneoSelectEntity(
                    coordinator, thermostat, config, SELECT_DESCRIPTIONS[1]
                ),
                "async_select_option",
                ("10k",),
            ),
            (TerneoRestartButton(coordinator, thermostat, config), "async_press", ()),
        ]
        coordinator.async_request_refresh = AsyncMock()
        for scenario, http in (
            ("timeout", {"side_effect": TimeoutError("offline")}),
            ("blocked", {"return_value": response({"success": "block"})}),
            ("error", {"return_value": response({"status": "error"})}),
            ("unconfirmed", {"return_value": response({"sn": thermostat.sn})}),
        ):
            with patch("tests.http.post", **http):
                for entity, method, args in entities:
                    with self.subTest(
                        scenario=scenario, platform=type(entity).__name__
                    ):
                        with self.assertRaises(HomeAssistantError):
                            await getattr(entity, method)(*args)
        coordinator.async_request_refresh.assert_not_awaited()

    async def test_confirmed_command_updates_cache_and_refreshes(self):
        config, thermostat, coordinator = self.make_coordinator()
        thermostat._power_on = True
        coordinator.async_request_refresh = AsyncMock()
        climate = TerneoClimateEntity(coordinator, thermostat, config)
        with patch(
            "tests.http.post",
            return_value=response(
                {
                    "sn": thermostat.sn,
                    "par": [[125, 7, "1"]],
                }
            ),
        ):
            await climate.async_turn_off()
        self.assertFalse(thermostat.power_on)
        coordinator.async_request_refresh.assert_awaited_once()

    async def test_uncertain_command_error_reaches_home_assistant(self):
        _, thermostat, coordinator = self.make_coordinator()
        thermostat._power_on = True
        with patch(
            "tests.http.post", side_effect=TimeoutError("private details")
        ) as post:
            with self.assertRaisesRegex(
                HomeAssistantError, "outcome is unknown"
            ) as error:
                await coordinator.async_execute_command(thermostat.turn_off)
        self.assertNotIn("private details", str(error.exception))
        self.assertTrue(thermostat.power_on)
        post.assert_called_once()

    async def test_entities_follow_coordinator_failure(self):
        config, thermostat, coordinator = self.make_coordinator()
        with patch("tests.http.post", side_effect=[response(PARAMS), response(STATUS)]):
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

        async def poll(*args):
            polling_started.set()
            await release_polling.wait()
            return True

        async def write():
            self.assertTrue(release_polling.is_set())
            return True

        with (
            patch.object(thermostat, "update", side_effect=poll),
            patch.object(thermostat, "turn_off", side_effect=write),
        ):
            polling = asyncio.create_task(coordinator._async_update_data())
            await polling_started.wait()
            command = asyncio.create_task(
                coordinator.async_execute_command(thermostat.turn_off)
            )
            await asyncio.sleep(0)
            self.assertFalse(command.done())
            release_polling.set()
            await asyncio.gather(polling, command)

    async def test_coordinator_retains_state_until_failure_threshold(self):
        config, thermostat, coordinator = self.make_coordinator()
        with patch("tests.http.post", side_effect=[response(PARAMS), response(STATUS)]):
            await coordinator.async_refresh()
        entity = TerneoClimateEntity(coordinator, thermostat, config)
        with patch("tests.http.post", side_effect=TimeoutError("offline")):
            for expected in (True, True, False):
                await coordinator.async_refresh()
                self.assertEqual(entity.available, expected)
        with patch("tests.http.post", side_effect=[response(PARAMS), response(STATUS)]):
            await coordinator.async_refresh()
        self.assertTrue(entity.available)

    async def test_successful_setup_stores_runtime_data(self):
        config = entry(state=ConfigEntryState.SETUP_IN_PROGRESS)
        self.hass.config_entries.async_forward_entry_setups = AsyncMock()
        with patch("tests.http.post", side_effect=[response(PARAMS), response(STATUS)]):
            self.assertTrue(await async_setup_entry(self.hass, config))
        self.coordinators.append(config.runtime_data)
        self.assertIsInstance(config.runtime_data, TerneoCoordinator)
        self.assertTrue(config.runtime_data.thermostat.has_state)

    async def test_unload_stops_coordinator_after_platforms_unload(self):
        config, _, coordinator = self.make_coordinator()
        self.hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        with patch.object(
            coordinator, "async_shutdown", new_callable=AsyncMock
        ) as shutdown:
            self.assertTrue(await async_unload_entry(self.hass, config))
            shutdown.assert_awaited_once()
        self.hass.config_entries.async_unload_platforms.return_value = False
        with patch.object(
            coordinator, "async_shutdown", new_callable=AsyncMock
        ) as shutdown:
            self.assertFalse(await async_unload_entry(self.hass, config))
            shutdown.assert_not_called()

    async def test_hvac_modes_write_once_and_publish_only_full_readback(self):
        expected = {
            HVACMode.OFF: [[125, 7, "1"]],
            HVACMode.HEAT: [[125, 7, "0"], [118, 7, "0"], [2, 2, "3"]],
            HVACMode.COOL: [[125, 7, "0"], [118, 7, "1"], [2, 2, "3"]],
            HVACMode.AUTO: [[125, 7, "0"], [2, 2, "0"]],
        }
        for device_type in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            for target, params in expected.items():
                with self.subTest(device_type=device_type, target=target):
                    params = [p[:] for p in params]
                    manual = "1" if device_type == DEVICE_TYPE_OLD else "3"
                    if target in (HVACMode.HEAT, HVACMode.COOL):
                        params[-1][2] = manual
                    config, thermostat, coordinator = self.make_coordinator(
                        device_type=device_type
                    )
                    device_params = {
                        125: [125, 7, "0"],
                        118: [118, 7, "1"],
                        2: [2, 2, manual],
                    }
                    thermostat._parameters = {
                        n: (p[1], p[2]) for n, p in device_params.items()
                    }
                    thermostat._power_on = True
                    thermostat._mode = 3
                    climate = TerneoClimateEntity(coordinator, thermostat, config)

                    def post(url, **kwargs):
                        data = kwargs["json"]
                        if "par" in data:
                            self.assertEqual(data["par"], params)
                            device_params.update({p[0]: p[:] for p in data["par"]})
                        if data.get("cmd") == 4:
                            return response(
                                {
                                    "sn": thermostat.sn,
                                    "t.1": "368",
                                    "t.5": "400",
                                    "m.1": "0" if device_params[2][2] == "0" else "3",
                                    "f.0": "0",
                                    "f.16": device_params[125][2],
                                }
                            )
                        return response(
                            {"sn": thermostat.sn, "par": list(device_params.values())}
                        )

                    async def refresh():
                        self.assertFalse(coordinator._request_lock.locked())
                        self.assertTrue(thermostat.power_on)
                        self.assertEqual(thermostat.mode, 3)
                        self.assertTrue(thermostat.cooling_mode)
                        await coordinator.async_refresh()

                    coordinator.async_request_refresh = AsyncMock(side_effect=refresh)
                    with patch("tests.http.post", side_effect=post) as http:
                        await climate.async_set_hvac_mode(target)
                    self.assertEqual(
                        [c.kwargs["json"] for c in http.call_args_list],
                        [
                            {"sn": thermostat.sn, "par": params},
                            {"sn": thermostat.sn, "cmd": 1},
                            {"sn": thermostat.sn, "cmd": 4},
                        ],
                    )
                    coordinator.async_request_refresh.assert_awaited_once()
                    self.assertEqual(thermostat.power_on, target != HVACMode.OFF)
                    if target == HVACMode.AUTO:
                        self.assertEqual(climate.preset_mode, PRESET_SCHEDULE)
                        self.assertTrue(thermostat.cooling_mode)
                    else:
                        self.assertEqual(climate.hvac_mode, target)

    async def test_uncertain_mode_failure_reads_actual_state_and_preserves_error(self):
        config, thermostat, coordinator = self.make_coordinator()
        thermostat._power_on = False
        thermostat._mode = -1
        climate = TerneoClimateEntity(coordinator, thermostat, config)
        coordinator.async_request_refresh = AsyncMock(
            side_effect=coordinator.async_refresh
        )
        with patch(
            "tests.http.post",
            side_effect=[
                TimeoutError("private transport details"),
                response(
                    {
                        "sn": thermostat.sn,
                        "par": [[125, 7, "0"], [118, 7, "1"], [2, 2, "1"]],
                    }
                ),
                response(
                    {
                        "sn": thermostat.sn,
                        "t.1": "368",
                        "t.5": "400",
                        "m.1": "3",
                        "f.0": "0",
                    }
                ),
            ],
        ) as http:
            with self.assertRaisesRegex(
                HomeAssistantError, "outcome is unknown"
            ) as error:
                await asyncio.wait_for(climate.async_set_hvac_mode(HVACMode.HEAT), 5)
        self.assertNotIn("private transport details", str(error.exception))
        self.assertIsNone(thermostat.last_update_error)
        self.assertEqual(climate.hvac_mode, HVACMode.COOL)
        self.assertEqual(http.call_count, 3)
        self.assertEqual(sum("par" in c.kwargs["json"] for c in http.call_args_list), 1)
        coordinator.async_request_refresh.assert_awaited_once()

    async def test_ax_success_marker_is_verified_under_lock_before_full_state_refresh(
        self,
    ):
        config, thermostat, coordinator = self.make_coordinator()
        thermostat._power_on = False
        thermostat._mode = -1
        thermostat._setpoint = 16
        thermostat._parameters = {125: (7, "1"), 118: (7, "0"), 2: (2, "1")}
        climate = TerneoClimateEntity(coordinator, thermostat, config)
        params = [[125, 7, "0"], [118, 7, "0"], [2, 2, "1"], [5, 1, "16"]]
        requests_seen = []

        def post(url, **kwargs):
            data = kwargs["json"]
            requests_seen.append(data)
            self.assertTrue(coordinator._request_lock.locked())
            self.assertFalse(thermostat.power_on)
            if len(requests_seen) <= 3:
                self.assertEqual(thermostat._parameters[125], (7, "1"))
            if "par" in data:
                return response({"success": "true"})
            if data["cmd"] == 1:
                return response({"sn": thermostat.sn, "par": params})
            return response(
                {
                    "sn": thermostat.sn,
                    "t.1": "341",
                    "t.5": "256",
                    "m.1": "3",
                    "f.16": "0",
                    "f.0": "0",
                }
            )

        async def refresh():
            self.assertFalse(coordinator._request_lock.locked())
            self.assertFalse(thermostat.power_on)
            await coordinator.async_refresh()

        coordinator.async_request_refresh = AsyncMock(side_effect=refresh)
        with patch("tests.http.post", side_effect=post):
            await asyncio.wait_for(climate.async_set_hvac_mode(HVACMode.HEAT), 5)
        self.assertEqual(
            requests_seen,
            [
                {"sn": thermostat.sn, "par": params[:3]},
                {"sn": thermostat.sn, "cmd": 1},
                {"sn": thermostat.sn, "cmd": 1},
                {"sn": thermostat.sn, "cmd": 4},
            ],
        )
        self.assertEqual(climate.hvac_mode, HVACMode.HEAT)
        self.assertEqual(thermostat._parameters[125], (7, "0"))
        self.assertEqual(thermostat.setpoint, 16)
        self.assertFalse(thermostat.relay_state)
        self.assertIsNone(thermostat.last_update_error)
        coordinator.async_request_refresh.assert_awaited_once()

    async def test_ax_success_marker_with_unchanged_settings_is_not_a_successful_write(
        self,
    ):
        # Observed on the kitchen AX: success=true, but power and setpoint unchanged.
        config, thermostat, coordinator = self.make_coordinator()
        thermostat._power_on = False
        thermostat._mode = -1
        thermostat._setpoint = 16
        climate = TerneoClimateEntity(coordinator, thermostat, config)
        coordinator.async_request_refresh = AsyncMock(
            side_effect=coordinator.async_refresh
        )
        with patch(
            "tests.http.post",
            side_effect=[
                response({"success": "true"}),
                response(
                    {
                        "sn": thermostat.sn,
                        "par": [
                            [125, 7, "1"],
                            [118, 7, "0"],
                            [2, 2, "1"],
                            [5, 1, "16"],
                        ],
                    }
                ),
                response(
                    {
                        "sn": thermostat.sn,
                        "par": [
                            [125, 7, "1"],
                            [118, 7, "0"],
                            [2, 2, "1"],
                            [5, 1, "16"],
                        ],
                    }
                ),
                response(
                    {
                        "sn": thermostat.sn,
                        "t.1": "341",
                        "t.5": "256",
                        "m.1": "3",
                        "f.16": "1",
                        "f.0": "0",
                    }
                ),
            ],
        ) as http:
            with self.assertRaisesRegex(
                HomeAssistantError, "did not confirm all requested"
            ):
                await asyncio.wait_for(climate.async_set_hvac_mode(HVACMode.HEAT), 5)
        self.assertIsNone(thermostat.last_update_error)
        self.assertEqual(climate.hvac_mode, HVACMode.OFF)
        self.assertFalse(thermostat.power_on)
        self.assertFalse(thermostat.relay_state)
        self.assertEqual(thermostat.setpoint, 16)
        self.assertEqual(thermostat._parameters[2], (2, "1"))
        self.assertEqual(http.call_count, 4)
        self.assertEqual(sum("par" in c.kwargs["json"] for c in http.call_args_list), 1)
        coordinator.async_request_refresh.assert_awaited_once()
        self.assertEqual(
            http.call_args_list[0].kwargs["json"]["par"],
            [
                [125, 7, "0"],
                [118, 7, "0"],
                [2, 2, "1"],
            ],
        )

    async def test_failed_mode_readback_does_not_invent_target_state(self):
        config, thermostat, coordinator = self.make_coordinator()
        thermostat._power_on = False
        thermostat._mode = -1
        thermostat._mark_update_successful()
        climate = TerneoClimateEntity(coordinator, thermostat, config)
        coordinator.async_request_refresh = AsyncMock(
            side_effect=coordinator.async_refresh
        )
        with patch("tests.http.post", side_effect=TimeoutError("offline")) as http:
            with self.assertRaisesRegex(HomeAssistantError, "outcome is unknown"):
                await asyncio.wait_for(climate.async_set_hvac_mode(HVACMode.HEAT), 5)
        self.assertEqual(climate.hvac_mode, HVACMode.OFF)
        self.assertTrue(climate.available)
        self.assertEqual(http.call_count, 2)
        coordinator.async_request_refresh.assert_awaited_once()

    async def test_mode_readback_exception_does_not_replace_command_error(self):
        config, thermostat, coordinator = self.make_coordinator()
        climate = TerneoClimateEntity(coordinator, thermostat, config)

        async def refresh():
            self.assertFalse(coordinator._request_lock.locked())
            raise HomeAssistantError("readback failed")

        coordinator.async_request_refresh = AsyncMock(side_effect=refresh)
        with patch(
            "tests.http.post", return_value=response({"success": "block"})
        ) as http:
            with self.assertRaisesRegex(HomeAssistantError, "LAN control is blocked"):
                await climate.async_set_hvac_mode(HVACMode.HEAT)
        http.assert_called_once()
        coordinator.async_request_refresh.assert_awaited_once()

    async def test_invalid_hvac_mode_sends_no_request_or_refresh(self):
        config, thermostat, coordinator = self.make_coordinator()
        climate = TerneoClimateEntity(coordinator, thermostat, config)
        coordinator.async_request_refresh = AsyncMock()
        with patch("tests.http.post") as http:
            with self.assertRaises(ServiceValidationError):
                await climate.async_set_hvac_mode(HVACMode.DRY)
        http.assert_not_called()
        coordinator.async_request_refresh.assert_not_awaited()

    async def test_presets_keep_existing_one_write_and_refresh_behavior(self):
        config, thermostat, coordinator = self.make_coordinator()
        climate = TerneoClimateEntity(coordinator, thermostat, config)
        coordinator.async_request_refresh = AsyncMock()
        for preset, mode in ((PRESET_SCHEDULE, "0"), (PRESET_MANUAL, "1")):
            with self.subTest(preset=preset):
                params = [[125, 7, "0"], [2, 2, mode]]
                with patch(
                    "tests.http.post",
                    return_value=response(
                        {
                            "sn": thermostat.sn,
                            "par": params,
                        }
                    ),
                ) as http:
                    await climate.async_set_preset_mode(preset)
                http.assert_called_once()
                self.assertEqual(http.call_args.kwargs["json"]["par"], params)
        self.assertEqual(coordinator.async_request_refresh.await_count, 2)

    async def assert_hvac_request_serialization(self, *, polling=False, cancel=False):
        config, thermostat, coordinator = self.make_coordinator()
        climate = TerneoClimateEntity(coordinator, thermostat, config)
        coordinator.async_request_refresh = AsyncMock()
        started = asyncio.Event()
        release = asyncio.Event()
        calls = []
        params = {125: [125, 7, "0"], 118: [118, 7, "0"], 2: [2, 2, "1"]}

        async def post(url, **kwargs):
            data = kwargs["json"]
            calls.append(data)
            if len(calls) == 1:
                started.set()
                await asyncio.wait_for(release.wait(), 5)
            if "par" in data:
                params.update({p[0]: p[:] for p in data["par"]})
            if data.get("cmd") == 4:
                return response(
                    {
                        "sn": thermostat.sn,
                        "t.1": "368",
                        "t.5": "400",
                        "m.1": "0" if params[2][2] == "0" else "3",
                        "f.0": "0",
                        "f.16": params[125][2],
                    }
                )
            return response({"sn": thermostat.sn, "par": list(params.values())})

        tasks = []
        with patch("tests.http.post", side_effect=post):
            initial = asyncio.create_task(
                coordinator._async_update_data()
                if polling
                else climate.async_set_hvac_mode(HVACMode.HEAT)
            )
            tasks.append(initial)
            try:
                await asyncio.wait_for(started.wait(), 2)
                if cancel:
                    initial.cancel()
                    await asyncio.sleep(0)
                    initial.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(initial.done())
                off = asyncio.create_task(
                    coordinator.async_execute_command(thermostat.turn_off)
                )
                tasks.append(off)
                if not polling and not cancel:
                    tasks.append(asyncio.create_task(coordinator._async_update_data()))
                await asyncio.sleep(0)
                self.assertTrue(coordinator._request_lock.locked())
                self.assertEqual(len(calls), 1)
                self.assertFalse(off.done())
            finally:
                release.set()
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), 5
                )
        if cancel:
            self.assertIsInstance(results[0], asyncio.CancelledError)
        else:
            self.assertFalse(isinstance(results[0], BaseException))
        self.assertTrue(all(not isinstance(r, BaseException) for r in results[1:]))
        self.assertFalse(coordinator._request_lock.locked())
        if polling:
            self.assertEqual([c.get("cmd") for c in calls[:2]], [1, 4])
            self.assertEqual(calls[2]["par"], [[125, 7, "1"]])
        else:
            self.assertEqual(
                calls[0]["par"], [[125, 7, "0"], [118, 7, "0"], [2, 2, "1"]]
            )
            self.assertEqual(calls[1]["par"], [[125, 7, "1"]])
        if cancel:
            coordinator.async_request_refresh.assert_not_awaited()
        elif not polling:
            coordinator.async_request_refresh.assert_awaited_once()

    async def test_mode_batch_blocks_a_queued_command_and_poll(self):
        await self.assert_hvac_request_serialization()

    async def test_cancelled_mode_holds_lock_until_request_finishes(self):
        await self.assert_hvac_request_serialization(cancel=True)

    async def test_cancelled_poll_holds_lock_until_request_finishes(self):
        await self.assert_hvac_request_serialization(polling=True, cancel=True)

    async def test_busy_thermostat_does_not_block_second_device(self):
        _, first, first_coordinator = self.make_coordinator()
        _, second, second_coordinator = self.make_coordinator("other")
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_command(mode):
            self.assertEqual(mode, HVACMode.HEAT)
            started.set()
            await release.wait()
            return True

        with (
            patch.object(first, "set_hvac_mode", side_effect=slow_command),
            patch.object(second, "set_hvac_mode", return_value=True) as second_command,
        ):
            first_task = asyncio.create_task(
                first_coordinator.async_execute_command(
                    first.set_hvac_mode, HVACMode.HEAT
                )
            )
            try:
                await asyncio.wait_for(started.wait(), 2)
                await asyncio.wait_for(
                    second_coordinator.async_execute_command(
                        second.set_hvac_mode, HVACMode.COOL
                    ),
                    2,
                )
                self.assertFalse(first_task.done())
                second_command.assert_awaited_once_with(HVACMode.COOL)
            finally:
                release.set()
                await asyncio.wait_for(first_task, 2)

    async def test_cancelled_worker_error_is_consumed_before_unlocking(self):
        _, thermostat, coordinator = self.make_coordinator()
        started = asyncio.Event()
        release = asyncio.Event()

        async def failing_command(*args):
            started.set()
            await release.wait()
            raise RuntimeError("Request failed after cancellation")

        with (
            patch.object(thermostat, "set_hvac_mode", side_effect=failing_command),
            patch.object(thermostat, "turn_off", return_value=True),
        ):
            initial = asyncio.create_task(
                coordinator.async_execute_command(
                    thermostat.set_hvac_mode, HVACMode.HEAT
                )
            )
            queued = None
            try:
                await asyncio.wait_for(started.wait(), 2)
                initial.cancel()
                await asyncio.sleep(0)
                queued = asyncio.create_task(
                    coordinator.async_execute_command(thermostat.turn_off)
                )
                await asyncio.sleep(0)
                self.assertTrue(coordinator._request_lock.locked())
                self.assertFalse(queued.done())
            finally:
                release.set()
                tasks = [initial] if queued is None else [initial, queued]
                results = await asyncio.wait_for(
                    asyncio.gather(*tasks, return_exceptions=True), 2
                )
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertIsNone(results[1])
        self.assertFalse(coordinator._request_lock.locked())

    async def test_invalid_client_mode_is_validation_error_without_readback(self):
        _, thermostat, coordinator = self.make_coordinator()
        coordinator.async_request_refresh = AsyncMock()
        with patch("tests.http.post") as http:
            with self.assertRaises(ServiceValidationError):
                await coordinator.async_execute_command(
                    thermostat.set_hvac_mode, "invalid", refresh_on_failure=True
                )
        http.assert_not_called()
        coordinator.async_request_refresh.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()

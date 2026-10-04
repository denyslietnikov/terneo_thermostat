"""Split-polling policy with real HA coordinators and deterministic HTTP timing."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace
from unittest.mock import MagicMock, patch

import voluptuous as vol
from homeassistant.components.climate import HVACMode
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.terneo.climate import TerneoClimateEntity
from custom_components.terneo.config_flow import TerneoOptionsFlowHandler
from custom_components.terneo.const import DEVICE_TYPE_NEW, DEVICE_TYPE_OLD, DOMAIN
from custom_components.terneo.coordinator import TerneoCoordinator
from custom_components.terneo.number import NUMBER_DESCRIPTIONS, TerneoNumberEntity
from custom_components.terneo.select import SELECT_DESCRIPTIONS, TerneoSelectEntity
from custom_components.terneo.sensor import SENSOR_DESCRIPTIONS, TerneoSensorEntity
from custom_components.terneo.switch import SWITCH_DESCRIPTIONS, TerneoSwitchEntity
from custom_components.terneo.thermostat import TerneoThermostat
from tests.http import FakeSession, Response


def response(data):
    result = Response()
    result.status_code = 200
    result._content = json.dumps(data).encode()
    return result


class PollingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.hass.config_entries = MagicMock()
        self.coordinators = []
        self.clock = 100.0
        self.commands = []
        self.parameters_error = False
        self.status_error = False
        self.params = [
            [125, 7, "0"],
            [23, 2, "2"],
            [17, 4, "100"],
            [3, 2, "0"],
            [118, 7, "0"],
            [26, 2, "35"],
            [27, 2, "5"],
        ]
        self.status = {
            "t.1": "368",
            "t.2": "352",
            "t.5": "400",
            "m.0": "0",
            "m.1": "3",
            "m.5": "0",
            "f.0": "1",
            "f.16": "0",
        }
        self.device_time = patch(
            "custom_components.terneo.thermostat.time",
            new=SimpleNamespace(
                monotonic=lambda: self.clock,
                sleep=self.sleep,
            ),
        )
        self.coordinator_time = patch(
            "custom_components.terneo.coordinator.time",
            new=SimpleNamespace(
                monotonic=lambda: self.clock,
            ),
        )
        self.http = patch("tests.http.post", side_effect=self.post)
        self.wait = patch(
            "custom_components.terneo.thermostat.sleep", side_effect=self.sleep
        )
        for mock in (self.device_time, self.coordinator_time, self.http, self.wait):
            mock.start()
            self.addCleanup(mock.stop)

    def sleep(self, seconds):
        self.clock += seconds

    def post(self, _url, **kwargs):
        payload = kwargs["json"]
        kind = payload.get("cmd", "write")
        self.commands.append(kind)
        self.clock += 0.1
        if kind == 1:
            if self.parameters_error:
                raise TimeoutError("private settings error")
            return response({"sn": "test", "par": self.params})
        if kind == 4:
            if self.status_error:
                raise TimeoutError("private telemetry error")
            return response({"sn": "test", **self.status})
        self.params = [
            next((p for p in payload["par"] if p[0] == old[0]), old)
            for old in self.params
        ]
        if self.profile == DEVICE_TYPE_OLD:
            return response({"success": "true"})
        return response({"sn": "test", "par": payload["par"]})

    async def asyncTearDown(self):
        for coordinator in self.coordinators:
            await coordinator.async_shutdown()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    def make_coordinator(self, profile=DEVICE_TYPE_OLD, options=None):
        self.profile = profile
        config = ConfigEntry(
            version=1,
            minor_version=1,
            domain=DOMAIN,
            title="test",
            data={"host": "192.0.2.1", "serial": "test"},
            options=options or {},
            source="user",
            unique_id="test",
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
            state=ConfigEntryState.LOADED,
        )
        client = TerneoThermostat("test", "192.0.2.1", profile, session=FakeSession())
        coordinator = TerneoCoordinator(self.hass, config, client)
        config.runtime_data = coordinator
        self.coordinators.append(coordinator)
        return config, client, coordinator

    async def poll(self, coordinator, after=30):
        self.clock += after
        start = len(self.commands)
        await coordinator.async_refresh()
        return self.commands[start:]

    def sensor(self, config, client, coordinator, key):
        description = next(d for d in SENSOR_DESCRIPTIONS if d.key == key)
        return TerneoSensorEntity(coordinator, client, config, description)

    async def test_initial_full_then_status_only_for_both_protocols(self):
        for profile in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            with self.subTest(profile=profile):
                _, client, coordinator = self.make_coordinator(profile)
                self.assertEqual(await self.poll(coordinator), [1, 4])
                full_at = client.last_successful_update
                self.assertEqual(await self.poll(coordinator), [4])
                self.assertEqual(client.last_successful_update, full_at)
                data = client.connection_diagnostics
                self.assertGreater(data["settings"]["age_seconds"], 30)
                self.assertAlmostEqual(data["status"]["age_seconds"], 0)
                self.assertTrue(data["settings"]["available"])
                self.assertAlmostEqual(
                    client.energy_counters["heating_time_seconds"], 30.1
                )
                self.assertAlmostEqual(
                    client.energy_counters["heating_energy_kwh"], 30.1 / 3600
                )

    async def test_hourly_request_count_and_latency_comparison(self):
        for interval, expected in ((30, 240), (300, 132)):
            _, client, coordinator = self.make_coordinator(
                options={"settings_scan_interval": interval}
            )
            started = len(self.commands)
            durations = []
            origin = self.clock
            for cycle in range(120):
                self.clock = origin + 30 * (cycle + 1)
                began = self.clock
                await coordinator.async_refresh()
                durations.append(self.clock - began)
            self.assertEqual(len(self.commands) - started, expected)
            self.assertTrue(client.available)
            if interval == 30:
                self.assertAlmostEqual(sum(durations) / 120, 1.2)
            else:
                self.assertAlmostEqual(sum(durations) / 120, 0.21)
                self.assertAlmostEqual(min(durations), 0.1)

    async def test_physical_changes_update_operating_state_before_settings(self):
        for profile in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            with self.subTest(profile=profile):
                config, client, coordinator = self.make_coordinator(profile)
                self.params[1] = [23, 2, "2"]
                self.assertEqual(await self.poll(coordinator), [1, 4])
                climate = TerneoClimateEntity(coordinator, client, config)
                self.params[1] = [23, 2, "8"]
                self.status.update(
                    {
                        "t.1": "384",
                        "t.5": "432",
                        "m.0": "1",
                        "m.5": "1",
                        "m.1": "0",
                        "f.16": "1",
                        "f.0": "0",
                    }
                )
                self.assertEqual(await self.poll(coordinator), [4])
                self.assertEqual(client.floor_temperature, 24)
                self.assertEqual(client.setpoint, 27)
                self.assertFalse(client.relay_state)
                self.assertFalse(client.power_on)
                self.assertEqual(climate.hvac_mode, HVACMode.OFF)
                self.assertEqual(client.control_type, 1)
                self.assertTrue(client.cooling_mode)
                self.assertEqual(client.brightness, 2)
                self.status["f.16"] = "0"
                await self.poll(coordinator)
                self.assertEqual(climate.hvac_mode, HVACMode.COOL)
                self.assertEqual(await self.poll(coordinator, after=300), [1, 4])
                self.assertEqual(client.brightness, 8)

    async def test_incomplete_telemetry_keeps_full_polling(self):
        for profile in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            with self.subTest(profile=profile):
                _, client, coordinator = self.make_coordinator(profile)
                self.status.pop("f.16", None)
                self.assertEqual(await self.poll(coordinator), [1, 4])
                self.assertFalse(client.fast_poll_supported)
                self.assertEqual(await self.poll(coordinator), [1, 4])
                self.status["f.16"] = "0"

    async def test_lost_fast_fields_trigger_full_refresh_on_next_poll(self):
        _, client, coordinator = self.make_coordinator()
        await self.poll(coordinator)
        self.status.pop("m.5")
        self.assertEqual(await self.poll(coordinator), [4])
        self.assertFalse(client.fast_poll_supported)
        self.assertEqual(await self.poll(coordinator), [1, 4])

    async def test_settings_failure_does_not_claim_freshness_or_stop_telemetry(self):
        config, client, coordinator = self.make_coordinator()
        await self.poll(coordinator)
        full_at = client.last_successful_update
        self.parameters_error = True
        self.status["t.1"] = "384"
        self.assertEqual(await self.poll(coordinator, after=300), [1, 4])
        self.assertTrue(coordinator.last_update_success)
        self.assertTrue(client.available)
        self.assertEqual(client.consecutive_update_failures, 0)
        self.assertEqual(client.floor_temperature, 24)
        self.assertEqual(client.last_successful_update, full_at)
        data = client.connection_diagnostics
        self.assertFalse(data["settings"]["available"])
        self.assertEqual(data["settings"]["error_category"], "timeout")
        self.assertGreater(data["settings"]["age_seconds"], 300)
        self.assertAlmostEqual(data["status"]["age_seconds"], 0)
        self.assertTrue(
            self.sensor(config, client, coordinator, "floor_temperature").available
        )
        self.assertTrue(
            self.sensor(config, client, coordinator, "heating_active").available
        )
        self.assertFalse(
            self.sensor(config, client, coordinator, "current_power").available
        )
        self.assertFalse(
            self.sensor(config, client, coordinator, "manual_floor_temp").available
        )
        self.assertFalse(
            TerneoNumberEntity(
                coordinator, client, config, NUMBER_DESCRIPTIONS[0]
            ).available
        )
        self.assertFalse(
            TerneoSelectEntity(
                coordinator, client, config, SELECT_DESCRIPTIONS[0]
            ).available
        )
        self.assertTrue(
            TerneoSwitchEntity(
                coordinator, client, config, SWITCH_DESCRIPTIONS[0]
            ).available
        )
        self.assertFalse(
            TerneoSwitchEntity(
                coordinator, client, config, SWITCH_DESCRIPTIONS[1]
            ).available
        )
        climate = TerneoClimateEntity(coordinator, client, config)
        self.assertTrue(climate.available)
        self.assertFalse(climate.extra_state_attributes["settings_confirmed"])
        self.assertNotIn("power_watts", climate.extra_state_attributes)
        totals = client.energy_counters
        self.parameters_error = False
        self.assertEqual(await self.poll(coordinator), [4])
        self.assertFalse(client.settings_available)
        self.assertEqual(
            client.energy_counters["heating_energy_kwh"], totals["heating_energy_kwh"]
        )
        self.assertGreater(
            client.energy_counters["heating_time_seconds"],
            totals["heating_time_seconds"],
        )
        self.assertEqual(await self.poll(coordinator, after=300), [1, 4])
        self.assertTrue(client.settings_available)
        self.assertTrue(
            self.sensor(config, client, coordinator, "current_power").available
        )

    async def test_setup_does_not_bootstrap_from_status_without_settings(self):
        _, client, coordinator = self.make_coordinator()
        self.parameters_error = True
        with self.assertRaises(UpdateFailed):
            await coordinator._async_update_data()
        self.assertEqual(self.commands, [1])
        self.assertFalse(client.has_state)
        self.assertIsNone(client.last_successful_update)

    async def test_one_status_failure_forces_full_recovery_with_new_settings(self):
        for profile in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            with self.subTest(profile=profile):
                _, client, coordinator = self.make_coordinator(profile)
                await self.poll(coordinator)
                self.status_error = True
                self.assertEqual(await self.poll(coordinator), [4])
                self.assertTrue(client.available)
                self.assertEqual(client.consecutive_update_failures, 1)
                self.params[1] = [23, 2, "9"]
                self.status_error = False
                self.assertEqual(await self.poll(coordinator), [1, 4])
                self.assertEqual(client.brightness, 9)
                self.assertEqual(client.consecutive_update_failures, 0)
                self.assertEqual(await self.poll(coordinator), [4])

    async def test_recovery_settings_error_remains_independent_of_status(self):
        _, client, coordinator = self.make_coordinator()
        await self.poll(coordinator)
        self.status_error = True
        await self.poll(coordinator)
        self.status_error = False
        self.parameters_error = True
        self.assertEqual(await self.poll(coordinator), [1, 4])
        self.assertTrue(client.available)
        self.assertFalse(client.settings_available)
        self.assertEqual(client.consecutive_update_failures, 0)
        self.assertEqual(await self.poll(coordinator), [4])

    async def test_both_endpoints_failing_count_as_one_poll_failure(self):
        _, client, coordinator = self.make_coordinator()
        await self.poll(coordinator)
        self.status_error = self.parameters_error = True
        for failures in range(1, 4):
            self.assertEqual(await self.poll(coordinator, after=300), [1, 4])
            self.assertEqual(client.consecutive_update_failures, failures)
            self.assertEqual(client.available, failures < 3)
        self.status_error = self.parameters_error = False
        self.assertEqual(await self.poll(coordinator), [1, 4])
        self.assertTrue(client.available)

    async def test_status_failure_rolls_back_scheduled_settings_atomically(self):
        _, client, coordinator = self.make_coordinator()
        await self.poll(coordinator)
        before = client.connection_diagnostics["settings"]["last_successful_update"]
        self.params[1] = [23, 2, "9"]
        self.status_error = True
        self.assertEqual(await self.poll(coordinator, after=300), [1, 4])
        self.assertEqual(client.brightness, 2)
        self.assertEqual(
            client.connection_diagnostics["settings"]["last_successful_update"], before
        )
        self.status_error = False
        self.assertEqual(await self.poll(coordinator), [1, 4])
        self.assertEqual(client.brightness, 9)

    async def test_successful_writes_force_full_refresh_and_keep_legacy_verification(
        self,
    ):
        for profile, write_requests in (
            (DEVICE_TYPE_OLD, ["write", 1]),
            (DEVICE_TYPE_NEW, ["write"]),
        ):
            with self.subTest(profile=profile):
                _, client, coordinator = self.make_coordinator(profile)
                await self.poll(coordinator)
                self.assertEqual(await self.poll(coordinator), [4])
                start = len(self.commands)
                await coordinator.async_execute_command(client.set_brightness, 7)
                self.assertEqual(self.commands[start:], write_requests)
                self.assertEqual(await self.poll(coordinator), [1, 4])
                self.assertEqual(client.brightness, 7)
                self.assertEqual(await self.poll(coordinator), [4])

    async def test_command_waits_for_status_poll_and_forces_full_refresh(self):
        _, client, coordinator = self.make_coordinator()
        await self.poll(coordinator)
        started = asyncio.Event()
        release = asyncio.Event()
        original = coordinator._async_execute_request

        async def executor(command, *args):
            if command == client.update_status:
                started.set()
                await release.wait()
            return await original(command, *args)

        with patch.object(coordinator, "_async_execute_request", side_effect=executor):
            poll = asyncio.create_task(coordinator.async_refresh())
            await started.wait()
            write = asyncio.create_task(
                coordinator.async_execute_command(client.set_brightness, 7)
            )
            await asyncio.sleep(0)
            self.assertFalse(write.done())
            release.set()
            await asyncio.gather(poll, write)
        self.assertEqual(await self.poll(coordinator), [1, 4])
        self.assertEqual(client.brightness, 7)

    async def test_cancelled_status_poll_keeps_lock_until_request_finishes(self):
        for profile in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            with self.subTest(profile=profile):
                _, client, coordinator = self.make_coordinator(profile)
                await self.poll(coordinator)
                started = asyncio.Event()
                release = asyncio.Event()

                original = client.update_status

                async def delayed_status():
                    started.set()
                    await release.wait()
                    return await original()

                with patch.object(client, "update_status", side_effect=delayed_status):
                    polling = asyncio.create_task(coordinator._async_update_data())
                    await started.wait()
                    polling.cancel()
                    await asyncio.sleep(0)
                    polling.cancel()
                    write = asyncio.create_task(
                        coordinator.async_execute_command(client.set_brightness, 6)
                    )
                    await asyncio.sleep(0)
                    self.assertFalse(polling.done())
                    self.assertFalse(write.done())
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await polling
                    await write
                self.assertEqual(await self.poll(coordinator), [1, 4])
                self.assertEqual(client.brightness, 6)

    async def test_invalid_operating_flags_do_not_publish_partial_telemetry(self):
        _, client, coordinator = self.make_coordinator()
        await self.poll(coordinator)
        for key in ("f.16", "m.0", "m.5"):
            with self.subTest(key=key):
                previous = self.status[key]
                self.status[key] = "9"
                self.status["t.1"] = "480"
                await self.poll(coordinator)
                self.assertEqual(client.floor_temperature, 23)
                self.assertEqual(
                    client.connection_diagnostics["last_refresh_error_category"],
                    "protocol",
                )
                self.status[key] = previous
                self.status["t.1"] = "368"
                await self.poll(coordinator)

    async def test_new_profile_without_air_temperature_uses_full_polling(self):
        _, client, coordinator = self.make_coordinator(DEVICE_TYPE_NEW)
        self.status.pop("t.2")
        self.assertEqual(await self.poll(coordinator), [1, 4])
        self.assertFalse(client.fast_poll_supported)
        self.assertEqual(await self.poll(coordinator), [1, 4])

    async def test_interval_clamped_to_status_cadence_and_options_form_defaults(self):
        config, _, coordinator = self.make_coordinator(
            options={"scan_interval": 300, "settings_scan_interval": 30}
        )
        self.assertEqual(
            (await coordinator.async_get_diagnostics())["coordinator"][
                "settings_interval_seconds"
            ],
            300,
        )
        self.hass.config_entries.async_get_known_entry.return_value = config
        flow = TerneoOptionsFlowHandler()
        flow.hass = self.hass
        flow.handler = config.entry_id
        schema = (await flow.async_step_init())["data_schema"]
        self.assertEqual(schema({})["settings_scan_interval"], 30)
        for value in (29, 3601):
            with self.assertRaises(vol.Invalid):
                schema({"settings_scan_interval": value})
        data = schema({"settings_scan_interval": "600"})
        result = await flow.async_step_init(data)
        self.assertEqual(result["data"]["settings_scan_interval"], 600)
        default, _, _ = self.make_coordinator()
        self.hass.config_entries.async_get_known_entry.return_value = default
        self.assertEqual(
            (await flow.async_step_init())["data_schema"]({})["settings_scan_interval"],
            300,
        )

    async def test_options_labels_exist_in_all_translations(self):
        root = Path(__file__).parents[1] / "custom_components" / DOMAIN
        for filename in (
            root / "strings.json",
            *(root / "translations").glob("*.json"),
        ):
            data = json.loads(filename.read_text())
            options = data["options"]["step"]["init"]
            self.assertIn("settings_scan_interval", options["data"])
            self.assertIn("settings_scan_interval", options["data_description"])

    async def test_only_english_translation_is_shipped(self):
        root = Path(__file__).parents[1] / "custom_components" / DOMAIN / "translations"
        self.assertEqual({path.stem for path in root.glob("*.json")}, {"en"})

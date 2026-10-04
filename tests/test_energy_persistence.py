"""Heating-counter accounting and HA Store lifecycle tests; device HTTP is mocked."""

from __future__ import annotations

import asyncio
import json
import unittest
from datetime import UTC, datetime
from tempfile import TemporaryDirectory
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import homeassistant  # noqa: F401
from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import (
    EVENT_HOMEASSISTANT_FINAL_WRITE,
    EVENT_HOMEASSISTANT_STOP,
)
from homeassistant.core import CoreState, HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from homeassistant.util.file import WriteError

from custom_components.terneo import (
    async_setup_entry,
    async_unload_entry,
    async_update_options,
)
from custom_components.terneo.const import (
    DEVICE_TYPE_NEW,
    DEVICE_TYPE_OLD,
    DOMAIN,
    ParamNum,
)
from custom_components.terneo.coordinator import ENERGY_SAVE_INTERVAL, TerneoCoordinator
from custom_components.terneo.sensor import SENSOR_DESCRIPTIONS, TerneoSensorEntity
from custom_components.terneo.thermostat import TerneoThermostat
from tests.http import FakeSession, Response


def response(data):
    result = Response()
    result.status_code = 200
    result._content = json.dumps(data).encode()
    return result


def entry(serial="test-private-serial", host="192.0.2.1", options=None):
    return ConfigEntry(
        version=1,
        minor_version=1,
        domain=DOMAIN,
        title=serial,
        data={"host": host, "serial": serial},
        options=options or {},
        source="user",
        unique_id=serial,
        discovery_keys=MappingProxyType({}),
        subentries_data=[],
        state=ConfigEntryState.SETUP_IN_PROGRESS,
    )


class EnergyAccountingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.clock = 100.0
        self.wall_clock = 10000.0
        self.time = patch(
            "custom_components.terneo.thermostat.time",
            new=SimpleNamespace(
                monotonic=lambda: self.clock,
                time=lambda: self.wall_clock,
                sleep=lambda _: None,
            ),
        )
        self.time.start()
        self.addCleanup(self.time.stop)
        self.thermostat = TerneoThermostat("test", "192.0.2.1", session=FakeSession())
        self.thermostat._parameters[ParamNum.POWER] = (4, "100")

    def sample(self, relay=True, elapsed=30):
        self.clock += elapsed
        self.thermostat._parse_status({"f.0": "1" if relay else "0"})

    def test_first_observation_never_backfills_heating(self):
        self.sample()
        self.assertEqual(
            self.thermostat.energy_counters,
            {
                "heating_energy_kwh": 0.0,
                "heating_time_seconds": 0.0,
            },
        )

    def test_on_off_intervals_use_previous_sample_for_both_profiles(self):
        for profile in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            with self.subTest(profile=profile):
                self.thermostat = TerneoThermostat(
                    "test", "192.0.2.1", profile, session=FakeSession()
                )
                self.thermostat._parameters[ParamNum.POWER] = (4, "100")
                self.sample(True)
                self.sample(True, 60)
                self.sample(False, 30)
                self.sample(False, 60)
                self.sample(True, 30)
                self.assertEqual(
                    self.thermostat.energy_counters["heating_time_seconds"], 90
                )
                self.assertAlmostEqual(
                    self.thermostat.energy_counters["heating_energy_kwh"], 0.025
                )

    def test_heating_time_does_not_require_positive_wattage(self):
        for value in (None, "0", "-1"):
            with self.subTest(power=value):
                self.thermostat.reset_energy_counter()
                if value is None:
                    self.thermostat._parameters.pop(ParamNum.POWER, None)
                else:
                    self.thermostat._parameters[ParamNum.POWER] = (4, value)
                self.sample()
                self.sample(elapsed=60)
                self.assertEqual(
                    self.thermostat.energy_counters["heating_time_seconds"], 60
                )
                self.assertEqual(
                    self.thermostat.energy_counters["heating_energy_kwh"], 0
                )

    def test_power_changes_apply_only_to_subsequent_observed_intervals(self):
        self.sample()
        self.thermostat._parameters[ParamNum.POWER] = (4, "150")
        self.sample(elapsed=60)
        self.sample(elapsed=60)
        self.assertAlmostEqual(
            self.thermostat.energy_counters["heating_energy_kwh"],
            (1000 + 1500) * 60 / 3600000,
        )
        self.assertEqual(self.thermostat.energy_counters["heating_time_seconds"], 120)

    def test_unknown_wattage_is_not_backfilled_when_configured_later(self):
        self.thermostat._parameters.pop(ParamNum.POWER)
        self.sample()
        self.thermostat._parameters[ParamNum.POWER] = (4, "100")
        self.sample(elapsed=60)
        self.assertEqual(self.thermostat.energy_counters["heating_energy_kwh"], 0)
        self.sample(elapsed=60)
        self.assertAlmostEqual(
            self.thermostat.energy_counters["heating_energy_kwh"], 1 / 60
        )
        self.assertEqual(self.thermostat.energy_counters["heating_time_seconds"], 120)

    def test_wall_clock_changes_do_not_change_elapsed_heating(self):
        self.sample()
        self.wall_clock -= 86400
        self.sample(elapsed=30)
        self.wall_clock += 172800
        self.sample(elapsed=30)
        self.assertEqual(self.thermostat.energy_counters["heating_time_seconds"], 60)
        self.assertAlmostEqual(
            self.thermostat.energy_counters["heating_energy_kwh"], 1 / 60
        )

    def test_invalid_or_long_monotonic_intervals_are_skipped_then_tracking_resumes(
        self,
    ):
        for elapsed in (0, -30, 301):
            with self.subTest(elapsed=elapsed):
                self.thermostat.reset_energy_counter()
                self.sample()
                self.sample(elapsed=elapsed)
                self.assertEqual(
                    self.thermostat.energy_counters["heating_time_seconds"], 0
                )
                self.sample(elapsed=30)
                self.assertEqual(
                    self.thermostat.energy_counters["heating_time_seconds"], 30
                )

    def test_missing_relay_status_breaks_interval_without_inventing_an_off_state(self):
        self.sample()
        self.clock += 30
        self.thermostat._parse_status({"t.1": "336"})
        self.assertTrue(self.thermostat.relay_state)
        self.sample(elapsed=30)
        self.assertEqual(self.thermostat.energy_counters["heating_time_seconds"], 0)
        self.sample(elapsed=30)
        self.assertEqual(self.thermostat.energy_counters["heating_time_seconds"], 30)

    async def test_failed_full_poll_breaks_interval_even_with_available_cached_state(
        self,
    ):
        self.thermostat._mark_update_successful()
        self.sample()
        self.clock += 30
        with patch("tests.http.post", side_effect=TimeoutError("offline")):
            self.assertFalse(await self.thermostat.update())
        self.assertTrue(self.thermostat.available)
        self.assertTrue(self.thermostat.relay_state)
        self.sample(elapsed=30)
        self.assertEqual(self.thermostat.energy_counters["heating_time_seconds"], 0)
        self.sample(elapsed=30)
        self.assertEqual(self.thermostat.energy_counters["heating_time_seconds"], 30)

    async def test_invalid_relay_value_is_a_failed_poll_not_confirmed_off(self):
        self.sample()
        for invalid in ("2", "-1", "true", True):
            with self.subTest(value=invalid):
                with patch(
                    "tests.http.post",
                    side_effect=[
                        response({"par": [[125, 7, "0"]]}),
                        response({"f.0": invalid}),
                    ],
                ):
                    self.assertFalse(await self.thermostat.update())
                self.assertTrue(self.thermostat.relay_state)
                self.assertIsNone(self.thermostat._last_relay_update)

    def test_restore_keeps_full_precision_and_does_not_restore_relay_anchor(self):
        saved = {"heating_energy_kwh": 1.23456789, "heating_time_seconds": 42.123456}
        self.sample()
        self.thermostat.restore_energy_counters(saved)
        self.assertEqual(self.thermostat.energy_counters, saved)
        self.clock += 10000
        self.sample()
        self.assertEqual(self.thermostat.energy_counters, saved)
        self.sample(elapsed=30)
        self.assertEqual(
            self.thermostat.energy_counters["heating_time_seconds"],
            saved["heating_time_seconds"] + 30,
        )
        copy = self.thermostat.energy_counters
        copy["heating_energy_kwh"] = 999
        self.assertNotEqual(self.thermostat.energy_counters, copy)

    def test_invalid_stored_totals_are_rejected_atomically(self):
        valid = {"heating_energy_kwh": 2.0, "heating_time_seconds": 120.0}
        self.thermostat.restore_energy_counters(valid)
        invalid = [None, [], {}, {"heating_energy_kwh": 1}]
        for field in valid:
            invalid.extend(
                {**valid, field: value}
                for value in (True, "1", -1, float("nan"), float("inf"), 10**1000)
            )
        for data in invalid:
            with self.subTest(data=data):
                with self.assertRaisesRegex(ValueError, "Invalid stored"):
                    self.thermostat.restore_energy_counters(data)
                self.assertEqual(self.thermostat.energy_counters, valid)

    def test_explicit_reset_clears_totals_and_does_not_count_old_interval_again(self):
        self.sample()
        self.sample(elapsed=60)
        self.clock += 10
        self.thermostat.reset_energy_counter()
        self.sample(elapsed=20)
        self.assertEqual(self.thermostat.energy_counters["heating_time_seconds"], 0)
        self.sample(elapsed=30)
        self.assertEqual(self.thermostat.energy_counters["heating_time_seconds"], 30)


class HomeAssistantEnergyPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.hass.config_entries = MagicMock()
        self.hass.config_entries.async_forward_entry_setups = AsyncMock()
        self.hass.config_entries.async_unload_platforms = AsyncMock(return_value=True)
        self.hass.config_entries.async_reload = AsyncMock()
        self.coordinators = []
        self.other_instances = []
        self.clock = 100.0
        self.time = patch(
            "custom_components.terneo.thermostat.time",
            new=SimpleNamespace(
                monotonic=lambda: self.clock,
                sleep=lambda _: None,
            ),
        )
        self.time.start()
        self.addCleanup(self.time.stop)

    async def asyncTearDown(self):
        for coordinator in self.coordinators:
            await coordinator.async_shutdown()
        for hass in [*self.other_instances, self.hass]:
            await hass.async_block_till_done()
            await hass.async_stop(force=True)
        self.temp.cleanup()

    def make_coordinator(self, config=None, hass=None):
        config = config or entry()
        hass = hass or self.hass
        thermostat = TerneoThermostat(
            config.unique_id, config.data["host"], session=FakeSession()
        )
        coordinator = TerneoCoordinator(hass, config, thermostat)
        config.runtime_data = coordinator
        self.coordinators.append(coordinator)
        return config, thermostat, coordinator

    async def poll(self, coordinator, relay="1", power="100"):
        params = [[125, 7, "0"], [2, 2, "1"]]
        if power is not None:
            params.append([17, 4, power])
        with patch(
            "tests.http.post",
            side_effect=[
                response({"sn": coordinator.thermostat.sn, "par": params}),
                response(
                    {
                        "sn": coordinator.thermostat.sn,
                        "t.1": "336",
                        "t.5": "256",
                        "m.1": "3",
                        "f.0": relay,
                    }
                ),
            ],
        ):
            await coordinator.async_refresh()
        self.assertTrue(coordinator.last_update_success)

    async def test_actual_store_survives_a_new_ha_instance_without_downtime_or_double_counting(
        self,
    ):
        config, thermostat, coordinator = self.make_coordinator()
        await coordinator.async_restore_energy_counters()
        thermostat.restore_energy_counters(
            {"heating_energy_kwh": 1.23456789, "heating_time_seconds": 42.123456}
        )
        await self.poll(coordinator)
        self.clock += 60
        await self.poll(coordinator)
        saved = thermostat.energy_counters
        await coordinator.async_shutdown()
        restarted_hass = HomeAssistant(self.temp.name)
        restarted_hass.config_entries = MagicMock()
        self.other_instances.append(restarted_hass)
        _, restored, new = self.make_coordinator(
            entry(host="192.0.2.2"), restarted_hass
        )
        self.assertNotEqual(config.entry_id, new.config_entry.entry_id)
        await new.async_restore_energy_counters()
        self.assertEqual(restored.energy_counters, saved)
        self.clock += 86400
        await self.poll(new)
        self.assertEqual(restored.energy_counters, saved)
        self.clock += 30
        await self.poll(new)
        self.assertEqual(
            restored.energy_counters["heating_time_seconds"],
            saved["heating_time_seconds"] + 30,
        )
        self.assertAlmostEqual(
            restored.energy_counters["heating_energy_kwh"],
            saved["heating_energy_kwh"] + 1 / 120,
        )

    async def test_setup_restores_before_first_state_and_supports_maximum_poll_interval(
        self,
    ):
        config, thermostat, seed = self.make_coordinator(
            entry(options={"scan_interval": 300})
        )
        await seed.async_restore_energy_counters()
        saved = {"heating_energy_kwh": 8.123456, "heating_time_seconds": 4567.89}
        thermostat.restore_energy_counters(saved)
        await seed.async_shutdown()

        async def forward(actual_entry, platforms):
            self.assertEqual(
                actual_entry.runtime_data.thermostat.energy_counters, saved
            )
            self.assertEqual(
                actual_entry.runtime_data.thermostat._max_heating_interval, 600
            )

        self.hass.config_entries.async_forward_entry_setups.side_effect = forward
        with patch(
            "tests.http.post",
            side_effect=[
                response({"par": [[125, 7, "0"], [17, 4, "100"]]}),
                response({"f.0": "1"}),
            ],
        ):
            self.assertTrue(await async_setup_entry(self.hass, config))
        coordinator = config.runtime_data
        self.coordinators.append(coordinator)
        self.assertIsNotNone(coordinator._unsub_energy_save)
        self.clock += 301
        await self.poll(coordinator)
        self.assertEqual(
            coordinator.thermostat.energy_counters["heating_time_seconds"],
            saved["heating_time_seconds"] + 301,
        )

    async def test_device_identity_isolation_and_safe_storage_key(self):
        _, first, coordinator = self.make_coordinator(entry("serial/one"))
        _, second, other = self.make_coordinator(entry("serial/two"))
        await coordinator.async_restore_energy_counters()
        first.restore_energy_counters(
            {"heating_energy_kwh": 5, "heating_time_seconds": 123}
        )
        await coordinator.async_save_energy_counters()
        await other.async_restore_energy_counters()
        self.assertEqual(second.energy_counters["heating_energy_kwh"], 0)
        self.assertNotEqual(coordinator._energy_store.key, other._energy_store.key)
        self.assertNotIn("serial", coordinator._energy_store.key)
        self.assertNotIn("/", coordinator._energy_store.key)
        self.assertEqual(
            await coordinator._energy_store.async_load(), first.energy_counters
        )

    async def test_periodic_save_is_independent_of_poll_frequency_and_stops_on_unload(
        self,
    ):
        _, thermostat, coordinator = self.make_coordinator()
        await coordinator.async_restore_energy_counters()
        cancel = MagicMock()
        with patch(
            "custom_components.terneo.coordinator.async_track_time_interval",
            return_value=cancel,
        ) as interval:
            coordinator.async_start_energy_persistence()
            coordinator.async_start_energy_persistence()
        interval.assert_called_once()
        self.assertEqual(interval.call_args.args[2], ENERGY_SAVE_INTERVAL)
        periodic_save = interval.call_args.args[1]
        with patch.object(
            coordinator._energy_store,
            "async_save",
            wraps=coordinator._energy_store.async_save,
        ) as save:
            for _ in range(10):
                self.clock += 30
                await self.poll(coordinator)
            save.assert_not_awaited()
            await periodic_save(datetime.now(UTC))
            save.assert_awaited_once_with(thermostat.energy_counters)
            await coordinator.async_shutdown()
            self.assertEqual(save.await_count, 2)
            await periodic_save(datetime.now(UTC))
            await coordinator.async_shutdown()
            self.assertEqual(save.await_count, 2)
        cancel.assert_called_once()

    async def test_ha_stop_flushes_counters_without_estimated_tail(self):
        _, thermostat, coordinator = self.make_coordinator()
        await coordinator.async_restore_energy_counters()
        coordinator.async_start_energy_persistence()
        await self.poll(coordinator)
        self.clock += 30
        await self.poll(coordinator)
        saved = thermostat.energy_counters
        self.clock += 200
        self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
        await self.hass.async_block_till_done()
        self.assertEqual(await coordinator._energy_store.async_load(), saved)
        self.assertTrue(coordinator._energy_shutdown_complete)
        self.assertIsNone(coordinator._unsub_energy_save)
        self.assertIsNone(coordinator._unsub_energy_stop)

    async def test_stopping_ha_uses_store_final_write_to_flush_snapshot(self):
        _, thermostat, coordinator = self.make_coordinator()
        await coordinator.async_restore_energy_counters()
        coordinator.async_start_energy_persistence()
        thermostat.restore_energy_counters(
            {"heating_energy_kwh": 2.3456, "heating_time_seconds": 345.67}
        )
        self.hass.set_state(CoreState.stopping)
        with patch.object(
            coordinator._energy_store,
            "_async_write_data",
            wraps=coordinator._energy_store._async_write_data,
        ) as write:
            self.hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
            await self.hass.async_block_till_done()
            write.assert_not_awaited()
            self.hass.bus.async_fire(EVENT_HOMEASSISTANT_FINAL_WRITE)
            await self.hass.async_block_till_done()
            write.assert_awaited_once()
        self.assertEqual(
            await coordinator._energy_store.async_load(), thermostat.energy_counters
        )
        self.hass.set_state(CoreState.not_running)

    async def test_failed_disk_write_retains_totals_and_next_save_retries(self):
        _, thermostat, coordinator = self.make_coordinator()
        await coordinator.async_restore_energy_counters()
        thermostat.restore_energy_counters(
            {"heating_energy_kwh": 2.3456, "heating_time_seconds": 345.67}
        )
        saved = thermostat.energy_counters
        with patch.object(
            coordinator._energy_store,
            "_async_write_data",
            side_effect=WriteError("disk unavailable"),
        ):
            await coordinator.async_save_energy_counters()
        self.assertEqual(thermostat.energy_counters, saved)
        await coordinator.async_save_energy_counters()
        self.assertEqual(await coordinator._energy_store.async_load(), saved)

    async def test_options_reload_and_unload_restore_identical_totals(self):
        config, thermostat, coordinator = self.make_coordinator()
        await coordinator.async_restore_energy_counters()
        thermostat.restore_energy_counters(
            {"heating_energy_kwh": 7.4321, "heating_time_seconds": 6543.21}
        )
        await async_update_options(self.hass, config)
        self.hass.config_entries.async_reload.assert_awaited_once_with(config.entry_id)
        self.assertTrue(await async_unload_entry(self.hass, config))
        _, restored, other = self.make_coordinator(config)
        await other.async_restore_energy_counters()
        self.assertEqual(restored.energy_counters, thermostat.energy_counters)
        self.assertIsNone(restored._last_relay_update)

    async def test_failed_platform_unload_does_not_shutdown_or_flush(self):
        config, _, coordinator = self.make_coordinator()
        await coordinator.async_restore_energy_counters()
        self.hass.config_entries.async_unload_platforms.return_value = False
        with patch.object(
            coordinator._energy_store, "async_save", new_callable=AsyncMock
        ) as save:
            self.assertFalse(await async_unload_entry(self.hass, config))
        save.assert_not_awaited()
        self.assertFalse(coordinator._energy_shutdown_complete)

    async def test_invalid_stored_totals_do_not_get_overwritten_or_publish_zero(self):
        config, _, coordinator = self.make_coordinator()
        invalid = {"heating_energy_kwh": -1, "heating_time_seconds": 10}
        await coordinator._energy_store.async_save(invalid)
        with patch("tests.http.post") as post:
            with self.assertRaises(ConfigEntryError):
                await async_setup_entry(self.hass, config)
        post.assert_not_called()
        self.hass.config_entries.async_forward_entry_setups.assert_not_awaited()
        await coordinator.async_shutdown()
        self.assertEqual(await coordinator._energy_store.async_load(), invalid)

    async def test_offline_setup_keeps_existing_store_without_scheduling_periodic_writes(
        self,
    ):
        config, thermostat, seed = self.make_coordinator()
        await seed.async_restore_energy_counters()
        saved = {"heating_energy_kwh": 3, "heating_time_seconds": 100}
        thermostat.restore_energy_counters(saved)
        await seed.async_shutdown()
        with patch("tests.http.post", side_effect=TimeoutError("offline")):
            with self.assertRaises(ConfigEntryNotReady):
                await async_setup_entry(self.hass, config)
        self.hass.config_entries.async_forward_entry_setups.assert_not_awaited()
        self.assertEqual(await seed._energy_store.async_load(), saved)

    async def test_save_waits_for_inflight_request_before_reading_totals(self):
        _, thermostat, coordinator = self.make_coordinator()
        await coordinator.async_restore_energy_counters()
        started = asyncio.Event()
        release = asyncio.Event()

        async def request(command, *args):
            started.set()
            await release.wait()
            thermostat.restore_energy_counters(
                {"heating_energy_kwh": 4, "heating_time_seconds": 80}
            )
            return True

        with patch.object(coordinator, "_async_execute_request", side_effect=request):
            poll = asyncio.create_task(coordinator._async_update_data())
            await started.wait()
            saving = asyncio.create_task(coordinator.async_save_energy_counters())
            await asyncio.sleep(0)
            self.assertFalse(saving.done())
            release.set()
            await poll
        await saving
        self.assertEqual(
            await coordinator._energy_store.async_load(), thermostat.energy_counters
        )

    async def test_unexpected_poll_failure_breaks_heating_interval(self):
        _, thermostat, coordinator = self.make_coordinator()
        await self.poll(coordinator)
        self.assertIsNotNone(thermostat._last_relay_update)
        with patch.object(thermostat, "update", side_effect=RuntimeError("unexpected")):
            await coordinator.async_refresh()
        self.assertIsNone(thermostat._last_relay_update)
        self.clock += 60
        await self.poll(coordinator)
        self.assertEqual(thermostat.energy_counters["heating_time_seconds"], 0)

    async def test_explicit_counter_reset_persists_zero_without_changing_sensor_identity(
        self,
    ):
        config, thermostat, coordinator = self.make_coordinator()
        await coordinator.async_restore_energy_counters()
        thermostat.restore_energy_counters(
            {"heating_energy_kwh": 3, "heating_time_seconds": 3600}
        )
        await coordinator.async_save_energy_counters()
        descriptions = {d.key: d for d in SENSOR_DESCRIPTIONS}
        sensors = [
            TerneoSensorEntity(coordinator, thermostat, config, descriptions[key])
            for key in ("heating_energy", "heating_time")
        ]
        before = [sensor.unique_id for sensor in sensors]
        thermostat.reset_energy_counter()
        await coordinator.async_save_energy_counters()
        _, restored, other = self.make_coordinator(config)
        await other.async_restore_energy_counters()
        self.assertEqual(
            restored.energy_counters,
            {"heating_energy_kwh": 0, "heating_time_seconds": 0},
        )
        self.assertEqual([sensor.unique_id for sensor in sensors], before)
        self.assertTrue(
            all(
                sensor.state_class is SensorStateClass.TOTAL_INCREASING
                for sensor in sensors
            )
        )
        self.assertEqual(sensors[0].native_unit_of_measurement, "kWh")
        self.assertEqual(sensors[1].device_class, SensorDeviceClass.DURATION)
        self.assertEqual(sensors[1].native_unit_of_measurement, "h")

    async def test_heating_time_sensor_is_available_without_power_but_energy_is_not(
        self,
    ):
        config, thermostat, coordinator = self.make_coordinator()
        await self.poll(coordinator, power=None)
        self.clock += 60
        await self.poll(coordinator, power=None)
        descriptions = {d.key: d for d in SENSOR_DESCRIPTIONS}
        heating_time = TerneoSensorEntity(
            coordinator, thermostat, config, descriptions["heating_time"]
        )
        energy = TerneoSensorEntity(
            coordinator, thermostat, config, descriptions["heating_energy"]
        )
        self.assertTrue(heating_time.available)
        self.assertGreater(heating_time.native_value, 0)
        self.assertFalse(energy.available)
        self.assertEqual(thermostat.energy_counters["heating_energy_kwh"], 0)

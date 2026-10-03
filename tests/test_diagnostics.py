"""Connection diagnostics regression tests without physical device requests."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import quote

import homeassistant  # noqa: F401
from homeassistant.components.diagnostics import REDACTED
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import EntityCategory
import requests

from custom_components.terneo.const import DEVICE_TYPE_NEW, DEVICE_TYPE_OLD, DOMAIN
from custom_components.terneo.coordinator import TerneoCoordinator
from custom_components.terneo.diagnostics import (
    _redact_diagnostics,
    async_get_config_entry_diagnostics,
)
from custom_components.terneo.sensor import (
    CONNECTION_SENSOR_DESCRIPTIONS,
    SENSOR_DESCRIPTIONS,
    TerneoSensorEntity,
    async_setup_entry,
)
from custom_components.terneo.thermostat import TerneoThermostat

SERIAL = "TEST-SERIAL-PRIVATE"
HOST = "192.0.2.123"
TITLE = "Kitchen Private Thermostat"
PARAMS = {"sn": SERIAL, "par": [[125, 7, "0"], [2, 2, "1"], [5, 1, "16"]]}
STATUS = {"sn": SERIAL, "t.1": "336", "t.5": "256", "m.1": "3", "f.16": "0", "f.0": "0"}


def response(payload, status=200):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload).encode()
    return result


class ConnectionMetricsTests(unittest.TestCase):
    def setUp(self):
        self.clock = 100.0
        self.now = datetime(2026, 10, 3, 12, tzinfo=timezone.utc)
        self.monotonic = patch("custom_components.terneo.thermostat.time.monotonic", side_effect=lambda: self.clock)
        self.sleep = patch("custom_components.terneo.thermostat.time.sleep")
        self.datetime = patch("custom_components.terneo.thermostat.datetime")
        self.monotonic.start()
        self.sleep.start()
        self.datetime.start().now.side_effect = lambda _: self.now
        self.addCleanup(self.monotonic.stop)
        self.addCleanup(self.sleep.stop)
        self.addCleanup(self.datetime.stop)
        self.thermostat = TerneoThermostat(SERIAL, HOST)

    def poll(self):
        with patch("requests.post", side_effect=[response(PARAMS), response(STATUS)]):
            self.assertTrue(self.thermostat.update())

    def test_initial_metrics_do_not_invent_freshness(self):
        data = self.thermostat.connection_diagnostics
        self.assertIsNone(data["last_successful_update"])
        self.assertIsNone(data["cached_state_age_seconds"])
        self.assertFalse(data["has_state"])
        self.assertEqual(data["request_count"], 0)
        self.assertEqual(sum(data["request_error_counts"].values()), 0)
        self.assertIsNone(data["last_request"]["duration_seconds"])

    def test_grace_period_age_and_recovery_metrics(self):
        self.poll()
        successful_at = self.thermostat.last_successful_update
        for failures, available in ((1, True), (2, True), (3, False)):
            self.clock += 30
            with patch("requests.post", side_effect=requests.Timeout("private exception")):
                self.assertFalse(self.thermostat.update())
            data = self.thermostat.connection_diagnostics
            self.assertEqual(data["last_successful_update"], successful_at)
            self.assertEqual(data["cached_state_age_seconds"], 30 * failures)
            self.assertEqual(data["consecutive_update_failures"], failures)
            self.assertEqual(data["available"], available)
            self.assertEqual(data["last_refresh_error_category"], "timeout")
            self.assertEqual(self.thermostat.floor_temperature, 21)
        self.now += timedelta(minutes=2)
        self.poll()
        data = self.thermostat.connection_diagnostics
        self.assertTrue(data["available"])
        self.assertEqual(data["consecutive_update_failures"], 0)
        self.assertEqual(data["cached_state_age_seconds"], 0)
        self.assertEqual(data["last_successful_update"], self.now)
        self.assertIsNone(data["last_refresh_error"])
        self.assertIsNone(data["last_refresh_error_category"])
        self.assertEqual(data["request_count"], 7)
        self.assertEqual(data["request_error_counts"]["timeout"], 3)

    def test_commands_and_partial_reads_do_not_refresh_full_state_age(self):
        self.poll()
        self.clock += 30
        with patch("requests.post", side_effect=requests.Timeout("private")):
            self.assertFalse(self.thermostat.update())
        self.clock += 10
        with patch("requests.post", return_value=response({"sn": SERIAL, "par": [[125, 7, "1"]]})):
            self.assertTrue(self.thermostat.turn_off())
        with patch("requests.post", return_value=response(PARAMS)):
            self.assertTrue(self.thermostat.get_parameters())
        data = self.thermostat.connection_diagnostics
        self.assertEqual(data["cached_state_age_seconds"], 40)
        self.assertEqual(data["consecutive_update_failures"], 1)
        self.assertEqual(data["last_refresh_error_category"], "timeout")
        self.assertIsNone(data["last_request"]["error_category"])
        with patch("requests.post", side_effect=requests.Timeout("private")):
            self.assertFalse(self.thermostat.turn_on())
        self.assertEqual(self.thermostat.consecutive_update_failures, 1)

    def test_wall_clock_jumps_do_not_change_cached_state_age(self):
        self.poll()
        successful_at = self.thermostat.last_successful_update
        self.clock += 17
        self.now -= timedelta(days=3)
        self.assertEqual(self.thermostat.cached_state_age, 17)
        self.assertEqual(self.thermostat.last_successful_update, successful_at)

    def test_request_duration_excludes_rate_limiting(self):
        self.sleep.side_effect = None
        def sleep(delay):
            self.clock += delay
        def post(*args, **kwargs):
            self.clock += 0.375
            return response(PARAMS)
        with patch("custom_components.terneo.thermostat.time.sleep", side_effect=sleep) as wait:
            with patch("requests.post", side_effect=post):
                self.assertTrue(self.thermostat.get_parameters())
        wait.assert_called_once_with(1.0)
        self.assertEqual(self.thermostat.last_request_duration, 0.375)
        self.assertEqual(self.thermostat.connection_diagnostics["last_request"]["http_status"], 200)
        self.assertIsNone(self.thermostat.last_successful_update)

    def test_transport_http_json_and_protocol_errors_are_distinct_for_both_profiles(self):
        invalid_json = response({})
        invalid_json._content = b'{"par":'
        cases = (
            (requests.Timeout("private"), "timeout", None),
            (requests.ConnectionError("private"), "transport", None),
            (response({}, 503), "http", 503),
            (invalid_json, "json", 200),
            (response([]), "protocol", 200),
            (response({"par": [[125]]}), "protocol", 200),
        )
        for profile in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            for result, category, status in cases:
                with self.subTest(profile=profile, category=category, result=type(result).__name__):
                    thermostat = TerneoThermostat(SERIAL, HOST, profile)
                    def post(*args, **kwargs):
                        self.clock += 0.25
                        if isinstance(result, Exception):
                            raise result
                        return result
                    with patch("requests.post", side_effect=post):
                        self.assertFalse(thermostat.update())
                    data = thermostat.connection_diagnostics
                    self.assertEqual(data["last_request"]["error_category"], category)
                    self.assertEqual(data["last_refresh_error_category"], category)
                    self.assertEqual(data["last_request"]["http_status"], status)
                    self.assertEqual(data["last_request"]["duration_seconds"], 0.25)
                    self.assertEqual(data["request_error_counts"][category], 1)
                    self.assertEqual(sum(data["request_error_counts"].values()), 1)
                    self.assertEqual(data["protocol"]["profile"], profile)

    def test_failed_status_does_not_refresh_timestamp_or_leak_device_text(self):
        self.poll()
        cached = self.thermostat.last_successful_update
        self.clock += 30
        bad = {"sn": SERIAL, "t.1": f"http://{HOST}/api.cgi?sn={SERIAL}"}
        with patch("requests.post", side_effect=[response(PARAMS), response(bad)]):
            self.assertFalse(self.thermostat.update())
        data = self.thermostat.connection_diagnostics
        self.assertEqual(data["last_refresh_error_category"], "protocol")
        self.assertEqual(data["last_refresh_error"], "Invalid status response")
        self.assertEqual(self.thermostat.last_successful_update, cached)
        self.assertEqual(self.thermostat.cached_state_age, 30)
        self.assertEqual(self.thermostat.floor_temperature, 21)

    def test_legacy_verification_timeout_is_not_double_counted_as_protocol_error(self):
        with patch("requests.post", side_effect=[
            response({"success": "true"}), requests.Timeout("private"),
        ]) as post:
            self.assertFalse(self.thermostat.turn_off())
        data = self.thermostat.connection_diagnostics
        self.assertEqual(post.call_count, 2)
        self.assertEqual(data["request_count"], 2)
        self.assertEqual(data["last_request"]["kind"], "parameters")
        self.assertEqual(data["last_request"]["error_category"], "timeout")
        self.assertEqual(data["request_error_counts"]["timeout"], 1)
        self.assertEqual(data["request_error_counts"]["protocol"], 0)
        self.assertEqual(data["consecutive_update_failures"], 0)

    def test_protocol_acknowledgement_failure_counts_once(self):
        with patch("requests.post", return_value=response({"sn": SERIAL, "par": [[125, 7, "0"]]})):
            self.assertFalse(self.thermostat.turn_off())
        data = self.thermostat.connection_diagnostics
        self.assertEqual(data["last_request"]["kind"], "parameter_write")
        self.assertEqual(data["request_error_counts"]["protocol"], 1)
        self.assertEqual(data["consecutive_update_failures"], 0)
        self.assertIsNone(data["cached_state_age_seconds"])

    def test_returned_metrics_do_not_mutate_client_counters(self):
        data = self.thermostat.connection_diagnostics
        data["request_error_counts"]["timeout"] = 999
        data["last_request"]["kind"] = "changed"
        self.assertEqual(self.thermostat.connection_diagnostics["request_error_counts"]["timeout"], 0)
        self.assertIsNone(self.thermostat.connection_diagnostics["last_request"]["kind"])


class RedactionTests(unittest.TestCase):
    def test_nested_keys_values_urls_and_identifiers_are_redacted_without_mutation(self):
        secret = "private-token/with spaces"
        data = {
            "entry": {"host": HOST, "serial": SERIAL, "title": TITLE},
            "nested": [{"Authorization": secret}, {"access-token": secret}],
            "tuple": ({"password": "private-password"},),
            "error": f"host={HOST}, sn={SERIAL.lower()}, name={TITLE}; token={quote(secret, safe='')}",
            f"error_for_{SERIAL}": "https://other-private-host.test/api?credential=unknown",
            "other": "wss://unknown-private.example/events",
            "file_url": "file:///private/user-directory",
            "ftp_url": "ftp://private-server.example/data",
            "count": 4,
        }
        before = deepcopy(data)
        result = _redact_diagnostics(data, [SERIAL, TITLE])
        serialized = json.dumps(result)
        for private in (HOST, SERIAL, SERIAL.lower(), TITLE, secret, quote(secret, safe=""),
                        "private-password", "other-private-host", "unknown-private.example",
                        "user-directory", "private-server.example"):
            self.assertNotIn(private, serialized)
        self.assertEqual(result["entry"]["host"], REDACTED)
        self.assertEqual(result["nested"][0]["Authorization"], REDACTED)
        self.assertEqual(result["tuple"][0]["password"], REDACTED)
        self.assertEqual(result["count"], 4)
        self.assertEqual(data, before)

    def test_numeric_identifiers_and_empty_values_are_safe(self):
        data = {"sn": 123456789, "copy": 123456789, "host": "", "token": None, "count": 3}
        result = _redact_diagnostics(data, [])
        self.assertEqual(result["sn"], REDACTED)
        self.assertEqual(result["copy"], REDACTED)
        self.assertEqual(result["count"], 3)

    def test_diagnostic_translations_match_descriptions_and_enum_states(self):
        root = Path(__file__).resolve().parents[1] / "custom_components" / "terneo"
        for filename in ("strings.json", "translations/en.json", "translations/ru.json"):
            translations = json.loads((root / filename).read_text())["entity"]["sensor"]
            for description in CONNECTION_SENSOR_DESCRIPTIONS:
                self.assertIn(description.translation_key, translations)
                if description.options:
                    self.assertEqual(set(translations[description.translation_key]["state"]), set(description.options))


class HomeAssistantDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.hass.config_entries = MagicMock()
        self.entry = ConfigEntry(
            version=1, minor_version=1, domain=DOMAIN, title=TITLE,
            data={"host": HOST, "serial": SERIAL, "device_type": DEVICE_TYPE_OLD},
            options={"scan_interval": 30, "timeout": 5, "show_advanced_sensors": True},
            source="user", unique_id=SERIAL, discovery_keys=MappingProxyType({}),
            subentries_data=[], state=ConfigEntryState.LOADED,
        )
        self.thermostat = TerneoThermostat(SERIAL, HOST)
        self.coordinator = TerneoCoordinator(self.hass, self.entry, self.thermostat)
        self.entry.runtime_data = self.coordinator
        self.sleep = patch("custom_components.terneo.thermostat.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    async def asyncTearDown(self):
        await self.coordinator.async_shutdown()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    async def test_export_is_read_only_json_serializable_and_redacts_config(self):
        with patch("requests.post", side_effect=[response(PARAMS), response(STATUS)]):
            await self.coordinator.async_refresh()
        with patch("requests.post") as post:
            result = await async_get_config_entry_diagnostics(self.hass, self.entry)
        post.assert_not_called()
        encoded = json.dumps(result, allow_nan=False)
        for identifier in (HOST, SERIAL, TITLE, self.entry.entry_id):
            self.assertNotIn(identifier, encoded)
        self.assertEqual(result["entry"]["data"]["host"], REDACTED)
        self.assertEqual(result["entry"]["options"]["scan_interval"], 30)
        self.assertEqual(result["runtime"]["connection"]["request_count"], 2)
        self.assertEqual(result["runtime"]["connection"]["protocol"]["parameter_count"], 3)
        self.assertIsInstance(result["runtime"]["connection"]["last_successful_update"], str)
        self.assertTrue(result["runtime"]["coordinator"]["last_update_success"])

    async def test_export_waits_for_device_lock_without_polling(self):
        async with self.coordinator._request_lock:
            with patch("requests.post") as post:
                snapshot = asyncio.create_task(async_get_config_entry_diagnostics(self.hass, self.entry))
                await asyncio.sleep(0)
                self.assertFalse(snapshot.done())
                post.assert_not_called()
        with patch("requests.post") as post:
            result = await asyncio.wait_for(snapshot, 5)
        post.assert_not_called()
        self.assertEqual(result["runtime"]["connection"]["request_count"], 0)

    async def test_export_handles_unloaded_entry_without_runtime(self):
        del self.entry.runtime_data
        with patch("requests.post") as post:
            result = await async_get_config_entry_diagnostics(self.hass, self.entry)
        post.assert_not_called()
        self.assertIsNone(result["runtime"])
        self.assertNotIn(HOST, json.dumps(result))

    async def test_export_never_includes_raw_responses_or_exception_text(self):
        self.thermostat._status = {"sn": SERIAL, "error": "private raw payload", "nested": {"token": "secret"}}
        self.thermostat._parameters = {999: (0, "private parameter payload")}
        with patch.object(self.thermostat, "update", side_effect=RuntimeError(f"private runtime {HOST}")):
            await self.coordinator.async_refresh()
        result = await async_get_config_entry_diagnostics(self.hass, self.entry)
        serialized = json.dumps(result)
        for private in (HOST, SERIAL, "private raw payload", "private parameter payload", "private runtime", "secret"):
            self.assertNotIn(private, serialized)
        self.assertEqual(result["runtime"]["coordinator"]["last_exception_type"], "RuntimeError")

    async def test_diagnostic_sensors_remain_available_through_outage_and_recovery(self):
        diagnostic = [TerneoSensorEntity(self.coordinator, self.thermostat, self.entry, d)
                      for d in CONNECTION_SENSOR_DESCRIPTIONS]
        ordinary = TerneoSensorEntity(self.coordinator, self.thermostat, self.entry, SENSOR_DESCRIPTIONS[0])
        for entity in diagnostic:
            self.assertEqual(entity.entity_category, EntityCategory.DIAGNOSTIC)
            self.assertFalse(entity.entity_registry_enabled_default)
            self.assertTrue(entity.available)
        with patch("requests.post", side_effect=[response(PARAMS), response(STATUS)]):
            await self.coordinator.async_refresh()
        timestamp = diagnostic[0].native_value
        self.assertIsInstance(timestamp, datetime)
        self.assertIsNotNone(timestamp.tzinfo)
        with patch("requests.post", side_effect=requests.Timeout("private")):
            for _ in range(3):
                await self.coordinator.async_refresh()
        self.assertFalse(ordinary.available)
        self.assertTrue(all(entity.available for entity in diagnostic))
        self.assertEqual(diagnostic[0].native_value, timestamp)
        self.assertGreaterEqual(diagnostic[1].native_value, 0)
        self.assertEqual(diagnostic[2].native_value, 3)
        self.assertEqual(diagnostic[4].native_value, "timeout")
        with patch("requests.post", side_effect=[response(PARAMS), response(STATUS)]):
            await self.coordinator.async_refresh()
        self.assertTrue(ordinary.available)
        self.assertEqual(diagnostic[2].native_value, 0)
        self.assertEqual(diagnostic[4].native_value, "none")

    async def test_both_profiles_create_diagnostic_sensors_without_network_requests(self):
        for profile in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            self.thermostat._is_new_version = profile == DEVICE_TYPE_NEW
            add_entities = MagicMock()
            with patch("requests.post") as post:
                await async_setup_entry(self.hass, self.entry, add_entities)
            post.assert_not_called()
            created = add_entities.call_args.args[0]
            diagnostics = [e for e in created if e.entity_category is EntityCategory.DIAGNOSTIC]
            self.assertEqual(len(diagnostics), 5)
            self.assertEqual(len({e.unique_id for e in created}), len(created))
            self.assertFalse(any(e.entity_registry_enabled_default for e in diagnostics))
            self.assertEqual(diagnostics[0].device_class, SensorDeviceClass.TIMESTAMP)
            self.assertEqual(diagnostics[3].native_unit_of_measurement, "s")

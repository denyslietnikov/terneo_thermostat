"""Command diagnostics retain useful values without exposing device payloads."""

import logging
import unittest
from copy import deepcopy
from unittest.mock import patch

from custom_components.terneo.const import DEVICE_TYPE_NEW, DEVICE_TYPE_OLD
from custom_components.terneo.thermostat import TerneoThermostat
from tests.http import FakeSession
from tests.test_command_acknowledgements import response

LOGGER = "custom_components.terneo.thermostat"
SERIAL = "private-device-serial"
HOST = "192.0.2.123"


class CommandLoggingTests(unittest.IsolatedAsyncioTestCase):
    def make_client(self, profile=DEVICE_TYPE_OLD):
        client = TerneoThermostat(SERIAL, HOST, profile, session=FakeSession())
        client._parameters = {125: (7, "1"), 118: (7, "0"), 2: (2, "1")}
        client._power_on = False
        return client

    async def test_ignored_legacy_heat_reports_all_mismatches_without_retrying(self):
        client = self.make_client()
        previous = deepcopy(client._parameters)
        with (
            self.assertLogs(LOGGER, level="DEBUG") as logs,
            patch(
                "tests.http.post",
                side_effect=[
                    response({"success": "true"}),
                    response(
                        {
                            "sn": SERIAL,
                            "par": [[125, 7, "1"], [118, 7, "1"], [2, 2, "0"]],
                        }
                    ),
                ],
            ) as http,
        ):
            self.assertFalse(await client.set_hvac_mode("heat"))
        warnings = [r for r in logs.records if r.levelno == logging.WARNING]
        self.assertEqual(len(warnings), 1)
        write_id, source, mismatches, requested, actual = warnings[0].args
        self.assertRegex(write_id, r"^[0-9a-f]{12}$")
        self.assertEqual(source, "legacy_readback")
        self.assertEqual(mismatches, [125, 118, 2])
        self.assertEqual(
            requested,
            {
                125: {"type": 7, "value": "0"},
                118: {"type": 7, "value": "0"},
                2: {"type": 2, "value": "1"},
            },
        )
        self.assertEqual(
            actual,
            {
                125: {"type": 7, "value": "1"},
                118: {"type": 7, "value": "1"},
                2: {"type": 2, "value": "0"},
            },
        )
        self.assertTrue(
            any(
                write_id in r.getMessage() and "started" in r.getMessage()
                for r in logs.records
            )
        )
        self.assertTrue(
            any(
                write_id in r.getMessage() and "legacy receipt" in r.getMessage()
                for r in logs.records
            )
        )
        self.assertEqual(http.call_count, 2)
        self.assertEqual(
            sum("par" in call.kwargs["json"] for call in http.call_args_list), 1
        )
        self.assertEqual(client._parameters, previous)
        self.assertFalse(client.power_on)
        self.assertIn("outcome is unknown", client.last_update_error)
        for sensitive in (SERIAL, HOST):
            self.assertNotIn(sensitive, " ".join(logs.output))

    async def test_missing_and_wrong_type_are_distinguishable_at_warning_level(self):
        for params, expected in (
            ([[23, 2, "2"]], "missing"),
            ([[125, 2, "0"]], {"type": 2, "value": "0"}),
        ):
            with self.subTest(params=params):
                client = self.make_client(DEVICE_TYPE_NEW)
                with (
                    self.assertLogs(LOGGER, level="WARNING") as logs,
                    patch(
                        "tests.http.post",
                        return_value=response({"sn": SERIAL, "par": params}),
                    ),
                ):
                    self.assertFalse(await client.turn_on())
                self.assertEqual(len(logs.records), 1)
                _, source, mismatches, requested, actual = logs.records[0].args
                self.assertEqual(source, "acknowledgement")
                self.assertEqual(mismatches, [125])
                self.assertEqual(requested, {125: {"type": 7, "value": "0"}})
                self.assertEqual(actual, {125: expected})

    async def test_success_logs_confirmed_values_and_unique_write_ids(self):
        for profile in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            with self.subTest(profile=profile):
                client = self.make_client(profile)
                with (
                    self.assertLogs(LOGGER, level="DEBUG") as logs,
                    patch(
                        "tests.http.post",
                        return_value=response({"sn": SERIAL, "par": [[5, 1, "17"]]}),
                    ) as http,
                ):
                    self.assertTrue(await client.set_parameters([[5, 1, "017"]]))
                    self.assertTrue(await client.set_parameters([[5, 1, "17"]]))
                self.assertFalse(
                    any(r.levelno >= logging.WARNING for r in logs.records)
                )
                confirmed = [r for r in logs.records if " confirmed:" in r.getMessage()]
                self.assertEqual(len(confirmed), 2)
                self.assertNotEqual(confirmed[0].args[0], confirmed[1].args[0])
                for record in confirmed:
                    self.assertEqual(
                        record.args[1:],
                        ("acknowledgement", {5: {"type": 1, "value": "17"}}),
                    )
                self.assertEqual(http.call_count, 2)

    async def test_unknown_string_and_oversized_values_are_redacted(self):
        client = self.make_client()
        secret = "private-token\nhttp://private-host/credential"
        numeric_secret = "12345678901234567890123456789012345678"
        client._parameters.update({5: (0, secret), 600: (6, numeric_secret)})
        requested = [
            [125, 7, "0"],
            [5, 0, secret],
            [600, 6, numeric_secret],
            [31, 6, numeric_secret],
        ]
        reply = {
            "sn": SERIAL,
            "token": secret,
            "host": HOST,
            "par": [
                [125, 7, "1"],
                [5, 0, "changed-" + secret],
                [600, 6, "42"],
                [31, 6, numeric_secret],
                [601, 0, "unrequested-secret"],
            ],
        }
        with (
            self.assertLogs(LOGGER, level="DEBUG") as logs,
            patch("tests.http.post", return_value=response(reply)),
        ):
            self.assertFalse(await client.set_parameters(requested))
        text = " ".join(logs.output)
        for sensitive in (
            SERIAL,
            HOST,
            secret,
            "private-token",
            "private-host",
            numeric_secret,
            "unrequested-secret",
        ):
            self.assertNotIn(sensitive, text)
        warning = next(r for r in logs.records if r.levelno == logging.WARNING)
        self.assertEqual(warning.args[3][5]["value"], "[redacted]")
        self.assertEqual(warning.args[3][600]["value"], "[redacted]")
        self.assertEqual(warning.args[3][31]["value"], "[redacted]")
        self.assertEqual(warning.args[4][600]["value"], "[redacted]")
        self.assertEqual(warning.args[4][125]["value"], "1")
        self.assertNotIn(601, warning.args[4])

    async def test_transport_failure_has_correlated_safe_debug_outcome(self):
        client = self.make_client()
        with (
            self.assertLogs(LOGGER, level="DEBUG") as logs,
            patch(
                "tests.http.post",
                side_effect=TimeoutError(f"http://{HOST} sn={SERIAL} token=secret"),
            ) as http,
        ):
            self.assertFalse(await client.turn_on())
        started = next(r for r in logs.records if " started:" in r.getMessage())
        failure = next(r for r in logs.records if " failed:" in r.getMessage())
        self.assertEqual(started.args[0], failure.args[0])
        self.assertIn("timed out", failure.getMessage())
        self.assertEqual(http.call_count, 1)
        for sensitive in (SERIAL, HOST, "secret", "token="):
            self.assertNotIn(sensitive, " ".join(logs.output))

    async def test_legacy_readback_failure_keeps_command_correlation(self):
        client = self.make_client()
        with (
            self.assertLogs(LOGGER, level="DEBUG") as logs,
            patch(
                "tests.http.post",
                side_effect=[
                    response({"success": "true"}),
                    TimeoutError("private-transport-detail"),
                ],
            ) as http,
        ):
            self.assertFalse(await client.turn_on())
        started = next(r for r in logs.records if " started:" in r.getMessage())
        failures = [r for r in logs.records if " failed:" in r.getMessage()]
        self.assertEqual(len(failures), 1)
        self.assertEqual(failures[0].args[0], started.args[0])
        self.assertIn("legacy_readback", failures[0].getMessage())
        self.assertIn("timed out", failures[0].getMessage())
        self.assertNotIn("private-transport-detail", " ".join(logs.output))
        self.assertEqual(http.call_count, 2)

    async def test_cached_invalid_values_and_numeric_whitespace_cannot_inject_logs(
        self,
    ):
        for cached, expected in (("cache-bad", "[redacted]"), ("17\n", "17")):
            with self.subTest(cached=cached):
                client = self.make_client()
                client._parameters[5] = (1, cached)
                with (
                    self.assertLogs(LOGGER, level="DEBUG") as logs,
                    patch(
                        "tests.http.post",
                        return_value=response({"sn": SERIAL, "par": [[5, 1, "17"]]}),
                    ),
                ):
                    self.assertTrue(await client.set_parameters([[5, 1, "17\n"]]))
                started = next(r for r in logs.records if " started:" in r.getMessage())
                self.assertEqual(started.args[3][5]["value"], expected)
                self.assertEqual(started.args[2][5]["value"], "17")
                self.assertNotIn("\n", started.getMessage())
                self.assertNotIn("cache-bad", started.getMessage())

    async def test_invalid_ack_does_not_log_untrusted_payload(self):
        client = self.make_client()
        for reply in (
            {"sn": "other-private-serial", "par": [[125, 7, "0"]]},
            {"sn": SERIAL, "par": [[125, 7, "private-response"]]},
            {"success": "block", "error": "private-response"},
        ):
            with self.subTest(reply=reply):
                with (
                    self.assertLogs(LOGGER, level="DEBUG") as logs,
                    patch("tests.http.post", return_value=response(reply)) as http,
                ):
                    self.assertFalse(await client.turn_on())
                self.assertFalse(
                    any(" confirmed:" in r.getMessage() for r in logs.records)
                )
                for sensitive in (
                    SERIAL,
                    HOST,
                    "other-private-serial",
                    "private-response",
                ):
                    self.assertNotIn(sensitive, " ".join(logs.output))
                self.assertEqual(http.call_count, 1)

    async def test_child_lock_boolean_values_are_visible_without_identifiers(self):
        client = self.make_client(DEVICE_TYPE_NEW)
        with (
            self.assertLogs(LOGGER, level="DEBUG") as logs,
            patch(
                "tests.http.post",
                return_value=response({"sn": SERIAL, "par": [[124, 7, "0"]]}),
            ),
        ):
            self.assertFalse(await client.set_children_lock(True))
        warning = next(r for r in logs.records if r.levelno == logging.WARNING)
        self.assertEqual(warning.args[3], {124: {"type": 7, "value": "1"}})
        self.assertEqual(warning.args[4], {124: {"type": 7, "value": "0"}})
        for identifier in (SERIAL, HOST):
            self.assertNotIn(identifier, " ".join(logs.output))

    async def test_normal_poll_does_not_emit_command_logs(self):
        client = self.make_client()
        with (
            self.assertNoLogs(LOGGER, level="DEBUG"),
            patch(
                "tests.http.post",
                side_effect=[
                    response({"sn": SERIAL, "par": [[125, 7, "1"]]}),
                    response(
                        {
                            "sn": SERIAL,
                            "t.1": "336",
                            "t.5": "256",
                            "f.0": "0",
                            "f.16": "1",
                        }
                    ),
                ],
            ),
        ):
            self.assertTrue(await client.update())

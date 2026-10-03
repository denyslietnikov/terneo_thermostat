"""Command acknowledgement tests; no physical thermostat is contacted."""
from __future__ import annotations

import json
import unittest
from unittest.mock import patch

# HA must initialize its validation compatibility before integration imports.
import homeassistant  # noqa: F401
import requests

from custom_components.terneo.const import (
    DataType,
    DEVICE_TYPE_NEW,
    DEVICE_TYPE_OLD,
    ParamNum,
)
from custom_components.terneo.thermostat import TerneoThermostat


def response(payload, status_code=200):
    result = requests.Response()
    result.status_code = status_code
    result._content = json.dumps(payload).encode()
    return result


class CommandAcknowledgementTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch("custom_components.terneo.thermostat.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def thermostat(self, device_type=DEVICE_TYPE_OLD):
        thermostat = TerneoThermostat("test-serial", "192.0.2.1", device_type)
        thermostat._power_on = True
        thermostat._setpoint = 25.0
        thermostat._parameters = {23: (2, "2"), 125: (7, "0")}
        thermostat._status = {"t.5": "400"}
        return thermostat

    def acknowledgement(self, thermostat, params):
        return {"sn": thermostat.sn, "par": params}

    def assert_cache_unchanged(self, thermostat):
        self.assertTrue(thermostat.power_on)
        self.assertEqual(thermostat.setpoint, 25.0)
        self.assertEqual(thermostat._parameters, {23: (2, "2"), 125: (7, "0")})
        self.assertEqual(thermostat._status, {"t.5": "400"})

    def test_power_write_requires_matching_parameter_acknowledgement(self):
        for device_type in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            with self.subTest(device_type=device_type):
                thermostat = self.thermostat(device_type)
                payload = self.acknowledgement(thermostat, [[23, 2, "2"], [125, 7, "1"]])
                with patch("requests.post", return_value=response(payload)) as post:
                    self.assertTrue(thermostat.turn_off())
                self.assertFalse(thermostat.power_on)
                self.assertIsNone(thermostat.last_update_error)
                self.assertEqual(post.call_args.kwargs["json"], {
                    "sn": thermostat.sn, "par": [[ParamNum.POWER_OFF, DataType.BOOL, "1"]],
                })
                post.assert_called_once()

    def test_turn_on_updates_cache_only_after_acknowledgement(self):
        thermostat = self.thermostat()
        thermostat._power_on = False
        payload = self.acknowledgement(thermostat, [[125, 7, "0"]])
        with patch("requests.post", return_value=response(payload)):
            self.assertTrue(thermostat.turn_on())
        self.assertTrue(thermostat.power_on)

    def test_setpoint_cache_uses_confirmed_protocol_precision(self):
        for device_type, value, data_type, expected in (
            (DEVICE_TYPE_OLD, "26", 1, 26.0),
            (DEVICE_TYPE_NEW, "267", 3, 26.7),
        ):
            with self.subTest(device_type=device_type):
                thermostat = self.thermostat(device_type)
                payload = self.acknowledgement(
                    thermostat, [[5, data_type, value], [2, 2, "1" if device_type == DEVICE_TYPE_OLD else "3"], [125, 7, "0"]]
                )
                with patch("requests.post", return_value=response(payload)):
                    self.assertTrue(thermostat.set_setpoint(26.75))
                self.assertEqual(thermostat.setpoint, expected)

    def test_protocol_errors_are_not_success_for_either_version(self):
        for device_type in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            thermostat = self.thermostat(device_type)
            for payload in (
                {"status": "error"}, {"status": "timeout"},
                {"success": "block"}, {"success": "false"}, {"success": False},
                {"status": "ok"}, {"error": "secret"},
                {}, [], None, {"sn": thermostat.sn},
                {"sn": "wrong-serial", "par": [[125, 7, "1"]]},
                {"par": [[125, 7, "1"]]},
            ):
                with self.subTest(device_type=device_type, payload=payload):
                    with patch("requests.post", return_value=response(payload)) as post:
                        self.assertFalse(thermostat.turn_off())
                    self.assert_cache_unchanged(thermostat)
                    self.assertIsNotNone(thermostat.last_update_error)
                    post.assert_called_once()

    def test_contradictory_error_markers_override_matching_parameters(self):
        thermostat = self.thermostat()
        for marker in (
            {"success": "block"}, {"success": "false"}, {"error": "secret"},
            {"status": "error"}, {"status": "timeout"},
        ):
            with self.subTest(marker=marker):
                payload = {**self.acknowledgement(thermostat, [[125, 7, "1"]]), **marker}
                with patch("requests.post", return_value=response(payload)):
                    self.assertFalse(thermostat.turn_off())
                self.assert_cache_unchanged(thermostat)

    def test_malformed_parameters_do_not_confirm_a_write(self):
        thermostat = self.thermostat()
        for params in (
            None, {}, [], [[125]], [[125, 7, "1", "extra"]],
            [[125, 7, "1"], [125, 7, "1"]],
            [[125, 7, "1"], [125, 7, "0"]],
            [[True, 7, "1"]], [[125, True, "1"]], [[-1, 7, "1"]],
            [[125, 99, "1"]], [[125, 7, 1]], [[125, 7, "true"]],
            [[125, 7, "1"], [23, 2, "secret"]],
        ):
            with self.subTest(params=params):
                with patch("requests.post", return_value=response(
                    self.acknowledgement(thermostat, params)
                )):
                    self.assertFalse(thermostat.turn_off())
                self.assert_cache_unchanged(thermostat)
                self.assertIn("Invalid command acknowledgement", thermostat.last_update_error)

    def test_missing_wrong_value_or_wrong_type_is_unconfirmed(self):
        thermostat = self.thermostat()
        for params in ([[23, 2, "2"]], [[125, 7, "0"]], [[125, 2, "1"]]):
            with self.subTest(params=params):
                with patch("requests.post", return_value=response(
                    self.acknowledgement(thermostat, params)
                )):
                    self.assertFalse(thermostat.turn_off())
                self.assert_cache_unchanged(thermostat)
                self.assertIn("did not confirm", thermostat.last_update_error)
                self.assertIn("outcome is unknown", thermostat.last_update_error)

    def test_partial_setpoint_acknowledgement_does_not_update_cache(self):
        thermostat = self.thermostat()
        for params in (
            [[125, 7, "0"], [2, 2, "1"]],
            [[125, 7, "0"], [2, 2, "0"], [5, 1, "27"]],
            [[125, 7, "0"], [2, 2, "1"], [5, 1, "25"]],
        ):
            with self.subTest(params=params):
                with patch("requests.post", return_value=response(
                    self.acknowledgement(thermostat, params)
                )):
                    self.assertFalse(thermostat.set_setpoint(27))
                self.assert_cache_unchanged(thermostat)

    def test_numeric_equivalence_and_extra_parameters_are_accepted(self):
        thermostat = self.thermostat()
        payload = self.acknowledgement(thermostat, [[125, 7, "0"], [23, 2, "02"]])
        with patch("requests.post", return_value=response(payload)):
            self.assertTrue(thermostat.set_brightness(2))

    def test_boolean_acknowledgement_cannot_substitute_for_parameters(self):
        thermostat = self.thermostat()
        with patch("requests.post", return_value=response({"success": "true"})):
            self.assertFalse(thermostat.set_children_lock(True))

    def test_legacy_success_marker_requires_matching_readback_without_cache_publication(self):
        thermostat = self.thermostat()
        params = [[125, 7, "1"], [2, 2, "1"], [5, 1, "17"]]
        confirmed = self.acknowledgement(thermostat, params + [[23, 2, "2"]])
        for marker in ({"success": "true"}, {"success": "true", "sn": thermostat.sn}):
            with self.subTest(marker=marker):
                with patch("requests.post", side_effect=[response(marker), response(confirmed)]) as post:
                    self.assertEqual(thermostat.set_parameters(params), confirmed)
                self.assertEqual([call.kwargs["json"] for call in post.call_args_list], [
                    {"sn": thermostat.sn, "par": params},
                    {"sn": thermostat.sn, "cmd": 1},
                ])
                self.assert_cache_unchanged(thermostat)

    def test_legacy_success_readback_failures_do_not_update_cache_or_repeat_write(self):
        for payload in (
            {"sn": "wrong", "par": [[125, 7, "1"]]},
            {"par": [[125, 7, "1"]]},
            {"sn": "test-serial", "par": [[125, 7, "0"]]},
            {"sn": "test-serial", "par": [[125, 2, "1"]]},
            {"sn": "test-serial", "par": [[23, 2, "2"]]},
            {"sn": "test-serial", "par": []},
            {"sn": "test-serial", "par": [[125, 7, "invalid"]]},
            {"success": "true"}, {"success": "block"}, {"status": "timeout"},
        ):
            with self.subTest(payload=payload):
                thermostat = self.thermostat()
                with patch("requests.post", side_effect=[
                    response({"success": "true"}), response(payload),
                ]) as post:
                    self.assertFalse(thermostat.turn_off())
                self.assertEqual(post.call_count, 2)
                self.assertEqual(sum("par" in c.kwargs["json"] for c in post.call_args_list), 1)
                self.assert_cache_unchanged(thermostat)
                self.assertIn("outcome is unknown", thermostat.last_update_error)

    def test_legacy_verification_transport_failure_preserves_uncertain_command_error(self):
        thermostat = self.thermostat()
        with patch("requests.post", side_effect=[
            response({"success": "true"}), requests.Timeout("private network details"),
        ]) as post:
            self.assertFalse(thermostat.turn_off())
        self.assertEqual(post.call_count, 2)
        self.assert_cache_unchanged(thermostat)
        self.assertIn("Unable to verify legacy parameter write", thermostat.last_update_error)
        self.assertIn("outcome is unknown", thermostat.last_update_error)
        self.assertNotIn("private network details", thermostat.last_update_error)

    def test_new_device_success_marker_does_not_trigger_legacy_fallback(self):
        thermostat = self.thermostat(DEVICE_TYPE_NEW)
        with patch("requests.post", return_value=response({"success": "true"})) as post:
            self.assertFalse(thermostat.turn_off())
        post.assert_called_once()
        self.assert_cache_unchanged(thermostat)

    def test_malformed_acknowledgement_cannot_be_rescued_by_legacy_readback(self):
        thermostat = self.thermostat()
        for payload in (
            {"success": "true", "par": None},
            {"success": "true", "sn": "wrong"},
            {"success": "true", "error": "private"},
            {"success": "true", "unexpected": 1},
        ):
            with self.subTest(payload=payload):
                with patch("requests.post", return_value=response(payload)) as post:
                    self.assertFalse(thermostat.turn_off())
                post.assert_called_once()
                self.assert_cache_unchanged(thermostat)

    def test_legacy_setpoint_cache_changes_only_after_matching_success_readback(self):
        thermostat = self.thermostat()
        params = [[125, 7, "0"], [2, 2, "1"], [5, 1, "17"]]
        with patch("requests.post", side_effect=[
            response({"success": "true"}), response(self.acknowledgement(thermostat, params)),
        ]) as post:
            self.assertTrue(thermostat.set_setpoint(17))
        self.assertEqual(post.call_count, 2)
        self.assertEqual(thermostat.setpoint, 17)
        self.assertEqual(thermostat._parameters, {23: (2, "2"), 125: (7, "0")})

    def test_http_failure_does_not_change_cache(self):
        thermostat = self.thermostat()
        with patch("requests.post", return_value=response(
            self.acknowledgement(thermostat, [[125, 7, "1"]]), status_code=503
        )) as post:
            self.assertFalse(thermostat.turn_off())
        self.assertIn("HTTP 503", thermostat.last_update_error)
        self.assertIn("outcome is unknown", thermostat.last_update_error)
        self.assert_cache_unchanged(thermostat)
        post.assert_called_once()

    def test_transport_errors_are_safe_and_not_retried(self):
        thermostat = self.thermostat()
        secret_message = f"http://192.0.2.1/api.cgi sn={thermostat.sn} auth=secret"
        for error in (
            requests.Timeout(secret_message), requests.ConnectionError(secret_message),
            requests.HTTPError(secret_message),
        ):
            with self.subTest(error=type(error).__name__):
                with self.assertLogs("custom_components.terneo.thermostat", level="DEBUG") as logs:
                    with patch("requests.post", side_effect=error) as post:
                        self.assertFalse(thermostat.turn_off())
                self.assert_cache_unchanged(thermostat)
                self.assertIn("outcome is unknown", thermostat.last_update_error)
                for sensitive in ("192.0.2.1", thermostat.sn, "secret", "auth="):
                    self.assertNotIn(sensitive, thermostat.last_update_error)
                    self.assertNotIn(sensitive, " ".join(logs.output))
                post.assert_called_once()

    def test_truncated_json_does_not_change_cache(self):
        thermostat = self.thermostat()
        result = response({})
        result._content = b'{"par":[[125,7,"'
        with patch("requests.post", return_value=result):
            self.assertFalse(thermostat.turn_off())
        self.assert_cache_unchanged(thermostat)
        self.assertIn("JSON", thermostat.last_update_error)
        self.assertIn("outcome is unknown", thermostat.last_update_error)

    def test_device_error_contents_are_not_logged(self):
        thermostat = self.thermostat()
        for payload in (
            {"error": f"{thermostat.sn} at 192.0.2.1"},
            {"status": f"secret {thermostat.sn}"},
            {"success": f"secret {thermostat.sn}"},
            self.acknowledgement(thermostat, [[23, 2, f"secret {thermostat.sn}"]]),
        ):
            with self.subTest(payload=payload):
                with self.assertLogs("custom_components.terneo.thermostat", level="DEBUG") as logs:
                    with patch("requests.post", return_value=response(payload)):
                        self.assertFalse(thermostat.turn_off())
                for sensitive in (thermostat.sn, "192.0.2.1", "secret"):
                    self.assertNotIn(sensitive, thermostat.last_update_error)
                    self.assertNotIn(sensitive, " ".join(logs.output))

    def test_success_clears_previous_command_error(self):
        thermostat = self.thermostat()
        with patch("requests.post", return_value=response({"success": "block"})):
            self.assertFalse(thermostat.turn_off())
        with patch("requests.post", return_value=response(
            self.acknowledgement(thermostat, [[125, 7, "1"]])
        )):
            self.assertTrue(thermostat.turn_off())
        self.assertIsNone(thermostat.last_update_error)

    def test_restart_requires_its_explicit_acknowledgement(self):
        for device_type in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            thermostat = self.thermostat(device_type)
            with self.subTest(device_type=device_type):
                with patch("requests.post", return_value=response({"success": "true"})) as post:
                    self.assertTrue(thermostat.restart())
                self.assertEqual(post.call_args.args[0], "http://192.0.2.1/test.cgi")
                self.assertEqual(post.call_args.kwargs["json"], {"cmd": "restart"})
                self.assert_cache_unchanged(thermostat)
                post.assert_called_once()

    def test_restart_rejects_missing_false_and_conflicting_acknowledgements(self):
        thermostat = self.thermostat()
        for payload in (
            {}, {"success": "false"}, {"success": False}, {"success": True},
            {"success": "block"}, {"status": "timeout"}, {"status": "ok"},
            {"success": "true", "status": "error"},
            {"success": "true", "error": "secret"},
            {"success": "true", "sn": "wrong-serial"},
            self.acknowledgement(thermostat, [[125, 7, "1"]]), [],
        ):
            with self.subTest(payload=payload):
                with patch("requests.post", return_value=response(payload)):
                    self.assertFalse(thermostat.restart())
                self.assertIsNotNone(thermostat.last_update_error)
                self.assert_cache_unchanged(thermostat)

    def test_invalid_outgoing_parameters_are_rejected_before_io(self):
        thermostat = self.thermostat()
        with patch("requests.post") as post:
            with self.assertRaises(ValueError):
                thermostat.set_parameters([[125, 7, "invalid"]])
        post.assert_not_called()
        self.assert_cache_unchanged(thermostat)


if __name__ == "__main__":
    unittest.main()

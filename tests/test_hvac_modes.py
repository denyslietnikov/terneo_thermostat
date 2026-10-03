"""HVAC transition tests without physical device communication."""
from __future__ import annotations

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

# Initialize HA's validation compatibility before importing the integration.
import homeassistant  # noqa: F401
import requests

from custom_components.terneo.const import DEVICE_TYPE_NEW, DEVICE_TYPE_OLD, OperationMode
from custom_components.terneo.thermostat import TerneoThermostat


MODE_PARAMETERS = {
    "off": [[125, 7, "1"]],
    "heat": [[125, 7, "0"], [118, 7, "0"], [2, 2, "1"]],
    "cool": [[125, 7, "0"], [118, 7, "1"], [2, 2, "1"]],
    "auto": [[125, 7, "0"], [2, 2, "0"]],
}


def response(payload):
    result = requests.Response()
    result.status_code = 200
    result._content = json.dumps(payload).encode()
    return result


def snapshot(thermostat):
    return (
        thermostat.power_on, thermostat.mode, thermostat.cooling_mode,
        thermostat.setpoint, deepcopy(thermostat._parameters),
        deepcopy(thermostat._status),
    )


class HvacTransitionTests(unittest.TestCase):
    def setUp(self):
        self.sleep = patch("custom_components.terneo.thermostat.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    def thermostat(self, device_type, source):
        thermostat = TerneoThermostat("test", "192.0.2.1", device_type)
        thermostat._power_on = source != "off"
        thermostat._mode = -1 if source == "off" else 0 if source == "auto" else 3
        thermostat._setpoint = 25
        thermostat._parameters = {
            125: (7, "1" if source == "off" else "0"),
            118: (7, "1" if source == "cool" else "0"),
            2: (2, "0" if source == "auto" else "1" if device_type == DEVICE_TYPE_OLD else "3"),
        }
        thermostat._status = {"t.5": "400"}
        return thermostat

    def test_every_transition_uses_one_complete_write_without_optimistic_state(self):
        for device_type in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            for source in MODE_PARAMETERS:
                for target, params in MODE_PARAMETERS.items():
                    with self.subTest(device_type=device_type, source=source, target=target):
                        params = deepcopy(params)
                        if device_type == DEVICE_TYPE_NEW and target in ("heat", "cool"):
                            params[-1][2] = "3"
                        thermostat = self.thermostat(device_type, source)
                        previous = snapshot(thermostat)
                        acknowledgement = {"sn": thermostat.sn, "par": params}
                        with patch("requests.post", return_value=response(acknowledgement)) as post:
                            self.assertTrue(thermostat.set_hvac_mode(target))
                        post.assert_called_once()
                        self.assertEqual(post.call_args.kwargs["json"], {
                            "sn": thermostat.sn, "par": params,
                        })
                        self.assertEqual(snapshot(thermostat), previous)

    def test_failed_batches_do_not_publish_partially_confirmed_state(self):
        for device_type in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            thermostat = self.thermostat(device_type, "off")
            previous = snapshot(thermostat)
            manual = "1" if device_type == DEVICE_TYPE_OLD else "3"
            for payload in (
                {"success": "block"},
                {"status": "error"},
                {"sn": thermostat.sn, "par": [[125, 7, "0"]]},
                {"sn": thermostat.sn, "par": [[125, 7, "0"], [118, 7, "1"], [2, 2, manual]]},
                {"sn": thermostat.sn, "par": [[125, 7, "0"], [118, 7, "0"], [2, 2, "0"]]},
            ):
                with self.subTest(device_type=device_type, payload=payload):
                    with patch("requests.post", return_value=response(payload)) as post:
                        self.assertFalse(thermostat.set_hvac_mode("heat"))
                    post.assert_called_once()
                    self.assertEqual(snapshot(thermostat), previous)
                    self.assertIsNotNone(thermostat.last_update_error)

    def test_unsupported_hvac_modes_are_rejected_before_io(self):
        thermostat = self.thermostat(DEVICE_TYPE_OLD, "off")
        previous = snapshot(thermostat)
        with patch("requests.post") as post:
            for mode in ("dry", "heat_cool", "invalid", None, True):
                with self.subTest(mode=mode):
                    with self.assertRaises(ValueError):
                        thermostat.set_hvac_mode(mode)
        post.assert_not_called()
        self.assertEqual(snapshot(thermostat), previous)

    def test_presets_retain_power_mode_batch_without_changing_cooling(self):
        for device_type in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            manual = "1" if device_type == DEVICE_TYPE_OLD else "3"
            for mode, api_value in ((OperationMode.SCHEDULE, "0"), (OperationMode.MANUAL, manual)):
                with self.subTest(device_type=device_type, mode=mode):
                    thermostat = self.thermostat(device_type, "cool")
                    params = [[125, 7, "0"], [2, 2, api_value]]
                    with patch("requests.post", return_value=response({
                        "sn": thermostat.sn, "par": params,
                    })) as post:
                        self.assertTrue(thermostat.set_mode(mode))
                    post.assert_called_once()
                    self.assertEqual(post.call_args.kwargs["json"]["par"], params)
                    self.assertTrue(thermostat.cooling_mode)

    def test_legacy_manual_parameter_and_telemetry_are_different_codes(self):
        thermostat = self.thermostat(DEVICE_TYPE_OLD, "off")
        # Sanitized values observed on Terneo AX firmware 2.24.1.Y.20.2.20.12.43.
        for power_off in ("0", "1"):
            with self.subTest(power_off=power_off):
                with patch("requests.post", side_effect=[
                    response({"sn": thermostat.sn, "par": [
                        [2, 2, "1"], [125, 7, power_off], [118, 7, "0"], [5, 1, "16"],
                    ]}),
                    response({"sn": thermostat.sn, "m.1": "3", "f.16": power_off,
                              "f.0": "0", "t.1": "341", "t.5": "256"}),
                ]):
                    self.assertTrue(thermostat.update())
                self.assertEqual(thermostat._parameters[2], (2, "1"))
                self.assertEqual(thermostat.mode, -1 if power_off == "1" else OperationMode.MANUAL)
                self.assertEqual(thermostat.setpoint, 16)

    def test_all_legacy_manual_commands_write_one_not_telemetry_three(self):
        for command, args, params in (
            ("set_hvac_mode", ("heat",), MODE_PARAMETERS["heat"]),
            ("set_hvac_mode", ("cool",), MODE_PARAMETERS["cool"]),
            ("set_mode", (OperationMode.MANUAL,), [[125, 7, "0"], [2, 2, "1"]]),
            ("set_setpoint", (27,), [[125, 7, "0"], [2, 2, "1"], [5, 1, "27"]]),
        ):
            with self.subTest(command=command, args=args):
                thermostat = self.thermostat(DEVICE_TYPE_OLD, "off")
                with patch("requests.post", return_value=response({
                    "sn": thermostat.sn, "par": params,
                })) as post:
                    self.assertTrue(getattr(thermostat, command)(*args))
                post.assert_called_once()
                self.assertEqual(post.call_args.kwargs["json"]["par"], params)

    def test_telemetry_code_cannot_acknowledge_legacy_manual_write(self):
        thermostat = self.thermostat(DEVICE_TYPE_OLD, "off")
        for command, args in (
            ("set_hvac_mode", ("heat",)), ("set_mode", (OperationMode.MANUAL,)),
            ("set_setpoint", (27,)),
        ):
            with self.subTest(command=command):
                previous = snapshot(thermostat)
                with patch("requests.post", return_value=response({
                    "sn": thermostat.sn, "par": [
                        [125, 7, "0"], [118, 7, "0"], [2, 2, "3"], [5, 1, "27"],
                    ],
                })):
                    self.assertFalse(getattr(thermostat, command)(*args))
                self.assertEqual(snapshot(thermostat), previous)

    def test_legacy_setters_match_terneo_parameter_numbers_and_types(self):
        for command, args, params in (
            ("turn_on", (), [[125, 7, "0"]]),
            ("turn_off", (), [[125, 7, "1"]]),
            ("set_control_type", (0,), [[3, 2, "0"]]),
            ("set_sensor_type", (2,), [[18, 2, "2"]]),
            ("set_hysteresis", (1,), [[19, 2, "10"]]),
            ("set_floor_correction", (0,), [[21, 1, "0"]]),
            ("set_brightness", (6,), [[23, 2, "6"]]),
            ("set_prop_koef", (15,), [[25, 2, "15"]]),
            ("set_floor_limits", (5, 45), [[27, 1, "5"], [26, 1, "45"]]),
            ("set_away_temperature", (5,), [[7, 1, "5"]]),
            ("set_night_brightness_time", (1320, 480), [[52, 4, "1320"], [53, 4, "480"]]),
            ("set_lan_block", (False,), [[114, 7, "0"]]),
            ("set_cloud_block", (True,), [[115, 7, "1"]]),
            ("set_nc_contact_control", (False,), [[117, 7, "0"]]),
            ("set_cooling_mode", (False,), [[118, 7, "0"]]),
            ("set_use_night_brightness", (False,), [[120, 7, "0"]]),
            ("set_pre_control", (False,), [[121, 7, "0"]]),
            ("set_children_lock", (False,), [[124, 7, "0"]]),
        ):
            with self.subTest(command=command):
                thermostat = self.thermostat(DEVICE_TYPE_OLD, "off")
                with patch("requests.post", return_value=response({
                    "sn": thermostat.sn, "par": params,
                })) as post:
                    self.assertTrue(getattr(thermostat, command)(*args))
                post.assert_called_once()
                self.assertEqual(post.call_args.kwargs["json"], {
                    "sn": thermostat.sn, "par": params,
                })

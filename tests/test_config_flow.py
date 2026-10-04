"""Config and options flow contracts against the supported HA release."""

import json
import unittest
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import requests
import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, FlowResultType

from custom_components.terneo.config_flow import (
    CannotConnect,
    TerneoConfigFlow,
    TerneoOptionsFlowHandler,
    validate_connection,
)
from custom_components.terneo.const import DEVICE_TYPE_NEW, DEVICE_TYPE_OLD, DOMAIN

SERIAL = "058009000543474239343620000159"
INPUT = {"host": "192.0.2.1", "serial": SERIAL}


def response(data):
    result = requests.Response()
    result.status_code = 200
    result._content = json.dumps(data).encode()
    return result


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.hass.config_entries = MagicMock()
        self.hass.config_entries.async_entry_for_domain_unique_id.return_value = None
        self.hass.config_entries.flow.async_progress_by_handler.return_value = []
        self.flow = TerneoConfigFlow()
        self.flow.hass = self.hass
        self.flow.handler = DOMAIN
        self.flow.flow_id = "test-flow"
        self.flow.context = {"source": "user"}
        self.sleep = patch("custom_components.terneo.thermostat.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    async def asyncTearDown(self):
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    async def test_initial_form_requires_host_and_serial(self):
        result = await self.flow.async_step_user()
        self.assertEqual(result["type"], FlowResultType.FORM)
        self.assertEqual(result["step_id"], "user")
        self.assertEqual(result["data_schema"](INPUT), INPUT)
        for missing in INPUT:
            with self.subTest(missing=missing), self.assertRaises(vol.Invalid):
                result["data_schema"]({k: v for k, v in INPUT.items() if k != missing})

    async def test_both_profiles_are_identified_from_valid_device_parameters(self):
        for profile, params in (
            (DEVICE_TYPE_OLD, [[5, 1, "25"]]),
            (DEVICE_TYPE_NEW, [[5, 3, "250"], [4, 3, "220"]]),
        ):
            with (
                self.subTest(profile=profile),
                patch(
                    "requests.post",
                    return_value=response({"sn": SERIAL, "par": params}),
                ) as post,
            ):
                info = await validate_connection(self.hass, INPUT)
                self.assertEqual(info["serial"], SERIAL)
                self.assertEqual(info["device_type"], profile)
                self.assertEqual(info["title"], f"terneo_{SERIAL}")
                self.assertEqual(
                    post.call_args.kwargs["json"], {"cmd": 1, "sn": SERIAL}
                )

    async def test_invalid_device_responses_cannot_connect(self):
        for data in (
            {"sn": "other", "par": [[5, 1, "25"]]},
            {"par": [[5, 1, "25"]]},
            {"sn": SERIAL, "par": [[5]]},
            {"sn": SERIAL, "par": []},
            [],
        ):
            with (
                self.subTest(data=data),
                patch("requests.post", return_value=response(data)),
            ):
                with self.assertRaises(CannotConnect):
                    await validate_connection(self.hass, INPUT)

    async def test_timeout_returns_correctable_form(self):
        with patch("requests.post", side_effect=requests.Timeout("offline")):
            result = await self.flow.async_step_user(INPUT)
        self.assertEqual(result["step_id"], "user")
        self.assertEqual(result["errors"], {"base": "cannot_connect"})
        self.assertNotIn("unique_id", self.flow.context)

    async def test_unexpected_error_returns_form(self):
        with (
            patch(
                "custom_components.terneo.config_flow.validate_connection",
                new=AsyncMock(side_effect=RuntimeError("unexpected")),
            ),
            self.assertLogs("custom_components.terneo.config_flow", level="ERROR"),
        ):
            result = await self.flow.async_step_user(INPUT)
        self.assertEqual(result["errors"], {"base": "unknown"})

    async def test_success_name_and_default_name_for_both_profiles(self):
        for profile in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            for name in (None, "Kitchen"):
                with (
                    self.subTest(profile=profile, name=name),
                    patch(
                        "custom_components.terneo.config_flow.validate_connection",
                        new=AsyncMock(
                            return_value={
                                "serial": SERIAL,
                                "device_type": profile,
                                "title": f"terneo_{SERIAL}",
                            }
                        ),
                    ),
                ):
                    form = await self.flow.async_step_user(INPUT)
                    self.assertEqual(form["step_id"], "options")
                    self.assertEqual(self.flow.context["unique_id"], SERIAL)
                    expected_label = (
                        "With air sensor"
                        if profile == DEVICE_TYPE_NEW
                        else "Without air sensor"
                    )
                    self.assertEqual(
                        form["description_placeholders"]["device_type"], expected_label
                    )
                    validated = form["data_schema"](
                        {} if name is None else {"name": name}
                    )
                    result = await self.flow.async_step_options(validated)
                    self.assertEqual(result["type"], FlowResultType.CREATE_ENTRY)
                    self.assertEqual(result["title"], name or f"terneo_{SERIAL}")
                    self.assertEqual(result["data"]["device_type"], profile)
                    self.assertEqual(result["data"]["serial"], SERIAL)
                    self.assertEqual(result["data"]["host"], INPUT["host"])
                    self.assertEqual(result["data"]["name"], result["title"])

    async def test_existing_entry_aborts_without_creation(self):
        self.hass.config_entries.async_entry_for_domain_unique_id.return_value = (
            SimpleNamespace(source="user", data=INPUT, state=None)
        )
        with patch(
            "requests.post",
            return_value=response({"sn": SERIAL, "par": [[5, 1, "25"]]}),
        ):
            with self.assertRaises(AbortFlow) as raised:
                await self.flow.async_step_user(INPUT)
        self.assertEqual(raised.exception.reason, "already_configured")
        self.assertEqual(self.flow._discovered_info, {})

    async def test_parallel_flow_aborts_without_creation(self):
        self.hass.config_entries.flow.async_progress_by_handler.return_value = [
            {
                "flow_id": "other-flow",
                "context": {"source": "user", "unique_id": SERIAL},
            }
        ]
        with patch(
            "requests.post",
            return_value=response({"sn": SERIAL, "par": [[5, 1, "25"]]}),
        ):
            with self.assertRaises(AbortFlow) as raised:
                await self.flow.async_step_user(INPUT)
        self.assertEqual(raised.exception.reason, "already_in_progress")
        self.assertEqual(self.flow._discovered_info, {})

    async def test_options_use_ha_owned_entry_and_save_validated_values(self):
        config_entry = SimpleNamespace(
            entry_id="entry",
            options={
                "scan_interval": 60,
                "settings_scan_interval": 600,
                "timeout": 10,
                "show_advanced_sensors": True,
            },
        )
        self.hass.config_entries.async_get_known_entry.return_value = config_entry
        flow = TerneoConfigFlow.async_get_options_flow(config_entry)
        self.assertIsInstance(flow, TerneoOptionsFlowHandler)
        flow.hass = self.hass
        flow.handler = config_entry.entry_id
        form = await flow.async_step_init()
        self.assertEqual(form["data_schema"]({}), config_entry.options)
        self.hass.config_entries.async_get_known_entry.assert_called_with("entry")
        values = form["data_schema"](
            {
                "scan_interval": "10",
                "settings_scan_interval": "30",
                "timeout": "3",
                "show_advanced_sensors": False,
            }
        )
        saved = await flow.async_step_init(values)
        self.assertEqual(saved["type"], FlowResultType.CREATE_ENTRY)
        self.assertEqual(saved["data"], values)

    async def test_options_defaults_and_boundaries(self):
        self.hass.config_entries.async_get_known_entry.return_value = SimpleNamespace(
            options={}
        )
        flow = TerneoOptionsFlowHandler()
        flow.hass = self.hass
        flow.handler = "entry"
        form = await flow.async_step_init()
        schema = form["data_schema"]
        self.assertEqual(
            schema({}),
            {
                "scan_interval": 30,
                "settings_scan_interval": 300,
                "timeout": 5,
                "show_advanced_sensors": False,
            },
        )
        for key, low, high in (
            ("scan_interval", 10, 300),
            ("settings_scan_interval", 30, 3600),
            ("timeout", 3, 120),
        ):
            for value in (low, high):
                with self.subTest(key=key, valid=value):
                    self.assertEqual(schema({key: value})[key], value)
            for value in (low - 1, high + 1, "invalid"):
                with (
                    self.subTest(key=key, invalid=value),
                    self.assertRaises(vol.Invalid),
                ):
                    schema({key: value})

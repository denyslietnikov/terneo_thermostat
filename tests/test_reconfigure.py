"""Reconfiguration using real HA entry updates; thermostat HTTP is mocked."""

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

import requests
import voluptuous as vol
from homeassistant.config_entries import ConfigEntries, ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import AbortFlow, FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from custom_components.terneo import async_update_options
from custom_components.terneo.climate import TerneoClimateEntity
from custom_components.terneo.config_flow import TerneoConfigFlow
from custom_components.terneo.const import (
    DEFAULT_TIMEOUT,
    DEVICE_TYPE_NEW,
    DEVICE_TYPE_OLD,
    DOMAIN,
)
from custom_components.terneo.coordinator import TerneoCoordinator
from custom_components.terneo.thermostat import TerneoThermostat

SERIAL = "058009000543474239343620000159"
OLD_HOST = "192.0.2.1"
NEW_HOST = "192.0.2.2"


def response(data):
    result = requests.Response()
    result.status_code = 200
    result._content = json.dumps(data).encode()
    return result


class ReconfigureTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.manager = ConfigEntries(self.hass, {})
        self.hass.config_entries = self.manager
        dr.async_setup(self.hass)
        await dr.async_load(self.hass, load_empty=True)
        await ar.async_load(self.hass, load_empty=True)
        await er.async_load(self.hass, load_empty=True)
        await self.manager.async_initialize()
        self.reload = AsyncMock(return_value=True)
        self.reload_patch = patch.object(self.manager, "async_reload", self.reload)
        self.reload_patch.start()
        self.addCleanup(self.reload_patch.stop)
        self.sleep = patch("custom_components.terneo.thermostat.time.sleep")
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    async def asyncTearDown(self):
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        # orjson 3.11.9 fragments must not outlive their type at interpreter shutdown.
        for entry in self.manager.async_entries():
            entry.clear_storage_cache()
        self.temp.cleanup()

    async def make_entry(
        self,
        profile=DEVICE_TYPE_OLD,
        *,
        listener=True,
        state=ConfigEntryState.LOADED,
        serial=SERIAL,
    ):
        entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain=DOMAIN,
            title="Kitchen",
            data={
                "host": OLD_HOST,
                "serial": serial,
                "device_type": profile,
                "name": "Floor",
                "title": "original-title",
            },
            options={
                "timeout": 10,
                "scan_interval": 60,
                "settings_scan_interval": 600,
                "show_advanced_sensors": True,
            },
            source="user",
            unique_id=serial,
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
            state=state,
        )
        with patch.object(
            self.manager, "async_setup", new=AsyncMock(return_value=True)
        ):
            await self.manager.async_add(entry)
        if listener:
            entry.add_update_listener(async_update_options)
        return entry

    def make_flow(self, entry):
        flow = TerneoConfigFlow()
        flow.hass = self.hass
        flow.handler = DOMAIN
        flow.flow_id = "reconfigure-test"
        flow.context = {"source": "reconfigure", "entry_id": entry.entry_id}
        return flow

    def valid_response(self, profile=DEVICE_TYPE_OLD, serial=SERIAL):
        params = (
            [[5, 1, "25"]]
            if profile == DEVICE_TYPE_OLD
            else [[5, 3, "250"], [4, 3, "220"]]
        )
        return response({"sn": serial, "par": params})

    async def test_form_prefills_only_host_and_does_not_contact_device(self):
        entry = await self.make_entry()
        with patch("requests.post") as post:
            result = await self.make_flow(entry).async_step_reconfigure()
        post.assert_not_called()
        self.assertEqual(result["type"], FlowResultType.FORM)
        self.assertEqual(result["step_id"], "reconfigure")
        self.assertEqual(result["errors"], {})
        self.assertEqual(result["data_schema"]({}), {"host": OLD_HOST})
        self.assertEqual(result["description_placeholders"], {"serial": SERIAL})
        for invalid in (
            {"host": ""},
            {"host": 123},
            {"host": NEW_HOST, "serial": "other"},
        ):
            with self.subTest(invalid=invalid), self.assertRaises(vol.Invalid):
                result["data_schema"](invalid)
        self.reload.assert_not_awaited()

    async def test_success_preserves_entry_entities_options_and_energy_identity(self):
        other = await self.make_entry(serial="other-device")
        other_data = dict(other.data)
        for profile in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            with self.subTest(profile=profile):
                serial = (
                    SERIAL if profile == DEVICE_TYPE_OLD else "second-profile-device"
                )
                entry = await self.make_entry(profile, serial=serial)
                before_data, before_options = dict(entry.data), dict(entry.options)
                old_client = TerneoThermostat(serial, OLD_HOST, profile)
                old_coordinator = TerneoCoordinator(self.hass, entry, old_client)
                original_entity = TerneoClimateEntity(
                    old_coordinator, old_client, entry
                )
                device = dr.async_get(self.hass).async_get_or_create(
                    config_entry_id=entry.entry_id, identifiers={(DOMAIN, serial)}
                )
                registry_entity = er.async_get(self.hass).async_get_or_create(
                    "climate",
                    DOMAIN,
                    original_entity.unique_id,
                    config_entry=entry,
                    device_id=device.id,
                )
                self.reload.reset_mock()
                with patch(
                    "requests.post", return_value=self.valid_response(profile, serial)
                ) as post:
                    result = await self.make_flow(entry).async_step_reconfigure(
                        {"host": NEW_HOST}
                    )
                await self.hass.async_block_till_done()
                self.assertEqual(result["type"], FlowResultType.ABORT)
                self.assertEqual(result["reason"], "reconfigure_successful")
                self.assertEqual(entry.data, {**before_data, "host": NEW_HOST})
                self.assertEqual(entry.options, before_options)
                self.assertEqual(entry.title, "Kitchen")
                self.assertEqual(entry.unique_id, serial)
                self.assertIs(self.manager.async_get_known_entry(entry.entry_id), entry)
                self.reload.assert_awaited_once_with(entry.entry_id)
                self.assertEqual(other.data, other_data)
                post.assert_called_once()
                self.assertEqual(post.call_args.args[0], f"http://{NEW_HOST}/api.cgi")
                self.assertEqual(
                    post.call_args.kwargs["json"], {"cmd": 1, "sn": serial}
                )
                self.assertEqual(post.call_args.kwargs["timeout"], 10)
                new_client = TerneoThermostat(
                    entry.data["serial"], entry.data["host"], entry.data["device_type"]
                )
                new_coordinator = TerneoCoordinator(self.hass, entry, new_client)
                new_entity = TerneoClimateEntity(new_coordinator, new_client, entry)
                self.assertEqual(new_entity.unique_id, original_entity.unique_id)
                self.assertEqual(
                    new_coordinator._energy_store.key, old_coordinator._energy_store.key
                )
                self.assertEqual(
                    er.async_get(self.hass).async_get_entity_id(
                        "climate", DOMAIN, new_entity.unique_id
                    ),
                    registry_entity.entity_id,
                )
                self.assertEqual(
                    dr.async_get(self.hass)
                    .async_get_device_by_identifier((DOMAIN, serial), entry.entry_id)
                    .id,
                    device.id,
                )

    async def test_missing_timeout_option_uses_default(self):
        entry = await self.make_entry(listener=False)
        self.manager.async_update_entry(entry, options={})
        with patch("requests.post", return_value=self.valid_response()) as post:
            result = await self.make_flow(entry).async_step_reconfigure(
                {"host": NEW_HOST}
            )
        await self.hass.async_block_till_done()
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(post.call_args.kwargs["timeout"], DEFAULT_TIMEOUT)
        self.assertEqual(entry.options, {})
        self.reload.assert_awaited_once_with(entry.entry_id)

    async def test_not_loaded_entry_without_listener_schedules_one_reload(self):
        entry = await self.make_entry(
            listener=False, state=ConfigEntryState.SETUP_RETRY
        )
        with patch("requests.post", return_value=self.valid_response()):
            result = await self.make_flow(entry).async_step_reconfigure(
                {"host": NEW_HOST}
            )
        await self.hass.async_block_till_done()
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(entry.data["host"], NEW_HOST)
        self.reload.assert_awaited_once_with(entry.entry_id)

    async def test_unchanged_address_does_not_reload(self):
        for listener in (True, False):
            with self.subTest(listener=listener):
                entry = await self.make_entry(
                    listener=listener, serial=f"serial-{listener}"
                )
                original_data = dict(entry.data)
                with patch(
                    "requests.post",
                    return_value=response(
                        {"sn": entry.unique_id, "par": [[5, 1, "25"]]}
                    ),
                ):
                    result = await self.make_flow(entry).async_step_reconfigure(
                        {"host": OLD_HOST}
                    )
                await self.hass.async_block_till_done()
                self.assertEqual(result["reason"], "reconfigure_successful")
                self.assertEqual(entry.data, original_data)
                self.reload.assert_not_awaited()

    async def test_failed_probes_keep_original_address_and_options(self):
        entry = await self.make_entry()
        flow = self.make_flow(entry)
        original_data, original_options = dict(entry.data), dict(entry.options)
        malformed = response({})
        malformed._content = b'{"sn":'
        for result in (
            requests.Timeout("offline"),
            requests.ConnectionError("unreachable"),
            malformed,
            response({"par": [[5, 1, "25"]]}),
            response({"sn": "other-device", "par": [[5, 1, "25"]]}),
            response({"sn": SERIAL, "par": [[5]]}),
            response({"sn": SERIAL, "par": []}),
            response([]),
        ):
            with (
                self.subTest(result=result),
                patch("requests.post", side_effect=[result]),
            ):
                form = await flow.async_step_reconfigure({"host": NEW_HOST})
            self.assertEqual(form["step_id"], "reconfigure")
            self.assertEqual(form["errors"], {"base": "cannot_connect"})
            marker = next(iter(form["data_schema"].schema))
            self.assertEqual(marker.description["suggested_value"], NEW_HOST)
            self.assertEqual(entry.data, original_data)
            self.assertEqual(entry.options, original_options)
            self.reload.assert_not_awaited()
        with patch("requests.post", return_value=self.valid_response()):
            result = await flow.async_step_reconfigure({"host": NEW_HOST})
        await self.hass.async_block_till_done()
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.reload.assert_awaited_once_with(entry.entry_id)

    async def test_unique_id_guard_rejects_identity_change(self):
        entry = await self.make_entry()
        original_data = dict(entry.data)
        with patch(
            "custom_components.terneo.config_flow.validate_connection",
            new=AsyncMock(return_value={"serial": "other-device"}),
        ):
            with self.assertRaises(AbortFlow) as raised:
                await self.make_flow(entry).async_step_reconfigure({"host": NEW_HOST})
        self.assertEqual(raised.exception.reason, "unique_id_mismatch")
        self.assertEqual(entry.data, original_data)
        self.reload.assert_not_awaited()

    async def test_unexpected_error_is_correctable_without_saving(self):
        entry = await self.make_entry()
        with (
            patch(
                "custom_components.terneo.config_flow.validate_connection",
                new=AsyncMock(side_effect=RuntimeError("unexpected")),
            ),
            self.assertLogs("custom_components.terneo.config_flow", level="ERROR"),
        ):
            form = await self.make_flow(entry).async_step_reconfigure(
                {"host": NEW_HOST}
            )
        self.assertEqual(form["errors"], {"base": "unknown"})
        self.assertEqual(entry.data["host"], OLD_HOST)
        self.reload.assert_not_awaited()


def test_reconfigure_translation_schema_matches_english_messages():
    root = Path(__file__).resolve().parents[1] / "custom_components/terneo"
    strings = json.loads((root / "strings.json").read_text())
    english = json.loads((root / "translations/en.json").read_text())
    assert english == strings
    assert set(strings["config"]["step"]["reconfigure"]["data"]) == {"host"}
    assert "{serial}" in strings["config"]["step"]["reconfigure"]["description"]
    for reason in ("unique_id_mismatch", "reconfigure_successful"):
        assert strings["config"]["abort"][reason]
    for error in ("cannot_connect", "unknown"):
        assert strings["config"]["error"][error]


def test_reconfigure_process_exits_cleanly():
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pytest",
            f"{Path(__file__).resolve()}::ReconfigureTests",
            "--no-cov",
            "-q",
            "-p",
            "no:cacheprovider",
        ],
        cwd=root,
        env={**os.environ, "PYTHONFAULTHANDLER": "1"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr

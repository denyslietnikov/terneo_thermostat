"""Platform setup, presentation and command delegation for both profiles."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.climate import HVACAction, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from custom_components.terneo import button, climate, number, select, sensor, switch
from custom_components.terneo.const import (
    DEVICE_TYPE_NEW,
    DEVICE_TYPE_OLD,
    DOMAIN,
    ControlType,
    DataType,
    OperationMode,
    ParamNum,
)
from custom_components.terneo.coordinator import TerneoCoordinator
from custom_components.terneo.thermostat import TerneoThermostat
from tests.http import FakeSession


class PlatformTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.hass.config_entries = MagicMock()

    async def asyncTearDown(self):
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.temp.cleanup()

    def make_entry(self, profile):
        thermostat = TerneoThermostat(
            "test", "192.0.2.1", profile, session=FakeSession()
        )
        entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain=DOMAIN,
            title="Kitchen",
            data={"host": "192.0.2.1", "serial": "test"},
            options={},
            source="user",
            unique_id="test",
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        coordinator = TerneoCoordinator(self.hass, entry, thermostat)
        entry.runtime_data = coordinator
        coordinator.async_execute_command = AsyncMock()
        coordinator.async_request_refresh = AsyncMock()
        return entry, thermostat, coordinator

    async def test_setup_filters_profile_specific_entities_and_preserves_identity(self):
        for profile in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            entry, thermostat, coordinator = self.make_entry(profile)
            for platform, descriptions in (
                (number, number.NUMBER_DESCRIPTIONS),
                (select, select.SELECT_DESCRIPTIONS),
                (switch, switch.SWITCH_DESCRIPTIONS),
                (
                    sensor,
                    (
                        *sensor.SENSOR_DESCRIPTIONS,
                        *sensor.CONNECTION_SENSOR_DESCRIPTIONS,
                    ),
                ),
            ):
                with self.subTest(profile=profile, platform=platform.__name__):
                    add = MagicMock()
                    await platform.async_setup_entry(self.hass, entry, add)
                    entities = add.call_args.args[0]
                    expected = [
                        d
                        for d in descriptions
                        if not d.new_version_only or thermostat.is_new_version
                    ]
                    self.assertEqual(
                        [e.entity_description.key for e in entities],
                        [d.key for d in expected],
                    )
                    for entity in entities:
                        self.assertIs(entity.coordinator, coordinator)
                        self.assertEqual(
                            entity.unique_id, f"test_{entity.entity_description.key}"
                        )
                        self.assertEqual(
                            entity.device_info["identifiers"], {(DOMAIN, "test")}
                        )
                        self.assertEqual(entity.device_info["name"], "Kitchen")
                        # Read absent settings as unknown, not an exception or a fabricated value.
                        if isinstance(entity, select.TerneoSelectEntity):
                            self.assertIsNone(entity.current_option)
                        elif isinstance(entity, switch.TerneoSwitchEntity):
                            self.assertIsNone(entity.is_on)
                        else:
                            entity.native_value
                        with patch.object(entity, "async_write_ha_state") as write:
                            entity._handle_coordinator_update()
                        write.assert_called_once_with()
            for platform, entity_type in (
                (climate, climate.TerneoClimateEntity),
                (button, button.TerneoRestartButton),
            ):
                add = MagicMock()
                await platform.async_setup_entry(self.hass, entry, add)
                entities = add.call_args.args[0]
                self.assertEqual(len(entities), 1)
                self.assertIsInstance(entities[0], entity_type)

    async def test_settings_commands_delegate_once_and_refresh(self):
        entry, thermostat, coordinator = self.make_entry(DEVICE_TYPE_NEW)
        numeric = number.TerneoNumberEntity(
            coordinator, thermostat, entry, number.NUMBER_DESCRIPTIONS[0]
        )
        await numeric.async_set_native_value(2)
        coordinator.async_execute_command.assert_awaited_once_with(
            numeric.entity_description.set_fn, thermostat, 2
        )
        coordinator.async_request_refresh.assert_awaited_once_with()
        coordinator.async_execute_command.reset_mock()
        coordinator.async_request_refresh.reset_mock()
        choice = select.TerneoSelectEntity(
            coordinator, thermostat, entry, select.SELECT_DESCRIPTIONS[1]
        )
        await choice.async_select_option("4_7k")
        coordinator.async_execute_command.assert_awaited_once_with(
            choice.entity_description.set_fn, thermostat, "4_7k"
        )
        coordinator.async_request_refresh.assert_awaited_once_with()
        power = switch.TerneoSwitchEntity(
            coordinator, thermostat, entry, switch.SWITCH_DESCRIPTIONS[0]
        )
        for method, action in (
            (power.async_turn_on, power.entity_description.turn_on_fn),
            (power.async_turn_off, power.entity_description.turn_off_fn),
        ):
            coordinator.async_execute_command.reset_mock()
            coordinator.async_request_refresh.reset_mock()
            await method()
            coordinator.async_execute_command.assert_awaited_once_with(
                action, thermostat
            )
            coordinator.async_request_refresh.assert_awaited_once_with()

    async def test_failed_command_does_not_request_success_refresh(self):
        entry, thermostat, coordinator = self.make_entry(DEVICE_TYPE_OLD)
        coordinator.async_execute_command.side_effect = RuntimeError("command failed")
        entity = number.TerneoNumberEntity(
            coordinator, thermostat, entry, number.NUMBER_DESCRIPTIONS[0]
        )
        with self.assertRaises(RuntimeError):
            await entity.async_set_native_value(2)
        coordinator.async_request_refresh.assert_not_awaited()

    async def test_climate_temperature_source_limits_and_attributes(self):
        for profile in (DEVICE_TYPE_OLD, DEVICE_TYPE_NEW):
            entry, thermostat, coordinator = self.make_entry(profile)
            entity = climate.TerneoClimateEntity(coordinator, thermostat, entry)
            thermostat._floor_temperature = 24
            thermostat._air_temperature = 21
            thermostat._setpoint = 25
            thermostat._parameters = thermostat._parse_parameters(
                [
                    [ParamNum.HYSTERESIS, DataType.UINT8, "10"],
                    [ParamNum.POWER, DataType.UINT16, "100"],
                ]
            )
            thermostat._last_settings_error = None
            thermostat._settings_available = True
            for control_type, label in (
                (ControlType.FLOOR, "floor"),
                (ControlType.AIR, "air"),
                (ControlType.AIR_WITH_FLOOR_LIMIT, "air_with_floor_limit"),
            ):
                with self.subTest(profile=profile, control_type=control_type):
                    thermostat._status = {"m.0": str(control_type)}
                    air_control = (
                        profile == DEVICE_TYPE_NEW and control_type != ControlType.FLOOR
                    )
                    self.assertEqual(
                        entity.current_temperature, 21 if air_control else 24
                    )
                    self.assertEqual(entity.target_temperature, 25)
                    self.assertEqual(entity.min_temp, 5)
                    self.assertEqual(entity.max_temp, 35 if air_control else 45)
                    attrs = entity.extra_state_attributes
                    self.assertEqual(attrs["control_type"], label)
                    self.assertEqual(attrs["hysteresis"], 1)
                    self.assertEqual(attrs["power_watts"], 1000)
            thermostat._parameters.update(
                thermostat._parse_parameters(
                    [
                        [ParamNum.LOWER_LIMIT, DataType.INT8, "10"],
                        [ParamNum.UPPER_LIMIT, DataType.INT8, "40"],
                        [ParamNum.LOWER_AIR_LIMIT, DataType.INT8, "15"],
                        [ParamNum.UPPER_AIR_LIMIT, DataType.INT8, "30"],
                    ]
                )
            )
            self.assertEqual(entity.min_temp, 15 if profile == DEVICE_TYPE_NEW else 10)
            self.assertEqual(entity.max_temp, 30 if profile == DEVICE_TYPE_NEW else 40)
            thermostat._status = {}
            self.assertEqual(entity.extra_state_attributes["control_type"], "unknown")
            thermostat._last_settings_error = "timeout"
            thermostat._settings_available = False
            self.assertNotIn("hysteresis", entity.extra_state_attributes)
            self.assertNotIn("power_watts", entity.extra_state_attributes)

    async def test_climate_hvac_action_and_preset_projection(self):
        entry, thermostat, coordinator = self.make_entry(DEVICE_TYPE_OLD)
        entity = climate.TerneoClimateEntity(coordinator, thermostat, entry)
        for powered, mode, cooling, relay, expected_mode, action, preset in (
            (False, 3, False, False, HVACMode.OFF, HVACAction.OFF, "manual"),
            (True, -1, False, False, HVACMode.OFF, HVACAction.OFF, "manual"),
            (True, 0, False, False, HVACMode.AUTO, HVACAction.IDLE, "schedule"),
            (True, 3, False, True, HVACMode.HEAT, HVACAction.HEATING, "manual"),
            (True, 3, True, True, HVACMode.COOL, HVACAction.COOLING, "manual"),
        ):
            with self.subTest(powered=powered, mode=mode, cooling=cooling, relay=relay):
                thermostat._power_on = powered
                thermostat._mode = mode
                thermostat._relay_state = relay
                thermostat._status = {"m.5": "1" if cooling else "0"}
                self.assertEqual(entity.hvac_mode, expected_mode)
                self.assertEqual(entity.hvac_action, action)
                self.assertEqual(entity.preset_mode, preset)

    async def test_climate_commands_and_empty_temperature(self):
        entry, thermostat, coordinator = self.make_entry(DEVICE_TYPE_OLD)
        entity = climate.TerneoClimateEntity(coordinator, thermostat, entry)
        await entity.async_set_temperature()
        coordinator.async_execute_command.assert_not_awaited()
        for method, kwargs, command, args in (
            (
                entity.async_set_temperature,
                {"temperature": 25},
                thermostat.set_setpoint,
                (25,),
            ),
            (entity.async_turn_on, {}, thermostat.turn_on, ()),
            (entity.async_turn_off, {}, thermostat.turn_off, ()),
            (
                entity.async_set_preset_mode,
                {"preset_mode": "manual"},
                thermostat.set_mode,
                (OperationMode.MANUAL,),
            ),
        ):
            coordinator.async_execute_command.reset_mock()
            coordinator.async_request_refresh.reset_mock()
            await method(**kwargs)
            coordinator.async_execute_command.assert_awaited_once_with(command, *args)
            coordinator.async_request_refresh.assert_awaited_once_with()
        with patch.object(entity, "async_write_ha_state") as write:
            entity._handle_coordinator_update()
        write.assert_called_once_with()

    async def test_select_options_round_trip_and_match_valid_translation_keys(self):
        entry, thermostat, coordinator = self.make_entry(DEVICE_TYPE_NEW)
        strings = json.loads(
            (Path(sensor.__file__).parent / "strings.json").read_text()
        )
        translations = strings["entity"]["select"]["sensor_type"]["state"]
        self.assertEqual(
            set(translations), set(select.get_sensor_type_options(thermostat))
        )
        for option, value in select.SENSOR_TYPE_OPTIONS.items():
            with self.subTest(option=option):
                self.assertRegex(option, r"^[a-z0-9]+(?:[_-][a-z0-9]+)*$")
                thermostat._parameters = thermostat._parse_parameters(
                    [[ParamNum.SENSOR_TYPE, DataType.UINT8, str(value)]]
                )
                entity = select.TerneoSelectEntity(
                    coordinator, thermostat, entry, select.SELECT_DESCRIPTIONS[1]
                )
                self.assertEqual(entity.current_option, option)
                with patch.object(
                    thermostat, "set_sensor_type", return_value=True
                ) as setter:
                    self.assertTrue(
                        await select.set_sensor_type_value(thermostat, option)
                    )
                setter.assert_called_once_with(value)
        for control in ControlType:
            thermostat._status = {"m.0": str(control)}
            option = select.get_control_type_value(thermostat)
            with patch.object(
                thermostat, "set_control_type", return_value=True
            ) as setter:
                self.assertTrue(await select.set_control_type_value(thermostat, option))
            setter.assert_called_once_with(control)

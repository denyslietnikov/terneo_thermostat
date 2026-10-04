"""Terneo/Welrok Thermostat API client."""

import asyncio
import logging
import math
import time
from asyncio import sleep
from datetime import UTC, datetime
from typing import Any

import aiohttp

from .const import (
    CMD_GET_PARAMS,
    CMD_GET_STATUS,
    DEFAULT_MAX_UPDATE_FAILURES,
    DEFAULT_TIMEOUT,
    DEVICE_TYPE_NEW,
    DEVICE_TYPE_OLD,
    ControlType,
    DataType,
    OperationMode,
    ParamNum,
)

_LOGGER = logging.getLogger(__name__)


class TerneoThermostat:
    """
    A class for interacting with the Terneo/Welrok Thermostat's HTTP API.

    Supports both old (before June 2025) and new (from June 2025) versions.

    Parameters
    ----------
    serial_number : str
        Serial Number of device
    host : str
        Hostname or IP address.
    device_type : str, optional
        Device type: 'old' or 'new'
    timeout : int, optional
        Total HTTP exchange timeout in seconds (default: 5), excluding rate limiting
    session : aiohttp.ClientSession
        Shared HA-managed session; this client never closes it
    """

    def __init__(
        self,
        serial_number: str,
        host: str,
        device_type: str = DEVICE_TYPE_OLD,
        timeout: int = DEFAULT_TIMEOUT,
        max_update_failures: int = DEFAULT_MAX_UPDATE_FAILURES,
        max_heating_interval: float = 300,
        *,
        session: aiohttp.ClientSession,
    ):
        """Initialize the thermostat."""
        self.sn = serial_number
        self._session = session
        self.device_type = device_type
        self._is_new_version = device_type == DEVICE_TYPE_NEW
        self._timeout = timeout
        self._max_update_failures = max_update_failures
        self._max_heating_interval = max_heating_interval

        self._base_url = f"http://{host}/{{endpoint}}.cgi"
        self._last_request = time.monotonic()

        # Cached state
        self._available = False
        self._parameters: dict[int, Any] = {}
        self._status: dict[str, Any] = {}
        self._consecutive_update_failures = 0
        self._has_state = False
        self._last_update_error: str | None = None
        self._last_successful_update: datetime | None = None
        self._last_successful_update_monotonic: float | None = None
        self._last_refresh_error: str | None = None
        self._last_refresh_error_category: str | None = None
        self._settings_available = False
        self._last_successful_settings_update: datetime | None = None
        self._last_successful_settings_monotonic: float | None = None
        self._last_settings_error: str | None = None
        self._last_settings_error_category: str | None = None
        self._last_successful_status_update: datetime | None = None
        self._last_successful_status_monotonic: float | None = None
        self._request_count = 0
        self._request_error_counts = dict.fromkeys(
            ("timeout", "transport", "http", "json", "protocol"), 0
        )
        self._last_request_duration: float | None = None
        self._last_request_at: datetime | None = None
        self._last_request_kind: str | None = None
        self._last_request_http_status: int | None = None
        self._last_request_error: str | None = None
        self._last_request_error_category: str | None = None

        # Derived state
        self._setpoint: float | None = None
        self._floor_temperature: float | None = None
        self._air_temperature: float | None = None
        self._mode: int | None = None
        self._relay_state: bool | None = None
        self._power_on: bool | None = None

        # Energy tracking
        self._last_relay_update: float | None = None
        self._last_relay_power_watts: int | None = None
        self._heating_energy_kwh: float = 0.0  # Accumulated energy in kWh
        self._heating_time_seconds: float = 0.0  # Accumulated heating time in seconds

    def _get_url(self, endpoint: str) -> str:
        """Get the full URL for an endpoint."""
        return self._base_url.format(endpoint=endpoint)

    def _request_failed(
        self,
        message: str,
        *,
        command: bool = False,
        uncertain: bool = False,
        category: str = "protocol",
    ) -> bool:
        """Record a safe error without exposing request or response contents."""
        if command and uncertain:
            message += (
                ". Command outcome is unknown; refresh device state before retrying"
            )
        self._last_update_error = message
        self._last_request_error = message
        # A failed legacy verification can be wrapped as an uncertain command.
        # Count that HTTP request once and preserve its original error category.
        if self._last_request_error_category is None:
            self._last_request_error_category = category
            self._request_error_counts[category] += 1
        _LOGGER.debug("%s", message)
        return False

    async def _post(
        self, endpoint: str = "api", *, command: bool = False, **kwargs
    ) -> dict | bool:
        """Perform a POST request with rate limiting."""
        request_kwargs = dict(kwargs)
        headers = request_kwargs.pop("headers", {}) or {}
        headers = {"Connection": "close", **headers}
        request_kwargs["headers"] = headers

        # Rate limiting
        self._last_update_error = None
        delay = 1 - (time.monotonic() - self._last_request)
        if delay > 0:
            await sleep(delay)

        self._request_count += 1
        self._last_request_at = datetime.now(UTC)
        self._last_request_http_status = None
        self._last_request_error = None
        self._last_request_error_category = None
        payload = request_kwargs.get("json", {})
        self._last_request_kind = (
            "restart"
            if endpoint == "test"
            else "parameter_write"
            if "par" in payload
            else "parameters"
            if payload.get("cmd") == CMD_GET_PARAMS
            else "status"
            if payload.get("cmd") == CMD_GET_STATUS
            else "other"
        )
        started = time.monotonic()
        try:
            return await self._perform_post(endpoint, command=command, **request_kwargs)
        except asyncio.CancelledError:
            self._request_failed(
                "Thermostat request cancelled",
                command=command,
                uncertain=True,
                category="transport",
            )
            raise
        finally:
            self._last_request = time.monotonic()
            self._last_request_duration = max(0.0, self._last_request - started)

    async def _perform_post(
        self, endpoint: str, *, command: bool, **request_kwargs
    ) -> dict | bool:
        """Perform and validate one HTTP exchange, without logging device payloads."""
        try:
            async with self._session.post(
                self._get_url(endpoint),
                timeout=aiohttp.ClientTimeout(total=self._timeout),
                allow_redirects=False,
                **request_kwargs,
            ) as response:
                self._last_request_http_status = response.status
                if not 200 <= response.status < 300:
                    return self._request_failed(
                        f"Thermostat returned HTTP {response.status}",
                        command=command,
                        uncertain=True,
                        category="http",
                    )
                content = await response.json(content_type=None)
        except (TimeoutError, aiohttp.ClientError) as err:
            if isinstance(err, TimeoutError):
                message = "Thermostat request timed out"
                category = "timeout"
            elif isinstance(err, aiohttp.ClientResponseError):
                category = "http"
                status_code = err.status
                message = (
                    f"Thermostat returned HTTP {status_code}"
                    if isinstance(status_code, int)
                    else "Thermostat returned an HTTP error"
                )
            else:
                message = "Unable to communicate with thermostat"
                category = "transport"
            return self._request_failed(
                message, command=command, uncertain=True, category=category
            )
        except ValueError:
            return self._request_failed(
                "Failed to parse JSON response",
                command=command,
                uncertain=True,
                category="json",
            )

        if not isinstance(content, dict):
            return self._request_failed(
                "Expected a JSON object from thermostat",
                command=command,
                uncertain=True,
            )
        if "sn" in content and content["sn"] != self.sn:
            return self._request_failed(
                "Thermostat serial number mismatch", command=command, uncertain=True
            )
        if content.get("success") == "block":
            return self._request_failed(
                "Thermostat rejected request: LAN control is blocked"
            )
        if "error" in content or (
            "success" in content and content["success"] != "true"
        ):
            return self._request_failed(
                "Thermostat returned an error response", command=command, uncertain=True
            )
        if "status" in content and content["status"] != "ok":
            message = (
                "Thermostat reported a timeout"
                if content["status"] == "timeout"
                else "Thermostat returned an unexpected status"
            )
            return self._request_failed(message, command=command, uncertain=True)

        return content

    def _mark_update_successful(self) -> None:
        """Mark a full data refresh as successful."""
        self._mark_status_successful()
        self._settings_available = True
        self._last_settings_error = None
        self._last_settings_error_category = None
        self._last_successful_settings_update = datetime.now(UTC)
        self._last_successful_settings_monotonic = time.monotonic()
        self._last_successful_update = datetime.now(UTC)
        self._last_successful_update_monotonic = time.monotonic()
        self._last_refresh_error = None
        self._last_refresh_error_category = None

    def _mark_status_successful(self) -> None:
        """Confirm telemetry without advancing settings or full-refresh timestamps."""
        self._available = True
        self._has_state = True
        self._consecutive_update_failures = 0
        self._last_update_error = None
        self._last_successful_status_update = datetime.now(UTC)
        self._last_successful_status_monotonic = time.monotonic()

    def _mark_update_failed(self) -> None:
        """Track failed refreshes without flapping availability on one miss."""
        self.break_heating_interval()
        self._consecutive_update_failures += 1
        self._last_refresh_error = self._last_update_error
        self._last_refresh_error_category = self._last_request_error_category
        if self._consecutive_update_failures >= self._max_update_failures:
            self._available = False
        else:
            _LOGGER.debug(
                "Terneo update failed (%s/%s); keeping cached state: %s",
                self._consecutive_update_failures,
                self._max_update_failures,
                self._last_update_error or "unknown error",
            )

    def _parse_parameters(self, params: Any) -> dict[int, tuple[int, str]]:
        """Validate protocol records without publishing them to the state cache."""
        if not isinstance(params, list) or not params:
            raise ValueError("Expected a nonempty parameter list")
        parsed = {}
        for param in params:
            if not isinstance(param, list) or len(param) != 3:
                raise ValueError("Invalid parameter record")
            number, data_type, value = param
            if (
                not isinstance(number, int)
                or isinstance(number, bool)
                or number < 0
                or not isinstance(data_type, int)
                or isinstance(data_type, bool)
                or data_type not in DataType
            ):
                raise ValueError("Invalid parameter number or type")
            if number in parsed:
                raise ValueError("Duplicate parameter record")
            if not isinstance(value, str):
                raise ValueError("Expected a string parameter value")
            if data_type == DataType.BOOL and value not in ("0", "1"):
                raise ValueError("Invalid boolean parameter value")
            try:
                self._convert_value(value, data_type)
            except ValueError as err:
                raise ValueError("Invalid numeric parameter value") from err
            parsed[number] = (data_type, value)
        return parsed

    async def get_parameters(self) -> dict | bool:
        """Get all parameters from the device."""
        result = await self._post(json={"cmd": CMD_GET_PARAMS, "sn": self.sn})
        if result is False:
            return False
        try:
            parsed = self._parse_parameters(result.get("par"))
        except ValueError as err:
            return self._request_failed(f"Invalid parameter response: {err}")
        self._parameters = parsed
        return result

    async def set_parameters(self, params: list[list]) -> dict | bool:
        """Confirm every change in a full acknowledgement or legacy readback."""
        requested = self._parse_parameters(params)
        result = await self._post(command=True, json={"sn": self.sn, "par": params})
        if result is False:
            return False
        if (
            not self._is_new_version
            and result.get("success") == "true"
            and set(result) <= {"success", "sn"}
        ):
            # AX firmware can acknowledge receipt even when a write is ignored.
            # Verify without publishing partial state or repeating the write.
            try:
                result = await self._post(json={"cmd": CMD_GET_PARAMS, "sn": self.sn})
            except asyncio.CancelledError:
                self._request_failed(
                    "Legacy parameter verification cancelled",
                    command=True,
                    uncertain=True,
                )
                raise
            if result is False:
                return self._request_failed(
                    "Unable to verify legacy parameter write",
                    command=True,
                    uncertain=True,
                )
        if result.get("sn") != self.sn:
            return self._request_failed(
                "Missing thermostat identity in command acknowledgement",
                command=True,
                uncertain=True,
            )
        try:
            confirmed = self._parse_parameters(result.get("par"))
        except ValueError as err:
            return self._request_failed(
                f"Invalid command acknowledgement: {err}", command=True, uncertain=True
            )
        for number, (data_type, value) in requested.items():
            if (
                number not in confirmed
                or confirmed[number][0] != data_type
                or (
                    self._convert_value(confirmed[number][1], data_type)
                    != self._convert_value(value, data_type)
                )
            ):
                return self._request_failed(
                    "Thermostat did not confirm all requested parameters",
                    command=True,
                    uncertain=True,
                )
        return result

    async def get_status(self) -> dict | bool:
        """Get the status dictionary from the thermostat."""
        result = await self._post(json={"cmd": CMD_GET_STATUS, "sn": self.sn})
        if result is False:
            return False
        try:
            if not any(key in result for key in ("t.1", "t.5", "m.1", "f.0")):
                raise ValueError("Response did not contain thermostat status")
            for key in ("t.1", "t.5", "t.2"):
                if key in result:
                    float(result[key])
            for key in ("m.0", "m.1", "m.5", "f.0", "f.16"):
                if key in result:
                    int(result[key])
            for key in ("f.0", "f.16", "m.5"):
                if key in result and str(result[key]) not in ("0", "1"):
                    raise ValueError("Invalid binary status")
            if "m.0" in result and str(result["m.0"]) not in ("0", "1", "2"):
                raise ValueError("Invalid control type")
        except TypeError, ValueError:
            return self._request_failed("Invalid status response")
        self._status = result
        return result

    async def restart(self) -> bool:
        """Restart the device."""
        result = await self._post(
            endpoint="test", command=True, json={"cmd": "restart"}
        )
        if result is False:
            return False
        if result.get("success") != "true":
            return self._request_failed(
                "Missing restart acknowledgement", command=True, uncertain=True
            )
        _LOGGER.info("Device restart command acknowledged")
        return True

    def _get_param_value(self, param_num: int) -> Any | None:
        """Get a parameter value from cache."""
        if param_num in self._parameters:
            data_type, value = self._parameters[param_num]
            return self._convert_value(value, data_type)
        return None

    @staticmethod
    def _convert_value(value: str, data_type: int) -> Any:
        """Convert string value to appropriate type."""
        if data_type == DataType.BOOL:
            return value == "1"
        elif data_type in (DataType.INT8, DataType.INT16, DataType.INT32):
            return int(value)
        elif data_type in (DataType.UINT8, DataType.UINT16, DataType.UINT32):
            return int(value)
        return value

    def _temperature_from_api(self, value: int, param_num: int) -> float:
        """Convert API temperature value to Celsius."""
        if self._is_new_version:
            # New version uses °C*10 for most temperature parameters
            return value / 10.0
        else:
            # Old version uses °C directly
            return float(value)

    def _temperature_to_api(self, value: float, param_num: int) -> str:
        """Convert Celsius to API temperature value."""
        if self._is_new_version:
            return str(int(value * 10))
        else:
            return str(int(value))

    # Properties

    @property
    def available(self) -> bool:
        """Return if device is available."""
        return self._available

    @property
    def has_state(self) -> bool:
        """Return if at least one full update populated cached state."""
        return self._has_state

    @property
    def last_update_error(self) -> str | None:
        """Return the last update error."""
        return self._last_update_error

    @property
    def last_successful_update(self) -> datetime | None:
        """Return the UTC time of the last complete, validated refresh."""
        return self._last_successful_update

    @property
    def cached_state_age(self) -> float | None:
        """Return full-refresh age, unaffected by wall-clock changes or commands."""
        if self._last_successful_update_monotonic is None:
            return None
        return max(0.0, time.monotonic() - self._last_successful_update_monotonic)

    @property
    def consecutive_update_failures(self) -> int:
        """Return consecutive failed polling cycles, not failed commands."""
        return self._consecutive_update_failures

    @property
    def settings_available(self) -> bool:
        """Whether cached settings have no subsequent parameter-read failure."""
        return self._settings_available

    @property
    def fast_poll_supported(self) -> bool:
        """Use status-only polling only with complete operating-state telemetry."""
        required = ("t.2",) if self._is_new_version else ()
        return all(
            key in self._status
            for key in (
                *required,
                "t.1",
                "t.5",
                "m.0",
                "m.1",
                "m.5",
                "f.0",
                "f.16",
            )
        )

    @property
    def last_refresh_error_category(self) -> str | None:
        """Return the last full-refresh error category until polling recovers."""
        return self._last_refresh_error_category

    @property
    def last_request_duration(self) -> float | None:
        """Return HTTP exchange duration in seconds, excluding rate-limit sleep."""
        return self._last_request_duration

    @property
    def connection_diagnostics(self) -> dict[str, Any]:
        """Return bounded connection metrics without raw device responses."""
        return {
            "available": self.available,
            "has_state": self.has_state,
            "last_successful_update": self.last_successful_update,
            "cached_state_age_seconds": self.cached_state_age,
            "settings": {
                "available": self.settings_available,
                "last_successful_update": self._last_successful_settings_update,
                "age_seconds": (
                    max(
                        0.0, time.monotonic() - self._last_successful_settings_monotonic
                    )
                    if self._last_successful_settings_monotonic is not None
                    else None
                ),
                "error": self._last_settings_error,
                "error_category": self._last_settings_error_category,
            },
            "status": {
                "last_successful_update": self._last_successful_status_update,
                "age_seconds": (
                    max(0.0, time.monotonic() - self._last_successful_status_monotonic)
                    if self._last_successful_status_monotonic is not None
                    else None
                ),
                "fast_poll_supported": self.fast_poll_supported,
            },
            "consecutive_update_failures": self.consecutive_update_failures,
            "max_update_failures": self._max_update_failures,
            "last_refresh_error": self._last_refresh_error,
            "last_refresh_error_category": self.last_refresh_error_category,
            "request_count": self._request_count,
            "request_error_counts": self._request_error_counts.copy(),
            "last_request": {
                "started_at": self._last_request_at,
                "kind": self._last_request_kind,
                "duration_seconds": self.last_request_duration,
                "http_status": self._last_request_http_status,
                "error": self._last_request_error,
                "error_category": self._last_request_error_category,
            },
            "protocol": {
                "profile": DEVICE_TYPE_NEW if self._is_new_version else DEVICE_TYPE_OLD,
                "parameter_count": len(self._parameters),
                "status_field_count": len(self._status),
            },
        }

    @property
    def is_new_version(self) -> bool:
        """Return if device is new version with air sensor."""
        return self._is_new_version

    @property
    def power_on(self) -> bool | None:
        """Return if device is powered on."""
        return self._power_on

    @property
    def floor_temperature(self) -> float | None:
        """Current floor temperature in Celsius."""
        return self._floor_temperature

    @property
    def air_temperature(self) -> float | None:
        """Current air temperature in Celsius (new version only)."""
        return self._air_temperature

    @property
    def setpoint(self) -> float | None:
        """Current temperature setpoint in Celsius."""
        return self._setpoint

    @property
    def mode(self) -> int | None:
        """Current operation mode."""
        return self._mode

    @property
    def relay_state(self) -> bool | None:
        """Current relay state (heating active)."""
        return self._relay_state

    @property
    def control_type(self) -> int | None:
        """Current control type (floor/air/air with floor limit)."""
        if "m.0" in self._status:
            return int(self._status["m.0"])
        return self._get_param_value(ParamNum.CONTROL_TYPE)

    @property
    def hysteresis(self) -> float | None:
        """Current hysteresis value in Celsius."""
        value = self._get_param_value(ParamNum.HYSTERESIS)
        if value is not None:
            return value / 10.0
        return None

    @property
    def children_lock(self) -> bool | None:
        """Return if children lock is enabled."""
        return self._get_param_value(ParamNum.CHILDREN_LOCK)

    @property
    def cooling_mode(self) -> bool | None:
        """Return if cooling mode is enabled (vs heating)."""
        if "m.5" in self._status:
            return int(self._status["m.5"]) == 1
        return self._get_param_value(ParamNum.COOLING_CONTROL_WAY)

    @property
    def upper_limit(self) -> int | None:
        """Maximum floor temperature setpoint."""
        return self._get_param_value(ParamNum.UPPER_LIMIT)

    @property
    def lower_limit(self) -> int | None:
        """Minimum floor temperature setpoint."""
        return self._get_param_value(ParamNum.LOWER_LIMIT)

    @property
    def upper_air_limit(self) -> int | None:
        """Maximum air temperature setpoint (new version only)."""
        if self._is_new_version:
            return self._get_param_value(ParamNum.UPPER_AIR_LIMIT)
        return None

    @property
    def lower_air_limit(self) -> int | None:
        """Minimum air temperature setpoint (new version only)."""
        if self._is_new_version:
            return self._get_param_value(ParamNum.LOWER_AIR_LIMIT)
        return None

    @property
    def brightness(self) -> int | None:
        """Display brightness (0-9)."""
        return self._get_param_value(ParamNum.BRIGHTNESS)

    @property
    def use_night_brightness(self) -> bool | None:
        """Return if night brightness mode is enabled."""
        return self._get_param_value(ParamNum.USE_NIGHT_BRIGHT)

    @property
    def pre_control(self) -> bool | None:
        """Return if pre-heating is enabled."""
        return self._get_param_value(ParamNum.PRE_CONTROL)

    @property
    def window_open_control(self) -> bool | None:
        """Return if window open detection is enabled (new version only)."""
        if self._is_new_version:
            return self._get_param_value(ParamNum.WINDOW_OPEN_CONTROL)
        return None

    @property
    def lan_block(self) -> bool | None:
        """Return if LAN API changes are blocked."""
        return self._get_param_value(ParamNum.LAN_BLOCK)

    @property
    def cloud_block(self) -> bool | None:
        """Return if cloud changes are blocked."""
        return self._get_param_value(ParamNum.CLOUD_BLOCK)

    @property
    def power_watts(self) -> int | None:
        """Connected power in Watts."""
        value = self._get_param_value(ParamNum.POWER)
        if value is not None:
            if value <= 150:
                return value * 10
            else:
                return value * 20 - 1500
        return None

    @property
    def floor_correction(self) -> float | None:
        """Floor sensor correction in Celsius."""
        value = self._get_param_value(ParamNum.FLOOR_CORRECTION)
        if value is not None:
            return value / 10.0
        return None

    @property
    def air_correction(self) -> float | None:
        """Air sensor correction in Celsius (new version only)."""
        if self._is_new_version:
            value = self._get_param_value(ParamNum.AIR_CORRECTION)
            if value is not None:
                return value / 10.0
        return None

    @property
    def sensor_type(self) -> int | None:
        """Temperature sensor type (resistance)."""
        return self._get_param_value(ParamNum.SENSOR_TYPE)

    @property
    def prop_koef(self) -> int | None:
        """Proportional mode coefficient (minutes of load in 30-min cycle)."""
        return self._get_param_value(ParamNum.PROP_KOEF)

    @property
    def nc_contact_control(self) -> bool | None:
        """Return if relay is inverted (NC mode)."""
        return self._get_param_value(ParamNum.NC_CONTACT_CONTROL)

    @property
    def night_bright_start(self) -> int | None:
        """Night brightness start time (minutes from 00:00)."""
        return self._get_param_value(ParamNum.NIGHT_BRIGHT_START)

    @property
    def night_bright_end(self) -> int | None:
        """Night brightness end time (minutes from 00:00)."""
        return self._get_param_value(ParamNum.NIGHT_BRIGHT_END)

    @property
    def relay_on_time_limit(self) -> int | None:
        """Continuous heating time limit for alarm (hours, read-only)."""
        return self._get_param_value(ParamNum.RELAY_ON_TIME_LIMIT)

    @property
    def button_minus_cor(self) -> int | None:
        """Minus button sensitivity correction (-30 to 30)."""
        return self._get_param_value(ParamNum.BUTTON_MINUS_COR)

    @property
    def button_menu_cor(self) -> int | None:
        """Menu button sensitivity correction (-30 to 30)."""
        return self._get_param_value(ParamNum.BUTTON_MENU_COR)

    @property
    def button_plus_cor(self) -> int | None:
        """Plus button sensitivity correction (-30 to 30)."""
        return self._get_param_value(ParamNum.BUTTON_PLUS_COR)

    @property
    def off_button_lock(self) -> bool | None:
        """Return if automatic button lock is disabled (read-only)."""
        return self._get_param_value(ParamNum.OFF_BUTTON_LOCK)

    @property
    def min_temp_advanced(self) -> int | None:
        """Min floor temp limit in air control mode (new version only)."""
        if self._is_new_version:
            return self._get_param_value(ParamNum.MIN_TEMP_ADVANCED)
        return None

    @property
    def max_temp_advanced(self) -> int | None:
        """Max floor temp limit in air control mode (new version only)."""
        if self._is_new_version:
            return self._get_param_value(ParamNum.MAX_TEMP_ADVANCED)
        return None

    @property
    def ble_sensor_interval(self) -> int | None:
        """Wireless air sensor poll interval in minutes (new version only)."""
        if self._is_new_version:
            return self._get_param_value(ParamNum.BLE_SENSOR_INTERVAL)
        return None

    @property
    def ble_sensor_bind(self) -> bool | None:
        """Return if wireless air sensor is connected (new version, read-only)."""
        if self._is_new_version:
            return self._get_param_value(ParamNum.BLE_SENSOR_BIND)
        return None

    @property
    def upper_warning_temp(self) -> int | None:
        """Upper temperature threshold for alarm (new version only)."""
        if self._is_new_version:
            return self._get_param_value(ParamNum.UPPER_WARNING_TEMP)
        return None

    @property
    def lower_warning_temp(self) -> int | None:
        """Lower temperature threshold for alarm (new version only)."""
        if self._is_new_version:
            return self._get_param_value(ParamNum.LOWER_WARNING_TEMP)
        return None

    @property
    def away_floor_temperature(self) -> float | None:
        """Away mode floor temperature setpoint."""
        value = self._get_param_value(ParamNum.AWAY_FLOOR)
        if value is not None:
            return self._temperature_from_api(value, ParamNum.AWAY_FLOOR)
        return None

    @property
    def away_air_temperature(self) -> float | None:
        """Away mode air temperature setpoint (new version only)."""
        if self._is_new_version:
            value = self._get_param_value(ParamNum.AWAY_AIR)
            if value is not None:
                return self._temperature_from_api(value, ParamNum.AWAY_AIR)
        return None

    @property
    def manual_floor_temperature(self) -> float | None:
        """Manual mode floor temperature setpoint."""
        value = self._get_param_value(ParamNum.MANUAL_FLOOR)
        if value is not None:
            return self._temperature_from_api(value, ParamNum.MANUAL_FLOOR)
        return None

    @property
    def manual_air_temperature(self) -> float | None:
        """Manual mode air temperature setpoint (new version only)."""
        if self._is_new_version:
            value = self._get_param_value(ParamNum.MANUAL_AIR)
            if value is not None:
                return self._temperature_from_api(value, ParamNum.MANUAL_AIR)
        return None

    # Setters

    def _operation_mode_to_api(self, mode: int) -> str:
        """Legacy Terneo manual parameter is 1; its telemetry reports 3."""
        if mode == OperationMode.MANUAL and not self._is_new_version:
            return "1"
        return str(int(mode))

    async def set_hvac_mode(self, hvac_mode: str) -> bool:
        """Send a complete HVAC transition in one validated parameter write."""
        if hvac_mode == "off":
            params = [[ParamNum.POWER_OFF, DataType.BOOL, "1"]]
        elif hvac_mode == "auto":
            params = [
                [ParamNum.POWER_OFF, DataType.BOOL, "0"],
                [ParamNum.MODE, DataType.UINT8, str(OperationMode.SCHEDULE)],
            ]
        elif hvac_mode == "heat":
            params = [
                [ParamNum.POWER_OFF, DataType.BOOL, "0"],
                [
                    ParamNum.COOLING_CONTROL_WAY,
                    DataType.BOOL,
                    "0",
                ],
                [
                    ParamNum.MODE,
                    DataType.UINT8,
                    self._operation_mode_to_api(OperationMode.MANUAL),
                ],
            ]
        else:
            raise ValueError("Unsupported HVAC mode")
        # Only a subsequent full poll publishes the resulting operating state.
        return bool(await self.set_parameters(params))

    async def set_setpoint(self, temperature: float) -> bool:
        """Set target temperature."""
        control_type = self.control_type or ControlType.FLOOR

        if control_type == ControlType.FLOOR:
            param = ParamNum.MANUAL_FLOOR
        else:
            param = (
                ParamNum.MANUAL_AIR if self._is_new_version else ParamNum.MANUAL_FLOOR
            )

        temp_value = self._temperature_to_api(temperature, param)

        # Turn on, set manual mode, and set temperature
        result = await self.set_parameters(
            [
                [ParamNum.POWER_OFF, DataType.BOOL, "0"],
                [
                    ParamNum.MODE,
                    DataType.UINT8,
                    self._operation_mode_to_api(OperationMode.MANUAL),
                ],
                [
                    param,
                    DataType.INT8 if not self._is_new_version else DataType.INT16,
                    temp_value,
                ],
            ]
        )

        if result:
            self._setpoint = self._temperature_from_api(int(temp_value), param)
        return bool(result)

    async def set_mode(self, mode: int) -> bool:
        """Set a telemetry-mode enum using the generation's parameter encoding."""
        if mode not in [OperationMode.SCHEDULE, OperationMode.MANUAL]:
            raise ValueError("Mode must be 0 (schedule) or 3 (manual)")

        result = await self.set_parameters(
            [
                [ParamNum.POWER_OFF, DataType.BOOL, "0"],
                [ParamNum.MODE, DataType.UINT8, self._operation_mode_to_api(mode)],
            ]
        )
        return bool(result)

    async def turn_on(self) -> bool:
        """Turn on the thermostat."""
        result = await self.set_parameters([[ParamNum.POWER_OFF, DataType.BOOL, "0"]])
        if result:
            self._power_on = True
        return bool(result)

    async def turn_off(self) -> bool:
        """Turn off the thermostat."""
        result = await self.set_parameters([[ParamNum.POWER_OFF, DataType.BOOL, "1"]])
        if result:
            self._power_on = False
        return bool(result)

    async def set_children_lock(self, enabled: bool) -> bool:
        """Set children lock."""
        result = await self.set_parameters(
            [[ParamNum.CHILDREN_LOCK, DataType.BOOL, "1" if enabled else "0"]]
        )
        return bool(result)

    async def set_cooling_mode(self, enabled: bool) -> bool:
        """Allow restoring heating, but never enable cooling for a floor heater."""
        if enabled:
            raise ValueError("Cooling is not supported for floor heating")
        result = await self.set_parameters(
            [[ParamNum.COOLING_CONTROL_WAY, DataType.BOOL, "0"]]
        )
        return bool(result)

    async def set_control_type(self, control_type: int) -> bool:
        """Set control type (0=floor, 1=air, 2=air with floor limit)."""
        if control_type not in [0, 1, 2]:
            raise ValueError("Control type must be 0, 1, or 2")

        result = await self.set_parameters(
            [[ParamNum.CONTROL_TYPE, DataType.UINT8, str(control_type)]]
        )
        return bool(result)

    async def set_hysteresis(self, value: float) -> bool:
        """Set hysteresis in Celsius."""
        api_value = int(value * 10)
        result = await self.set_parameters(
            [[ParamNum.HYSTERESIS, DataType.UINT8, str(api_value)]]
        )
        return bool(result)

    async def set_brightness(self, value: int) -> bool:
        """Set display brightness (0-9)."""
        if not 0 <= value <= 9:
            raise ValueError("Brightness must be between 0 and 9")

        result = await self.set_parameters(
            [[ParamNum.BRIGHTNESS, DataType.UINT8, str(value)]]
        )
        return bool(result)

    async def set_pre_control(self, enabled: bool) -> bool:
        """Set pre-heating mode."""
        result = await self.set_parameters(
            [[ParamNum.PRE_CONTROL, DataType.BOOL, "1" if enabled else "0"]]
        )
        return bool(result)

    async def set_window_open_control(self, enabled: bool) -> bool:
        """Set window open detection (new version only)."""
        if not self._is_new_version:
            _LOGGER.warning("Window open control is only available on new version")
            return False

        result = await self.set_parameters(
            [[ParamNum.WINDOW_OPEN_CONTROL, DataType.BOOL, "1" if enabled else "0"]]
        )
        return bool(result)

    async def set_use_night_brightness(self, enabled: bool) -> bool:
        """Set night brightness mode."""
        result = await self.set_parameters(
            [[ParamNum.USE_NIGHT_BRIGHT, DataType.BOOL, "1" if enabled else "0"]]
        )
        return bool(result)

    async def set_floor_limits(self, lower: int, upper: int) -> bool:
        """Set floor temperature limits."""
        result = await self.set_parameters(
            [
                [ParamNum.LOWER_LIMIT, DataType.INT8, str(lower)],
                [ParamNum.UPPER_LIMIT, DataType.INT8, str(upper)],
            ]
        )
        return bool(result)

    async def set_air_limits(self, lower: int, upper: int) -> bool:
        """Set air temperature limits (new version only)."""
        if not self._is_new_version:
            _LOGGER.warning("Air limits are only available on new version")
            return False

        result = await self.set_parameters(
            [
                [ParamNum.LOWER_AIR_LIMIT, DataType.INT8, str(lower)],
                [ParamNum.UPPER_AIR_LIMIT, DataType.INT8, str(upper)],
            ]
        )
        return bool(result)

    async def set_sensor_type(self, sensor_type: int) -> bool:
        """Set temperature sensor type (0-6)."""
        if not 0 <= sensor_type <= 6:
            raise ValueError("Sensor type must be between 0 and 6")

        result = await self.set_parameters(
            [[ParamNum.SENSOR_TYPE, DataType.UINT8, str(sensor_type)]]
        )
        return bool(result)

    async def set_prop_koef(self, value: int) -> bool:
        """Set proportional mode coefficient (minutes in 30-min cycle)."""
        if not 0 <= value <= 30:
            raise ValueError("Proportional coefficient must be between 0 and 30")

        result = await self.set_parameters(
            [[ParamNum.PROP_KOEF, DataType.UINT8, str(value)]]
        )
        return bool(result)

    async def set_nc_contact_control(self, enabled: bool) -> bool:
        """Set relay inversion (NC mode)."""
        result = await self.set_parameters(
            [[ParamNum.NC_CONTACT_CONTROL, DataType.BOOL, "1" if enabled else "0"]]
        )
        return bool(result)

    async def set_night_brightness_time(
        self, start_minutes: int, end_minutes: int
    ) -> bool:
        """Set night brightness time range (minutes from 00:00)."""
        if not 0 <= start_minutes <= 1439 or not 0 <= end_minutes <= 1439:
            raise ValueError("Time must be between 0 and 1439 minutes")

        result = await self.set_parameters(
            [
                [ParamNum.NIGHT_BRIGHT_START, DataType.UINT16, str(start_minutes)],
                [ParamNum.NIGHT_BRIGHT_END, DataType.UINT16, str(end_minutes)],
            ]
        )
        return bool(result)

    async def set_button_corrections(self, minus: int, menu: int, plus: int) -> bool:
        """Set button sensitivity corrections (-30 to 30)."""
        for val in [minus, menu, plus]:
            if not -30 <= val <= 30:
                raise ValueError("Button correction must be between -30 and 30")

        result = await self.set_parameters(
            [
                [ParamNum.BUTTON_MINUS_COR, DataType.INT8, str(minus)],
                [ParamNum.BUTTON_MENU_COR, DataType.INT8, str(menu)],
                [ParamNum.BUTTON_PLUS_COR, DataType.INT8, str(plus)],
            ]
        )
        return bool(result)

    async def set_floor_correction(self, value: float) -> bool:
        """Set floor sensor correction in Celsius."""
        api_value = int(value * 10)
        if not -127 <= api_value <= 127:
            raise ValueError("Floor correction must be between -12.7 and 12.7")

        result = await self.set_parameters(
            [[ParamNum.FLOOR_CORRECTION, DataType.INT8, str(api_value)]]
        )
        return bool(result)

    async def set_air_correction(self, value: float) -> bool:
        """Set air sensor correction in Celsius (new version only)."""
        if not self._is_new_version:
            _LOGGER.warning("Air correction is only available on new version")
            return False

        api_value = int(value * 10)
        if not -127 <= api_value <= 127:
            raise ValueError("Air correction must be between -12.7 and 12.7")

        result = await self.set_parameters(
            [[ParamNum.AIR_CORRECTION, DataType.INT8, str(api_value)]]
        )
        return bool(result)

    async def set_lan_block(self, enabled: bool) -> bool:
        """Set LAN API block."""
        result = await self.set_parameters(
            [[ParamNum.LAN_BLOCK, DataType.BOOL, "1" if enabled else "0"]]
        )
        return bool(result)

    async def set_cloud_block(self, enabled: bool) -> bool:
        """Set cloud block."""
        result = await self.set_parameters(
            [[ParamNum.CLOUD_BLOCK, DataType.BOOL, "1" if enabled else "0"]]
        )
        return bool(result)

    async def set_advanced_floor_limits(self, min_temp: int, max_temp: int) -> bool:
        """Set floor temp limits for air control mode (new version only)."""
        if not self._is_new_version:
            _LOGGER.warning("Advanced floor limits are only available on new version")
            return False

        result = await self.set_parameters(
            [
                [ParamNum.MIN_TEMP_ADVANCED, DataType.INT8, str(min_temp)],
                [ParamNum.MAX_TEMP_ADVANCED, DataType.INT8, str(max_temp)],
            ]
        )
        return bool(result)

    async def set_ble_sensor_interval(self, minutes: int) -> bool:
        """Set wireless sensor poll interval in minutes (new version only)."""
        if not self._is_new_version:
            _LOGGER.warning("BLE sensor interval is only available on new version")
            return False

        if not 1 <= minutes <= 60:
            raise ValueError("BLE sensor interval must be between 1 and 60 minutes")

        result = await self.set_parameters(
            [[ParamNum.BLE_SENSOR_INTERVAL, DataType.UINT8, str(minutes)]]
        )
        return bool(result)

    async def set_warning_temps(self, lower: int, upper: int) -> bool:
        """Set temperature warning thresholds (new version only)."""
        if not self._is_new_version:
            _LOGGER.warning("Warning temps are only available on new version")
            return False

        result = await self.set_parameters(
            [
                [ParamNum.LOWER_WARNING_TEMP, DataType.INT8, str(lower)],
                [ParamNum.UPPER_WARNING_TEMP, DataType.INT8, str(upper)],
            ]
        )
        return bool(result)

    async def set_away_temperature(
        self, floor_temp: float, air_temp: float | None = None
    ) -> bool:
        """Set away mode temperatures."""
        params = [
            [
                ParamNum.AWAY_FLOOR,
                DataType.INT8 if not self._is_new_version else DataType.INT16,
                self._temperature_to_api(floor_temp, ParamNum.AWAY_FLOOR),
            ]
        ]

        if self._is_new_version and air_temp is not None:
            params.append(
                [
                    ParamNum.AWAY_AIR,
                    DataType.INT16,
                    self._temperature_to_api(air_temp, ParamNum.AWAY_AIR),
                ]
            )

        result = await self.set_parameters(params)
        return bool(result)

    async def set_power(self, watts: int) -> bool:
        """Set connected power in Watts."""
        # Convert watts to API value
        if watts <= 1500:
            api_value = watts // 10
        else:
            api_value = (watts + 1500) // 20

        result = await self.set_parameters(
            [[ParamNum.POWER, DataType.UINT16, str(api_value)]]
        )
        return bool(result)

    async def update(self, status_on_settings_failure: bool = False) -> bool:
        """Update all state from device."""
        # Publish parameters and status from the same successful polling cycle.
        previous_parameters = self._parameters
        params_result = await self.get_parameters()
        if not params_result:
            self._settings_available = False
            self._last_settings_error = self._last_update_error
            self._last_settings_error_category = self._last_request_error_category
            if status_on_settings_failure and self.has_state:
                self._last_refresh_error = self._last_update_error
                self._last_refresh_error_category = self._last_request_error_category
                self.break_heating_interval()
                # One polling failure at most; healthy telemetry keeps its own grace.
                return await self.update_status()
            self._mark_update_failed()
            return False

        # Get status
        pending_parameters = self._parameters
        self._parameters = previous_parameters
        try:
            status_result = await self.get_status()
        except asyncio.CancelledError:
            self._parameters = previous_parameters
            self.break_heating_interval()
            raise
        if not status_result:
            self._parameters = previous_parameters
            self._mark_update_failed()
            return False

        # Parse status
        self._parameters = pending_parameters
        self._settings_available = True
        self._last_settings_error = None
        self._last_settings_error_category = None
        self._parse_status(status_result)

        self._mark_update_successful()
        return True

    async def update_status(self) -> bool:
        """Refresh operating state using confirmed settings, never bootstrap with it."""
        if not self._has_state:
            return await self.update()
        status_result = await self.get_status()
        if not status_result:
            self._mark_update_failed()
            return False
        self._parse_status(status_result)
        self._mark_status_successful()
        return True

    def _parse_status(self, data: dict) -> None:
        """Parse status response."""
        self._power_on = (
            int(data["f.16"]) == 0
            if "f.16" in data
            else not self._get_param_value(ParamNum.POWER_OFF)
        )
        # Floor temperature (t.1 = raw * 16)
        if "t.1" in data:
            self._floor_temperature = float(data["t.1"]) / 16.0

        # Air temperature (t.2 for new version)
        if self._is_new_version and "t.2" in data:
            self._air_temperature = float(data["t.2"]) / 16.0

        # Setpoint (t.5 = raw * 16)
        if "t.5" in data:
            self._setpoint = float(data["t.5"]) / 16.0

        # Mode
        if "m.1" in data:
            mode_value = int(data["m.1"])
            # Check power state
            if not self._power_on:
                self._mode = -1  # Off
            else:
                self._mode = mode_value

        # Relay state and energy tracking
        if "f.0" in data:
            new_relay_state = int(data["f.0"]) == 1
            self._update_energy_tracking(new_relay_state)
            self._relay_state = new_relay_state
        else:
            self.break_heating_interval()

    def break_heating_interval(self) -> None:
        """Do not integrate time with unknown relay state, including grace periods."""
        self._last_relay_update = None
        self._last_relay_power_watts = None

    def _update_energy_tracking(self, new_relay_state: bool) -> None:
        """Estimate the preceding confirmed relay interval using its sampled wattage."""
        current_time = time.monotonic()

        # If we have a previous measurement and relay was ON, calculate energy
        if self._last_relay_update is not None and self._relay_state is True:
            elapsed_seconds = current_time - self._last_relay_update

            if 0 < elapsed_seconds <= self._max_heating_interval:
                self._heating_time_seconds += elapsed_seconds
                power_watts = self._last_relay_power_watts
                if power_watts and power_watts > 0:
                    energy_kwh = (power_watts * elapsed_seconds) / 3600000.0
                    self._heating_energy_kwh += energy_kwh

        self._last_relay_update = current_time
        self._last_relay_power_watts = (
            self.power_watts
            if new_relay_state and self._last_settings_error is None
            else None
        )

    @property
    def energy_counters(self) -> dict[str, float]:
        """Return unrounded totals; never persist an interval anchor or relay state."""
        return {
            "heating_energy_kwh": self._heating_energy_kwh,
            "heating_time_seconds": self._heating_time_seconds,
        }

    def restore_energy_counters(self, data: Any) -> None:
        """Validate stored totals atomically and begin a new observation interval."""
        keys = ("heating_energy_kwh", "heating_time_seconds")
        if not isinstance(data, dict) or any(key not in data for key in keys):
            raise ValueError("Invalid stored heating counters")
        values = []
        for key in keys:
            value = data[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError("Invalid stored heating counters")
            try:
                number = float(value)
            except OverflowError as err:
                raise ValueError("Invalid stored heating counters") from err
            if not math.isfinite(number) or number < 0:
                raise ValueError("Invalid stored heating counters")
            values.append(number)
        self._heating_energy_kwh, self._heating_time_seconds = values
        self.break_heating_interval()

    @property
    def heating_energy_kwh(self) -> float:
        """Persisted estimated energy consumed by heating in kWh."""
        return round(self._heating_energy_kwh, 3)

    @property
    def heating_time_hours(self) -> float:
        """Persisted observed heating time in hours, independent of configured power."""
        return round(self._heating_time_seconds / 3600.0, 2)

    def reset_energy_counter(self) -> None:
        """Reset energy and time counters."""
        self._heating_energy_kwh = 0.0
        self._heating_time_seconds = 0.0
        self.break_heating_interval()


# Backward compatibility alias
Thermostat = TerneoThermostat

"""Privacy-safe, read-only diagnostics for Terneo config entries."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
import re
from typing import Any
from urllib.parse import quote, quote_plus

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant

from .coordinator import TerneoConfigEntry

_SENSITIVE_KEYS = {
    "host", "hostname", "ip", "ip_address", "serial", "serial_number", "sn",
    "unique_id", "entry_id", "device_id", "mac", "mac_address", "name", "title",
    "url", "base_url", "username", "password", "token", "access_token",
    "refresh_token", "api_key", "authorization", "ssid", "bssid",
    "auth", "key", "address", "device_name", "friendly_name", "location",
    "latitude", "longitude",
}
_URL = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s\"'<>]+", re.IGNORECASE)


def _redact_diagnostics(data: Any, identifiers: list[str]) -> Any:
    """Use HA key redaction, also removing identifying values embedded in text."""
    keys = set(_SENSITIVE_KEYS)
    values = {value for value in identifiers if value}

    def collect(value: Any, sensitive: bool = False) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                normalized = str(key).lower().replace("-", "_")
                private = normalized in _SENSITIVE_KEYS or any(
                    part in normalized
                    for part in ("password", "token", "secret", "credential")
                )
                if private:
                    keys.add(key)
                collect(item, sensitive or private)
        elif isinstance(value, (list, tuple)):
            for item in value:
                collect(item, sensitive)
        elif sensitive and isinstance(value, (str, int)) and not isinstance(value, bool):
            if str(value):
                values.add(str(value))

    collect(data)
    # Encoded credentials/identifiers may occur in URLs or exception messages.
    replacements = sorted(
        {
            encoded for value in values
            for encoded in (value, quote(value, safe=""), quote_plus(value))
        },
        key=len, reverse=True,
    )

    def scrub(value: Any) -> Any:
        if isinstance(value, Mapping):
            return {
                scrub(key): scrub(item)
                for key, item in async_redact_data(value, keys).items()
            }
        if isinstance(value, (list, tuple)):
            return [scrub(item) for item in value]
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, str):
            if value == REDACTED:
                return value
            value = _URL.sub(REDACTED, value)
            for identifier in replacements:
                value = re.sub(
                    re.escape(identifier), REDACTED, value, flags=re.IGNORECASE
                )
            return value
        if isinstance(value, int) and str(value) in values:
            return REDACTED
        return value

    return scrub(data)


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TerneoConfigEntry
) -> dict[str, Any]:
    """Export a stable cached snapshot; never make a request or include raw payloads."""
    identifiers = [entry.entry_id, entry.title, entry.unique_id or ""]
    coordinator = getattr(entry, "runtime_data", None)
    runtime = None
    if coordinator is not None:
        identifiers.append(coordinator.thermostat.sn)
        runtime = await coordinator.async_get_diagnostics()
    return _redact_diagnostics({
        "entry": {
            "title": entry.title,
            "unique_id": entry.unique_id,
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "runtime": runtime,
    }, identifiers)

"""Initialize HA's validation runtime before importing voluptuous in tests."""

from unittest.mock import AsyncMock

import aiohttp
import homeassistant  # noqa: F401
import pytest
from yarl import URL

from tests.http import FakeSession


@pytest.fixture(autouse=True)
def block_device_network(monkeypatch, request):
    """Tests must mock HTTP; never contact a thermostat by accident."""

    def blocked_request(*args, **kwargs):
        raise AssertionError("Unexpected HTTP request; mock the device transport")

    monkeypatch.setattr(
        "custom_components.terneo.async_get_clientsession", lambda hass: FakeSession()
    )
    monkeypatch.setattr(
        "custom_components.terneo.config_flow.async_get_clientsession",
        lambda hass: FakeSession(),
    )
    if request.node.get_closest_marker("local_http") is None:
        monkeypatch.setattr(aiohttp.ClientSession, "_request", blocked_request)
        monkeypatch.setattr("custom_components.terneo.thermostat.sleep", AsyncMock())
    else:
        original = aiohttp.ClientSession._request

        async def loopback_only(session, method, url, **kwargs):
            assert URL(url).host == "127.0.0.1", (
                "Only the local HTTP test server is allowed"
            )
            return await original(session, method, url, **kwargs)

        monkeypatch.setattr(aiohttp.ClientSession, "_request", loopback_only)

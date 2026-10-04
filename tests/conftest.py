"""Initialize HA's validation runtime before importing voluptuous in tests."""

import homeassistant  # noqa: F401
import pytest
import requests


@pytest.fixture(autouse=True)
def block_device_network(monkeypatch):
    """Tests must mock HTTP; never contact a thermostat by accident."""

    def blocked_request(*args, **kwargs):
        raise AssertionError("Unexpected HTTP request; mock the device transport")

    monkeypatch.setattr(requests.Session, "request", blocked_request)

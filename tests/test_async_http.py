"""Exercise the HA-managed transport against a loopback-only HTTP server."""

import asyncio
import json
import time
import unittest
from tempfile import TemporaryDirectory
from types import MappingProxyType
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import ThreadedResolver, web
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from custom_components.terneo.config_flow import validate_connection
from custom_components.terneo.const import DOMAIN
from custom_components.terneo.coordinator import TerneoCoordinator
from custom_components.terneo.thermostat import TerneoThermostat

SERIAL = "local-test-serial"
PARAMS = {"sn": SERIAL, "par": [[125, 7, "0"], [23, 2, "2"]]}
STATUS = {"sn": SERIAL, "t.1": "368", "t.5": "400", "f.0": "1"}


@pytest.mark.local_http
class AsyncHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = TemporaryDirectory()
        self.hass = HomeAssistant(self.temp.name)
        self.hass.config_entries = MagicMock()
        # Do not initialize mDNS adapters for a loopback-only transport test.
        with patch(
            "homeassistant.helpers.aiohttp_client._async_make_resolver",
            return_value=ThreadedResolver(),
        ):
            self.session = async_get_clientsession(self.hass)
        self.calls = []
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.coordinators = []
        self.handler = self.confirm

        async def route(request):
            payload = await request.json()
            self.calls.append(
                (payload, request.headers, request.transport, time.monotonic())
            )
            self.started.set()
            return await self.handler(request, payload)

        app = web.Application()
        app.router.add_post("/{endpoint}", route)
        self.runner = web.AppRunner(app, shutdown_timeout=0.1)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.host = f"127.0.0.1:{port}"
        self.client = self.make_client()

    async def asyncTearDown(self):
        for coordinator in self.coordinators:
            await coordinator.async_shutdown()
        self.release.set()
        await self.runner.cleanup()
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.assertTrue(self.session.closed)
        self.temp.cleanup()

    def make_client(self, *, timeout=0.15):
        client = TerneoThermostat(
            SERIAL, self.host, timeout=timeout, session=self.session
        )
        client._last_request -= 1
        return client

    def make_coordinator(self):
        entry = ConfigEntry(
            version=1,
            minor_version=1,
            domain=DOMAIN,
            title="Test",
            data={"serial": SERIAL, "host": self.host},
            options={},
            source="user",
            unique_id=SERIAL,
            discovery_keys=MappingProxyType({}),
            subentries_data=[],
        )
        coordinator = TerneoCoordinator(self.hass, entry, self.client)
        self.coordinators.append(coordinator)
        return coordinator

    async def confirm(self, request, payload):
        result = (
            {"sn": SERIAL, "par": payload["par"]}
            if "par" in payload
            else STATUS
            if payload.get("cmd") == 4
            else PARAMS
        )
        # Legacy firmware can return JSON with a non-JSON content type.
        return web.Response(text=json.dumps(result), content_type="text/plain")

    async def wait_for_request(self):
        await asyncio.wait_for(self.started.wait(), 2)

    async def test_connection_close_and_one_second_spacing_are_preserved(self):
        self.assertTrue(await self.client.get_parameters())
        self.assertTrue(await self.client.get_parameters())
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(call[1]["Connection"] == "close" for call in self.calls))
        self.assertIsNot(self.calls[0][2], self.calls[1][2])
        self.assertGreaterEqual(self.calls[1][3] - self.calls[0][3], 0.99)
        self.assertEqual(self.client._parameters, {125: (7, "0"), 23: (2, "2")})

    async def test_rate_limit_wait_is_nonblocking_and_cancellable_before_io(self):
        self.client._last_request = time.monotonic()
        task = asyncio.create_task(self.client.get_parameters())
        await asyncio.sleep(0.05)
        self.assertFalse(task.done())
        self.assertEqual(self.calls, [])
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(self.client._request_count, 0)
        self.assertFalse(self.session.closed)

    async def test_total_timeout_covers_response_headers_and_body(self):
        for body in (False, True):
            with self.subTest(body=body):
                self.started.clear()
                self.client = self.make_client(timeout=0.1)

                async def stalled(request, payload):
                    if body:
                        response = web.StreamResponse(headers={"Content-Length": "100"})
                        await response.prepare(request)
                        await response.write(b"{")
                    await self.release.wait()
                    return web.Response(text="{}")

                self.handler = stalled
                began = time.monotonic()
                self.assertFalse(await self.client.turn_off())
                self.assertLess(time.monotonic() - began, 0.8)
                self.assertEqual(
                    self.client.connection_diagnostics["last_request"][
                        "error_category"
                    ],
                    "timeout",
                )
                self.assertIn("outcome is unknown", self.client.last_update_error)
                self.assertEqual(self.client._parameters, {})
        self.assertEqual(len(self.calls), 2)

    async def test_truncated_json_does_not_publish_state_or_retry(self):
        async def truncated(request, payload):
            return web.Response(text='{"sn":')

        self.handler = truncated
        self.assertFalse(await self.client.turn_off())
        self.assertEqual(
            self.client.connection_diagnostics["last_request"]["error_category"], "json"
        )
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.client._parameters, {})

    async def test_incomplete_http_payload_is_transport_failure_without_retry(self):
        async def incomplete(request, payload):
            response = web.StreamResponse(headers={"Content-Length": "100"})
            await response.prepare(request)
            await response.write(b'{"sn":')
            request.transport.close()
            return response

        self.handler = incomplete
        self.assertFalse(await self.client.turn_off())
        self.assertEqual(
            self.client.connection_diagnostics["last_request"]["error_category"],
            "transport",
        )
        self.assertEqual(len(self.calls), 1)
        self.assertIsNone(self.client.power_on)

    async def test_disconnect_after_write_is_not_retried(self):
        async def disconnect(request, payload):
            request.transport.close()
            return web.Response(text="{}")

        self.handler = disconnect
        self.assertFalse(await self.client.turn_off())
        self.assertIn("outcome is unknown", self.client.last_update_error)
        self.assertEqual(len(self.calls), 1)

    async def test_redirect_does_not_resend_device_command(self):
        async def redirect(request, payload):
            return web.Response(status=307, headers={"Location": "/another.cgi"})

        self.handler = redirect
        self.assertFalse(await self.client.turn_off())
        self.assertEqual(
            self.client.connection_diagnostics["last_request"]["http_status"], 307
        )
        self.assertEqual(
            self.client.connection_diagnostics["last_request"]["error_category"], "http"
        )
        self.assertEqual(len(self.calls), 1)

    async def test_caller_cancel_waits_for_acknowledgement_before_unlocking(self):
        self.client._timeout = 2
        coordinator = self.make_coordinator()

        async def delayed(request, payload):
            await self.release.wait()
            return await self.confirm(request, payload)

        self.handler = delayed
        task = asyncio.create_task(
            coordinator.async_execute_command(self.client.turn_off)
        )
        try:
            await self.wait_for_request()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertTrue(coordinator._request_lock.locked())
        finally:
            self.release.set()
            result = await asyncio.wait_for(
                asyncio.gather(task, return_exceptions=True), 2
            )
        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertFalse(coordinator._request_lock.locked())
        self.assertEqual(coordinator._requests, set())
        self.assertFalse(self.client.power_on)
        self.assertEqual(len(self.calls), 1)

    async def test_unload_cancels_http_rejects_queue_and_preserves_shared_session(self):
        self.client._timeout = 60
        coordinator = self.make_coordinator()

        async def stalled(request, payload):
            await self.release.wait()
            return await self.confirm(request, payload)

        self.handler = stalled
        active = asyncio.create_task(
            coordinator.async_execute_command(self.client.turn_off)
        )
        await self.wait_for_request()
        queued = asyncio.create_task(
            coordinator.async_execute_command(self.client.turn_on)
        )
        await asyncio.sleep(0)
        await asyncio.wait_for(coordinator.async_shutdown(), 1)
        results = await asyncio.gather(active, queued, return_exceptions=True)
        self.assertIsInstance(results[0], asyncio.CancelledError)
        self.assertIsInstance(results[1], HomeAssistantError)
        self.assertEqual(coordinator._requests, set())
        self.assertFalse(coordinator._request_lock.locked())
        self.assertIn("outcome is unknown", self.client.last_update_error)
        self.assertIsNone(self.client.power_on)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(self.session.closed)
        with self.assertRaises(HomeAssistantError):
            await coordinator.async_execute_command(self.client.turn_on)
        self.handler = self.confirm
        self.assertTrue(await self.make_client().get_parameters())

    async def test_cancelled_full_refresh_rolls_back_partial_parameters(self):
        self.client._timeout = 60
        previous = {23: (2, "8")}
        self.client._parameters = previous
        self.client._last_relay_update = time.monotonic()
        status_started = asyncio.Event()

        async def stalled_status(request, payload):
            if payload.get("cmd") == 4:
                status_started.set()
                await self.release.wait()
            return await self.confirm(request, payload)

        self.handler = stalled_status
        task = asyncio.create_task(self.client.update())
        try:
            await asyncio.wait_for(status_started.wait(), 2)
            self.assertIs(self.client._parameters, previous)
            self.assertEqual(self.client.brightness, 8)
        finally:
            task.cancel()
            result = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertIs(self.client._parameters, previous)
        self.assertIsNone(self.client._last_relay_update)
        self.assertFalse(self.client.has_state)

    async def test_cancelled_legacy_readback_marks_command_outcome_unknown(self):
        self.client._timeout = 60
        verify_started = asyncio.Event()

        async def legacy(request, payload):
            if "par" in payload:
                return web.json_response({"success": "true"})
            verify_started.set()
            await self.release.wait()
            return web.json_response(PARAMS)

        self.handler = legacy
        task = asyncio.create_task(self.client.turn_off())
        try:
            await asyncio.wait_for(verify_started.wait(), 2)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertIn("outcome is unknown", self.client.last_update_error)
        self.assertIsNone(self.client.power_on)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(
            self.client.connection_diagnostics["request_error_counts"]["transport"], 1
        )

    async def test_config_probe_uses_shared_session_and_cancels_cleanly(self):
        async def stalled(request, payload):
            await self.release.wait()
            return web.json_response(PARAMS)

        self.handler = stalled
        with patch(
            "custom_components.terneo.config_flow.async_get_clientsession",
            return_value=self.session,
        ):
            task = asyncio.create_task(
                validate_connection(
                    self.hass, {"host": self.host, "serial": SERIAL}, timeout=60
                )
            )
            try:
                await self.wait_for_request()
            finally:
                task.cancel()
                result = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertFalse(self.session.closed)
        self.handler = self.confirm
        self.assertTrue(await self.make_client().get_parameters())

    async def test_unload_during_rate_limiting_sends_no_command(self):
        coordinator = self.make_coordinator()
        self.client._last_request = time.monotonic()
        task = asyncio.create_task(
            coordinator.async_execute_command(self.client.turn_off)
        )
        await asyncio.sleep(0.05)
        self.assertTrue(coordinator._request_lock.locked())
        await asyncio.wait_for(coordinator.async_shutdown(), 1)
        result = await asyncio.gather(task, return_exceptions=True)
        self.assertIsInstance(result[0], asyncio.CancelledError)
        self.assertFalse(coordinator._request_lock.locked())
        self.assertEqual(coordinator._requests, set())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.client._request_count, 0)
        self.assertFalse(self.session.closed)

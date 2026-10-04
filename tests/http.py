"""In-memory asynchronous HTTP transport for protocol regression tests."""

import inspect
import json

from aiohttp import ClientResponseError


def http_error(message):
    return ClientResponseError(None, (), message=message)


class Response:
    """Small response DTO; content is parsed by the same JSON decoder."""

    status_code = 200
    _content = b"{}"

    @property
    def status(self):
        return self.status_code

    async def json(self, *, content_type=None):
        return json.loads(self._content)


def post(*args, **kwargs):
    raise AssertionError("Unexpected HTTP request; mock the device transport")


class Exchange:
    def __init__(self, url, kwargs):
        self.url = url
        self.kwargs = kwargs

    async def __aenter__(self):
        result = post(self.url, **self.kwargs)
        return await result if inspect.isawaitable(result) else result

    async def __aexit__(self, *args):
        return False


class FakeSession:
    def post(self, url, **kwargs):
        return Exchange(url, kwargs)

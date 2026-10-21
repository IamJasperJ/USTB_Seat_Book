from __future__ import annotations

import unittest

import httpx

from app import main


class LibraryFailureIsBadGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def test_upstream_http_error_becomes_502_with_status(self) -> None:
        request = httpx.Request("GET", "https://libzw.example/ic-web/device/tips")
        error = httpx.HTTPStatusError("404", request=request, response=httpx.Response(404, request=request))
        response = await main.library_request_failed(None, error)
        self.assertEqual(response.status_code, 502)
        self.assertIn("HTTP 404", response.body.decode())

    async def test_timeout_becomes_502(self) -> None:
        response = await main.library_request_failed(None, httpx.ReadTimeout("slow"))
        self.assertEqual(response.status_code, 502)
        self.assertIn("暂时无法访问", response.body.decode())

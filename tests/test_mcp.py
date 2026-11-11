"""The /mcp endpoint: HTTP Basic only, account-scoped tools that reuse the HTTP routes."""
from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app import main
from app.mcp_server import mcp
from tests import test_temp_leave as support


class McpTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.TempLeaveTests.asyncSetUp

    def client(self, auth=("alice", "correct-horse")):
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=main.app), base_url="http://app", auth=auth)
        return Client(streamable_http_client("http://app/mcp", http_client=http))

    # The session manager can run only once per process, so this is one scenario.
    async def test_auth_and_tools(self):
        main._verified_credentials.clear()
        async with mcp.session_manager.run():
            raw = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://app")
            listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            accept = {"accept": "application/json, text/event-stream"}
            # No credentials, a web-console cookie, or a wrong password are all refused.
            missing = await raw.post("/mcp", json=listing, headers=accept)
            self.assertEqual(missing.status_code, 401)
            self.assertIn("Basic", missing.headers["www-authenticate"])
            raw.cookies.set("session_token", main.session_tokens.issue(self.account.id))
            with_cookie = await raw.post("/mcp", json=listing, headers=accept)
            self.assertEqual(with_cookie.status_code, 401)
            raw.cookies.clear()
            wrong = await raw.post("/mcp", json=listing, headers=accept, auth=("alice", "nope"))
            self.assertEqual(wrong.status_code, 401)
            await raw.aclose()

            row = self.state.seed({"status": 1 | 4})  # today, effective, not signed in
            with patch.object(main.account_store, "verify", wraps=main.account_store.verify) as verify:
                async with self.client() as client:
                    tools = {t.name: t for t in (await client.list_tools()).tools}
                    self.assertIn("my_reservations", tools)
                    self.assertTrue(tools["reserve_seat"].annotations.destructive_hint)
                    self.assertTrue(tools["get_config"].annotations.read_only_hint)
                    self.assertFalse(any(name.startswith("admin") for name in tools))

                    mine = await client.call_tool("my_reservations", {"day": "today", "auth_session_id": "own"})
                    self.assertFalse(mine.is_error, mine.content)
                    self.assertEqual([r["uuid"] for r in mine.structured_content["records"]], [row["uuid"]])

                    # Route-level rules still apply: no confirm, no temporary leave.
                    refused = await client.call_tool("temp_leave", {"uuid": row["uuid"], "auth_session_id": "own"})
                    self.assertTrue(refused.is_error)
                    self.assertIn("请明确确认暂离", refused.content[0].text)
                    self.assertFalse(row["resvStatus"] & 2048)

                    # Another account's session is not usable.
                    foreign = await client.call_tool("my_reservations", {"day": "today", "auth_session_id": "foreign"})
                    self.assertTrue(foreign.is_error)
            # Many requests, one password check: verified credentials are remembered.
            self.assertEqual(verify.call_count, 1)
            main.forget_verified_credentials(self.account.id)
            self.assertEqual(main._verified_credentials, {})


if __name__ == "__main__":
    unittest.main()

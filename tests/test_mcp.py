"""The /mcp endpoint: credentials as tool arguments or HTTP Basic, account-scoped tools."""
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

    def client(self, auth=None, headers=None):
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=main.app), base_url="http://app",
                                  auth=auth, headers=headers)
        return Client(streamable_http_client("http://app/mcp", http_client=http))

    # The session manager can run only once per process, so this is one scenario.
    async def test_auth_and_tools(self):
        main._verified_credentials.clear()
        async with mcp.session_manager.run():
            raw = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://app")
            listing = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
            accept = {"accept": "application/json, text/event-stream"}
            # A wrong Basic header is refused before MCP; listing tools needs no credentials.
            wrong = await raw.post("/mcp", json=listing, headers=accept, auth=("alice", "nope"))
            self.assertEqual(wrong.status_code, 401)
            self.assertIn("Basic", wrong.headers["www-authenticate"])
            self.assertEqual((await raw.post("/mcp", json=listing, headers=accept)).status_code, 200)
            await raw.aclose()

            row = self.state.seed({"status": 1 | 4})  # today, effective, not signed in
            creds = {"username": "alice", "password": "correct-horse"}
            # A gateway's Bearer token occupies Authorization; credentials go in the arguments.
            gateway = {"authorization": "Bearer gateway-token"}
            with patch.object(main.account_store, "verify", wraps=main.account_store.verify) as verify:
                async with self.client(headers=gateway) as client:
                    tools = {t.name: t for t in (await client.list_tools()).tools}
                    self.assertIn("my_reservations", tools)
                    self.assertTrue(all({"username", "password"} <= set(t.input_schema["properties"])
                                        for t in tools.values()))
                    self.assertTrue(tools["reserve_seat"].annotations.destructive_hint)
                    self.assertTrue(tools["get_config"].annotations.read_only_hint)
                    self.assertFalse(any(name.startswith("admin") for name in tools))

                    mine = await client.call_tool("my_reservations",
                                                  {"day": "today", "auth_session_id": "own", **creds})
                    self.assertFalse(mine.is_error, mine.content)
                    self.assertEqual([r["uuid"] for r in mine.structured_content["records"]], [row["uuid"]])

                    # Route-level rules still apply: no confirm, no temporary leave.
                    refused = await client.call_tool("temp_leave", {"uuid": row["uuid"], "auth_session_id": "own", **creds})
                    self.assertTrue(refused.is_error)
                    self.assertIn("请明确确认暂离", refused.content[0].text)
                    self.assertFalse(row["resvStatus"] & 2048)

                    # Another account's session is not usable.
                    foreign = await client.call_tool("my_reservations",
                                                     {"day": "today", "auth_session_id": "foreign", **creds})
                    self.assertTrue(foreign.is_error)

                    # No credentials at all (the Bearer header is not ours): a clear error.
                    anonymous = await client.call_tool("get_config", {})
                    self.assertTrue(anonymous.is_error)
                    self.assertIn("username 和 password", anonymous.content[0].text)
                    wrong_arg = await client.call_tool("get_config", {**creds, "password": "nope"})
                    self.assertTrue(wrong_arg.is_error)
                    self.assertIn("用户名或密码不正确", wrong_arg.content[0].text)

                # A client that sends HTTP Basic itself can omit the arguments.
                async with self.client(auth=("alice", "correct-horse")) as client:
                    config = await client.call_tool("get_config", {})
                    self.assertFalse(config.is_error, config.content)

                # A web-console cookie never authenticates.
                plain = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://app")
                plain.cookies.set("session_token", main.session_tokens.issue(self.account.id))
                call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                        "params": {"name": "get_config", "arguments": {}}}
                result = (await plain.post("/mcp", json=call, headers=accept)).json()
                self.assertTrue(result["result"]["isError"])
                await plain.aclose()
            # Many requests, few password checks: verified credentials are remembered
            # (one for the right password, one for the wrong one).
            self.assertEqual(verify.call_count, 2)
            main.forget_verified_credentials(self.account.id)
            self.assertEqual(main._verified_credentials, {})


if __name__ == "__main__":
    unittest.main()

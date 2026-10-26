from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import main
from app.accounts import Account
from app.auth import LoginSession
from app.config import Settings


def make_session(session_id: str, owner: str, created_at: float, status: str = "authenticated") -> LoginSession:
    session = LoginSession(session_id, AsyncMock(), AsyncMock(), created_at=created_at, status=status)
    session.owner_account_id = owner
    return session


class SessionListApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.alice = Account(id="alice-acct", username="alice", is_admin=False)
        self.runtime = main.UserRuntime(self.alice, Settings(), main.auth)
        self.addCleanup(lambda: Path(self.runtime.automation.history.path).unlink(missing_ok=True))
        self.older = make_session("alice-old", self.alice.id, 100.0)
        self.older.user_info = {"userName": "Alice", "accNo": "1001", "token": "never-shown"}
        self.newer = make_session("alice-new", self.alice.id, 200.0, status="waiting_scan")
        self.foreign = make_session("bob-session", "bob-acct", 300.0)
        sessions = {s.session_id: s for s in (self.older, self.newer, self.foreign)}
        self.calls: list[str] = []

        async def close_session(session_id: str) -> None:
            self.calls.append(f"close:{session_id}")
            sessions.pop(session_id, None)

        self.fake_auth = SimpleNamespace(
            sessions=sessions,
            get=AsyncMock(side_effect=lambda session_id: sessions[session_id]),
            close_session=AsyncMock(side_effect=close_session),
        )
        self.patches = [patch.object(main, "auth", self.fake_auth), patch.dict(main.runtimes, {self.alice.id: self.runtime})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    async def bind_running_task(self, session_id: str) -> None:
        self.runtime.automation.session_id = session_id
        self.runtime.automation.task = asyncio.create_task(asyncio.sleep(3600))
        self.addAsyncCleanup(self.runtime.automation.stop)

    async def test_list_shows_only_own_sessions_newest_first_with_own_capacity(self) -> None:
        await self.bind_running_task("alice-old")
        result = await main.list_auth_sessions(account=self.alice)
        self.assertEqual([s["session_id"] for s in result["sessions"]], ["alice-new", "alice-old"])
        old = result["sessions"][1]
        self.assertEqual((old["user_name"], old["acc_no"], old["used_by_automation"]), ("Alice", "1001", True))
        self.assertFalse(result["sessions"][0]["used_by_automation"])
        self.assertNotIn("never-shown", str(result))
        # The allowance is per account: Bob's session doesn't count against Alice,
        # and only a yes/no about the server-wide ceiling leaks out.
        self.assertEqual(result["capacity"], {
            "in_use": 2, "max": main.global_settings.max_sessions_per_account,
            "server_full": main.global_settings.max_browser_sessions is not None and 3 >= main.global_settings.max_browser_sessions,
        })

    async def test_deleting_bound_session_closes_it_before_stopping_the_task(self) -> None:
        await self.bind_running_task("alice-old")
        stop = AsyncMock(side_effect=lambda: self.calls.append("stop"))
        with patch.object(self.runtime.automation, "stop", stop):
            result = await main.delete_auth_session("alice-old", account=self.alice)
        self.assertEqual(result, {"status": "closed", "automation_stopped": True})
        self.assertEqual(self.calls, ["close:alice-old", "stop"])

    async def test_deleting_unbound_session_leaves_the_task_running(self) -> None:
        await self.bind_running_task("alice-old")
        result = await main.delete_auth_session("alice-new", account=self.alice)
        self.assertFalse(result["automation_stopped"])
        self.assertTrue(self.runtime.automation.running)

    async def test_cannot_delete_another_accounts_session(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            await main.delete_auth_session("bob-session", account=self.alice)
        self.assertEqual(caught.exception.status_code, 404)
        self.fake_auth.close_session.assert_not_awaited()
        self.assertIn("bob-session", self.fake_auth.sessions)


if __name__ == "__main__":
    unittest.main()

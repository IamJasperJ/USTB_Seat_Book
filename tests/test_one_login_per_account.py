from __future__ import annotations

import asyncio
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app import main
from app.accounts import Account
from app.auth import AuthManager, LoginSession, SessionCapacityError
from app.config import Settings


class OneLoginPerAccountTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = AuthManager(Settings(keepalive_enabled=False, max_browser_sessions=5, max_sessions_per_account=2))
        self.manager._create_session_locked = AsyncMock(return_value="created")
        self.superseded: list[tuple[str, str]] = []
        self.manager.on_session_superseded = lambda old_id, new: self.superseded.append((old_id, new.session_id))

    def add(self, session_id: str, owner: str, status: str = "waiting_scan", logged_in: bool = False) -> LoginSession:
        session = LoginSession(session_id, AsyncMock(), AsyncMock(), status=status)
        session.owner_account_id = owner
        session.sso_authenticated = logged_in
        self.manager.sessions[session_id] = session
        return session

    async def settle(self) -> None:
        await asyncio.gather(*self.manager._supersede_tasks)

    async def test_each_account_is_limited_on_its_own(self) -> None:
        self.add("a1", "alice")
        self.add("a2", "alice")
        with self.assertRaisesRegex(SessionCapacityError, "每个账号最多保留 2 个"):
            await self.manager.create_session("alice")
        # Alice being full doesn't block Bob.
        self.assertEqual(await self.manager.create_session("bob"), "created")

    async def test_server_wide_ceiling_still_protects_memory(self) -> None:
        self.manager.settings.max_browser_sessions = 3
        self.add("a1", "alice")
        self.add("b1", "bob")
        self.add("b2", "bob")
        with self.assertRaisesRegex(SessionCapacityError, "服务器浏览器资源已满"):
            await self.manager.create_session("carol")

    async def test_new_login_replaces_the_accounts_old_login_only(self) -> None:
        old = self.add("alice-old", "alice", status="authenticated", logged_in=True)
        new = self.add("alice-new", "alice")
        bob = self.add("bob-login", "bob", status="authenticated", logged_in=True)
        self.manager._mark_sso_authenticated(new)
        await self.settle()
        self.assertEqual(set(self.manager.sessions), {"alice-new", "bob-login"})
        old.context.close.assert_awaited_once()
        bob.context.close.assert_not_awaited()
        self.assertEqual(self.superseded, [("alice-old", "alice-new")])

    async def test_pending_qr_of_same_account_is_not_closed(self) -> None:
        # The rule is about logged-in sessions; an unscanned QR is just a slot.
        pending = self.add("alice-pending", "alice")
        winner = self.add("alice-login", "alice")
        self.manager._mark_sso_authenticated(winner)
        await self.settle()
        self.assertIn("alice-pending", self.manager.sessions)
        pending.context.close.assert_not_awaited()

    async def test_keepalive_refresh_of_an_existing_login_closes_nothing(self) -> None:
        first = self.add("alice-1", "alice", status="sso_authenticated", logged_in=True)
        second = self.add("alice-2", "alice", status="sso_authenticated", logged_in=True)
        self.manager._mark_sso_authenticated(first)  # already logged in: a refresh, not a new login
        await self.settle()
        self.assertEqual(set(self.manager.sessions), {"alice-1", "alice-2"})
        second.context.close.assert_not_awaited()

    async def test_library_first_login_also_replaces_old_login(self) -> None:
        self.add("alice-old", "alice", status="authenticated", logged_in=True)
        new = self.add("alice-new", "alice")
        self.manager._mark_library_authenticated(new, {"token": "t"})
        await self.settle()
        self.assertEqual(set(self.manager.sessions), {"alice-new"})


class UnscannedSessionExpiryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = AuthManager(Settings(keepalive_enabled=False))
        self.manager.refresh_status = AsyncMock()

    def add(self, session_id: str, status: str, age: float, logged_in: bool = False) -> LoginSession:
        session = LoginSession(session_id, AsyncMock(), AsyncMock(), status=status, ttl_seconds=self.manager.settings.auth_session_ttl_seconds)
        session.owner_account_id = "alice"
        session.sso_authenticated = logged_in
        session.created_at -= age
        self.manager.sessions[session_id] = session
        return session

    def test_default_lifetime_of_an_unscanned_session_is_five_minutes(self) -> None:
        self.assertEqual(self.manager.settings.auth_session_ttl_seconds, 300)

    async def test_extra_session_is_dropped_after_five_minutes_but_the_login_stays(self) -> None:
        login = self.add("login", "authenticated", age=3600, logged_in=True)
        extra = self.add("extra", "waiting_scan", age=301)
        await self.manager.recycle_sessions()
        self.assertEqual(set(self.manager.sessions), {"login"})
        extra.context.close.assert_awaited_once()
        login.context.close.assert_not_awaited()

    async def test_young_pending_session_is_kept(self) -> None:
        self.add("login", "authenticated", age=3600, logged_in=True)
        self.add("extra", "waiting_scan", age=240)
        await self.manager.recycle_sessions()
        self.assertEqual(set(self.manager.sessions), {"login", "extra"})

    async def test_with_no_login_both_sessions_are_dropped(self) -> None:
        self.add("one", "waiting_scan", age=400)
        self.add("two", "starting", age=400)
        await self.manager.recycle_sessions()
        self.assertEqual(self.manager.sessions, {})


class RebindSupersededTaskTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.alice = Account(id="alice-acct", username="alice", is_admin=False)
        self.bob = Account(id="bob-acct", username="bob", is_admin=False)
        self.alice_rt = main.UserRuntime(self.alice, Settings(), main.auth)
        self.bob_rt = main.UserRuntime(self.bob, Settings(), main.auth)
        for rt in (self.alice_rt, self.bob_rt):
            self.addCleanup(lambda rt=rt: Path(rt.automation.history.path).unlink(missing_ok=True))
        patcher = patch.dict(main.runtimes, {self.alice.id: self.alice_rt, self.bob.id: self.bob_rt})
        patcher.start()
        self.addCleanup(patcher.stop)

    async def run_task(self, runtime, session_id: str) -> None:
        runtime.automation.session_id = session_id
        runtime.automation.task = asyncio.create_task(asyncio.sleep(3600))
        self.addAsyncCleanup(runtime.automation.stop)

    async def test_running_task_follows_the_new_login(self) -> None:
        await self.run_task(self.alice_rt, "alice-old")
        await self.run_task(self.bob_rt, "bob-session")
        main.rebind_superseded_session("alice-old", Mock(session_id="alice-new", owner_account_id=self.alice.id))
        self.assertEqual(self.alice_rt.automation.session_id, "alice-new")
        self.assertTrue(self.alice_rt.automation.running)
        self.assertEqual(self.bob_rt.automation.session_id, "bob-session")

    async def test_task_bound_elsewhere_is_left_alone(self) -> None:
        await self.run_task(self.alice_rt, "alice-other")
        main.rebind_superseded_session("alice-old", Mock(session_id="alice-new", owner_account_id=self.alice.id))
        self.assertEqual(self.alice_rt.automation.session_id, "alice-other")


if __name__ == "__main__":
    unittest.main()

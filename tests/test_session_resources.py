import asyncio
import time
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app.auth import AuthManager, LoginSession, SessionCapacityError
from app.config import Settings
from app.runtime import MemoryMonitor, memory_snapshot


class SessionResourcesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = AuthManager(Settings(keepalive_enabled=False))

    def session(self, name, status="waiting_scan", old=True):
        page = AsyncMock()
        page.url = "https://sso.ustb.edu.cn/cas/login"
        page.evaluate.return_value = None
        session = LoginSession(name, AsyncMock(), page, status=status,
                               created_at=time.time() - (3600 if old else 0))
        self.manager.sessions[name] = session
        return session

    async def test_old_qr_closed_and_expired_status_remains_pollable(self):
        session = self.session("qr")
        await self.manager.recycle_sessions()
        session.context.close.assert_awaited_once()
        self.assertFalse(self.manager.sessions)
        self.assertEqual((await self.manager.get("qr")).status, "expired")

    async def test_live_sso_and_library_and_renewal_are_never_recycled(self):
        for status in ("authenticated", "sso_authenticated", "library_scan_required"):
            session = self.session(status, status)
            session.sso_authenticated = True
        await self.manager.recycle_sessions()
        self.assertEqual(len(self.manager.sessions), 3)
        for session in self.manager.sessions.values():
            session.context.close.assert_not_awaited()

    async def test_protected_automation_session_not_recycled(self):
        session = self.session("task", "expired")
        self.manager.protected_session_ids = lambda: {"task"}
        await self.manager.recycle_sessions()
        session.context.close.assert_not_awaited()

    async def test_completed_scan_without_ui_poll_is_preserved(self):
        session = self.session("scanned")
        session.page.url = "https://jwgl.ustb.edu.cn/"
        self.manager._try_library_locked = AsyncMock()
        await self.manager.recycle_sessions()
        self.assertTrue(session.sso_authenticated)
        session.context.close.assert_not_awaited()

    async def test_failure_and_expiration_recycled_but_fresh_qr_kept(self):
        error = self.session("error", "error")
        expired = self.session("expired", "expired")
        fresh = self.session("fresh", old=False)
        await self.manager.recycle_sessions()
        self.assertEqual(list(self.manager.sessions), ["fresh"])
        error.context.close.assert_awaited_once()
        expired.context.close.assert_awaited_once()
        fresh.context.close.assert_not_awaited()

    async def test_concurrent_creation_respects_cap(self):
        self.manager.settings.max_browser_sessions = 1
        async def launch(account_id, login_source):
            await asyncio.sleep(0.01)
            return self.session("new", old=False)
        self.manager._create_session_locked = launch
        results = await asyncio.gather(self.manager.create_session("acct"), self.manager.create_session("acct"), return_exceptions=True)
        self.assertEqual(sum(isinstance(result, SessionCapacityError) for result in results), 1)
        self.assertEqual(len(self.manager.sessions), 1)

    async def test_failed_close_keeps_capacity_occupied(self):
        session = self.session("bad-close", "error")
        session.context.close.side_effect = RuntimeError("close failed")
        self.manager.settings.max_browser_sessions = 1
        with self.assertRaises(SessionCapacityError):
            await self.manager.create_session("acct")
        self.assertIn("bad-close", self.manager.sessions)

    async def test_retired_metadata_is_bounded(self):
        for index in range(40):
            self.session(str(index), "expired")
        await self.manager.recycle_sessions()
        self.assertEqual(len(self.manager._retired), 32)

    async def test_memory_log_contains_counts_not_credentials(self):
        session = self.session("secret-session")
        session.user_info = {"token": "secret-token"}
        monitor = MemoryMonitor(self.manager.settings, self.manager)
        with patch("app.runtime.memory_snapshot", return_value={"cgroup_usage_mib": 123}), self.assertLogs("ustb-seat-book.memory") as logs:
            await monitor.sample()
        self.assertEqual(monitor.latest["browser_sessions"], 1)
        self.assertNotIn("secret-session", "".join(logs.output))
        self.assertNotIn("secret-token", "".join(logs.output))

    async def test_config_bounds(self):
        for fields in ({"max_browser_sessions": 0}, {"memory_log_interval_seconds": 0}):
            with self.assertRaises(ValueError):
                Settings(**fields)

    async def test_missing_memory_counters_are_null(self):
        with patch("app.runtime.Path.read_text", side_effect=OSError), patch("app.runtime.Path.is_dir", return_value=False):
            sample = memory_snapshot()
        self.assertIsNone(sample["cgroup_usage_mib"])
        self.assertIsNone(sample["host_available_mib"])

    async def test_capacity_api_returns_409(self):
        from app import main
        from app.accounts import Account
        from fastapi import HTTPException
        account = Account(id="test-capacity-acct", username="tester", is_admin=False)
        runtime = main.UserRuntime(account, Settings(), main.auth)
        self.addCleanup(lambda: Path(runtime.automation.history.path).unlink(missing_ok=True))
        with patch.dict(main.runtimes, {account.id: runtime}), \
                patch.object(main.auth, "create_session", AsyncMock(side_effect=SessionCapacityError("full"))):
            with self.assertRaises(HTTPException) as error:
                await main.create_auth_session(account=account)
        self.assertEqual(error.exception.status_code, 409)

    @unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "opt-in local browser test")
    async def test_real_browser_recycled_and_capacity_reusable(self):
        from playwright.async_api import async_playwright
        self.manager.settings.max_browser_sessions = 1
        self.manager._login_url = AsyncMock(return_value="data:text/html,<html>Waiting for scan</html>")
        async with async_playwright() as playwright:
            self.manager._playwright = playwright
            self.manager._browser = await playwright.chromium.launch(headless=True)
            try:
                first = await self.manager.create_session("acct", "library")
                with self.assertRaises(SessionCapacityError):
                    await self.manager.create_session("acct", "library")
                first.created_at = time.time() - 3600
                await self.manager.recycle_sessions()
                self.assertTrue(first.page.is_closed())
                self.assertEqual(len(self.manager.sessions), 0)
                second = await self.manager.create_session("acct", "library")
                self.assertFalse(second.page.is_closed())
                self.assertEqual(len(self.manager.sessions), 1)
            finally:
                await self.manager.close()

import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.auth import LoginSession
from app.health import BrowserHealthMonitor


class HealthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.browser = Mock()
        self.browser.is_connected.return_value = True
        self.auth = SimpleNamespace(_browser=self.browser, sessions={})
        self.monitor = BrowserHealthMonitor(self.auth, timeout=0.03)

    def session(self, sid="own", owner="A"):
        page = Mock()
        page.is_closed.return_value = False
        page.evaluate = AsyncMock(return_value=1)
        session = LoginSession(sid, AsyncMock(), page, status="authenticated",
                               owner_account_id=owner, sso_authenticated=True,
                               user_info={"token": "never-output"})
        self.auth.sessions[sid] = session
        return session

    async def test_actual_page_probe_and_privacy(self):
        session = self.session()
        await self.monitor.sample()
        session.page.evaluate.assert_awaited_once_with("() => 1")
        self.assertTrue(self.monitor.summary()["healthy"])
        self.assertNotIn("never-output", str(self.monitor.details("A")))

    async def test_crash_event_immediately_overrides_cached_success(self):
        session = self.session()
        await self.monitor.sample()
        session.page.on.call_args.args[1]()
        self.assertFalse(self.monitor.summary()["healthy"])
        self.assertEqual(self.monitor.details("A")["sessions"][0]["reason"], "page_crashed")

    async def test_probe_error_timeout_closed_and_disconnect(self):
        session = self.session()
        session.page.evaluate.side_effect = RuntimeError("secret-url-token")
        await self.monitor.sample()
        self.assertFalse(self.monitor.summary()["healthy"])
        self.assertNotIn("secret-url-token", str(self.monitor.details("A")))
        async def slow(_): await asyncio.sleep(1)
        session.page.evaluate.side_effect = slow
        await self.monitor.sample()
        self.assertFalse(self.monitor.summary()["healthy"])
        session.page.is_closed.return_value = True
        self.assertEqual(self.monitor.details("A")["sessions"][0]["reason"], "page_closed")
        self.browser.is_connected.return_value = False
        self.assertFalse(self.monitor.summary()["healthy"])

    async def test_busy_is_not_interrupted_but_cannot_stay_green_forever(self):
        session = self.session()
        async with session.page_lock:
            await self.monitor.sample()
            self.assertEqual(self.monitor.summary()["status"], "degraded")
            session.page.evaluate.assert_not_awaited()
            self.monitor._last_success["own"] -= 181
            await self.monitor.sample()
            self.assertFalse(self.monitor.summary()["healthy"])

    async def test_stale_monitor_and_cross_account_isolation(self):
        self.session(); self.session("foreign", "B")
        await self.monitor.sample()
        self.assertEqual(len(self.monitor.details("A")["sessions"]), 1)
        self.assertEqual(len(self.monitor.details("A", admin=True)["sessions"]), 2)
        self.monitor.checked_monotonic -= 200
        self.assertFalse(self.monitor.summary()["healthy"])

    async def test_idle_probe_closes_only_its_own_context(self):
        context = AsyncMock()
        context.new_page.return_value.evaluate.return_value = 1
        self.browser.new_context = AsyncMock(return_value=context)
        await self.monitor.sample()
        context.close.assert_awaited_once()
        self.assertTrue(self.monitor.summary()["healthy"])

    async def test_idle_close_failure_does_not_accumulate_contexts(self):
        context = AsyncMock()
        context.new_page.return_value.evaluate.return_value = 1
        context.close.side_effect = RuntimeError("close failed")
        self.browser.new_context = AsyncMock(return_value=context)
        await self.monitor.sample(); await self.monitor.sample()
        self.browser.new_context.assert_awaited_once()
        self.assertFalse(self.monitor.summary()["healthy"])

    async def test_api_status_codes_and_authentication(self):
        from fastapi import Response, HTTPException
        from app import main
        self.session()
        await self.monitor.sample()
        with patch.object(main, "browser_health", self.monitor):
            response = Response()
            self.assertTrue((await main.healthz(response))["healthy"])
            self.assertEqual(response.status_code, 200)
            self.browser.is_connected.return_value = False
            await main.healthz(response)
            self.assertEqual(response.status_code, 503)
            with self.assertRaises(HTTPException) as exc:
                await main.current_account(None)
            self.assertEqual(exc.exception.status_code, 401)


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "opt-in local browser test")
class RealBrowserHealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_renderer_crash_is_unhealthy_without_closing_login(self):
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                page = await context.new_page()
                auth = SimpleNamespace(_browser=browser, sessions={})
                session = LoginSession("test", context, page, status="authenticated", owner_account_id="A")
                auth.sessions["test"] = session
                monitor = BrowserHealthMonitor(auth)
                await monitor.sample()
                self.assertTrue(monitor.summary()["healthy"])
                try:
                    await page.goto("chrome://crash", timeout=3000)
                except Exception:
                    pass
                await monitor.sample()
                self.assertFalse(monitor.summary()["healthy"])
                self.assertTrue(browser.is_connected())
                self.assertIn(context, browser.contexts)
            finally:
                await browser.close()

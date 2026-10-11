"""Opt-in real headless browser regression: RUN_BROWSER_TESTS=1 python -m unittest ..."""
from __future__ import annotations

import os
import unittest
from unittest.mock import AsyncMock

from playwright.async_api import async_playwright

from app.auth import AuthManager, LoginSession
from app.config import Settings


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "opt-in local browser test")
class LibraryRenewalBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_expired_spa_flag_and_delayed_sso_callback(self) -> None:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                # No requests reach USTB: reproduce the actual SPA login flag
                # gate and a callback taking longer than the old 1s wait.
                await context.route("https://libzw.ustb.edu.cn/**", lambda route: route.fulfill(
                    content_type="text/html",
                    body="""<html><script>
                        if (sessionStorage.getItem('isLogin') !== 'true') {
                            setTimeout(() => {
                                sessionStorage.setItem('userInfo', JSON.stringify({token:'test-new-token'}));
                                sessionStorage.setItem('isLogin', 'true');
                            }, 1800);
                        }
                    </script></html>""",
                ))
                await context.route("https://sso.ustb.edu.cn/**", lambda route: route.fulfill(
                    content_type="text/html",
                    body="<script>location.href='https://libzw.ustb.edu.cn/'</script>",
                ))
                page = await context.new_page()
                await page.goto("https://libzw.ustb.edu.cn/")
                await page.wait_for_function("() => !!sessionStorage.getItem('userInfo')")
                await page.evaluate("sessionStorage.setItem('userInfo', JSON.stringify({token:'expired'}))")
                manager = AuthManager(Settings(keepalive_enabled=False))
                manager._login_url = AsyncMock(return_value="https://sso.ustb.edu.cn/cas/login")
                session = LoginSession("browser-regression", context, page)
                session.status = "authenticated"
                session.sso_authenticated = True
                session.user_info = {"token": "expired"}
                await manager.invalidate_library_session(session)
                self.assertIsNone(await page.evaluate("sessionStorage.getItem('isLogin')"))
                self.assertTrue(await manager.ensure_library_session(session, force=True))
                self.assertEqual(session.user_info["token"], "test-new-token")
                self.assertEqual(await page.evaluate("sessionStorage.getItem('isLogin')"), "true")

                # Repair even when invalidation happens on another origin.
                await page.evaluate("sessionStorage.removeItem('userInfo')")
                await page.goto("https://sso.ustb.edu.cn/pending", wait_until="commit")
                session.status = "sso_authenticated"
                session.user_info = None
                self.assertTrue(await manager.ensure_library_session(session, force=True))
                self.assertEqual(session.user_info["token"], "test-new-token")
            finally:
                await browser.close()

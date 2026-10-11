from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, Mock

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.auth import AuthManager, LoginSession, LIBRARY_LOGIN_RESULT, LIBRARY_RESET_STATE
from app.config import Settings


class LibraryRenewalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.manager = AuthManager(Settings(keepalive_enabled=False))
        self.page = AsyncMock()
        self.page.url = "https://libzw.ustb.edu.cn/#/ic/home"
        self.page.goto.return_value = Mock(status=200)
        self.session = LoginSession("renewal", AsyncMock(), self.page)
        self.session.sso_authenticated = True
        self.session.status = "sso_authenticated"
        self.manager._login_url = AsyncMock(return_value="https://sso.ustb.edu.cn/cas/login")

    async def test_invalidation_resets_both_spa_login_fields(self) -> None:
        self.session.status = "authenticated"
        self.session.user_info = {"token": "old-secret"}
        await self.manager.invalidate_library_session(self.session)
        self.page.evaluate.assert_awaited_once_with(LIBRARY_RESET_STATE)
        self.assertIn("removeItem('isLogin')", LIBRARY_RESET_STATE)
        self.assertIsNone(self.session.user_info)
        self.assertEqual(self.session.status, "sso_authenticated")

    async def test_renewal_waits_for_real_token_and_installs_origin_guard(self) -> None:
        self.manager._library_user_info = AsyncMock(return_value={"token": "new-secret"})
        with self.assertLogs("ustb-seat-book.auth", level="INFO") as logs:
            await self.manager._try_library_locked(self.session, force=True)
        self.page.wait_for_function.assert_awaited_once_with(
            LIBRARY_LOGIN_RESULT, arg="libzw.ustb.edu.cn", timeout=20000,
        )
        self.page.wait_for_timeout.assert_not_awaited()
        self.page.add_init_script.assert_awaited_once()
        self.assertEqual(self.session.status, "authenticated")
        self.assertEqual(self.session.user_info["token"], "new-secret")
        self.assertNotIn("new-secret", "\n".join(logs.output))

    async def test_sso_redirect_without_qr_does_not_stop_automation(self) -> None:
        self.page.url = "https://sso.ustb.edu.cn/cas/login?ticket=secret"
        self.page.wait_for_function.side_effect = PlaywrightTimeoutError("pending")
        self.page.evaluate.return_value = False
        self.manager._library_user_info = AsyncMock(return_value=None)
        with self.assertLogs("ustb-seat-book.auth", level="INFO") as logs:
            await self.manager._try_library_locked(self.session, force=True)
        self.assertEqual(self.session.status, "sso_authenticated")
        self.assertNotIn("ticket=secret", "\n".join(logs.output))

    async def test_visible_sso_qr_requires_user_scan(self) -> None:
        self.page.url = "https://sso.ustb.edu.cn/cas/login"
        self.page.evaluate.return_value = "scan_required"
        self.manager._library_user_info = AsyncMock(return_value=None)
        await self.manager._try_library_locked(self.session, force=True)
        self.assertEqual(self.session.status, "library_scan_required")

    async def test_late_token_after_wait_timeout_is_accepted(self) -> None:
        self.page.wait_for_function.side_effect = PlaywrightTimeoutError("slow callback")
        self.manager._library_user_info = AsyncMock(return_value={"token": "late-token"})
        await self.manager._try_library_locked(self.session, force=True)
        self.assertEqual(self.session.status, "authenticated")

    async def test_origin_guard_is_only_installed_once(self) -> None:
        self.page.wait_for_function.side_effect = PlaywrightTimeoutError("pending")
        self.page.evaluate.return_value = False
        self.manager._library_user_info = AsyncMock(return_value=None)
        await self.manager._try_library_locked(self.session, force=True)
        await self.manager._try_library_locked(self.session, force=True)
        self.page.add_init_script.assert_awaited_once()

    async def test_service_outage_remains_retriable(self) -> None:
        self.manager._login_url.side_effect = RuntimeError("service unavailable")
        with self.assertLogs("ustb-seat-book.auth", level="WARNING"):
            await self.manager._try_library_locked(self.session, force=True)
        self.assertEqual(self.session.status, "sso_authenticated")
        self.assertIsNone(self.session.user_info)


if __name__ == "__main__":
    unittest.main()

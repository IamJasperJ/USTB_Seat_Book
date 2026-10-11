from __future__ import annotations

import asyncio
import time
from contextlib import suppress
import unittest
from unittest.mock import AsyncMock

from app.auth import AuthManager, LoginSession
from app.config import Settings


class AuthLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = Settings(keepalive_interval_seconds=30)
        self.manager = AuthManager(self.settings)
        self.page = AsyncMock()
        self.page.url = "https://libzw.ustb.edu.cn/#/ic/home"
        self.context = AsyncMock()
        self.session = LoginSession("test-session", self.context, self.page)

    async def test_successful_probes_are_logged(self) -> None:
        self.manager._probe_sso = AsyncMock(return_value=True)
        self.page.evaluate.return_value = {"httpStatus": 200, "body": {"code": 0, "data": {}}}
        self.session.sso_authenticated_at = "2026-09-26T00:00:00+00:00"
        self.session.authenticated_at = "2026-09-26T00:00:00+00:00"
        self.session.sso_authenticated = True
        self.session.status = "authenticated"
        self.session.user_info = {"token": "not-for-logs"}

        with self.assertLogs("ustb-seat-book.auth", level="INFO") as captured:
            task = asyncio.create_task(self.manager._keepalive_loop(self.session))
            for _ in range(100):
                if self.session.last_valid_at:
                    break
                await asyncio.sleep(0.01)
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

        output = "\n".join(captured.output)
        self.assertIn("JWGL application keepalive succeeded", output)
        self.assertIn("Library keepalive succeeded", output)
        self.assertIn("alive_seconds=", output)
        self.assertNotIn("not-for-logs", output)

    async def test_close_records_last_known_state_not_expiration(self) -> None:
        self.session.status = "authenticated"
        self.session.authenticated_at = "2026-09-26T00:00:00+00:00"
        self.session.last_valid_at = "2026-09-26T01:00:00+00:00"
        self.manager.sessions[self.session.session_id] = self.session

        with self.assertLogs("ustb-seat-book.auth", level="INFO") as captured:
            await self.manager.close_session(self.session.session_id)

        output = "\n".join(captured.output)
        self.assertIn("Session closed", output)
        self.assertIn("previous_status=authenticated", output)
        self.assertIn("library_last_valid_at=2026-09-26T01:00:00+00:00", output)
        self.assertNotIn("Session expired", output)

    async def test_automation_can_retry_library_before_background_interval(self) -> None:
        self.session.status = "sso_authenticated"
        self.session.sso_authenticated = True
        self.session.next_library_attempt = time.monotonic() + 3600
        self.manager._library_user_info = AsyncMock(return_value=None)
        self.manager._login_url = AsyncMock(side_effect=RuntimeError("library unavailable"))

        ready = await self.manager.ensure_library_session(self.session, force=True)

        self.assertFalse(ready)
        self.assertEqual(self.session.status, "sso_authenticated")
        self.manager._login_url.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()

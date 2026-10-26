from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from fastapi import HTTPException

from app import main
from app.auth import LoginSession
from app.automation import AuthenticationRequiredError, AutoReservation, AwaitingLibraryError, SessionOwnershipError
from app.config import Settings


class OvernightAutomationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.settings = Settings(submit_enabled=True, auto_trigger_time="06:00:00")
        self.auth = AsyncMock()
        self.automation = AutoReservation(self.settings, self.auth, account_id="overnight-acct")

    def test_evening_start_waits_for_next_morning_in_beijing(self) -> None:
        now = datetime(2026, 9, 26, 20, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.assertEqual(
            self.automation._next_trigger(now),
            datetime(2026, 9, 27, 6, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        )

    async def test_start_freezes_concrete_date_and_rejects_stale_date(self) -> None:
        trigger = datetime(2026, 9, 28, 6, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.settings.target_date = "2026-09-28"
        with patch.object(self.automation, "_next_trigger", return_value=trigger), \
                patch.object(self.automation, "_run", new=AsyncMock()):
            await self.automation.start("overnight")
        self.assertEqual(self.automation.status()["target_date"], "2026-09-28")
        self.assertEqual(self.automation.status()["next_trigger_at"], "2026-09-28T06:00:00+08:00")
        await self.automation.stop()

        self.settings.target_date = "2026-09-27"
        with patch.object(self.automation, "_next_trigger", return_value=trigger):
            with self.assertRaisesRegex(ValueError, "请重新选择日期"):
                await self.automation.start("overnight")

    async def test_day_after_tomorrow_can_be_booked_on_next_morning(self) -> None:
        trigger = datetime(2026, 9, 28, 6, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
        self.settings.target_date = "2026-09-29"
        with patch.object(self.automation, "_next_trigger", return_value=trigger), \
                patch.object(self.automation, "_run", new=AsyncMock()):
            await self.automation.start("overnight")
        self.assertEqual(self.automation.status()["target_date"], "2026-09-29")
        await self.automation.stop()

        self.settings.target_date = "2026-09-30"
        with patch.object(self.automation, "_next_trigger", return_value=trigger):
            with self.assertRaisesRegex(ValueError, "请重新选择日期"):
                await self.automation.start("overnight")

    async def test_attempt_uses_frozen_date_not_day_relative_to_attempt(self) -> None:
        session = LoginSession("overnight", AsyncMock(), AsyncMock())
        session.status = "authenticated"
        session.sso_authenticated = True
        session.owner_account_id = "overnight-acct"
        self.auth.get.return_value = session
        self.auth.credentials.return_value = ({}, {"token": "test"})
        self.automation.session_id = session.session_id
        self.automation.target_date = datetime(2026, 9, 28).date()
        client = AsyncMock()
        client.seats.side_effect = RuntimeError("stop after seat query")
        with patch("app.automation.SeatApi", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "stop after seat query"):
                await self.automation._attempt_once()
        client.seats.assert_awaited_once_with(self.automation.target_date)

    async def test_library_outage_is_retriable_with_school_sso(self) -> None:
        session = LoginSession("overnight", AsyncMock(), AsyncMock())
        session.status = "sso_authenticated"
        session.sso_authenticated = True
        session.owner_account_id = "overnight-acct"
        session.library_error = "图书馆服务不可用"
        self.auth.get.return_value = session
        self.auth.ensure_library_session.return_value = False
        self.automation.session_id = session.session_id

        with self.assertRaises(AwaitingLibraryError):
            await self.automation._attempt_once()
        self.auth.ensure_library_session.assert_awaited_once_with(session, force=True)

    async def test_library_outage_retries_then_succeeds(self) -> None:
        self.automation.session_id = "overnight"
        self.automation._wait_for_trigger = AsyncMock()
        self.automation._sleep_in_chunks = AsyncMock()
        self.automation._attempt_once = AsyncMock(
            side_effect=[AwaitingLibraryError("图书馆停服"), True]
        )

        await self.automation._run()

        self.assertEqual(self.automation.state, "success")
        self.assertEqual(self.automation.attempts, 2)
        self.automation._sleep_in_chunks.assert_awaited_once_with(5)

    async def test_attempt_once_rejects_session_owned_by_another_account(self) -> None:
        # Defense in depth: even if a foreign session_id ever got bound to
        # this task (e.g. a future bug upstream of here), the one place every
        # attempt actually uses the session must still refuse to act under
        # another account's identity.
        session = LoginSession("overnight", AsyncMock(), AsyncMock())
        session.status = "authenticated"
        session.sso_authenticated = True
        session.owner_account_id = "someone-else"
        self.auth.get.return_value = session
        self.automation.session_id = session.session_id
        with self.assertRaises(SessionOwnershipError):
            await self.automation._attempt_once()

    async def test_run_stops_terminally_on_session_ownership_mismatch(self) -> None:
        self.automation.session_id = "overnight"
        self.automation._wait_for_trigger = AsyncMock()
        self.automation._sleep_in_chunks = AsyncMock()
        self.automation._attempt_once = AsyncMock(side_effect=SessionOwnershipError("mismatch"))
        await self.automation._run()
        self.assertEqual(self.automation.state, "error")
        self.automation._sleep_in_chunks.assert_not_awaited()

    async def test_missing_school_login_stops(self) -> None:
        session = LoginSession("overnight", AsyncMock(), AsyncMock())
        session.status = "expired"
        session.owner_account_id = "overnight-acct"
        self.auth.get.return_value = session
        self.automation.session_id = session.session_id

        with self.assertRaises(AuthenticationRequiredError):
            await self.automation._attempt_once()

    async def test_api_allows_arming_during_library_outage(self) -> None:
        from app.accounts import Account
        session = LoginSession("overnight", AsyncMock(), AsyncMock())
        session.status = "sso_authenticated"
        session.sso_authenticated = True
        account = Account(id="overnight-acct", username="tester", is_admin=False)
        settings = Settings(submit_enabled=True, keepalive_enabled=True)
        runtime = main.UserRuntime(account, settings, main.auth)
        self.addCleanup(lambda: Path(runtime.automation.history.path).unlink(missing_ok=True))
        with patch.dict(main.runtimes, {account.id: runtime}), \
                patch.object(main, "get_session", new=AsyncMock(return_value=session)), \
                patch.object(main.auth, "refresh_status", new=AsyncMock()), \
                patch.object(runtime.automation, "start", new=AsyncMock()) as start:
            await main.start_automation(main.AutomationRequest(auth_session_id=session.session_id), account=account)
            start.assert_awaited_once_with(session.session_id)

    async def test_api_requires_keepalive_for_library_outage(self) -> None:
        from app.accounts import Account
        session = LoginSession("overnight", AsyncMock(), AsyncMock())
        session.status = "sso_authenticated"
        session.sso_authenticated = True
        account = Account(id="overnight-acct-2", username="tester", is_admin=False)
        settings = Settings(keepalive_enabled=False)
        runtime = main.UserRuntime(account, settings, main.auth)
        self.addCleanup(lambda: Path(runtime.automation.history.path).unlink(missing_ok=True))
        with patch.dict(main.runtimes, {account.id: runtime}), \
                patch.object(main, "get_session", new=AsyncMock(return_value=session)), \
                patch.object(main.auth, "refresh_status", new=AsyncMock()):
            with self.assertRaises(HTTPException) as caught:
                await main.start_automation(main.AutomationRequest(auth_session_id=session.session_id), account=account)
            self.assertEqual(caught.exception.status_code, 409)


if __name__ == "__main__":
    unittest.main()

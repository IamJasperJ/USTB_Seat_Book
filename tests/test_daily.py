from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.auth import LoginSession
from app.automation import AutoReservation, CHINA_TZ
from app.config import Settings
from app.daily import DailyReservation


class DailyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(daily_auto_enabled=True, submit_enabled=True,
                                 auto_trigger_time="06:10:30", target_date="2026-09-30")
        self.session = LoginSession("logged-in", AsyncMock(), AsyncMock())
        self.session.status = "authenticated"
        self.session.sso_authenticated = True
        self.auth = SimpleNamespace(sessions={"logged-in": self.session}, get=AsyncMock(), refresh_status=AsyncMock())
        self.auto = AutoReservation(self.settings, self.auth, Path(self.temp.name) / "history.sqlite3")
        self.addAsyncCleanup(self.auto.stop)
        async def wait() -> None:
            await asyncio.sleep(3600)
        self.auto._wait_for_trigger = wait
        self.daily = DailyReservation(self.auto)

    async def test_night_enable_books_day_after_today_and_rolls_daily(self) -> None:
        await self.daily.tick(datetime(2026, 10, 1, 23, tzinfo=CHINA_TZ))
        self.assertEqual(self.auto.next_trigger_at, "2026-10-02T06:10:30+08:00")
        self.assertEqual(self.auto.target_date.isoformat(), "2026-10-03")
        self.assertEqual(self.settings.target_date, "2026-09-30")
        self.auto.state = "success"
        self.auto.attempts = 1
        self.auto._finish()
        await self.auto.stop()
        await self.daily.tick(datetime(2026, 10, 2, 7, tzinfo=CHINA_TZ))
        self.assertEqual(self.auto.target_date.isoformat(), "2026-10-04")
        self.assertEqual(self.auto.mode, "daily")
        self.assertEqual(self.auto.history.list()["records"][0]["mode"], "daily")

    async def test_successful_day_is_not_repeated_before_trigger_on_restart(self) -> None:
        self.auto.history.save({"task_id": "done", "started_at": "2026-10-01T21:00:00+08:00",
                               "finished_at": "2026-10-02T06:11:00+08:00", "mode": "daily",
                               "next_trigger_at": "2026-10-02T06:10:30+08:00", "state": "success", "attempts": 1})
        await self.daily.tick(datetime(2026, 10, 2, 6, tzinfo=CHINA_TZ))
        self.assertEqual(self.auto.next_trigger_at, "2026-10-03T06:10:30+08:00")

    async def test_fresh_scan_resumes_today_after_auth_requirement(self) -> None:
        self.auto.mode = "daily"
        self.auto.state = "auth_required"
        self.auto.next_trigger_at = "2026-10-02T06:10:30+08:00"
        self.auth.sessions = {}
        await self.daily.tick(datetime(2026, 10, 2, 8, tzinfo=CHINA_TZ))
        self.assertEqual(self.daily.state, "waiting_login")
        self.assertTrue(self.settings.daily_auto_enabled)
        self.auth.sessions = {"logged-in": self.session}
        await self.daily.tick(datetime(2026, 10, 2, 8, tzinfo=CHINA_TZ))
        self.assertEqual(self.auto.target_date.isoformat(), "2026-10-03")
        self.assertEqual(self.auto.next_trigger_at, "2026-10-02T06:10:30+08:00")

    async def test_detects_scan_without_web_poll_and_disabled_mode_does_nothing(self) -> None:
        self.session.status = "waiting_scan"
        self.session.sso_authenticated = False
        async def complete_scan(session) -> None:
            session.status = "sso_authenticated"
            session.sso_authenticated = True
        self.auth.refresh_status.side_effect = complete_scan
        await self.daily.tick(datetime(2026, 10, 1, 23, tzinfo=CHINA_TZ))
        self.auth.refresh_status.assert_awaited_once_with(self.session)
        self.assertTrue(self.auto.running)
        await self.auto.stop()
        self.settings.daily_auto_enabled = False
        await self.daily.tick()
        self.assertFalse(self.auto.running)
        self.assertEqual(self.daily.state, "disabled")

    async def test_concurrent_ticks_start_only_one_task(self) -> None:
        now = datetime(2026, 10, 1, 23, tzinfo=CHINA_TZ)
        with patch.object(self.auto, "start", wraps=self.auto.start) as start:
            await asyncio.gather(self.daily.tick(now), self.daily.tick(now))
            self.assertEqual(start.await_count, 1)

    async def test_daily_attempts_cannot_continue_past_midnight(self) -> None:
        self.auto.mode = "daily"
        self.auto.next_trigger_at = "2026-10-02T06:10:30+08:00"
        self.auto._wait_for_trigger = AsyncMock()
        self.auto._attempt_once = AsyncMock()
        with patch("app.automation.datetime") as clock:
            clock.now.return_value = datetime(2026, 10, 3, 0, tzinfo=CHINA_TZ)
            clock.fromisoformat.side_effect = datetime.fromisoformat
            await self.auto._run()
        self.assertEqual(self.auto.state, "expired")
        self.auto._attempt_once.assert_not_awaited()

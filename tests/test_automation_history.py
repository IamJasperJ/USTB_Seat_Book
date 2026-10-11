from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from app.automation import AutoReservation, AuthenticationRequiredError
from app.automation_history import AutomationHistory
from app.config import Settings


class TaskHistoryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "tasks.sqlite3"
        self.settings = Settings(target_date="2026-09-29", submit_enabled=True)
        self.auto = AutoReservation(self.settings, AsyncMock(), self.path)
        self.trigger = datetime(2026, 9, 28, 6, tzinfo=ZoneInfo("Asia/Shanghai"))

    async def start_waiting(self) -> None:
        async def wait() -> None:
            await asyncio.sleep(3600)
        self.auto._wait_for_trigger = wait
        with patch.object(self.auto, "_next_trigger", return_value=self.trigger):
            await self.auto.start("private-session-id")

    async def test_snapshot_is_frozen_and_stop_is_persistent(self) -> None:
        await self.start_waiting()
        task_id = self.auto.task_id
        self.settings.seat_names.append("F4B999")
        self.settings.start_time = "12:00"
        self.assertEqual(self.auto.status()["start_time"], "07:00")
        self.assertEqual(self.auto.status()["seat_names"], ["F4B114"])
        self.assertEqual(self.auto.history.list()["total"], 0)
        await self.auto.stop()
        records = AutomationHistory(self.path).list()["records"]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["task_id"], task_id)
        self.assertEqual(records[0]["state"], "stopped")
        self.assertFalse(records[0]["running"])
        self.assertIsNotNone(records[0]["finished_at"])
        self.assertNotIn("session_id", records[0])
        await self.auto.stop()
        self.assertEqual(self.auto.history.list()["total"], 1)

    async def test_success_is_archived_and_not_overwritten_by_stop(self) -> None:
        self.auto._wait_for_trigger = AsyncMock()
        self.auto._attempt_once = AsyncMock(return_value=True)
        with patch.object(self.auto, "_next_trigger", return_value=self.trigger):
            await self.auto.start("test")
        await self.auto.task
        await self.auto.stop()
        result = self.auto.history.list()["records"][0]
        self.assertEqual(result["state"], "success")
        self.assertEqual(result["attempts"], 1)
        self.assertIsNone(result["last_error"])

    async def test_auth_failure_and_new_task_have_distinct_history(self) -> None:
        self.auto._wait_for_trigger = AsyncMock()
        self.auto._attempt_once = AsyncMock(side_effect=AuthenticationRequiredError("请重新扫码"))
        with patch.object(self.auto, "_next_trigger", return_value=self.trigger):
            await self.auto.start("test")
        await self.auto.task
        await self.start_waiting()
        await self.auto.stop()
        rows = self.auto.history.list()["records"]
        self.assertEqual(len(rows), 2)
        self.assertEqual({r["state"] for r in rows}, {"stopped", "auth_required"})
        self.assertEqual(len({r["task_id"] for r in rows}), 2)

    async def test_restart_marks_only_unfinished_records_as_interrupted(self) -> None:
        store = self.auto.history
        store.save({"task_id": "unfinished", "started_at": "2026-09-28T01:00:00+08:00", "finished_at": None, "state": "waiting_trigger"})
        restarted = AutoReservation(self.settings, AsyncMock(), self.path)
        row = restarted.history.list()["records"][0]
        self.assertEqual(row["state"], "interrupted")
        self.assertFalse(row["running"])
        self.assertFalse(restarted.running)
        self.assertEqual(restarted.status()["state"], "idle")

    async def test_pagination_and_latest_first(self) -> None:
        for i in range(12):
            self.auto.history.save({"task_id": str(i), "started_at": f"2026-09-28T01:{i:02d}:00+08:00", "finished_at": "2026-09-28T02:00:00+08:00", "state": "success"})
        first = self.auto.history.list(1, 10)
        second = self.auto.history.list(2, 10)
        self.assertEqual(first["total"], 12)
        self.assertEqual(first["records"][0]["task_id"], "11")
        self.assertEqual(len(second["records"]), 2)

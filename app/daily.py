from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

from .automation import AutoReservation, CHINA_TZ

logger = logging.getLogger("ustb-seat-book.daily")


class DailyReservation:
    """Single-account daily schedule; login is selected from live auth sessions."""

    def __init__(self, automation: AutoReservation):
        self.automation = automation
        self.task: asyncio.Task | None = None
        self.state = "disabled"
        self.last_error: str | None = None
        self.next_trigger_at: str | None = None
        self.target_date: str | None = None
        self._tick_lock = asyncio.Lock()

    async def start(self) -> None:
        self.task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def tick(self, now: datetime | None = None) -> None:
        async with self._tick_lock:
            await self._tick(now)

    async def _tick(self, now: datetime | None = None) -> None:
        auto = self.automation
        settings = auto.settings
        if not settings.daily_auto_enabled:
            self.state = "disabled"
            self.last_error = None
            self.next_trigger_at = self.target_date = None
            return
        now = now or datetime.now(CHINA_TZ)
        if not settings.submit_enabled or not settings.keepalive_enabled:
            if auto.running and auto.mode == "daily":
                await auto.stop()
            self.state = "blocked"
            self.last_error = "每日模式需要开启预约提交和登录保活"
            return
        if auto.running:
            self.last_error = None
            self.state = "scheduled" if auto.mode == "daily" else "waiting_single_task"
            self.next_trigger_at = auto.next_trigger_at
            self.target_date = auto.target_date.isoformat() if auto.target_date else None
            return
        trigger = auto._next_trigger(now)
        # After a genuine QR requirement, a fresh scan can resume today's job.
        if auto.mode == "daily" and auto.state == "auth_required" and auto.next_trigger_at:
            previous = datetime.fromisoformat(auto.next_trigger_at)
            if previous.date() == now.date():
                trigger = previous
        if auto.history and auto.history.daily_finished(trigger.date().isoformat()):
            trigger = auto._next_trigger(trigger + timedelta(seconds=1))
        self.next_trigger_at = trigger.isoformat(timespec="seconds")
        self.target_date = (trigger.date() + timedelta(days=1)).isoformat()
        # Detect completion of a scan even when no web page is polling status.
        for pending in list(auto.auth.sessions.values()):
            if pending.status in {"waiting_scan", "callback_received", "library_scan_required"}:
                await auto.auth.get(pending.session_id)
                await auto.auth.refresh_status(pending)
        sessions = [s for s in auto.auth.sessions.values()
                    if s.status in {"authenticated", "sso_authenticated"} and s.sso_authenticated]
        if not sessions:
            self.state = "waiting_login"
            self.last_error = "请扫码登录；每日模式已保留，登录后自动继续"
            return
        session = max(sessions, key=lambda s: s.created_at)
        if not settings.daily_auto_enabled:
            self.state = "disabled"
            return
        await auto.start(session.session_id, trigger_at=trigger,
                         target_date=trigger.date() + timedelta(days=1), mode="daily")
        self.state = "scheduled"
        self.last_error = None
        logger.info("daily reservation scheduled: trigger_at=%s target_date=%s", self.next_trigger_at, self.target_date)

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state = "error"
                self.last_error = str(exc)
                logger.exception("daily scheduler error")
            await asyncio.sleep(10)

    def status(self) -> dict[str, Any]:
        return {"enabled": self.automation.settings.daily_auto_enabled, "state": self.state,
                "next_trigger_at": self.next_trigger_at, "target_date": self.target_date,
                "last_error": self.last_error}

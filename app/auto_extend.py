"""Automatically push back today's reservations that are about to lapse.

A reservation that is effective but not signed in, with under N minutes left to
sign in, or one on temporary leave with under N minutes left to return, is
released and rebooked on the same seat N minutes from now (the same planner and
steps as POST /api/reservations/rebook). Signing in still needs the on-seat QR
code; this only buys time.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timedelta
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from . import library
from .accounts import Account
from .dependencies import current_account
from .automation import CHINA_TZ
from .library import _parse_moment

logger = logging.getLogger("ustb-seat-book.auto-extend")
TICK_SECONDS = 10
router = APIRouter()


def minutes_left(record: dict, now: datetime) -> tuple[str, float | None] | None:
    """("check_in" | "temp_leave", minutes left) for a record to watch, else None.

    Minutes left is None when the library gave no deadline; nothing is done then.
    """
    if record.get("finished"):
        return None
    if record.get("on_temp_leave"):
        value = record.get("temp_leave_end_time")
        # The live site sends the minutes left (e.g. 59); accept a moment as well.
        if isinstance(value, (int, float)) and value < 10**11:
            return "temp_leave", float(value)
        deadline = _parse_moment(value, now.date())
        return "temp_leave", (deadline - now).total_seconds() / 60 if deadline else None
    if record.get("needs_check_in"):
        deadline = _parse_moment(record.get("latest_check_in_time"), now.date())
        return "check_in", (deadline - now).total_seconds() / 60 if deadline else None
    return None


class AutoExtend:
    """One account's auto-extend loop; reads its settings and lock from the runtime."""

    def __init__(self, runtime):
        self.runtime = runtime
        self.task: asyncio.Task | None = None
        self.state = "disabled"
        self.last_error: str | None = None
        self.last_check_at: str | None = None
        self.next_check_at: str | None = None
        self.watching: list[dict[str, Any]] = []
        self.actions: deque[dict[str, Any]] = deque(maxlen=20)
        self._last_checked: datetime | None = None

    async def start(self) -> None:
        self.task = asyncio.create_task(self._loop())

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    def _session(self):
        from . import main

        sessions = [s for s in main.auth.sessions.values()
                    if s.status == "authenticated" and s.owner_account_id == self.runtime.account.id]
        return max(sessions, key=lambda s: s.created_at) if sessions else None

    async def tick(self, now: datetime | None = None) -> None:
        settings = self.runtime.settings
        now = now or library.beijing_now()
        if not settings.auto_extend_enabled:
            self.state, self.last_error, self.next_check_at, self.watching = "disabled", None, None, []
            self._last_checked = None
            return
        try:
            after = datetime.strptime(settings.auto_extend_after, "%H:%M").time()
        except ValueError:
            after = datetime.min.time()
        start = datetime.combine(now.date(), after)
        if now < start:
            self.state, self.last_error, self.watching = "waiting_start", None, []
            self.next_check_at = start.replace(tzinfo=CHINA_TZ).isoformat(timespec="seconds")
            return
        interval = timedelta(minutes=settings.auto_extend_interval_minutes)
        if self._last_checked and self._last_checked.date() == now.date() and now - self._last_checked < interval:
            return
        if not settings.submit_enabled:
            self.state, self.last_error = "blocked", "自动延后需要开启“允许预约提交”"
            return
        session = self._session()
        if session is None:
            self.state, self.last_error = "waiting_login", "请扫码登录；登录后自动继续检查"
            return
        if self.runtime.operation_lock.locked():
            return  # another operation is running; try again on the next tick
        self._last_checked = now
        self.next_check_at = (now + interval).replace(tzinfo=CHINA_TZ).isoformat(timespec="seconds")
        await self.check(session.session_id, now=now)

    async def check(self, session_id: str, *, dry_run: bool = False, now: datetime | None = None) -> dict[str, Any]:
        """One pass over today's reservations; dry_run only reports the plans."""
        from . import main
        from .reservation_ops import _owns_record, execute_rebook

        settings = self.runtime.settings
        account = self.runtime.account
        delays = {"check_in": settings.auto_extend_checkin_minutes,
                  "temp_leave": settings.auto_extend_temp_leave_minutes}
        now = now or library.beijing_now()
        watching, results = [], []
        async with self.runtime.operation_lock:
            try:
                client = await main.authenticated_api(session_id, account)
            except HTTPException as exc:
                self.state, self.last_error = "error", str(exc.detail)
                raise
            try:
                page, found = 1, []
                while True:
                    listed = await client.my_reservations(now.date(), page)
                    records = main._reservation_records(listed, client.user_info)
                    own = {str(r.get("uuid") or "") for r in listed["data"]
                           if isinstance(r, dict) and _owns_record(r, client.user_info)}
                    found += [(page, r) for r in records if r["uuid"] in own]
                    if page * 20 >= listed.get("count", len(records)):
                        break
                    page += 1
                for page, record in found:
                    seen = minutes_left(record, now)
                    if seen is None:
                        continue
                    kind, left = seen
                    seat = next((d["name"] for d in record["devices"] if d.get("name")), None)
                    entry = {"uuid": record["uuid"], "seat": seat, "kind": kind,
                             "minutes_left": None if left is None else round(left, 1),
                             "threshold": delays[kind]}
                    watching.append(entry)
                    if left is None or left >= delays[kind]:
                        continue
                    outcome = {"seat": seat, "action": record["rebook_action"], "state": "refused"}
                    try:
                        result = await execute_rebook(client, settings, record["uuid"], page,
                                                      delays[kind], dry_run, outcome)
                    except HTTPException as exc:
                        result = {"state": "refused", "success": False, "message": str(exc.detail)}
                    action = {"time": now.replace(tzinfo=CHINA_TZ).isoformat(timespec="seconds"),
                              **entry, "delay_minutes": delays[kind], **result}
                    results.append(action)
                    if result.get("state") == "rebooked":
                        watching.remove(entry)  # replaced; the new booking is seen next check
                    if not dry_run:
                        self.actions.appendleft(action)
                        logger.info("auto extend: account=%s seat=%s kind=%s left=%.1f action=%s state=%s",
                                    account.username, seat, kind, left, outcome["action"], outcome["state"])
            except HTTPException as exc:
                self.state, self.last_error = "error", str(exc.detail)
                raise
            except Exception as exc:
                self.state, self.last_error = "error", "查询今日预约失败，请稍后重试"
                raise HTTPException(status_code=502, detail=self.last_error) from exc
            finally:
                await client.close()
        if not dry_run:
            self.state, self.last_error, self.watching = "active", None, watching
            self.last_check_at = now.replace(tzinfo=CHINA_TZ).isoformat(timespec="seconds")
        return {"dry_run": dry_run, "checked_at": now.replace(tzinfo=CHINA_TZ).isoformat(timespec="seconds"),
                "watching": watching, "actions": results}

    async def _loop(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except HTTPException:
                pass  # state/last_error already describe it
            except Exception as exc:
                self.state, self.last_error = "error", str(exc)
                logger.exception("auto extend error")
            await asyncio.sleep(TICK_SECONDS)

    def status(self) -> dict[str, Any]:
        settings = self.runtime.settings
        return {"enabled": settings.auto_extend_enabled, "state": self.state, "last_error": self.last_error,
                "after": settings.auto_extend_after, "interval_minutes": settings.auto_extend_interval_minutes,
                "checkin_minutes": settings.auto_extend_checkin_minutes,
                "temp_leave_minutes": settings.auto_extend_temp_leave_minutes,
                "last_check_at": self.last_check_at, "next_check_at": self.next_check_at,
                "watching": self.watching, "actions": list(self.actions)}


@router.get("/api/auto-extend")
async def auto_extend_status(account: Account = Depends(current_account)) -> dict[str, Any]:
    from . import main

    return (await main.ensure_runtime(account)).auto_extend.status()


class AutoExtendCheck(BaseModel):
    auth_session_id: str | None = None
    dry_run: bool = True
    confirm: bool = False


@router.post("/api/auto-extend/check")
async def auto_extend_check(request: AutoExtendCheck, account: Account = Depends(current_account)) -> dict[str, Any]:
    """Run one check now with the saved thresholds; dry_run (default) only reports plans."""
    from . import main

    runtime = await main.ensure_runtime(account)
    if not request.dry_run:
        if not runtime.settings.submit_enabled:
            raise HTTPException(status_code=403, detail="submit_enabled is false（预约提交未开启）")
        if not request.confirm:
            raise HTTPException(status_code=400, detail="请明确确认立即执行自动延后")
    session_id = request.auth_session_id
    if session_id is None:
        session = runtime.auto_extend._session()
        if session is None:
            raise HTTPException(status_code=409, detail="没有已登录图书馆的会话，请先扫码登录")
        session_id = session.session_id
    if runtime.operation_lock.locked():
        raise HTTPException(status_code=409, detail="上一个预约操作仍在进行，请稍候")
    return await runtime.auto_extend.check(session_id, dry_run=request.dry_run)

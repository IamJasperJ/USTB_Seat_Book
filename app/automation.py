from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timedelta
from typing import Any
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from .auth import AuthManager
from .automation_history import AutomationHistory
from .config import Settings
from . import library
from .library import SeatApi, build_seat_view, is_valid_time_range

logger = logging.getLogger("ustb-seat-book.automation")
CHINA_TZ = ZoneInfo("Asia/Shanghai")
# Library refusals about the account rather than a seat: trying other seats can't help.
ACCOUNT_LEVEL_REFUSALS = ("不能连续预约", "超出预约数量", "没有权限")
# Library refusals that mean this particular seat was taken: only these move on to
# the next candidate. Unknown wording stops the attempt; the next retry's seat list
# then shows the seat as taken and skips it without a request.
SEAT_TAKEN_REFUSALS = ("已被预约", "被占用", "已被占", "被他人", "冲突")


class AwaitingLibraryError(RuntimeError):
    """The campus SSO is usable, but the library cannot issue a session yet."""


class AuthenticationRequiredError(RuntimeError):
    """The user must scan a QR code again before automation can continue."""


class SessionOwnershipError(RuntimeError):
    """The session bound to this task belongs to a different account.

    This should be unreachable in normal operation — callers are expected to
    only ever pass this account's own session_id into start(). It exists as a
    last-resort check at the one place every attempt actually uses the
    session, so a bug anywhere upstream (today's or a future one) can't result
    in submitting a reservation under another account's identity.
    """


class AutoReservation:
    def __init__(self, settings: Settings, auth: AuthManager, history_path: str | Path | None = None, *, account_id: str):
        self.settings = settings
        self.auth = auth
        self.account_id = account_id
        self.task: asyncio.Task | None = None
        self.session_id: str | None = None
        self.state = "idle"
        self.attempts = 0
        self.last_error: str | None = None
        self.last_attempt_at: str | None = None
        self.next_trigger_at: str | None = None
        self.target_date: date | None = None
        self.task_id: str | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None
        self.run_settings: Settings | None = None
        self.reserved_seat: str | None = None
        self.mode = "once"
        self.history = AutomationHistory(history_path) if history_path else None
        self._lifecycle_lock = asyncio.Lock()
        self._submit_uncertain = False
        if self.history:
            self.history.interrupt_unfinished(library.beijing_now().replace(tzinfo=CHINA_TZ).isoformat(timespec="seconds"))

    @property
    def running(self) -> bool:
        return bool(self.task and not self.task.done())

    async def start(self, session_id: str, *, trigger_at: datetime | None = None,
                    target_date: date | None = None, mode: str = "once") -> None:
        async with self._lifecycle_lock:
            await self._start(session_id, trigger_at=trigger_at, target_date=target_date, mode=mode)

    async def _start(self, session_id: str, *, trigger_at: datetime | None = None,
                     target_date: date | None = None, mode: str = "once") -> None:
        trigger_at = trigger_at or self._next_trigger()
        target_date = target_date or self._resolve_target_date()
        if not trigger_at.date() <= target_date <= trigger_at.date() + timedelta(days=1):
            raise ValueError(
                f"目标日期 {target_date.isoformat()} 不在触发日 {trigger_at.date().isoformat()} 的今天/明天范围内，请重新选择日期"
            )
        await self._stop()
        self.session_id = session_id
        self.target_date = target_date
        self.attempts = 0
        self._submit_uncertain = False
        self.last_error = None
        self.last_attempt_at = None
        self.next_trigger_at = trigger_at.isoformat(timespec="seconds")
        self.state = "scheduled"
        self.task_id = uuid4().hex
        self.started_at = library.beijing_now().replace(tzinfo=CHINA_TZ).isoformat(timespec="seconds")
        self.finished_at = None
        self.run_settings = self.settings.model_copy(deep=True)
        self.mode = mode
        self.reserved_seat = None
        self._checkpoint()
        self.task = asyncio.create_task(self._run())
        logger.info("automatic reservation started: trigger_at=%s target_date=%s", self.next_trigger_at, self.target_date)

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            await self._stop()

    async def _stop(self) -> None:
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.task_id and not self.finished_at:
            self.state = "stopped"
            self._finish()
        self.task = None
        self.session_id = None

    def _checkpoint(self) -> None:
        if not self.history or not self.task_id:
            return
        record = self.status()
        record.pop("session_id", None)
        record["running"] = self.finished_at is None
        try:
            self.history.save(record)
        except Exception:
            logger.exception("cannot persist automatic reservation history")

    def _finish(self) -> None:
        if self.task_id and not self.finished_at:
            self.finished_at = library.beijing_now().replace(tzinfo=CHINA_TZ).isoformat(timespec="seconds")
            self._checkpoint()

    async def _sleep_in_chunks(self, seconds: float) -> None:
        remaining = max(0, seconds)
        while remaining > 0:
            step = min(30, remaining)
            await asyncio.sleep(step)
            remaining -= step

    def _trigger_seconds(self) -> int:
        raw = self.settings.auto_trigger_time.strip()
        parts = [int(part) for part in raw.split(":")]
        if len(parts) == 2:
            parts.append(0)
        if len(parts) != 3:
            raise ValueError("auto_trigger_time 应为 HH:MM 或 HH:MM:SS")
        hour, minute, second = parts
        if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
            raise ValueError("auto_trigger_time 超出范围")
        return hour * 3600 + minute * 60 + second

    def _next_trigger(self, now: datetime | None = None) -> datetime:
        now = now or library.beijing_now().replace(tzinfo=CHINA_TZ)
        trigger = self._trigger_seconds()
        trigger_today = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(seconds=trigger)
        return trigger_today if trigger_today > now else trigger_today + timedelta(days=1)

    def _resolve_target_date(self) -> date:
        raw = self.settings.target_date.strip().lower()
        today = library.beijing_now().replace(tzinfo=CHINA_TZ).date()
        if raw == "today":
            return today
        if raw == "tomorrow":
            return today + timedelta(days=1)
        try:
            return date.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError("目标日期应为今天、明天或 YYYY-MM-DD 日期") from exc

    async def _wait_for_trigger(self) -> None:
        target = datetime.fromisoformat(self.next_trigger_at) if self.next_trigger_at else self._next_trigger()
        self.next_trigger_at = target.isoformat(timespec="seconds")
        self.state = "waiting_trigger"
        self._checkpoint()
        logger.info("automatic reservation waiting for trigger_at=%s", self.next_trigger_at)
        while (remaining := (target - library.beijing_now().replace(tzinfo=CHINA_TZ)).total_seconds()) > 0:
            await asyncio.sleep(min(10, remaining))

    async def _attempt_once(self) -> bool:
        settings = self.run_settings or self.settings
        if not self.session_id:
            raise RuntimeError("没有绑定登录会话")
        try:
            session = await self.auth.get(self.session_id)
        except KeyError as exc:
            raise AuthenticationRequiredError("登录会话已丢失，请重新扫码") from exc
        if session.owner_account_id != self.account_id:
            raise SessionOwnershipError(
                f"登录会话不属于当前账号（session owner={session.owner_account_id!r}, "
                f"task account={self.account_id!r}），任务已终止"
            )
        if session.status in {"error", "expired", "closed"}:
            raise AuthenticationRequiredError("学校登录已失效，请重新扫码")
        if session.status == "library_scan_required":
            raise AuthenticationRequiredError("图书馆要求再次扫码，请在页面完成扫码")
        if session.status != "authenticated":
            if not session.sso_authenticated:
                raise AuthenticationRequiredError("学校登录尚未完成或已失效，请重新扫码")
            ready = await self.auth.ensure_library_session(session, force=True)
            if session.status == "library_scan_required":
                raise AuthenticationRequiredError("图书馆要求再次扫码，请在页面完成扫码")
            if not ready:
                raise AwaitingLibraryError(session.library_error or "图书馆服务尚未恢复，等待重试")
        try:
            cookies, user_info = await self.auth.credentials(session)
        except RuntimeError as exc:
            if session.sso_authenticated:
                raise AwaitingLibraryError("图书馆会话尚未建立，等待重试") from exc
            raise AuthenticationRequiredError("学校登录已失效，请重新扫码") from exc
        client = SeatApi(settings, cookies, user_info)
        try:
            target = self.target_date or client.target_date()
            if self._submit_uncertain:
                # A previous submit may have gone through without us seeing the
                # answer; booking another seat now could double-book.
                found = await self._find_own_booking(client, target, settings)
                self._submit_uncertain = False
                if found:
                    self.reserved_seat = found
                    logger.info("automatic reservation confirmed after an unanswered submit: %s", found)
                    return True
            raw = await client.seats(target)
            candidates = build_seat_view(raw, settings.seat_names, day=target)
            if not candidates:
                raise RuntimeError("没有找到配置中的座位")
            begin = f"{target.isoformat()} {settings.start_time}:00"
            end = f"{target.isoformat()} {settings.end_time}:00"
            wanted = (library._parse_moment(begin, target), library._parse_moment(end, target))
            skipped: list[str] = []
            # Try every configured seat in order; one taken seat no longer fails the attempt.
            for candidate in candidates:
                name = candidate["dev_name"]
                reason = self._skip_reason(candidate, settings, target, wanted)
                if reason:
                    skipped.append(f"{name}：{reason}")
                    continue
                try:
                    result = await client.reserve(candidate["dev_id"], begin, end)
                except Exception:
                    self._submit_uncertain = True
                    raise
                if result.get("code") == 0:
                    self.reserved_seat = name
                    logger.info("automatic reservation succeeded for %s", name)
                    return True
                message = result.get("message") or "预约请求失败"
                if not any(kw in message for kw in SEAT_TAKEN_REFUSALS):
                    # Anything but "this seat is taken" (booking not open yet, login,
                    # account limits…) would be the same for every seat: stop this
                    # attempt and let the normal retry handle it, one request per retry.
                    raise RuntimeError(message if not skipped else "；".join([*skipped, f"{name}：{message}"]))
                skipped.append(f"{name}：{message}")
                logger.info("automatic reservation refused for %s: %s; trying next seat", name, message)
            raise RuntimeError("；".join(skipped) or "没有可尝试的座位")
        except RuntimeError as exc:
            if "登录" in str(exc) or "扫码" in str(exc):
                await self.auth.invalidate_library_session(session)
                if session.sso_authenticated:
                    raise AwaitingLibraryError("图书馆会话失效，等待自动重新获取") from exc
                raise AuthenticationRequiredError("学校登录已失效，请重新扫码") from exc
            raise
        finally:
            await client.close()

    @staticmethod
    def _skip_reason(candidate: dict[str, Any], settings: Settings, target: date,
                     wanted: tuple[datetime | None, datetime | None]) -> str | None:
        if candidate["open_state"] != 1 or candidate["dev_status"] != 0:
            return "座位未开放"
        if not is_valid_time_range(candidate, settings.start_time, settings.end_time):
            return "配置时间不在该座位实际可选范围内"
        busy = library.busy_ranges(candidate, target)
        if busy is None:
            return "已有预约且时间无法解析" if candidate["reservations"] else None
        start, end = wanted
        if any(start < b_end and end > b_begin for b_begin, b_end in busy):
            return "所选时段已被预约"
        return None

    async def _find_own_booking(self, client: SeatApi, target: date, settings: Settings) -> str | None:
        """Name of a configured seat this account already holds for exactly the wanted slot."""
        data = await client.my_reservations(target)
        start = library._parse_moment(f"{target.isoformat()} {settings.start_time}:00", target)
        end = library._parse_moment(f"{target.isoformat()} {settings.end_time}:00", target)
        acc_no = client.user_info.get("accNo")
        for row in data.get("data") or []:
            if not isinstance(row, dict) or int(row.get("resvStatus") or 0) & 128:
                continue
            if acc_no is not None and row.get("appAccNo") is not None and str(row["appAccNo"]) != str(acc_no):
                continue
            if (library._parse_moment(row.get("resvBeginTime"), target) != start
                    or library._parse_moment(row.get("resvEndTime"), target) != end):
                continue
            for device in row.get("resvDevInfoList") or []:
                if isinstance(device, dict) and device.get("devName") in settings.seat_names:
                    return device["devName"]
        return None

    async def _run(self) -> None:
        settings = self.run_settings or self.settings
        try:
            if not settings.submit_enabled:
                self.state = "blocked"
                self.last_error = "submit_enabled 为 false，自动预约未执行"
                logger.warning(self.last_error)
                return
            await self._wait_for_trigger()
            while True:
                if self.mode == "daily" and self.next_trigger_at and (
                    library.beijing_now().replace(tzinfo=CHINA_TZ).date() > datetime.fromisoformat(self.next_trigger_at).date()
                ):
                    self.state = "expired"
                    self.last_error = "当天预约窗口已结束，等待下次每日任务"
                    return
                self.attempts += 1
                self.last_attempt_at = library.beijing_now().replace(tzinfo=CHINA_TZ).isoformat(timespec="seconds")
                self.state = "attempting"
                self._checkpoint()
                try:
                    if await self._attempt_once():
                        self.state = "success"
                        self.last_error = None
                        return
                except asyncio.CancelledError:
                    raise
                except AuthenticationRequiredError as exc:
                    self.state = "auth_required"
                    self.last_error = str(exc)
                    logger.error("automatic reservation stopped: %s", self.last_error)
                    return
                except SessionOwnershipError as exc:
                    self.state = "error"
                    self.last_error = str(exc)
                    # Retrying would just repeat the mismatch with the same
                    # wrong session; this needs a human to re-arm the task.
                    logger.error("automatic reservation stopped — session ownership mismatch: %s", self.last_error)
                    return
                except AwaitingLibraryError as exc:
                    self.state = "waiting_library"
                    self.last_error = str(exc)
                    logger.info("automatic reservation waiting for library: %s", self.last_error)
                except Exception as exc:
                    self.last_error = str(exc)
                    # Business-rule refusals are terminal; temporary library
                    # unavailability is handled separately above.
                    _TERMINAL = (*ACCOUNT_LEVEL_REFUSALS, "已预约")
                    if any(kw in self.last_error for kw in _TERMINAL):
                        self.state = "failed"
                        logger.error("automatic reservation stopped: %s", self.last_error)
                        return
                    self.state = "retry_wait"
                    logger.warning("automatic reservation attempt %s failed: %s", self.attempts, self.last_error)
                self._checkpoint()
                await self._sleep_in_chunks(max(5, settings.auto_retry_interval_seconds))
        except asyncio.CancelledError:
            self.state = "stopped"
            raise
        except Exception as exc:
            self.state = "error"
            self.last_error = str(exc)
            logger.exception("automatic reservation stopped unexpectedly")
        finally:
            self._finish()

    def status(self) -> dict[str, Any]:
        settings = self.run_settings or self.settings
        return {
            "task_id": self.task_id,
            "mode": self.mode,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "reserved_seat": self.reserved_seat,
            "room_id": settings.room_id,
            "room_name": settings.room_name,
            "seat_names": list(settings.seat_names),
            "start_time": settings.start_time,
            "end_time": settings.end_time,
            "running": self.running,
            "state": self.state,
            "session_id": self.session_id,
            "attempts": self.attempts,
            "last_error": self.last_error,
            "last_attempt_at": self.last_attempt_at,
            "next_trigger_at": self.next_trigger_at,
            "target_date": self.target_date.isoformat() if self.target_date else None,
            "trigger_time": settings.auto_trigger_time,
            "retry_interval_seconds": settings.auto_retry_interval_seconds,
        }

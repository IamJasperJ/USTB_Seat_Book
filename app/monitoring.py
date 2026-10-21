"""Read-only reservation checks, with bounded queries and no login navigation."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from typing import Any
from zoneinfo import ZoneInfo

from .library import SeatApi

CHINA = ZoneInfo("Asia/Shanghai")


def reservation_time(value) -> datetime:
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000 if value > 100_000_000_000 else value, CHINA)
    result = datetime.fromisoformat(str(value).replace("/", "-").replace("Z", "+00:00"))
    return result.replace(tzinfo=CHINA) if result.tzinfo is None else result.astimezone(CHINA)


def reservation_state(status: int) -> str:
    # Official profile frontend: chunk-45cf0db3_1751607848958.js, statusOption.
    # Higher-priority terminal/pending bits override the historical success bit.
    if status & 16:
        return "violation"
    if status & 128:
        return "finished"
    if status & 512 and not status & 1024:
        return "rejected"
    if status & 8192 or (status & 256 and not status & 1024) or status & 8:
        return "pending"
    if status & (1 | 2 | 4 | 64 | 1024):
        return "booked"
    return "unknown"


class ReservationMonitor:
    def __init__(self, auth, ttl: float = 30, timeout: float = 20):
        self.auth = auth
        self.ttl = ttl
        self.timeout = timeout
        self._cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def clear(self, account_id: str) -> None:
        for key in list(self._cache):
            if key[0] == account_id:
                self._cache.pop(key, None)

    async def day(self, account, settings, target) -> dict[str, Any]:
        key = (account.id, target.isoformat())
        self._locks.setdefault(account.id, asyncio.Lock())
        # Both lock waiting and remote requests are bounded. Concurrent pollers
        # share the resulting snapshot rather than issuing duplicate queries.
        try:
            async with asyncio.timeout(self.timeout + 2):
                async with self._locks[account.id]:
                    now = time.monotonic()
                    for old_key, (expires, _) in list(self._cache.items()):
                        if expires <= now:
                            self._cache.pop(old_key, None)
                    if key in self._cache:
                        return {**self._cache[key][1], "cached": True}
                    async with asyncio.timeout(self.timeout):
                        result = await self._query(account, settings, target)
                    self._cache[key] = (time.monotonic() + self.ttl, result)
                    return {**result, "cached": False}
        except TimeoutError:
            return self._unknown(target, "query_timeout")

    @staticmethod
    def _unknown(target, reason: str) -> dict[str, Any]:
        return {"target_date": target.isoformat(), "booking_status": "unknown",
                "has_reservation": None, "confirmed": False, "records": [],
                "reason": reason, "checked_at": datetime.now(timezone.utc).isoformat(),
                "cached": False}

    async def _query(self, account, settings, target) -> dict[str, Any]:
        candidates = [s for s in list(self.auth.sessions.values())
                      if s.owner_account_id == account.id and s.status == "authenticated"
                      and isinstance(s.user_info, dict) and s.user_info.get("token")]
        if not candidates:
            return self._unknown(target, "library_login_required")
        session = max(candidates, key=lambda s: s.created_at)
        client = None
        try:
            # Do not call refresh_status: polling must never navigate or reset
            # a user's browser page. The library validates the saved credential.
            user_info = dict(session.user_info)
            cookies = {x["name"]: x["value"] for x in await session.context.cookies()}
            client = SeatApi(settings, cookies, user_info)
            records = []
            complete = False
            for page in range(1, 6):
                result = await client.my_reservations(target, page)
                rows = result.get("data")
                if result.get("code") != 0 or not isinstance(rows, list):
                    return self._unknown(target, "library_response_error")
                for row in rows:
                    if not isinstance(row, dict):
                        return self._unknown(target, "invalid_record")
                    if row.get("classKind") is not None and int(row["classKind"]) != 8:
                        continue
                    if row.get("appAccNo") is not None and user_info.get("accNo") is not None:
                        if str(row["appAccNo"]) != str(user_info["accNo"]):
                            return self._unknown(target, "reservation_owner_mismatch")
                    elif row.get("logonName") and user_info.get("logonName"):
                        if row["logonName"] != user_info["logonName"]:
                            return self._unknown(target, "reservation_owner_mismatch")
                    begin, end = reservation_time(row.get("resvBeginTime")), reservation_time(row.get("resvEndTime"))
                    if begin.date() != target:
                        return self._unknown(target, "reservation_date_mismatch")
                    status = int(row.get("resvStatus") or 0)
                    devices = [{"name": d.get("devName"), "room": d.get("roomName")}
                               for d in (row.get("resvDevInfoList") or []) if isinstance(d, dict)]
                    devices.sort(key=lambda d: (str(d["name"] or ""), str(d["room"] or "")))
                    identity = json.dumps([account.id, target.isoformat(), row.get("resvId"),
                                           begin.isoformat(), end.isoformat(), devices], sort_keys=True)
                    records.append({"event_id": hashlib.sha256(identity.encode()).hexdigest(),
                                    "begin_time": begin.isoformat(), "end_time": end.isoformat(),
                                    "status": status, "booking_state": reservation_state(status),
                                    "devices": devices})
                count = result.get("count")
                if count is not None and (int(count) < 0 or (len(rows) < 20 and int(count) > (page - 1) * 20 + len(rows))):
                    return self._unknown(target, "inconsistent_pagination")
                complete = not rows or (count is not None and page * 20 >= int(count)) or (count is None and len(rows) < 20)
                if complete or any(r["booking_state"] == "booked" for r in records):
                    break
            booked = any(r["booking_state"] == "booked" for r in records)
            if not booked and (not complete or any(r["booking_state"] == "unknown" for r in records)):
                return self._unknown(target, "incomplete_or_unrecognized_records")
            pending = any(r["booking_state"] == "pending" for r in records)
            return {"target_date": target.isoformat(),
                    "booking_status": "booked" if booked else "pending" if pending else "not_booked",
                    "has_reservation": booked, "confirmed": True, "records": records,
                    "reason": None, "complete": complete,
                    "checked_at": datetime.now(timezone.utc).isoformat()}
        except RuntimeError:
            return self._unknown(target, "library_login_expired")
        except Exception:
            # Never return exception text, URLs, cookies or credentials.
            return self._unknown(target, "library_unavailable")
        finally:
            if client is not None:
                try:
                    await asyncio.wait_for(client.close(), timeout=2)
                except Exception:
                    pass

    async def snapshot(self, account, settings, day: str) -> dict[str, Any]:
        today = datetime.now(CHINA).date()
        selected = {"today": today, "tomorrow": today + timedelta(days=1)}
        days = {}
        for name, target in selected.items():
            if day == "both" or day == name:
                days[name] = await self.day(account, settings, target)
        values = [item["has_reservation"] for item in days.values()]
        return {"account": account.username, "timezone": "Asia/Shanghai", "day": day,
                "has_reservation": True if True in values else None if None in values else False,
                "days": days, "cache_seconds": self.ttl,
                "served_at": datetime.now(timezone.utc).isoformat()}

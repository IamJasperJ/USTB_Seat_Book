"""Scripted-control API: everything the web console can do, addressable by API.

Adds a single status overview, per-seat time slots and an
explicit "book this seat at this time" endpoint. All library calls reuse
:class:`app.library.SeatApi`; no new library endpoints are introduced.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from .accounts import Account
from .dependencies import current_account
from . import library
from .library import _parse_moment, build_room_seat_view, busy_ranges, is_valid_time_range

router = APIRouter()
SHANGHAI = ZoneInfo("Asia/Shanghai")
OVERVIEW_TIMEOUT_SECONDS = 20


def _today() -> date:
    return library.beijing_now().date()


def resolve_date(value: str | None, *, max_days_ahead: int) -> date:
    """Accept today / tomorrow / dayafter / YYYY-MM-DD, within [today, today+max_days_ahead]."""
    today = _today()
    raw = (value or "tomorrow").strip().lower()
    aliases = {"today": 0, "tomorrow": 1, "dayafter": 2}
    if raw in aliases:
        target = today + timedelta(days=aliases[raw])
    else:
        try:
            target = datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=422, detail="日期格式应为 today、tomorrow、dayafter 或 YYYY-MM-DD")
    if not today <= target <= today + timedelta(days=max_days_ahead):
        raise HTTPException(status_code=422, detail=f"日期须在今天到 {max_days_ahead} 天后之间（北京时间）")
    return target


# ---------------------------------------------------------------- overview

def _pick_session(account: Account, runtime: Any, session_id: str | None):
    from . import main

    own = [s for s in main.auth.sessions.values() if s.owner_account_id == account.id]
    if session_id:
        return next((s for s in own if s.session_id == session_id), None)
    authed = [s for s in own if s.status == "authenticated"]
    bound = runtime.automation.session_id if runtime.automation.running else None
    return next((s for s in authed if s.session_id == bound), None) or (
        max(authed, key=lambda s: s.created_at) if authed else None)


@router.get("/api/overview")
async def overview(
    auth_session_id: str | None = Query(default=None),
    include_reservations: bool = Query(default=True),
    account: Account = Depends(current_account),
) -> dict[str, Any]:
    """One call for everything a script needs to decide what to do next."""
    from . import main

    runtime = await main.ensure_runtime(account)
    settings = runtime.settings
    session = _pick_session(account, runtime, auth_session_id)
    if auth_session_id and session is None:
        raise HTTPException(status_code=404, detail="auth session not found")
    result: dict[str, Any] = {
        "account": {"username": account.username, "is_admin": account.is_admin},
        "now": library.beijing_now().replace(tzinfo=SHANGHAI).isoformat(),
        "config": {
            "room_id": settings.room_id, "room_name": settings.room_name,
            "seat_names": settings.seat_names, "target_date": settings.target_date,
            "start_time": settings.start_time, "end_time": settings.end_time,
            "submit_enabled": settings.submit_enabled, "keepalive_enabled": settings.keepalive_enabled,
            "auto_trigger_time": settings.auto_trigger_time,
        },
        "sessions": main.describe_sessions(account.id, runtime),
        "automation": runtime.automation.status(),
        "daily_automation": runtime.daily.status(),
        "session": None,
        "ready": False,
        "library": {"confirmed": False, "status": "unknown", "reason": "library_login_required"},
    }
    if session is None:
        result["hint"] = "没有已登录的会话，请先 POST /api/auth/sessions 并扫码"
        return result
    user = session.user_info or {}
    result["session"] = {
        "session_id": session.session_id,
        "status": session.status,
        "sso_authenticated": session.sso_authenticated,
        "sso_last_valid_at": session.sso_last_valid_at,
        "error": session.error,
        "library_error": session.library_error,
        "keepalive_running": bool(session.keepalive_task and not session.keepalive_task.done()),
        "user": {"acc_no": user.get("accNo"), "user_name": user.get("userName") or user.get("name")}
        if session.status == "authenticated" else None,
    }
    if session.status != "authenticated" or not session.user_info:
        result["hint"] = "图书馆会话尚未建立，请先完成扫码登录"
        return result
    client = None
    reservations: dict[str, Any] = {}
    today = _today()
    label, day = "today", today
    try:
        async with asyncio.timeout(OVERVIEW_TIMEOUT_SECONDS):
            client = await main.authenticated_api(session.session_id, account)
            days = (("today", today), ("tomorrow", today + timedelta(days=1))) if include_reservations else (("today", today),)
            for label, day in days:
                # Even a lightweight overview must validate the credential
                # against the server, not trust a cached authenticated flag.
                raw = await client.my_reservations(day)
                records = main._reservation_records(raw, client.user_info)
                if include_reservations:
                    reservations[label] = {"date": day.isoformat(), "records": records}
        result["ready"] = True
        result["library"] = {"confirmed": True, "status": "verified", "reason": None}
    except (RuntimeError, HTTPException) as exc:
        reason = "library_login_expired" if isinstance(exc, RuntimeError) or exc.status_code == 401 else "library_response_error"
        result["library"] = {"confirmed": False, "status": "unknown", "reason": reason}
    except TimeoutError:
        result["library"] = {"confirmed": False, "status": "unknown", "reason": "query_timeout"}
    except Exception:
        # Do not expose exception URLs, tokens, or cookie values to callers.
        result["library"] = {"confirmed": False, "status": "unknown", "reason": "library_unavailable"}
    finally:
        if client is not None:
            try:
                await asyncio.wait_for(client.close(), timeout=2)
            except Exception:
                main.logger.warning("Overview request client cleanup failed")
    result["library"]["checked_at"] = library.beijing_now().replace(tzinfo=SHANGHAI).isoformat()
    if not result["ready"]:
        result["hint"] = "图书馆凭证失效或服务暂不可用，请查看 library.reason"
        if include_reservations:
            reservations[label] = {"date": day.isoformat(), "records": None, "error": result["library"]["reason"]}
    if include_reservations:
        result["reservations"] = reservations
    return result


# ---------------------------------------------------------------- seats & slots

def _find_seat(seats: list[dict[str, Any]], seat: str) -> dict[str, Any] | None:
    for item in seats:
        if str(item.get("dev_id")) == seat or item.get("dev_name") == seat:
            return item
    return None


def free_time_options(seat: dict[str, Any], day: date) -> list[dict[str, Any]] | None:
    """time_options minus anything overlapping an existing reservation (None if unknown)."""
    busy = busy_ranges(seat, day)
    if busy is None:
        return None
    result = []
    for option in seat.get("time_options") or []:
        start = datetime.combine(day, datetime.strptime(option["start"], "%H:%M").time())
        ends = [e for e in option["ends"]
                if not any(start < b_end and datetime.combine(day, datetime.strptime(e, "%H:%M").time()) > b_begin
                           for b_begin, b_end in busy)]
        if ends:
            result.append({"start": option["start"], "ends": ends})
    return result


def seat_bookable(seat: dict[str, Any]) -> bool:
    return seat.get("open_state") == 1 and seat.get("dev_status") == 0


@router.get("/api/rooms/{room_id}/availability")
async def room_availability(
    room_id: int,
    auth_session_id: str = Query(...),
    date_: str | None = Query(default=None, alias="date"),
    only_free: bool = Query(default=False),
    account: Account = Depends(current_account),
) -> dict[str, Any]:
    """All seats of a room on a chosen date, with free time slots per seat."""
    from . import main

    target = resolve_date(date_, max_days_ahead=2)
    client = await main.authenticated_api(auth_session_id, account)
    try:
        seats = build_room_seat_view(await client.seats(target, room_id=room_id), day=target)
    finally:
        await client.close()
    rows = []
    for seat in seats:
        free = free_time_options(seat, target) if seat_bookable(seat) else []
        row = {**seat, "bookable": seat_bookable(seat), "free_time_options": free,
               "has_reservations": bool(seat.get("reservations"))}
        if only_free and not (row["bookable"] and (free if free is not None else not row["has_reservations"])):
            continue
        rows.append(row)
    return {"room_id": room_id, "target_date": target.isoformat(), "total": len(seats),
            "listed": len(rows), "seats": rows}


@router.get("/api/rooms/{room_id}/seats/{seat}/slots")
async def seat_slots(
    room_id: int,
    seat: str,
    auth_session_id: str = Query(...),
    date_: str | None = Query(default=None, alias="date"),
    begin: str | None = Query(default=None, pattern=r"^\d{2}:\d{2}$"),
    account: Account = Depends(current_account),
) -> dict[str, Any]:
    """One seat's rules, existing reservations and selectable start/end times.

    With ``begin=HH:MM`` the library's own ``/device/tips`` hint is included as well.
    """
    from . import main

    target = resolve_date(date_, max_days_ahead=2)
    client = await main.authenticated_api(auth_session_id, account)
    try:
        found = _find_seat(build_room_seat_view(await client.seats(target, room_id=room_id), day=target), seat)
        if found is None:
            raise HTTPException(status_code=404, detail="该区域中没有这个座位")
        result = {"room_id": room_id, "target_date": target.isoformat(), "seat": found,
                  "bookable": seat_bookable(found), "free_time_options": free_time_options(found, target)}
        if begin:
            result["tips"] = await client.device_tips(found["dev_id"], f"{target.isoformat()} {begin}:00",
                                                      found.get("reservation_rule"))
        return result
    finally:
        await client.close()


# ---------------------------------------------------------------- reserve

class ReserveRequest(BaseModel):
    auth_session_id: str
    room_id: int | None = None  # defaults to the account's configured room
    seat: str  # devName (e.g. "A101") or devId
    date: str = "tomorrow"
    start: str = Field(pattern=r"^\d{2}:\d{2}$")
    end: str = Field(pattern=r"^\d{2}:\d{2}$")
    dry_run: bool = False
    confirm: bool = False


@router.post("/api/reservations")
async def reserve_seat(request: ReserveRequest, account: Account = Depends(current_account)) -> dict[str, Any]:
    """Book an explicitly chosen seat/time. ``dry_run`` validates without submitting."""
    from . import main

    runtime = await main.ensure_runtime(account)
    if not request.dry_run:
        if not runtime.settings.submit_enabled:
            raise HTTPException(status_code=403, detail="submit_enabled is false")
        if not request.confirm:
            raise HTTPException(status_code=400, detail="confirm must be true")
    target = resolve_date(request.date, max_days_ahead=1)
    if request.start >= request.end:
        raise HTTPException(status_code=422, detail="结束时间必须晚于开始时间")
    room_id = request.room_id if request.room_id is not None else runtime.settings.room_id
    client = await main.authenticated_api(request.auth_session_id, account)
    try:
        found = _find_seat(build_room_seat_view(await client.seats(target, room_id=room_id), day=target), request.seat)
        if found is None:
            raise HTTPException(status_code=404, detail="该区域中没有这个座位")
        if not seat_bookable(found):
            raise HTTPException(status_code=409, detail="seat is not open")
        if not is_valid_time_range(found, request.start, request.end):
            raise HTTPException(status_code=409, detail=(
                f"{request.start}-{request.end} 不在座位实际可选范围内；"
                f"可选开始时间：{[o['start'] for o in found['time_options']]}"))
        busy = busy_ranges(found, target)
        start_dt = datetime.combine(target, datetime.strptime(request.start, "%H:%M").time())
        end_dt = datetime.combine(target, datetime.strptime(request.end, "%H:%M").time())
        if busy is None:
            if found.get("reservations"):
                raise HTTPException(status_code=409, detail="seat already has a reservation")
        elif any(start_dt < b_end and end_dt > b_begin for b_begin, b_end in busy):
            raise HTTPException(status_code=409, detail="所选时段与该座位已有预约冲突")
        begin = f"{target.isoformat()} {request.start}:00"
        end = f"{target.isoformat()} {request.end}:00"
        if request.dry_run:
            return {"dry_run": True, "would_submit": True, "seat": found, "begin": begin, "end": end}
        result = await client.reserve(found["dev_id"], begin, end)
        main.logger.info("explicit reservation request completed: code=%s message=%s",
                         result.get("code"), result.get("message"))
        return {"success": result.get("code") == 0, "code": result.get("code"), "message": result.get("message"),
                "seat": found, "begin": begin, "end": end}
    finally:
        await client.close()

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
import json
import logging
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

from fastapi import Cookie, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse
from pydantic import BaseModel

from .accounts import Account, AccountStore, RegistrationRateLimiter, SessionTokenStore
from .auth import AuthManager, SessionCapacityError
from .automation import AutoReservation
from .daily import DailyReservation
from .config import GlobalSettings, Settings, extract_legacy_personal_settings, load_global_settings
from .library import SeatApi, build_room_seat_view, build_seat_view, is_valid_time_range, normalize_area_nodes
from .runtime import configure_logging, MemoryMonitor


CONFIG_PATH = Path("config.json")
global_settings: GlobalSettings = load_global_settings(CONFIG_PATH)
auth = AuthManager(global_settings)
account_store = AccountStore("logs/accounts.sqlite3")
session_tokens = SessionTokenStore()
register_rate_limiter = RegistrationRateLimiter()
memory_monitor = MemoryMonitor(global_settings, auth)
log_handler = configure_logging()
logger = logging.getLogger("ustb-seat-book")


class UserRuntime:
    """Everything one account owns: its booking preferences and its one task."""

    def __init__(self, account: Account, settings: Settings, auth: AuthManager):
        self.account = account
        self.settings = settings
        self.automation = AutoReservation(
            settings, auth, history_path=f"logs/automation-history/{account.id}.sqlite3",
            account_id=account.id,
        )
        self.daily = DailyReservation(self.automation)


runtimes: dict[str, UserRuntime] = {}


def build_effective_settings(personal: Settings) -> Settings:
    # Global/infra knobs always win over whatever happens to be stored in an
    # account's blob (harmless leftovers from before the fields were split).
    return personal.model_copy(update=global_settings.model_dump())


async def ensure_runtime(account: Account) -> UserRuntime:
    runtime = runtimes.get(account.id)
    if runtime is None:
        personal = account_store.load_settings(account.id)
        runtime = UserRuntime(account, build_effective_settings(personal), auth)
        runtimes[account.id] = runtime
        await runtime.daily.start()
    return runtime


auth.protected_session_ids = lambda: {
    rt.automation.session_id for rt in runtimes.values() if rt.automation.running
}
auth.account_settings_for = lambda account_id: runtimes[account_id].settings if account_id in runtimes else None


async def current_account(session_token: str | None = Cookie(default=None)) -> Account:
    account_id = session_token and session_tokens.resolve(session_token)
    account = account_id and account_store.get(account_id)
    if not account:
        raise HTTPException(status_code=401, detail="请先登录")
    await ensure_runtime(account)
    return account


def set_session_cookie(response: Response, account_id: str) -> None:
    token = session_tokens.issue(account_id)
    response.set_cookie(key="session_token", value=token, httponly=True, samesite="lax", path="/")


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("service starting")
    await auth.start()
    for account_id in account_store.list_ids():
        account = account_store.get(account_id)
        if account:
            await ensure_runtime(account)
    await memory_monitor.start()
    yield
    await memory_monitor.close()
    for runtime in runtimes.values():
        await runtime.daily.close()
        await runtime.automation.stop()
    await auth.close()
    logger.info("service stopped")


app = FastAPI(title="USTB Seat Book", version="0.2.0", lifespan=lifespan)


@app.get("/", include_in_schema=False)
async def web_index() -> FileResponse:
    return FileResponse(Path(__file__).parent / "web" / "index.html")


@app.get("/web/{asset_path:path}", include_in_schema=False)
async def web_asset(asset_path: str) -> FileResponse:
    root = (Path(__file__).parent / "web").resolve()
    target = (root / asset_path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise HTTPException(status_code=404, detail="asset not found")
    return FileResponse(target)


class AccountCredentials(BaseModel):
    username: str
    password: str


@app.post("/api/account/register")
async def register_account(payload: AccountCredentials, request: Request, response: Response) -> dict[str, Any]:
    client_host = request.client.host if request.client else "unknown"
    if not register_rate_limiter.allow(client_host):
        raise HTTPException(status_code=429, detail="注册过于频繁，请稍后再试")
    seed = extract_legacy_personal_settings(CONFIG_PATH) if account_store.count() == 0 else None
    try:
        account = account_store.create(payload.username, payload.password, seed_settings=seed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await ensure_runtime(account)
    set_session_cookie(response, account.id)
    logger.info("account registered: username=%s is_admin=%s", account.username, account.is_admin)
    return {"username": account.username, "is_admin": account.is_admin}


@app.post("/api/account/login")
async def login_account(payload: AccountCredentials, response: Response) -> dict[str, Any]:
    account = account_store.verify(payload.username, payload.password)
    if not account:
        raise HTTPException(status_code=401, detail="用户名或密码不正确")
    await ensure_runtime(account)
    set_session_cookie(response, account.id)
    return {"username": account.username, "is_admin": account.is_admin}


@app.post("/api/account/logout")
async def logout_account(response: Response, session_token: str | None = Cookie(default=None)) -> dict[str, str]:
    if session_token:
        session_tokens.revoke(session_token)
    response.delete_cookie("session_token", path="/")
    return {"status": "logged_out"}


@app.get("/api/account/me")
async def account_me(account: Account = Depends(current_account)) -> dict[str, Any]:
    return {"username": account.username, "is_admin": account.is_admin}


def public_config(settings: Settings) -> dict[str, Any]:
    return settings.model_dump()


class ConfigUpdate(BaseModel):
    login_source: Literal["jwgl", "library"] | None = None
    room_id: int | None = None
    room_name: str | None = None
    target_date: str | None = None
    seat_names: list[str] | None = None
    start_time: str | None = None
    end_time: str | None = None
    poll_interval_seconds: float | None = None
    keepalive_enabled: bool | None = None
    keepalive_interval_seconds: int | None = None
    auto_trigger_time: str | None = None
    auto_retry_interval_seconds: int | None = None
    submit_enabled: bool | None = None


@app.get("/api/config")
async def get_config(account: Account = Depends(current_account)) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    return public_config(runtime.settings)


@app.put("/api/config")
async def update_config(update: ConfigUpdate, account: Account = Depends(current_account)) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    changes = update.model_dump(exclude_none=True)
    try:
        Settings.model_validate({**runtime.settings.model_dump(), **changes})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="配置参数超出允许范围") from exc
    for key, value in changes.items():
        setattr(runtime.settings, key, value)
    account_store.save_settings(account.id, runtime.settings)
    runtime.automation.settings = runtime.settings
    logger.info("configuration updated: account=%s fields=%s", account.username, ", ".join(sorted(changes)))
    return public_config(runtime.settings)


@app.get("/api/logs")
async def get_logs(
    limit: int = Query(default=200, ge=1, le=500),
    account: Account = Depends(current_account),
) -> dict[str, Any]:
    if not account.is_admin:
        raise HTTPException(status_code=403, detail="仅管理员可查看运行日志")
    return {"logs": list(log_handler.records)[-limit:]}


@app.get("/api/status")
async def runtime_status(account: Account = Depends(current_account)) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    own_sessions = [s for s in auth.sessions.values() if s.owner_account_id == account.id]
    payload: dict[str, Any] = {
        "service": "ok",
        "auth_sessions": len(own_sessions),
        "submit_enabled": runtime.settings.submit_enabled,
        "keepalive_enabled": runtime.settings.keepalive_enabled,
        "automation": runtime.automation.status(),
        "daily_automation": runtime.daily.status(),
        "sessions": [
            {
                "session_id": session.session_id,
                "status": session.status,
                "keepalive_running": bool(session.keepalive_task and not session.keepalive_task.done()),
            }
            for session in own_sessions
        ],
    }
    if account.is_admin:
        payload["memory"] = memory_monitor.latest
        payload["max_browser_sessions"] = global_settings.max_browser_sessions
        payload["all_sessions"] = [
            {"session_id": s.session_id, "owner_account_id": s.owner_account_id, "status": s.status}
            for s in auth.sessions.values()
        ]
    return payload


async def get_session(session_id: str, account: Account):
    try:
        session = await auth.get(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="auth session not found")
    if session.owner_account_id != account.id:
        # Same 404 as a missing session: don't reveal another account's session exists.
        raise HTTPException(status_code=404, detail="auth session not found")
    return session


class SubmitRequest(BaseModel):
    auth_session_id: str
    confirm: bool = False


class CancelReservationRequest(BaseModel):
    auth_session_id: str
    uuid: str
    day: Literal["today", "tomorrow"]
    page: int = 1
    confirm: bool = False


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/auth/sessions")
async def create_auth_session(account: Account = Depends(current_account)) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    try:
        session = await auth.create_session(account.id, runtime.settings.login_source)
    except SessionCapacityError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:
        logger.error("SSO login session creation failed: %s", type(exc).__name__)
        raise HTTPException(status_code=502, detail=f"登录入口或浏览器暂不可用（{type(exc).__name__}）") from exc
    if session.status == "error":
        message = session.error or "登录页面无法打开"
        await auth.close_session(session.session_id)
        raise HTTPException(status_code=502, detail=message)
    logger.info("SSO login session created: account=%s", account.username)
    return {
        "session_id": session.session_id,
        "status": session.status,
        "qr_url": f"/api/auth/sessions/{session.session_id}/qr",
        "status_url": f"/api/auth/sessions/{session.session_id}",
    }


@app.get("/api/automation")
async def automation_status(account: Account = Depends(current_account)) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    return {**runtime.automation.status(), "daily": runtime.daily.status()}


@app.get("/api/automation/history")
async def automation_history(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=50),
    account: Account = Depends(current_account),
) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    return runtime.automation.history.list(page, page_size)


class AutomationRequest(BaseModel):
    auth_session_id: str


@app.post("/api/automation/start")
async def start_automation(
    request: AutomationRequest,
    account: Account = Depends(current_account),
) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    if runtime.settings.daily_auto_enabled:
        raise HTTPException(status_code=409, detail="请先关闭每日模式，再启动单次预约")
    session = await get_session(request.auth_session_id, account)
    await auth.refresh_status(session)
    if session.status not in {"authenticated", "sso_authenticated"} or not session.sso_authenticated:
        raise HTTPException(status_code=401, detail="请先完成学校统一登录；若图书馆要求再次扫码，请先完成扫码")
    if session.status == "sso_authenticated" and not runtime.settings.keepalive_enabled:
        raise HTTPException(status_code=409, detail="等待图书馆恢复期间需要开启登录会话保活")
    if not runtime.settings.submit_enabled:
        raise HTTPException(status_code=403, detail="submit_enabled 为 false，请先在参数中开启预约提交")
    try:
        await runtime.automation.start(request.auth_session_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return runtime.automation.status()


@app.post("/api/automation/daily/start")
async def start_daily_automation(
    request: AutomationRequest, account: Account = Depends(current_account),
) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    session = await get_session(request.auth_session_id, account)
    await auth.refresh_status(session)
    if session.status not in {"authenticated", "sso_authenticated"} or not session.sso_authenticated:
        raise HTTPException(status_code=401, detail="请先完成扫码登录")
    if not runtime.settings.submit_enabled or not runtime.settings.keepalive_enabled:
        raise HTTPException(status_code=409, detail="请先开启预约提交和登录保活")
    if runtime.automation.running and runtime.automation.mode != "daily":
        raise HTTPException(status_code=409, detail="请先停止正在运行的单次预约")
    try:
        runtime.automation._trigger_seconds()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    runtime.settings.daily_auto_enabled = True
    account_store.save_settings(account.id, runtime.settings)
    await runtime.daily.tick()
    logger.info("daily reservation enabled: account=%s", account.username)
    return runtime.daily.status()


@app.post("/api/automation/daily/stop")
async def stop_daily_automation(account: Account = Depends(current_account)) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    runtime.settings.daily_auto_enabled = False
    account_store.save_settings(account.id, runtime.settings)
    if runtime.automation.running and runtime.automation.mode == "daily":
        await runtime.automation.stop()
    await runtime.daily.tick()
    logger.info("daily reservation disabled: account=%s", account.username)
    return runtime.daily.status()


@app.post("/api/automation/stop")
async def stop_automation(account: Account = Depends(current_account)) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    if runtime.settings.daily_auto_enabled:
        await stop_daily_automation(account)
    await runtime.automation.stop()
    logger.info("automatic reservation stopped by user: account=%s", account.username)
    return runtime.automation.status()


@app.get("/api/auth/sessions/{session_id}/qr")
async def get_qr(session_id: str, account: Account = Depends(current_account)) -> Response:
    session = await get_session(session_id, account)
    try:
        image = await auth.qr(session)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return Response(content=image, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.get("/api/auth/sessions/{session_id}")
async def auth_status(session_id: str, account: Account = Depends(current_account)) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    session = await get_session(session_id, account)
    await auth.refresh_status(session)
    output: dict[str, Any] = {
        "session_id": session.session_id,
        "status": session.status,
        "error": session.error,
        "sso_authenticated": session.sso_authenticated,
        "sso_last_valid_at": session.sso_last_valid_at,
        "library_error": session.library_error,
        "keepalive_enabled": runtime.settings.keepalive_enabled,
        "keepalive_running": bool(session.keepalive_task and not session.keepalive_task.done()),
    }
    if session.status == "authenticated" and session.user_info:
        output["user"] = {
            "acc_no": session.user_info.get("accNo"),
            "user_name": session.user_info.get("userName") or session.user_info.get("name"),
        }
    return output


@app.delete("/api/auth/sessions/{session_id}")
async def delete_auth_session(session_id: str, account: Account = Depends(current_account)) -> dict[str, str]:
    await get_session(session_id, account)
    await auth.close_session(session_id)
    logger.info("SSO login session closed: account=%s", account.username)
    return {"status": "closed"}


async def authenticated_api(session_id: str, account: Account) -> SeatApi:
    session = await get_session(session_id, account)
    try:
        cookies, user_info = await auth.credentials(session)
    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    runtime = await ensure_runtime(account)
    return SeatApi(runtime.settings, cookies, user_info)


def _reservation_day(day: Literal["today", "tomorrow"]) -> date:
    today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    return today if day == "today" else today + timedelta(days=1)


def _reservation_records(data: dict[str, Any], user_info: dict[str, Any]) -> list[dict[str, Any]]:
    if data.get("code") != 0:
        raise HTTPException(status_code=502, detail=data.get("message") or "查询预约失败")
    rows = data.get("data")
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="图书馆返回的预约列表格式不正确")
    records = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        devices = row.get("resvDevInfoList") or []
        if not isinstance(devices, list):
            devices = []
        own = True
        if row.get("appAccNo") is not None and user_info.get("accNo") is not None:
            own = str(row["appAccNo"]) == str(user_info["accNo"])
        elif row.get("logonName") and user_info.get("logonName"):
            own = row["logonName"] == user_info["logonName"]
        status = int(row.get("resvStatus") or 0)
        records.append({
            "uuid": str(row.get("uuid") or ""),
            "resv_id": row.get("resvId"),
            "begin_time": row.get("resvBeginTime"),
            "end_time": row.get("resvEndTime"),
            "status": status,
            "cancellable": own and bool(row.get("uuid")) and (status & 4) == 0 and (status & 128) == 0,
            "devices": [
                {"name": item.get("devName"), "room": item.get("roomName")}
                for item in devices if isinstance(item, dict)
            ],
        })
    return records


@app.get("/api/reservations/mine")
async def my_reservations(
    auth_session_id: str = Query(...),
    day: Literal["today", "tomorrow"] = Query(default="tomorrow"),
    page: int = Query(default=1, ge=1),
    account: Account = Depends(current_account),
) -> dict[str, Any]:
    target = _reservation_day(day)
    client = await authenticated_api(auth_session_id, account)
    try:
        result = await client.my_reservations(target, page)
        records = _reservation_records(result, client.user_info)
        return {"records": records, "count": result.get("count", len(records)),
                "page": page, "page_size": 20, "day": day, "target_date": target.isoformat()}
    finally:
        await client.close()


@app.post("/api/reservations/cancel")
async def cancel_reservation(
    request: CancelReservationRequest,
    account: Account = Depends(current_account),
) -> dict[str, Any]:
    if not request.confirm:
        raise HTTPException(status_code=400, detail="请明确确认取消预约")
    if request.page < 1 or not request.uuid.strip():
        raise HTTPException(status_code=422, detail="预约标识或页码无效")
    target = _reservation_day(request.day)
    client = await authenticated_api(request.auth_session_id, account)
    try:
        listed = await client.my_reservations(target, request.page)
        record = next((item for item in _reservation_records(listed, client.user_info)
                       if item["uuid"] == request.uuid), None)
        if record is None:
            raise HTTPException(status_code=404, detail="当前账号的查询结果中未找到这条预约，请刷新列表")
        if not record["cancellable"]:
            raise HTTPException(status_code=409, detail="这条预约当前不能取消")
        result = await client.cancel_reservation(request.uuid)
        if result.get("code") != 0:
            raise HTTPException(status_code=409, detail=result.get("message") or "取消预约失败")
        logger.info("reservation cancelled: resv_id=%s", record["resv_id"])
        return {"success": True, "message": result.get("message") or "取消预约成功"}
    finally:
        await client.close()


@app.get("/api/seats")
async def seats(
    auth_session_id: str = Query(...),
    account: Account = Depends(current_account),
) -> dict[str, Any]:
    client = await authenticated_api(auth_session_id, account)
    try:
        settings = client.settings
        target = client.target_date()
        raw = await client.seats(target)
        logger.info("seat query completed for %s", target.isoformat())
        seat_views = build_seat_view(raw, settings.seat_names)
        return {
            "target_date": target.isoformat(),
            "room_id": settings.room_id,
            "room_name": settings.room_name,
            "configured_seats": settings.seat_names,
            "seats": seat_views,
            "time_options": seat_views[0].get("time_options", []) if seat_views else [],
        }
    finally:
        await client.close()


@app.get("/api/areas")
async def areas(
    auth_session_id: str = Query(...),
    account: Account = Depends(current_account),
) -> dict[str, Any]:
    client = await authenticated_api(auth_session_id, account)
    try:
        raw = await client.areas()
        normalized = normalize_area_nodes(raw.get("data"))
        logger.info("area query completed: %s areas", len(normalized))
        return {"areas": normalized}
    finally:
        await client.close()


@app.get("/api/rooms/{room_id}/seats")
async def room_seats(
    room_id: int,
    auth_session_id: str = Query(...),
    account: Account = Depends(current_account),
) -> dict[str, Any]:
    """Every seat in a room, for the config picker — not filtered to configured names,
    so the picker can preview a room before it's saved."""
    client = await authenticated_api(auth_session_id, account)
    try:
        target = client.target_date()
        raw = await client.seats(target, room_id=room_id)
        seats = build_room_seat_view(raw)
        logger.info("room seat query completed: room_id=%s seats=%s", room_id, len(seats))
        return {"room_id": room_id, "target_date": target.isoformat(), "seats": seats}
    finally:
        await client.close()


@app.post("/api/reservations/submit")
async def submit_reservation(
    request: SubmitRequest,
    account: Account = Depends(current_account),
) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    if not runtime.settings.submit_enabled:
        raise HTTPException(status_code=403, detail="submit_enabled is false")
    if not request.confirm:
        raise HTTPException(status_code=400, detail="confirm must be true")

    client = await authenticated_api(request.auth_session_id, account)
    try:
        settings = client.settings
        target = client.target_date()
        raw = await client.seats(target)
        candidates = build_seat_view(raw, settings.seat_names)
        if not candidates:
            raise HTTPException(status_code=409, detail="configured seat not found")
        candidate = candidates[0]
        if candidate["open_state"] != 1 or candidate["dev_status"] != 0:
            raise HTTPException(status_code=409, detail="seat is not open")
        if candidate["reservations"]:
            raise HTTPException(status_code=409, detail="seat already has a reservation")
        if not is_valid_time_range(candidate, settings.start_time, settings.end_time):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"当前预约时间 {settings.start_time}-{settings.end_time} 不在座位 "
                    "实际可选范围内，请先查询座位并重新选择时间；"
                    f"可选开始时间：{[option['start'] for option in candidate['time_options']]}"
                ),
            )

        begin = f"{target.isoformat()} {settings.start_time}:00"
        end = f"{target.isoformat()} {settings.end_time}:00"
        result = await client.reserve(candidate["dev_id"], begin, end)
        logger.info("reservation request completed: code=%s message=%s", result.get("code"), result.get("message"))
        return {
            "success": result.get("code") == 0,
            "code": result.get("code"),
            "message": result.get("message"),
            "seat": candidate,
            "begin": begin,
            "end": end,
        }
    finally:
        await client.close()

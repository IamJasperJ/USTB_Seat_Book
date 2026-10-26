from __future__ import annotations

import asyncio
import base64
import binascii
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
import httpx
from pydantic import BaseModel, Field

from .accounts import Account, AccountStore, RegistrationRateLimiter, SessionTokenStore
from .auth import AuthManager, SessionCapacityError
from .auto_extend import AutoExtend
from .automation import AutoReservation
from .daily import DailyReservation
from .config import GlobalSettings, Settings, extract_legacy_personal_settings, load_global_settings
from . import library
from .library import SeatApi, build_room_seat_view, build_seat_view, is_valid_time_range, normalize_area_nodes
from .runtime import configure_logging, MemoryMonitor
from .health import BrowserHealthMonitor
from .monitoring import ReservationMonitor


CONFIG_PATH = Path("config.json")
global_settings: GlobalSettings = load_global_settings(CONFIG_PATH)
auth = AuthManager(global_settings)
account_store = AccountStore("logs/accounts.sqlite3")
session_tokens = SessionTokenStore()
register_rate_limiter = RegistrationRateLimiter()
monitor_auth_rate_limiter = RegistrationRateLimiter(max_per_window=30, window_seconds=60)
memory_monitor = MemoryMonitor(global_settings, auth)
browser_health = BrowserHealthMonitor(auth)
reservation_monitor = ReservationMonitor(auth)
log_handler = configure_logging()
logger = logging.getLogger("ustb-seat-book")


class UserRuntime:
    """Everything one account owns: its booking preferences and its one task."""

    def __init__(self, account: Account, settings: Settings, auth: AuthManager):
        self.account = account
        self.settings = settings
        self.operation_lock = asyncio.Lock()
        self.automation = AutoReservation(
            settings, auth, history_path=f"logs/automation-history/{account.id}.sqlite3",
            account_id=account.id,
        )
        self.daily = DailyReservation(self.automation)
        self.auto_extend = AutoExtend(self)


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
        await runtime.auto_extend.start()
    return runtime


auth.protected_session_ids = lambda: {
    rt.automation.session_id for rt in runtimes.values() if rt.automation.running
}
auth.account_settings_for = lambda account_id: runtimes[account_id].settings if account_id in runtimes else None


def rebind_superseded_session(old_session_id: str, new_session) -> None:
    """A re-login replaces the account's old session; keep its task running on the new one."""
    runtime = runtimes.get(new_session.owner_account_id)
    if runtime and runtime.automation.running and runtime.automation.session_id == old_session_id:
        runtime.automation.session_id = new_session.session_id
        logger.info("automation moved to newer login: account=%s", runtime.account.username)


auth.on_session_superseded = rebind_superseded_session


async def verify_password_account(username: str, password: str, request: Request | None = None) -> Account:
    """Bound password work before moving PBKDF2 off the event loop."""
    host = request.client.host if request is not None and request.client else "direct"
    if not monitor_auth_rate_limiter.allow(host):
        raise HTTPException(status_code=429, detail="账号密码认证过于频繁，请稍后再试",
                            headers={"Retry-After": "60", "Cache-Control": "no-store"})
    account = await asyncio.to_thread(account_store.verify, username, password)
    if not account:
        raise HTTPException(status_code=401, detail="用户名或密码不正确",
                            headers={"Cache-Control": "no-store"})
    return account


async def current_account(
    session_token: str | None = Cookie(default=None),
    authorization: str | None = Header(default=None),
    request: Request = None,
) -> Account:
    account_id = session_token and session_tokens.resolve(session_token)
    if not account_id and isinstance(authorization, str):
        # Scripted use: HTTP Basic with the account's own username/password,
        # checked per request; no server-side login session is created.
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "basic" and value.strip():
            try:
                username, sep, password = base64.b64decode(value.strip(), validate=True).decode("utf-8").partition(":")
            except (binascii.Error, UnicodeDecodeError):
                sep = ""
            verified = await verify_password_account(username, password, request) if sep else None
            account_id = verified.id if verified else None
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
    await browser_health.start()
    for account_id in account_store.list_ids():
        account = account_store.get(account_id)
        if account:
            await ensure_runtime(account)
    await memory_monitor.start()
    yield
    await browser_health.close()
    await memory_monitor.close()
    for runtime in runtimes.values():
        await runtime.daily.close()
        await runtime.auto_extend.close()
        await runtime.automation.stop()
    await auth.close()
    logger.info("service stopped")


app = FastAPI(title="USTB Seat Book", version="0.2.0", lifespan=lifespan)


@app.exception_handler(httpx.HTTPError)
async def library_request_failed(_: Request, exc: httpx.HTTPError) -> JSONResponse:
    """An upstream library failure (HTTP error, timeout, refused) is a 502, not our own 500."""
    status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else None
    logger.warning("library request failed: %s %s", type(exc).__name__, status or "")
    detail = f"图书馆接口请求失败（HTTP {status}）" if status else "图书馆接口暂时无法访问，请稍后重试"
    return JSONResponse(status_code=502, content={"detail": detail})


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
    poll_interval_seconds: float | None = Field(default=None, ge=5, le=86400, allow_inf_nan=False)
    keepalive_enabled: bool | None = None
    keepalive_interval_seconds: int | None = Field(default=None, ge=30, le=86400)
    auto_trigger_time: str | None = None
    auto_retry_interval_seconds: int | None = Field(default=None, ge=5, le=86400)
    submit_enabled: bool | None = None
    auto_extend_enabled: bool | None = None
    auto_extend_after: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    auto_extend_interval_minutes: int | None = Field(default=None, ge=1, le=60)
    auto_extend_checkin_minutes: int | None = Field(default=None, ge=5, le=120)
    auto_extend_temp_leave_minutes: int | None = Field(default=None, ge=5, le=120)


def check_auto_extend_interval(settings: Settings) -> None:
    # A check less often than the threshold could skip the whole window.
    if settings.auto_extend_interval_minutes >= min(settings.auto_extend_checkin_minutes,
                                                    settings.auto_extend_temp_leave_minutes):
        raise HTTPException(status_code=422, detail="自动延后的检查间隔必须小于签到和暂离的提前分钟数")


@app.get("/api/config")
async def get_config(account: Account = Depends(current_account)) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    return public_config(runtime.settings)


@app.put("/api/config")
async def update_config(update: ConfigUpdate, account: Account = Depends(current_account)) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    changes = update.model_dump(exclude_none=True)
    try:
        merged = Settings.model_validate({**runtime.settings.model_dump(), **changes})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="配置参数超出允许范围") from exc
    check_auto_extend_interval(merged)
    for key, value in changes.items():
        setattr(runtime.settings, key, value)
    account_store.save_settings(account.id, runtime.settings)
    runtime.automation.settings = runtime.settings
    logger.info("configuration updated: account=%s fields=%s", account.username, ", ".join(sorted(changes)))
    return public_config(runtime.settings)


class AdminSettingsUpdate(ConfigUpdate):
    daily_auto_enabled: bool | None = None


class PasswordReset(BaseModel):
    password: str


class RoleUpdate(BaseModel):
    is_admin: bool


async def require_admin(account: Account = Depends(current_account)) -> Account:
    if not account.is_admin:
        raise HTTPException(status_code=403, detail="仅管理员可使用")
    return account


def audit(admin: Account, action: str, target: str, detail: str = "") -> None:
    logger.info("admin action: admin=%s action=%s target=%s %s", admin.username, action, target, detail)


async def managed_user(user_id: str) -> tuple[Account, "UserRuntime"]:
    target = account_store.get(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    return target, await ensure_runtime(target)


def validate_setting_changes(changes: dict[str, Any]) -> None:
    """Only the fields being changed: they're parsed later (at trigger time),
    so a typo saved here would otherwise surface hours afterwards."""
    def clock(value: str, formats: tuple[str, ...], label: str) -> None:
        # strptime alone accepts "7:5"; the zero-padded shape is what the rest of the app parses.
        if re.fullmatch(r"\d{2}:\d{2}(:\d{2})?", value):
            for fmt in formats:
                try:
                    datetime.strptime(value, fmt)
                    return
                except ValueError:
                    continue
        raise HTTPException(status_code=422, detail=f"{label}格式不正确")

    for key, label in (("start_time", "开始时间"), ("end_time", "结束时间")):
        if key in changes:
            clock(changes[key], ("%H:%M",), label)
    if "auto_trigger_time" in changes:
        clock(changes["auto_trigger_time"], ("%H:%M", "%H:%M:%S"), "触发时间")
    if "target_date" in changes and changes["target_date"].lower() not in {"today", "tomorrow"}:
        try:
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", changes["target_date"]):
                raise ValueError
            date.fromisoformat(changes["target_date"])
        except ValueError:
            raise HTTPException(status_code=422, detail="目标日期应为 today、tomorrow 或 YYYY-MM-DD")
    if "seat_names" in changes and not [n for n in changes["seat_names"] if n.strip()]:
        raise HTTPException(status_code=422, detail="候选座位不能为空")


def editable_settings(settings: Settings) -> dict[str, Any]:
    keys = AdminSettingsUpdate.model_fields.keys()
    return {k: v for k, v in settings.model_dump().items() if k in keys}


@app.get("/api/admin/users")
async def admin_list_users(admin: Account = Depends(require_admin)) -> dict[str, Any]:
    users = []
    for row in account_store.list_accounts():
        _, runtime = await managed_user(row["id"])
        sessions = describe_sessions(row["id"], runtime)
        task = runtime.automation.status()
        users.append({
            **row,
            "daily_enabled": runtime.settings.daily_auto_enabled,
            "submit_enabled": runtime.settings.submit_enabled,
            "task_running": task["running"],
            "task_state": task["state"],
            "task_mode": task["mode"],
            "sessions": len(sessions),
            "logged_in": any(s["status"] in {"authenticated", "sso_authenticated"} for s in sessions),
        })
    return {"users": users, "you": admin.id}


@app.get("/api/admin/users/{user_id}")
async def admin_user_detail(user_id: str, admin: Account = Depends(require_admin)) -> dict[str, Any]:
    target, runtime = await managed_user(user_id)
    task = runtime.automation.status()
    task.pop("session_id", None)
    row = next(r for r in account_store.list_accounts() if r["id"] == user_id)
    return {
        "user": row,
        "settings": editable_settings(runtime.settings),
        "automation": task,
        "daily": runtime.daily.status(),
        "sessions": describe_sessions(user_id, runtime),
    }


@app.get("/api/admin/users/{user_id}/history")
async def admin_user_history(
    user_id: str,
    page: int = Query(default=1, ge=1, le=1_000_000),
    page_size: int = Query(default=10, ge=1, le=50),
    admin: Account = Depends(require_admin),
) -> dict[str, Any]:
    _, runtime = await managed_user(user_id)
    result = runtime.automation.history.list(page, page_size)
    for record in result["records"]:
        record.pop("session_id", None)
    return result


@app.put("/api/admin/users/{user_id}/settings")
async def admin_update_settings(
    user_id: str, update: AdminSettingsUpdate, admin: Account = Depends(require_admin),
) -> dict[str, Any]:
    target, runtime = await managed_user(user_id)
    changes = update.model_dump(exclude_none=True)
    if not changes:
        raise HTTPException(status_code=422, detail="没有要修改的内容")
    try:
        merged = Settings.model_validate({**runtime.settings.model_dump(), **changes})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="配置参数超出允许范围") from exc
    check_auto_extend_interval(merged)
    validate_setting_changes(changes)
    for key, value in changes.items():
        setattr(runtime.settings, key, value)
    account_store.save_settings(user_id, runtime.settings)
    runtime.automation.settings = runtime.settings
    if changes.get("daily_auto_enabled") is False and runtime.automation.running and runtime.automation.mode == "daily":
        await runtime.automation.stop()
    # Re-evaluate the daily schedule now (stops a task whose prerequisites were
    # switched off, arms one that was just enabled) instead of at the next 10s
    # tick. A task already running keeps the snapshot it started with.
    await runtime.daily.tick()
    audit(admin, "update_settings", target.username, "fields=" + ",".join(sorted(changes)))
    return {"settings": editable_settings(runtime.settings), "daily": runtime.daily.status()}


@app.post("/api/admin/users/{user_id}/automation/stop")
async def admin_stop_automation(user_id: str, admin: Account = Depends(require_admin)) -> dict[str, Any]:
    target, runtime = await managed_user(user_id)
    was_running = runtime.automation.running
    daily_was_on = runtime.settings.daily_auto_enabled
    # Turning daily mode off first: otherwise the scheduler would start a
    # replacement task on its next tick and the stop would look like it failed.
    runtime.settings.daily_auto_enabled = False
    account_store.save_settings(user_id, runtime.settings)
    await runtime.automation.stop()
    await runtime.daily.tick()
    audit(admin, "stop_automation", target.username, f"was_running={was_running} daily_was_on={daily_was_on}")
    return {"stopped": was_running, "daily_disabled": daily_was_on}


@app.delete("/api/admin/users/{user_id}/sessions/{session_id}")
async def admin_close_session(
    user_id: str, session_id: str, admin: Account = Depends(require_admin),
) -> dict[str, Any]:
    target, runtime = await managed_user(user_id)
    session = auth.sessions.get(session_id)
    if not session or session.owner_account_id != user_id:
        raise HTTPException(status_code=404, detail="会话不存在")
    stopped = await close_account_session(runtime, session_id)
    audit(admin, "close_session", target.username, f"automation_stopped={stopped}")
    return {"status": "closed", "automation_stopped": stopped}


@app.post("/api/admin/users/{user_id}/password")
async def admin_reset_password(
    user_id: str, body: PasswordReset, admin: Account = Depends(require_admin),
) -> dict[str, Any]:
    target, _ = await managed_user(user_id)
    try:
        account_store.set_password(user_id, body.password)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    signed_out = session_tokens.revoke_account(user_id)
    audit(admin, "reset_password", target.username, f"signed_out_logins={signed_out}")
    return {"status": "ok", "signed_out": signed_out}


@app.put("/api/admin/users/{user_id}/role")
async def admin_set_role(user_id: str, body: RoleUpdate, admin: Account = Depends(require_admin)) -> dict[str, Any]:
    target, _ = await managed_user(user_id)
    if user_id == admin.id:
        raise HTTPException(status_code=409, detail="不能修改自己的角色，避免失去管理权限")
    if target.is_admin and not body.is_admin and account_store.admin_count() <= 1:
        raise HTTPException(status_code=409, detail="至少需要保留一个管理员")
    account_store.set_admin(user_id, body.is_admin)
    audit(admin, "set_role", target.username, f"is_admin={body.is_admin}")
    return {"is_admin": body.is_admin}


@app.delete("/api/admin/users/{user_id}")
async def admin_delete_user(user_id: str, admin: Account = Depends(require_admin)) -> dict[str, Any]:
    target, runtime = await managed_user(user_id)
    if user_id == admin.id:
        raise HTTPException(status_code=409, detail="不能删除自己")
    if target.is_admin and account_store.admin_count() <= 1:
        raise HTTPException(status_code=409, detail="至少需要保留一个管理员")
    await runtime.daily.close()
    await runtime.auto_extend.close()
    await runtime.automation.stop()
    for session in [s for s in auth.sessions.values() if s.owner_account_id == user_id]:
        await auth.close_session(session.session_id)
    runtimes.pop(user_id, None)
    session_tokens.revoke_account(user_id)
    account_store.delete(user_id)
    # Moved aside, not erased: booking history is the only record of what was
    # attempted under this account, and a deletion by mistake stays recoverable.
    history = Path(runtime.automation.history.path)
    archived = None
    if history.exists():
        archive_dir = history.parent / "deleted"
        archive_dir.mkdir(exist_ok=True)
        archived = archive_dir / history.name
        history.replace(archived)
    audit(admin, "delete_user", target.username, f"history_archived={archived}")
    return {"status": "deleted", "history_archived": archived is not None}


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
        "auto_extend": runtime.auto_extend.status(),
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
async def healthz(response: Response) -> dict[str, Any]:
    result = browser_health.summary()
    response.status_code = 200 if result["healthy"] else 503
    response.headers["Cache-Control"] = "no-store"
    return result


@app.get("/api/health")
async def health_status(response: Response, account: Account = Depends(current_account)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    return browser_health.details(account.id, account.is_admin)


@app.get("/api/monitor/status")
async def monitor_status(response: Response, day: Literal["today", "tomorrow", "both"] = Query(default="both"),
                         account: Account = Depends(current_account)) -> dict[str, Any]:
    response.headers["Cache-Control"] = "no-store"
    runtime = await ensure_runtime(account)
    result = await reservation_monitor.snapshot(account, runtime.settings, day)
    task = runtime.automation.status()
    result["automation"] = {key: task.get(key) for key in (
        "state", "running", "mode", "attempts", "target_date", "next_trigger_at", "reserved_seat")}
    result["health"] = browser_health.summary()
    return result


class MonitorCredentials(AccountCredentials):
    day: Literal["today", "tomorrow", "both"] = "both"


@app.post("/api/monitor/status")
async def monitor_status_with_password(payload: MonitorCredentials, request: Request,
                                       response: Response) -> dict[str, Any]:
    """Authenticate this one read-only query; do not create a login session."""
    account = await verify_password_account(payload.username, payload.password, request)
    return await monitor_status(response, payload.day, account)


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
    page: int = Query(default=1, ge=1, le=1_000_000),
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


def describe_sessions(account_id: str, runtime: "UserRuntime") -> list[dict[str, Any]]:
    bound = runtime.automation.session_id if runtime.automation.running else None
    own = sorted(
        (s for s in auth.sessions.values() if s.owner_account_id == account_id),
        key=lambda s: s.created_at, reverse=True,
    )
    rows = []
    for session in own:
        user = session.user_info or {}
        rows.append({
            "session_id": session.session_id,
            "status": session.status,
            "created_at": datetime.fromtimestamp(session.created_at, timezone.utc).isoformat(),
            "user_name": user.get("userName") or user.get("name"),
            "acc_no": user.get("accNo"),
            "used_by_automation": session.session_id == bound,
        })
    return rows


async def close_account_session(runtime: "UserRuntime", session_id: str) -> bool:
    """Close one of an account's sessions; returns whether its automation task was stopped."""
    bound = runtime.automation.running and runtime.automation.session_id == session_id
    # Close before stopping: once the session is gone from auth.sessions, the
    # daily scheduler's next tick can't re-bind the task to it in between.
    await auth.close_session(session_id)
    if bound:
        await runtime.automation.stop()
    return bound


@app.get("/api/auth/sessions")
async def list_auth_sessions(account: Account = Depends(current_account)) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    sessions = describe_sessions(account.id, runtime)
    return {
        "sessions": sessions,
        "capacity": {
            "in_use": len(sessions),
            "max": global_settings.max_sessions_per_account,
            # Only a yes/no about other accounts: enough to explain a 409 that
            # this account's own sessions can't fix, without exposing theirs.
            "server_full": global_settings.max_browser_sessions is not None
            and len(auth.sessions) >= global_settings.max_browser_sessions,
        },
    }


@app.delete("/api/auth/sessions/{session_id}")
async def delete_auth_session(session_id: str, account: Account = Depends(current_account)) -> dict[str, Any]:
    runtime = await ensure_runtime(account)
    await get_session(session_id, account)
    bound = await close_account_session(runtime, session_id)
    logger.info("SSO login session closed: account=%s automation_stopped=%s", account.username, bound)
    return {"status": "closed", "automation_stopped": bound}


async def authenticated_api(session_id: str, account: Account) -> SeatApi:
    session = await get_session(session_id, account)
    try:
        cookies, user_info = await auth.credentials(session)
    except RuntimeError as exc:
        raise HTTPException(status_code=401, detail=str(exc))
    runtime = await ensure_runtime(account)
    return SeatApi(runtime.settings, cookies, user_info)


def _reservation_day(day: Literal["today", "tomorrow"]) -> date:
    today = library.beijing_now().date()
    return today if day == "today" else today + timedelta(days=1)


def _reservation_records(data: dict[str, Any], user_info: dict[str, Any]) -> list[dict[str, Any]]:
    if data.get("code") != 0:
        raise HTTPException(status_code=502, detail=data.get("message") or "查询预约失败")
    rows = data.get("data")
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="图书馆返回的预约列表格式不正确")
    status_labels = {
        1: "预约成功", 2: "未生效", 4: "已生效", 8: "待支付", 16: "违约",
        32: "已支付", 64: "已签到", 128: "已结束", 256: "审核中",
        512: "审核未通过", 1024: "审核通过", 2048: "暂离", 8192: "待同意",
    }
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
        device = devices[0] if devices and isinstance(devices[0], dict) else {}
        cancellable = own and bool(row.get("uuid")) and (status & 4) == 0 and (status & 128) == 0
        end_early_allowed = own and bool(row.get("endEarly")) and (status & 128) == 0
        records.append({
            "uuid": str(row.get("uuid") or ""),
            "resv_id": row.get("resvId"),
            "begin_time": row.get("resvBeginTime"),
            "end_time": row.get("resvEndTime"),
            "status": status,
            "cancellable": cancellable,
            "dev_id": device.get("devId"),
            "room_id": device.get("roomId"),
            "signed_in": bool(status & 64),
            "on_temp_leave": bool(status & 2048),
            "finished": bool(status & 128),
            "status_labels": [label for bit, label in status_labels.items() if status & bit],
            "temp_leave_end_time": row.get("tempLeaveEndTime"),
            # Latest time to sign in at the seat (the site shows it once a reservation is
            # effective but not signed in); signing in itself needs the on-seat QR code.
            "latest_check_in_time": row.get("latestCheckInTime"),
            "needs_check_in": own and bool(status & 4) and not status & (64 | 128 | 8192),
            "end_early_allowed": end_early_allowed,
            "temp_leave_allowed": own and bool(status & 64) and not bool(status & (128 | 2048)),
            "rebook_action": "cancel" if cancellable else "end_early" if end_early_allowed else None,
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
    page: int = Query(default=1, ge=1, le=1_000_000),
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


@app.get("/api/time-rule/default")
async def default_time_rule(
    day: str = Query(default="tomorrow"),
    account: Account = Depends(current_account),
) -> dict[str, Any]:
    if day not in {"today", "tomorrow"}:
        raise HTTPException(status_code=422, detail="日期应为 today 或 tomorrow")
    now = library.beijing_now()
    target = now.date() + timedelta(days=day == "tomorrow")
    return {**library.DEFAULT_TIME_RULE, "day": day, "target_date": target.isoformat(),
            "now": now.isoformat(),
            "time_options": library.build_time_options(library.DEFAULT_TIME_RULE, day=target, now=now)}


@app.get("/api/seats")
async def seats(
    auth_session_id: str = Query(...),
    account: Account = Depends(current_account),
    date_: str | None = Query(default=None, alias="date"),
) -> dict[str, Any]:
    client = await authenticated_api(auth_session_id, account)
    try:
        settings = client.settings
        from .control_api import resolve_date
        target = resolve_date(date_, max_days_ahead=2) if isinstance(date_, str) else client.target_date()
        raw = await client.seats(target)
        logger.info("seat query completed for %s", target.isoformat())
        seat_views = build_seat_view(raw, settings.seat_names, day=target)
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
    date_: str | None = Query(default=None, alias="date"),
) -> dict[str, Any]:
    """Every seat in a room, for the config picker — not filtered to configured names,
    so the picker can preview a room before it's saved."""
    client = await authenticated_api(auth_session_id, account)
    try:
        from .control_api import resolve_date
        target = resolve_date(date_, max_days_ahead=2) if isinstance(date_, str) else client.target_date()
        raw = await client.seats(target, room_id=room_id)
        seats = build_room_seat_view(raw, day=target)
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
        candidates = build_seat_view(raw, settings.seat_names, day=target)
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


# Scripted-control API (status overview, seat availability, explicit seat booking).
# Imported last: the router module reads helpers from this module at call time.
from .control_api import router as control_router  # noqa: E402

app.include_router(control_router)

from .reservation_ops import router as reservation_ops_router  # noqa: E402

app.include_router(reservation_ops_router)

from .auto_extend import router as auto_extend_router  # noqa: E402

app.include_router(auto_extend_router)

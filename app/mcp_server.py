"""MCP endpoint (Streamable HTTP at /mcp) for one account's own data and actions.

Every tool takes the account's ``username``/``password`` as arguments, for clients
whose Authorization header is taken by a gateway's Bearer token. A client that can
send HTTP Basic itself may omit them (the password then never reaches the model).
Each tool calls the app's own HTTP routes in-process as that account, so validation,
permissions, confirmations and error messages are exactly the web console's.
Admin features are not exposed.
"""
from __future__ import annotations

import json
import base64
from typing import Annotated, Any, Literal

from fastapi import HTTPException
import httpx
from mcp.server.mcpserver import Context, Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from mcp_types import ToolAnnotations
from starlette.requests import Request
from pydantic import Field
from starlette.responses import JSONResponse

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
CHANGE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)

mcp = MCPServer(
    name="ustb-seat-book",
    title="USTB 图书馆座位预约",
    instructions=(
        "操作一个账号的北京科技大学图书馆座位预约。每个工具都要带本服务的 username 和 password"
        "（不是学校统一认证密码）；客户端已用 HTTP Basic 请求头认证时可省略。"
        "先用 get_overview 了解状态；需要图书馆登录时用 start_login 取二维码交给用户用微信扫码，"
        "再用 login_status 确认。预约、取消、暂离、重约等会改变真实预约：先 dry_run 预览，"
        "向用户确认后再带 confirm=true 执行。签到只能本人到座位扫码，无法通过工具完成。"
    ),
)
# Stateless JSON responses: every call carries its own credentials. DNS-rebinding
# checks are off because every tool authenticates and the service is reached by IP /
# various host names.
mcp.streamable_http_app(stateless_http=True, json_response=True,
                        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

Username = Annotated[str | None, Field(description="本服务的账号（请求头已用 HTTP Basic 认证时可省略）")]
Password = Annotated[str | None, Field(description="本服务账号的密码，不是学校统一认证密码（请求头已认证时可省略）")]


class RejectBadBasic:
    """Checks HTTP Basic early when a client sends it; anything else passes through.

    Credentials may instead arrive as tool arguments, and a gateway's Bearer token may
    occupy the Authorization header, so a missing or non-Basic header is not an error
    here. Cookies are never used: a browser page must not drive this endpoint with a
    signed-in user's session.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        from . import main

        if scope["type"] == "http":
            request = Request(scope)
            header = request.headers.get("authorization", "")
            if header[:6].lower() == "basic ":
                try:
                    await main.current_account(None, header, request)
                except HTTPException as exc:
                    headers = dict(exc.headers or {})
                    if exc.status_code == 401:
                        headers["WWW-Authenticate"] = 'Basic realm="ustb-seat-book", charset="UTF-8"'
                    response = JSONResponse({"error": exc.detail}, status_code=exc.status_code, headers=headers)
                    return await response(scope, receive, send)
        await self.app(scope, receive, send)


mcp_endpoint = RejectBadBasic(StreamableHTTPASGIApp(mcp.session_manager))


def _auth(ctx: Context, username: str | None, password: str | None) -> str:
    """The Authorization value to act with: tool arguments first, else the client's Basic header."""
    if username and password:
        return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
    header = (ctx.headers or {}).get("authorization", "")
    if header[:6].lower() == "basic ":
        return header
    raise ToolError("需要本服务的账号和密码：请在工具参数中提供 username 和 password")


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return f"HTTP {response.status_code}"
    if isinstance(detail, list):  # FastAPI validation errors
        return "；".join(f"{'.'.join(str(p) for p in item.get('loc', [])[1:])}: {item.get('msg')}" for item in detail)
    return str(detail or f"HTTP {response.status_code}")


async def _call(auth: str, method: str, path: str, *, params: dict | None = None,
                body: dict | None = None, raw: bool = False) -> Any:
    """Call one of the app's own routes as the account behind ``auth``."""
    from . import main

    headers = {"authorization": auth}
    params = {k: v for k, v in (params or {}).items() if v is not None}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://mcp.internal",
                                 headers=headers, timeout=60) as client:
        response = await client.request(method, path, params=params, json=body)
    if response.status_code >= 400:
        raise ToolError(f"{_detail(response)}（HTTP {response.status_code}）")
    return response.content if raw else response.json()


async def _session(auth: str, auth_session_id: str | None) -> str:
    """The given login session, or the account's newest session logged in to the library."""
    if auth_session_id:
        return auth_session_id
    sessions = (await _call(auth, "GET", "/api/auth/sessions"))["sessions"]
    ready = [s for s in sessions if s.get("status") == "authenticated"]
    if not ready:
        raise ToolError("没有已登录图书馆的会话：请先调用 start_login，让用户用微信扫码，再用 login_status 确认")
    return max(ready, key=lambda s: s.get("created_at") or "")["session_id"]


# ---------- Overview & login ----------

@mcp.tool(annotations=READ)
async def get_overview(ctx: Context, include_reservations: bool = True,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """一次查看账号全部状态：配置、登录会话、自动预约/每日模式/自动延后、今明两天预约。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "GET", "/api/overview", params={"include_reservations": include_reservations})


@mcp.tool(annotations=READ)
async def list_login_sessions(ctx: Context,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """列出本账号的学校/图书馆登录会话（每个账号最多 2 个，同时只保留 1 个登录）。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "GET", "/api/auth/sessions")


@mcp.tool(annotations=CHANGE, structured_output=False)  # text + QR image, not JSON
async def start_login(ctx: Context,
        username: Username = None, password: Password = None) -> list[Any]:
    """生成扫码登录二维码。把图片交给用户用微信扫码，随后用 login_status 查询是否登录成功。"""
    auth = _auth(ctx, username, password)
    created = await _call(auth, "POST", "/api/auth/sessions")
    qr = await _call(auth, "GET", f"/api/auth/sessions/{created['session_id']}/qr", raw=True)
    note = {"session_id": created["session_id"], "status": created.get("status"),
            "hint": "请用微信扫描二维码；二维码 5 分钟内有效。"}
    return [json.dumps(note, ensure_ascii=False), Image(data=qr, format="png")]


@mcp.tool(annotations=READ)
async def login_status(ctx: Context, session_id: str,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """查询某个登录会话的状态（authenticated 表示已登录图书馆）。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "GET", f"/api/auth/sessions/{session_id}")


@mcp.tool(annotations=DESTRUCTIVE)
async def close_login_session(ctx: Context, session_id: str,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """关闭一个登录会话。关闭后依赖它的自动预约需要重新扫码。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "DELETE", f"/api/auth/sessions/{session_id}")


# ---------- Configuration ----------

@mcp.tool(annotations=READ)
async def get_config(ctx: Context,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """读取本账号的预约参数（区域、候选座位、时段、触发时间、提交开关、自动延后等）。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "GET", "/api/config")


@mcp.tool(annotations=CHANGE)
async def update_config(ctx: Context, changes: dict[str, Any],
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """修改预约参数，只传要改的字段。

    可用字段：room_id、room_name、target_date（today/tomorrow/YYYY-MM-DD）、seat_names（按顺序尝试的座位名列表）、
    start_time / end_time（HH:MM）、auto_trigger_time（HH:MM:SS）、auto_retry_interval_seconds、
    submit_enabled、keepalive_enabled、keepalive_interval_seconds、poll_interval_seconds、login_source，
    以及自动延后 auto_extend_enabled、auto_extend_after（HH:MM）、auto_extend_interval_minutes、
    auto_extend_checkin_minutes、auto_extend_temp_leave_minutes。
    """
    auth = _auth(ctx, username, password)
    return await _call(auth, "PUT", "/api/config", body=changes)


# ---------- Seats ----------

@mcp.tool(annotations=READ)
async def list_areas(ctx: Context, auth_session_id: str | None = None,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """图书馆楼层/区域树（含空闲数），用于取得 room_id。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "GET", "/api/areas", params={"auth_session_id": await _session(auth, auth_session_id)})


@mcp.tool(annotations=READ)
async def room_availability(ctx: Context, room_id: int, date: str = "tomorrow", only_free: bool = False,
                            auth_session_id: str | None = None,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """某区域全部座位及每个座位可约的时段。date：today、tomorrow 或 YYYY-MM-DD。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "GET", f"/api/rooms/{room_id}/availability", params={
        "auth_session_id": await _session(auth, auth_session_id), "date": date, "only_free": only_free})


@mcp.tool(annotations=READ)
async def seat_slots(ctx: Context, room_id: int, seat: str, date: str = "tomorrow", begin: str | None = None,
                     auth_session_id: str | None = None,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """单个座位的规则、已有预约和可选开始/结束时间；begin=HH:MM 时附带图书馆的时段提示。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "GET", f"/api/rooms/{room_id}/seats/{seat}/slots", params={
        "auth_session_id": await _session(auth, auth_session_id), "date": date, "begin": begin})


@mcp.tool(annotations=DESTRUCTIVE)
async def reserve_seat(ctx: Context, seat: str, start: str, end: str, date: str = "tomorrow",
                       room_id: int | None = None, dry_run: bool = True, confirm: bool = False,
                       auth_session_id: str | None = None,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """预约指定座位和时段（HH:MM）。默认 dry_run 只校验；真正提交需 dry_run=false 且 confirm=true，并已开启 submit_enabled。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "POST", "/api/reservations", body={
        "auth_session_id": await _session(auth, auth_session_id), "seat": seat, "start": start, "end": end,
        "date": date, "room_id": room_id, "dry_run": dry_run, "confirm": confirm})


# ---------- My reservations ----------

@mcp.tool(annotations=READ)
async def my_reservations(ctx: Context, day: Literal["today", "tomorrow"] = "today", page: int = 1,
                          auth_session_id: str | None = None,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """本人在图书馆的预约（含状态、是否已签到、签到截止、能否取消/暂离/重约）。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "GET", "/api/reservations/mine", params={
        "auth_session_id": await _session(auth, auth_session_id), "day": day, "page": page})


@mcp.tool(annotations=DESTRUCTIVE)
async def cancel_reservation(ctx: Context, uuid: str, day: Literal["today", "tomorrow"], confirm: bool = False,
                             page: int = 1, auth_session_id: str | None = None,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """取消一条尚未开始的预约（uuid 来自 my_reservations）。需 confirm=true。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "POST", "/api/reservations/cancel", body={
        "auth_session_id": await _session(auth, auth_session_id), "uuid": uuid, "day": day,
        "page": page, "confirm": confirm})


@mcp.tool(annotations=DESTRUCTIVE)
async def temp_leave(ctx: Context, uuid: str, confirm: bool = False, page: int = 1,
                     auth_session_id: str | None = None,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """为今天已签到的预约办理暂离（须在图书馆规定时间内返回）。需 confirm=true。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "POST", "/api/reservations/temp-leave", body={
        "auth_session_id": await _session(auth, auth_session_id), "uuid": uuid, "page": page, "confirm": confirm})


@mcp.tool(annotations=DESTRUCTIVE)
async def rebook_later(ctx: Context, uuid: str, delay_minutes: int = 120, dry_run: bool = True,
                       confirm: bool = False, page: int = 1, auth_session_id: str | None = None,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """取消或提前结束今天的预约，并立即预约同一座位从“现在+delay_minutes”到原结束时间。默认 dry_run 预览。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "POST", "/api/reservations/rebook", body={
        "auth_session_id": await _session(auth, auth_session_id), "uuid": uuid, "delay_minutes": delay_minutes,
        "dry_run": dry_run, "confirm": confirm, "page": page})


# ---------- Automation ----------

@mcp.tool(annotations=READ)
async def automation_status(ctx: Context,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """自动预约任务和每日模式的当前状态。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "GET", "/api/automation")


@mcp.tool(annotations=READ)
async def automation_history(ctx: Context, page: int = 1, page_size: int = 10,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """已结束的自动预约任务历史（每页最多 50 条）。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "GET", "/api/automation/history", params={"page": page, "page_size": page_size})


@mcp.tool(annotations=DESTRUCTIVE)
async def start_automation(ctx: Context, auth_session_id: str | None = None,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """按已保存的参数启动一次自动预约（到触发时间后自动尝试提交）。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "POST", "/api/automation/start",
                       body={"auth_session_id": await _session(auth, auth_session_id)})


@mcp.tool(annotations=CHANGE)
async def stop_automation(ctx: Context,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """停止当前自动预约任务（每日模式开启时一并关闭）。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "POST", "/api/automation/stop")


@mcp.tool(annotations=DESTRUCTIVE)
async def start_daily(ctx: Context, auth_session_id: str | None = None,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """开启每日模式：每天在触发时间自动预约次日座位。需开启提交和保活。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "POST", "/api/automation/daily/start",
                       body={"auth_session_id": await _session(auth, auth_session_id)})


@mcp.tool(annotations=CHANGE)
async def stop_daily(ctx: Context,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """关闭每日模式，并停止正在运行的每日任务。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "POST", "/api/automation/daily/stop")


@mcp.tool(annotations=READ)
async def auto_extend_status(ctx: Context,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """自动延后的状态：正在关注的预约、剩余分钟、最近动作。开关和阈值用 update_config 修改。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "GET", "/api/auto-extend")


@mcp.tool(annotations=DESTRUCTIVE)
async def auto_extend_check(ctx: Context, dry_run: bool = True, confirm: bool = False,
        username: Username = None, password: Password = None) -> dict[str, Any]:
    """立即按已保存阈值检查一次自动延后。默认 dry_run 只返回计划；真正执行需 dry_run=false 且 confirm=true。"""
    auth = _auth(ctx, username, password)
    return await _call(auth, "POST", "/api/auto-extend/check", body={"dry_run": dry_run, "confirm": confirm})

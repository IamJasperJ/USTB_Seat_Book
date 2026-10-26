from __future__ import annotations

import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from .config import GlobalSettings


logger = logging.getLogger("ustb-seat-book.auth")
JWGL_URL = "https://jwgl.ustb.edu.cn/"
LIBRARY_RETRY_SECONDS = 60
LIBRARY_LOGIN_WAIT_MS = 20000


class SessionCapacityError(RuntimeError):
    pass

# The library SPA reads both fields when initializing its Vue store. An old
# isLogin=true without userInfo makes it skip disCaseUserInfo after CAS returns.
LIBRARY_RESET_STATE = """() => {
    sessionStorage.removeItem('userInfo');
    sessionStorage.removeItem('isLogin');
}"""
LIBRARY_LOGIN_RESULT = """libraryHost => {
    if (location.hostname === libraryHost) {
        try {
            const info = JSON.parse(sessionStorage.getItem('userInfo') || 'null');
            if (info && info.token) return 'authenticated';
        } catch (_) {}
    }
    // A redirect through SSO is normal; only an actual visible QR means that
    // user interaction is required. Do not mistake an intermediate URL for it.
    if (location.hostname === 'sso.ustb.edu.cn') {
        const qr = document.querySelector("iframe[src*='/connect/qrpage'], img[src*='/connect/qrimg']");
        if (qr && qr.getClientRects().length) return 'scan_required';
    }
    return false;
}"""


def _elapsed_seconds(started_at: str | None, checked_at: str) -> int | None:
    if not started_at:
        return None
    return max(0, int((datetime.fromisoformat(checked_at) - datetime.fromisoformat(started_at)).total_seconds()))


def _is_logged_in(session: "LoginSession") -> bool:
    # A library login can outlive a lapsed SSO probe, so either counts.
    return session.sso_authenticated or session.status == "authenticated"


@dataclass
class LoginSession:
    session_id: str
    context: BrowserContext
    page: Page
    ttl_seconds: int = 300
    created_at: float = field(default_factory=time.time)
    status: str = "starting"
    error: str | None = None
    user_info: dict[str, Any] | None = None
    sso_authenticated: bool = False
    sso_authenticated_at: str | None = None
    sso_last_valid_at: str | None = None
    sso_misses: int = 0
    authenticated_at: str | None = None
    last_valid_at: str | None = None
    expired_at: str | None = None
    library_error: str | None = None
    next_library_attempt: float = 0.0
    next_library_navigation: float = 0.0
    library_probe_unavailable: bool = False
    library_guard_installed: bool = False
    owner_account_id: str = ""
    _latest_qr: bytes | None = None
    keepalive_task: asyncio.Task | None = None
    page_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def expired(self) -> bool:
        return time.time() - self.created_at > self.ttl_seconds


class AuthManager:
    def __init__(self, settings: GlobalSettings):
        self.settings = settings
        self._playwright = None
        self._browser: Browser | None = None
        self.sessions: dict[str, LoginSession] = {}
        self._lock = asyncio.Lock()
        self._retired: OrderedDict[str, LoginSession] = OrderedDict()
        self._maintenance_task: asyncio.Task | None = None
        self.protected_session_ids = lambda: set()
        # Per-account preferences the global AuthManager otherwise has no way to see.
        self.account_settings_for = lambda account_id: None
        # Called with (old_session_id, new_session) right before an account's
        # older login is closed in favour of a newer one, so work bound to the
        # old session (e.g. an automation task) can move over instead of dying.
        self.on_session_superseded = lambda old_session_id, new_session: None
        self._supersede_tasks: set[asyncio.Task] = set()

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        # One shared browser process for every login session: a BrowserContext
        # per session is far lighter than launch_persistent_context's full
        # extra Chromium process per session.
        self._browser = await self._playwright.chromium.launch(headless=self.settings.headless)
        self._maintenance_task = asyncio.create_task(self._maintenance_loop())

    async def close(self) -> None:
        for task in list(self._supersede_tasks):
            task.cancel()
        await asyncio.gather(*self._supersede_tasks, return_exceptions=True)
        if self._maintenance_task:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
            self._maintenance_task = None
        for session in list(self.sessions.values()):
            await self.close_session(session.session_id)
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()

    def _mark_expired(
        self, session: LoginSession, reason: str, http_status: int | None = None
    ) -> None:
        if session.status == "expired":
            return
        expired_at = datetime.now(timezone.utc).isoformat()
        session.status = "expired"
        session.expired_at = expired_at
        session.error = reason
        logger.warning(
            "Session expired: session_id=%s sso_login_at=%s sso_last_valid_at=%s library_login_at=%s library_last_valid_at=%s expired_detected_at=%s reason=%s http_status=%s",
            session.session_id,
            session.sso_authenticated_at,
            session.sso_last_valid_at,
            session.authenticated_at,
            session.last_valid_at,
            expired_at,
            reason,
            http_status,
        )

    def _mark_sso_authenticated(self, session: LoginSession) -> None:
        now = datetime.now(timezone.utc).isoformat()
        newly_logged_in = not _is_logged_in(session)
        if not session.sso_authenticated_at:
            session.sso_authenticated_at = now
            logger.info("Campus SSO login succeeded: session_id=%s login_at=%s", session.session_id, now)
        session.sso_authenticated = True
        session.sso_misses = 0
        session.sso_last_valid_at = now
        if session.status != "authenticated":
            session.status = "sso_authenticated"
        session.error = None
        self._ensure_keepalive(session)
        if newly_logged_in:
            self._schedule_supersede(session)

    def _schedule_supersede(self, winner: LoginSession) -> None:
        if not winner.owner_account_id:
            return
        # Closing needs self._lock, and this runs from paths that may already
        # hold it (e.g. recycling), so it has to happen in its own task.
        task = asyncio.create_task(self._close_superseded_logins(winner))
        self._supersede_tasks.add(task)
        task.add_done_callback(self._supersede_tasks.discard)

    async def _close_superseded_logins(self, winner: LoginSession) -> None:
        """One account keeps at most one logged-in session: the newest login wins."""
        async with self._lock:
            if self.sessions.get(winner.session_id) is not winner or winner.status in {"error", "expired", "closed"}:
                return
            losers = [
                s for s in self.sessions.values()
                if s is not winner and s.owner_account_id == winner.owner_account_id and _is_logged_in(s)
            ]
            for loser in losers:
                try:
                    self.on_session_superseded(loser.session_id, winner)
                except Exception:
                    logger.exception("Session supersede callback failed: old=%s new=%s", loser.session_id, winner.session_id)
                try:
                    await self._close_session_locked(loser.session_id)
                except Exception:
                    logger.warning("Superseded session could not be closed: session_id=%s", loser.session_id)
                else:
                    logger.info("Login superseded by a newer login of the same account: closed=%s kept=%s", loser.session_id, winner.session_id)

    def _mark_library_authenticated(self, session: LoginSession, user_info: dict[str, Any]) -> None:
        if not session.sso_authenticated_at:
            self._mark_sso_authenticated(session)
        now = datetime.now(timezone.utc).isoformat()
        if session.status != "authenticated":
            session.authenticated_at = now
            logger.info("Library login succeeded: session_id=%s login_at=%s", session.session_id, now)
        session.user_info = user_info
        session.last_valid_at = now
        session.status = "authenticated"
        # Backoff is for incomplete/failed renewals, not a future loss of a
        # successfully established credential (including another-origin reset).
        session.next_library_navigation = 0
        session.error = None
        session.library_error = None
        self._ensure_keepalive(session)

    async def _probe_sso(self, session: LoginSession) -> bool:
        """Check the JWGL application session, not proof of a fresh CAS ticket."""
        response = await session.context.request.get(
            JWGL_URL,
            timeout=int(self.settings.request_timeout_seconds * 1000),
            max_redirects=8,
        )
        try:
            if response.status >= 500:
                raise RuntimeError(f"教务系统暂时不可用（HTTP {response.status}）")
            return response.status < 400 and urlsplit(response.url).hostname == "jwgl.ustb.edu.cn"
        finally:
            await response.dispose()

    @staticmethod
    def _error_reason(exc: Exception) -> str:
        # Browser navigation errors can contain one-time SSO URLs. Keep those
        # URLs, cookies and tokens out of logs and API responses.
        if isinstance(exc, httpx.HTTPStatusError):
            return f"HTTP {exc.response.status_code}"
        return type(exc).__name__

    async def _library_user_info(self, session: LoginSession) -> dict[str, Any] | None:
        if urlsplit(session.page.url).hostname != urlsplit(self.settings.base_url).hostname:
            return None
        raw = await session.page.evaluate("sessionStorage.getItem('userInfo')")
        if not raw:
            return None
        import json

        data = json.loads(raw)
        return data if isinstance(data, dict) and data.get("token") else None

    async def _try_library_locked(self, session: LoginSession, *, force: bool = False) -> None:
        if not force and time.monotonic() < session.next_library_attempt:
            return
        session.next_library_attempt = time.monotonic() + LIBRARY_RETRY_SECONDS
        try:
            # This small HTTP request is both a readiness probe and the source
            # of the real SSO URL. Never navigate or reset page state on a 503.
            login_url = await self._login_url()
            if session.library_probe_unavailable:
                session.library_probe_unavailable = False
                session.next_library_navigation = 0
                logger.info("Library lightweight probe recovered; authentication may resume")
            if time.monotonic() < session.next_library_navigation:
                session.library_error = "图书馆入口可访问，等待认证跳转冷却；无需重复加载页面"
                return
            session.next_library_navigation = time.monotonic() + self.settings.library_navigation_interval_seconds
            library_host = urlsplit(self.settings.base_url).hostname
            if not session.library_guard_installed:
                import json

                await session.page.add_init_script(
                    """(() => {
                        if (location.hostname !== LIBRARY_HOST) return;
                        let info;
                        try { info = JSON.parse(sessionStorage.getItem('userInfo') || 'null'); } catch (_) {}
                        if (!info || !info.token) sessionStorage.removeItem('isLogin');
                    })();""".replace("LIBRARY_HOST", json.dumps(library_host))
                )
                session.library_guard_installed = True
            if urlsplit(session.page.url).hostname == library_host:
                await session.page.evaluate(LIBRARY_RESET_STATE)
            logger.info("Library renewal started: session_id=%s", session.session_id)
            response = await session.page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            logger.info(
                "Library renewal navigation: session_id=%s http_status=%s host=%s path=%s",
                session.session_id, response.status if response else None,
                urlsplit(session.page.url).hostname, urlsplit(session.page.url).path,
            )
            if response and response.status >= 400:
                raise RuntimeError(f"图书馆站点暂时不可用（HTTP {response.status}）")
            try:
                await session.page.wait_for_function(
                    LIBRARY_LOGIN_RESULT, arg=library_host, timeout=LIBRARY_LOGIN_WAIT_MS,
                )
            except PlaywrightTimeoutError:
                # Keep the page alive; a late callback can still be collected
                # by the next ensure_library_session/keepalive/status check.
                pass
            user_info = await self._library_user_info(session)
            if user_info:
                self._mark_library_authenticated(session, user_info)
                logger.info("Library renewal completed: session_id=%s", session.session_id)
            elif await session.page.evaluate(LIBRARY_LOGIN_RESULT, library_host) == "scan_required":
                session.status = "library_scan_required"
                session.library_error = "图书馆要求单独扫码，请扫描当前二维码"
                logger.warning("Library renewal requires visible QR scan: session_id=%s", session.session_id)
            else:
                session.status = "sso_authenticated"
                session.library_error = "学校统一登录已完成，正在等待图书馆签发会话"
                logger.warning(
                    "Library renewal pending: session_id=%s host=%s path=%s wait_ms=%s",
                    session.session_id, urlsplit(session.page.url).hostname,
                    urlsplit(session.page.url).path, LIBRARY_LOGIN_WAIT_MS,
                )
        except Exception as exc:
            if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code >= 500:
                if not session.library_probe_unavailable:
                    logger.info("Library lightweight probe unavailable: http_status=%s; browser navigation suspended", exc.response.status_code)
                session.library_probe_unavailable = True
            session.status = "sso_authenticated"
            reason = self._error_reason(exc)
            session.library_error = f"图书馆会话暂不可用（{reason}），稍后自动重试"
            logger.warning("Library session bootstrap failed: session_id=%s reason=%s", session.session_id, reason)

    async def _ping_library_locked(self, session: LoginSession) -> None:
        result = await session.page.evaluate(
            """async () => {
                const raw = sessionStorage.getItem('userInfo');
                const userInfo = raw ? JSON.parse(raw) : {};
                const response = await fetch('/ic-web/auth/userInfo', {
                    method: 'GET', credentials: 'include',
                    headers: { 'token': userInfo.token || '', 'lan': '1' }
                });
                let body = null;
                try { body = await response.json(); } catch (_) {}
                return { httpStatus: response.status, body };
            }"""
        )
        body = result.get("body") or {}
        if body.get("code") == 300 or result.get("httpStatus") in (401, 403):
            await self._invalidate_library_locked(session, result.get("httpStatus"))
            return
        if body.get("code") == 0 and isinstance(body.get("data"), dict):
            session.user_info = {**(session.user_info or {}), **body["data"]}
            session.last_valid_at = datetime.now(timezone.utc).isoformat()
            logger.info(
                "Library keepalive succeeded: session_id=%s login_at=%s last_valid_at=%s alive_seconds=%s",
                session.session_id,
                session.authenticated_at,
                session.last_valid_at,
                _elapsed_seconds(session.authenticated_at, session.last_valid_at),
            )
        else:
            logger.warning(
                "Library keepalive inconclusive: session_id=%s http_status=%s code=%s last_valid_at=%s",
                session.session_id,
                result.get("httpStatus"),
                body.get("code"),
                session.last_valid_at,
            )

    async def _invalidate_library_locked(self, session: LoginSession, http_status: int | None = None) -> None:
        try:
            if urlsplit(session.page.url).hostname == urlsplit(self.settings.base_url).hostname:
                await session.page.evaluate(LIBRARY_RESET_STATE)
        except Exception:
            # The page may have crashed during a library outage. The in-memory
            # credential must still be discarded before the next retry.
            pass
        session.user_info = None
        if session.sso_authenticated:
            session.status = "sso_authenticated"
            session.next_library_attempt = 0
            session.library_error = "图书馆会话已失效，正在用学校统一登录重新建立会话"
            logger.warning(
                "Library session expired; SSO renewal pending: session_id=%s login_at=%s last_valid_at=%s detected_at=%s",
                session.session_id,
                session.authenticated_at,
                session.last_valid_at,
                datetime.now(timezone.utc).isoformat(),
            )
        else:
            self._mark_expired(session, "登录会话已失效，请重新扫码登录", http_status)

    async def invalidate_library_session(self, session: LoginSession) -> None:
        async with session.page_lock:
            await self._invalidate_library_locked(session)

    async def ensure_library_session(self, session: LoginSession, *, force: bool = False) -> bool:
        if session.status == "authenticated" and session.user_info:
            return True
        if not session.sso_authenticated or session.status in {"error", "expired", "closed"}:
            return False
        async with session.page_lock:
            if session.status == "authenticated" and session.user_info:
                return True
            user_info = await self._library_user_info(session)
            if user_info:
                self._mark_library_authenticated(session, user_info)
            else:
                await self._try_library_locked(session, force=force)
            return session.status == "authenticated" and bool(session.user_info)

    async def _keepalive_loop(self, session: LoginSession) -> None:
        next_sso_check = 0.0
        next_library_check = 0.0
        while session.status not in {"error", "expired", "closed"}:
            try:
                # The global AuthManager has no personal settings of its own; it
                # asks the owning account's runtime, falling back to self.settings
                # (which lets standalone/unit usage still pass a full Settings in).
                account_settings = self.account_settings_for(session.owner_account_id) or self.settings
                if not getattr(account_settings, "keepalive_enabled", True):
                    return
                interval = max(30, getattr(account_settings, "keepalive_interval_seconds", 300))
                now = time.monotonic()
                if session.sso_authenticated and now >= next_sso_check:
                    next_sso_check = now + interval
                    if await self._probe_sso(session):
                        session.sso_last_valid_at = datetime.now(timezone.utc).isoformat()
                        session.sso_misses = 0
                        logger.info(
                            "JWGL application keepalive succeeded (CAS renewal not verified): session_id=%s login_at=%s last_valid_at=%s alive_seconds=%s",
                            session.session_id,
                            session.sso_authenticated_at,
                            session.sso_last_valid_at,
                            _elapsed_seconds(session.sso_authenticated_at, session.sso_last_valid_at),
                        )
                    else:
                        session.sso_misses += 1
                        logger.warning(
                            "Campus SSO keepalive rejected: session_id=%s misses=%s last_valid_at=%s",
                            session.session_id,
                            session.sso_misses,
                            session.sso_last_valid_at,
                        )
                        if session.sso_misses >= 2:
                            session.sso_authenticated = False
                            if session.status != "authenticated":
                                self._mark_expired(session, "学校统一登录已失效，请重新扫码登录")
                                return
                if session.status == "authenticated" and now >= next_library_check:
                    next_library_check = now + interval
                    async with session.page_lock:
                        await self._ping_library_locked(session)
                elif session.status == "sso_authenticated":
                    async with session.page_lock:
                        user_info = await self._library_user_info(session)
                        if user_info:
                            self._mark_library_authenticated(session, user_info)
                        elif now >= session.next_library_attempt:
                            await self._try_library_locked(session)
            except asyncio.CancelledError:
                return
            except Exception as exc:
                session.error = f"保活请求暂时失败：{self._error_reason(exc)}"
                logger.warning(
                    "Session keepalive failed temporarily: session_id=%s reason=%s sso_last_valid_at=%s library_last_valid_at=%s",
                    session.session_id,
                    self._error_reason(exc),
                    session.sso_last_valid_at,
                    session.last_valid_at,
                )
            await asyncio.sleep(10)

    async def _login_url(self) -> str:
        base = self.settings.base_url.rstrip("/")
        final_address = f"{base}/#/ic/home"
        error_address = f"{base}/#/error"
        params = {
            "finalAddress": final_address,
            "errPageUrl": error_address,
            "manager": "false",
            "consoleType": "16",
        }
        async with httpx.AsyncClient(timeout=self.settings.request_timeout_seconds) as client:
            response = await client.get(f"{base}/ic-web/auth/address", params=params)
            response.raise_for_status()
            data = response.json()
        if data.get("code") != 0 or not data.get("data"):
            raise RuntimeError(data.get("message") or "无法获取 SSO 登录地址")
        return data["data"]

    async def create_session(self, account_id: str, login_source: str = "jwgl") -> LoginSession:
        # Hold the lock through launch: concurrent requests cannot exceed the cap.
        async with self._lock:
            await self._recycle_locked()
            owned = sum(1 for s in self.sessions.values() if s.owner_account_id == account_id)
            if owned >= self.settings.max_sessions_per_account:
                raise SessionCapacityError(
                    f"每个账号最多保留 {self.settings.max_sessions_per_account} 个登录会话，你已有 {owned} 个；"
                    "请先在“我的登录会话”里删除不再使用的会话"
                )
            cap = self.settings.max_browser_sessions
            if cap is not None and len(self.sessions) >= cap:
                raise SessionCapacityError(
                    f"服务器浏览器资源已满（共 {cap} 个会话），请稍后再试或联系管理员"
                )
            return await self._create_session_locked(account_id, login_source)

    async def _create_session_locked(self, account_id: str, login_source: str) -> LoginSession:
        if not self._browser:
            raise RuntimeError("认证管理器尚未启动")

        login_url = JWGL_URL if login_source == "jwgl" else await self._login_url()
        session_id = secrets.token_urlsafe(18)

        # A lightweight context in one shared browser process, instead of a
        # whole separate Chromium process per login (launch_persistent_context).
        context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            device_scale_factor=1,
        )
        page = await context.new_page()
        session = LoginSession(
            session_id=session_id,
            context=context,
            page=page,
            ttl_seconds=self.settings.auth_session_ttl_seconds,
            owner_account_id=account_id,
        )
        self.sessions[session_id] = session

        try:
            response = await page.goto(login_url, wait_until="domcontentloaded", timeout=60000)
            if response and response.status >= 400:
                raise RuntimeError(f"登录入口暂不可用（HTTP {response.status}）")
            await page.wait_for_timeout(1500)
            session.status = "waiting_scan"
        except Exception as exc:
            session.status = "error"
            session.error = f"登录页面无法打开（{self._error_reason(exc)}）"
        return session

    async def get(self, session_id: str) -> LoginSession:
        session = self.sessions.get(session_id) or self._retired.get(session_id)
        if not session:
            raise KeyError(session_id)
        if session.expired and session.status in {"starting", "waiting_scan", "callback_received"}:
            await self.refresh_status(session)
            if session.status in {"starting", "waiting_scan", "callback_received"} and not session.sso_authenticated:
                self._mark_expired(session, "二维码已过期，请重新生成")
        return session

    async def refresh_status(self, session: LoginSession) -> None:
        if session.status in {"error", "expired", "closed"}:
            return
        async with session.page_lock:
            try:
                user_info = await self._library_user_info(session)
                if user_info:
                    self._mark_library_authenticated(session, user_info)
                    return
                if session.status == "authenticated":
                    return
                host = urlsplit(session.page.url).hostname
                if host == "jwgl.ustb.edu.cn" and not session.sso_authenticated:
                    self._mark_sso_authenticated(session)
                if session.sso_authenticated:
                    if session.status == "sso_authenticated":
                        await self._try_library_locked(session)
                    return
                if "/ic-web/auth/token" in session.page.url:
                    session.status = "callback_received"
                else:
                    session.status = "waiting_scan"
            except Exception as exc:
                session.error = f"登录状态检查暂时失败：{self._error_reason(exc)}"

    def _ensure_keepalive(self, session: LoginSession) -> None:
        account_settings = self.account_settings_for(session.owner_account_id) or self.settings
        if not getattr(account_settings, "keepalive_enabled", True):
            return
        if session.keepalive_task is None or session.keepalive_task.done():
            session.keepalive_task = asyncio.create_task(self._keepalive_loop(session))

    async def qr(self, session: LoginSession) -> bytes:
        await self.refresh_status(session)
        if session.status in {"error", "expired", "closed"}:
            raise RuntimeError(session.error or "二维码会话不可用，请重新生成")
        # The USTB SSO page renders the QR in a cross-origin iframe. Fetch the
        # original 360x360 PNG with this browser context's cookies: a screenshot
        # of the small on-screen element loses detail.
        for frame in session.page.frames:
            if "/connect/qrpage" not in frame.url:
                continue
            qr_image = frame.locator("img[src*='/connect/qrimg']").first
            if await qr_image.count() and await qr_image.is_visible():
                source = await qr_image.get_attribute("src")
                if source:
                    response = None
                    try:
                        response = await session.context.request.get(
                            urljoin(frame.url, source),
                            timeout=int(self.settings.request_timeout_seconds * 1000),
                        )
                        image = await response.body()
                        if response.ok and image.startswith(b"\x89PNG\r\n\x1a\n"):
                            session._latest_qr = image
                            return image
                    except Exception:
                        pass
                    finally:
                        if response is not None:
                            await response.dispose()
                return await qr_image.screenshot(type="png")
        qr_frame = session.page.locator("iframe[src*='/connect/qrpage']").first
        if await qr_frame.count() and await qr_frame.is_visible():
            return await qr_frame.screenshot(type="png")
        qr_container = session.page.locator("[class*='qr' i]").first
        if await qr_container.count() and await qr_container.is_visible():
            return await qr_container.screenshot(type="png")
        if session._latest_qr is None:
            session._latest_qr = await session.page.screenshot(type="png", full_page=False)
        return session._latest_qr

    async def close_session(self, session_id: str) -> None:
        async with self._lock:
            await self._close_session_locked(session_id)

    async def _close_session_locked(self, session_id: str) -> None:
        session = self.sessions.get(session_id)
        self._retired.pop(session_id, None)
        if session:
            previous_status = session.status
            session.status = "closed"
            if session.keepalive_task and not session.keepalive_task.done():
                session.keepalive_task.cancel()
                await asyncio.gather(session.keepalive_task, return_exceptions=True)
            try:
                await asyncio.wait_for(session.context.close(), timeout=30)
                self.sessions.pop(session_id, None)
            except Exception:
                # Keep counting a browser whose closure was not confirmed.
                session.status = "error"
                logger.warning("Browser close failed; capacity remains occupied")
                raise
            else:
                logger.info(
                    "Session closed: session_id=%s previous_status=%s closed_at=%s sso_login_at=%s sso_last_valid_at=%s library_login_at=%s library_last_valid_at=%s expired_detected_at=%s",
                    session.session_id,
                    previous_status,
                    datetime.now(timezone.utc).isoformat(),
                    session.sso_authenticated_at,
                    session.sso_last_valid_at,
                    session.authenticated_at,
                    session.last_valid_at,
                    session.expired_at,
                )

    async def recycle_sessions(self) -> None:
        async with self._lock:
            await self._recycle_locked()

    async def _recycle_locked(self) -> None:
        protected = self.protected_session_ids()
        for session in list(self.sessions.values()):
            if session.session_id in protected:
                continue
            # A live SSO session must survive library shutdown and QR renewal.
            if session.status not in {"error", "expired", "closed"}:
                if session.sso_authenticated or session.status not in {"starting", "waiting_scan", "callback_received"}:
                    continue
                if not session.expired:
                    continue
            if session.status in {"starting", "waiting_scan", "callback_received"}:
                # Check a completed scan before discarding an old QR browser.
                await self.refresh_status(session)
                if session.sso_authenticated or session.status not in {"starting", "waiting_scan", "callback_received", "error"}:
                    continue
                self._mark_expired(session, "二维码已过期，浏览器资源已回收，请重新生成")
            async with session.page_lock:
                if session.status not in {"error", "expired", "closed"}:
                    continue
                terminal_status = session.status
                try:
                    await self._close_session_locked(session.session_id)
                except Exception:
                    continue
                session.status = terminal_status
                session._latest_qr = None
                session.user_info = None
                self._retired[session.session_id] = session
                while len(self._retired) > 32:
                    self._retired.popitem(last=False)
                logger.info("Browser session recycled: reason=%s active_browsers=%s", terminal_status, len(self.sessions))

    async def _maintenance_loop(self) -> None:
        while True:
            await asyncio.sleep(self.settings.session_cleanup_interval_seconds)
            try:
                await self.recycle_sessions()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("Session cleanup failed temporarily")

    async def credentials(self, session: LoginSession) -> tuple[dict[str, str], dict[str, Any]]:
        await self.refresh_status(session)
        if session.status != "authenticated" or not session.user_info:
            raise RuntimeError("图书馆会话尚未建立，请查看登录状态")
        cookies = {cookie["name"]: cookie["value"] for cookie in await session.context.cookies()}
        return cookies, session.user_info

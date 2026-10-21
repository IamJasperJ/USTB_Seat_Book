"""Bounded browser probes; never navigate, reserve, or restart a login context."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("ustb-seat-book.health")


class BrowserHealthMonitor:
    def __init__(self, auth, interval: float = 60, timeout: float = 3):
        self.auth = auth
        self.interval = interval
        self.timeout = timeout
        self.task: asyncio.Task | None = None
        self.checked_at: str | None = None
        self.checked_monotonic: float | None = None
        self.rows: dict[str, dict[str, Any]] = {}
        self.idle_probe = "starting"
        self._watched: dict[str, Any] = {}
        self._faults: dict[str, str] = {}
        self._last_success: dict[str, float] = {}
        self._idle_pending = None
        self._sample_lock = asyncio.Lock()
        self._last_state: str | None = None

    def connected(self) -> bool:
        browser = self.auth._browser
        return browser is not None and browser.is_connected()

    def _watch(self, session) -> None:
        sid, page = session.session_id, session.page
        if self._watched.get(sid) is page:
            return
        self._watched[sid] = page
        self._faults.pop(sid, None)
        self._last_success[sid] = time.monotonic()

        def crashed(*_):
            if self._watched.get(sid) is page:
                self._faults[sid] = "page_crashed"
                logger.error("Browser page crashed; health check is unhealthy")

        page.on("crash", crashed)

    async def _probe_session(self, session) -> dict[str, Any]:
        self._watch(session)
        sid = session.session_id
        reason = self._faults.get(sid)
        if not reason and session.page.is_closed():
            reason = "page_closed"
        result = {"session_id": sid, "owner_account_id": session.owner_account_id,
                  "auth_status": session.status,
                  "sso_authenticated": bool(session.sso_authenticated),
                  "library_authenticated": session.status == "authenticated" and bool(session.user_info)}
        if reason:
            return {**result, "status": "unhealthy", "reason": reason}
        # Navigation and keepalive own this lock. Never block or interrupt them.
        if session.page_lock.locked():
            if time.monotonic() - self._last_success[sid] > self.interval * 3:
                return {**result, "status": "unhealthy", "reason": "page_busy_too_long"}
            return {**result, "status": "busy", "reason": "authentication_operation_in_progress"}
        try:
            async with asyncio.timeout(self.timeout):
                async with session.page_lock:
                    value = await session.page.evaluate("() => 1")
            if value != 1:
                raise ValueError("unexpected probe result")
            self._last_success[sid] = time.monotonic()
            return {**result, "status": "healthy", "reason": None}
        except Exception:
            # Exception messages may contain credential-bearing URLs.
            return {**result, "status": "unhealthy", "reason": "page_probe_failed"}

    async def _probe_idle_browser(self) -> str:
        if self._idle_pending is not None:
            try:
                await asyncio.wait_for(self._idle_pending.close(), timeout=self.timeout)
                self._idle_pending = None
            except Exception:
                return "unhealthy"
        context = None
        outcome = "unhealthy"
        try:
            async with asyncio.timeout(self.timeout):
                context = await self.auth._browser.new_context()
                page = await context.new_page()
                assert await page.evaluate("() => 1") == 1
            outcome = "healthy"
        except Exception:
            outcome = "unhealthy"
        finally:
            if context is not None:
                try:
                    await asyncio.wait_for(context.close(), timeout=self.timeout)
                except Exception:
                    # Do not accumulate a new context every minute on failure.
                    self._idle_pending = context
                    outcome = "unhealthy"
        return outcome

    async def sample(self) -> None:
        async with self._sample_lock:
            sessions = [s for s in list(self.auth.sessions.values())
                        if s.status not in {"error", "expired", "closed"}]
            active_ids = {s.session_id for s in sessions}
            for sid in list(self._watched):
                if sid not in active_ids:
                    self._watched.pop(sid, None)
                    self._faults.pop(sid, None)
                    self._last_success.pop(sid, None)
            if self.connected():
                self.rows = {s.session_id: await self._probe_session(s) for s in sessions}
                self.idle_probe = await self._probe_idle_browser() if not sessions else "not_needed"
            else:
                self.rows = {}
                self.idle_probe = "unhealthy"
            self.checked_at = datetime.now(timezone.utc).isoformat()
            self.checked_monotonic = time.monotonic()
            state = self.summary()["status"]
            if state != self._last_state:
                logger.log(logging.INFO if state == "healthy" else logging.WARNING,
                           "Browser health changed: status=%s", state)
                self._last_state = state

    def summary(self) -> dict[str, Any]:
        connected = self.connected()
        stale = self.checked_monotonic is None or (
            time.monotonic() - self.checked_monotonic > self.interval * 2 + self.timeout * 4
        )
        live = {sid: s for sid, s in self.auth.sessions.items()
                if s.status not in {"error", "expired", "closed"}}
        failed = sum(1 for sid, session in live.items()
                     if self._faults.get(sid) or session.page.is_closed()
                     or self.rows.get(sid, {}).get("status") == "unhealthy")
        busy = any(row.get("status") == "busy" for sid, row in self.rows.items() if sid in live)
        pending = any(sid not in self.rows for sid in live)
        healthy = connected and not stale and not failed and self.idle_probe != "unhealthy"
        state = "unhealthy" if not healthy else "degraded" if busy or pending else "healthy"
        return {"status": state, "healthy": healthy, "browser_connected": connected,
                "checked_at": self.checked_at, "stale": stale,
                "failed_pages": failed, "check_interval_seconds": self.interval}

    def details(self, account_id: str, admin: bool = False) -> dict[str, Any]:
        rows = []
        for sid, session in list(self.auth.sessions.items()):
            if session.status in {"error", "expired", "closed"}:
                continue
            if not admin and session.owner_account_id != account_id:
                continue
            row = dict(self.rows.get(sid, {"session_id": sid, "status": "pending", "reason": "awaiting_probe"}))
            row.pop("owner_account_id", None)
            if self._faults.get(sid):
                row.update(status="unhealthy", reason=self._faults[sid])
            elif session.page.is_closed():
                row.update(status="unhealthy", reason="page_closed")
            rows.append(row)
        return {**self.summary(), "sessions": rows,
                "idle_browser_probe": self.idle_probe,
                "automatic_restart": False,
                "note": "Browser health does not prove CAS renewal or reservation readiness."}

    async def start(self) -> None:
        await self.sample()
        self.task = asyncio.create_task(self._loop())

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.sample()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failing monitor must eventually become stale, not stay green.
                logger.warning("Browser health probe could not complete")

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

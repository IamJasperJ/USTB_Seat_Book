from __future__ import annotations

import base64
import asyncio
import os
from datetime import timedelta
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import control_api, main
from app.accounts import Account, AccountStore, RegistrationRateLimiter
from app.auth import LoginSession
from app.config import Settings


def seat_row(name: str, dev_id: int, resv: list | None = None, open_state: int = 1) -> dict:
    return {
        "devName": name, "devId": dev_id, "roomId": 7, "roomName": "Room", "openState": open_state,
        "devStatus": 0, "resvInfo": resv or [],
        "openTimes": [{"openStartTime": "08:00", "openEndTime": "12:00"}],
        "resvRule": {"timeInterval": 60, "minResvTime": 60, "maxResvTime": 240},
    }


class FakeSeatApi:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.user_info = {"accNo": "1001"}
        self.reserve = AsyncMock(return_value={"code": 0, "message": "ok"})
        self.device_tips = AsyncMock(return_value={"code": 0, "data": "tips"})
        self.my_reservations = AsyncMock(return_value={"code": 0, "data": []})
        self.close = AsyncMock()

    async def seats(self, target, room_id=None):
        return {"data": self.rows}


def basic(user: str, pw: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


class BasicAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = AccountStore(Path(self.tmp.name) / "a.sqlite3")
        self.alice = self.store.create("alice", "correct-horse")
        for p in (patch.object(main, "account_store", self.store), patch.object(main, "ensure_runtime", AsyncMock()),
                  patch.object(main, "monitor_auth_rate_limiter", RegistrationRateLimiter(30, 60))):
            p.start()
            self.addCleanup(p.stop)

    async def test_basic_credentials_resolve_account_without_session(self) -> None:
        before = dict(main.session_tokens._tokens)
        account = await main.current_account(None, basic("alice", "correct-horse"))
        self.assertEqual(account.id, self.alice.id)
        self.assertEqual(main.session_tokens._tokens, before)

    async def test_bad_basic_credentials_rejected(self) -> None:
        for header in (basic("alice", "wrong"), basic("nobody", "x"), "Basic !!!notb64", "Basic " +
                       base64.b64encode(b"nocolon").decode(), "Bearer something"):
            with self.subTest(header=header), self.assertRaises(HTTPException) as ctx:
                await main.current_account(None, header)
            self.assertEqual(ctx.exception.status_code, 401)

    async def test_basic_password_work_does_not_block_loop(self) -> None:
        loop_thread = threading.get_ident()
        entered, release = threading.Event(), threading.Event()

        def slow_verify(*_):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            entered.set()
            release.wait(timeout=2)
            return self.alice

        with patch.object(self.store, "verify", side_effect=slow_verify):
            task = asyncio.create_task(main.current_account(None, basic("alice", "correct-horse")))
            try:
                async with asyncio.timeout(1):
                    while not entered.is_set():
                        if task.done():
                            await task
                        await asyncio.sleep(.001)
                # The loop remains available while the password worker waits.
                self.assertFalse(task.done())
            finally:
                release.set()
                await task

    async def test_basic_rate_limit_precedes_password_work(self) -> None:
        limiter = RegistrationRateLimiter(1, 60)
        with patch.object(main, "monitor_auth_rate_limiter", limiter), \
             patch.object(self.store, "verify", wraps=self.store.verify) as verify:
            await main.current_account(None, basic("alice", "correct-horse"))
            with self.assertRaises(HTTPException) as ctx:
                await main.current_account(None, basic("alice", "correct-horse"))
            self.assertEqual(ctx.exception.status_code, 429)
            self.assertEqual(ctx.exception.headers["Retry-After"], "60")
            self.assertEqual(verify.call_count, 1)

    def test_http_basic_on_endpoints(self) -> None:
        client = TestClient(main.app)  # no lifespan: no browser started
        self.assertEqual(client.get("/api/account/me", headers={"Authorization": basic("alice", "correct-horse")}).json(),
                         {"username": "alice", "is_admin": self.alice.is_admin})
        self.assertEqual(client.get("/api/overview", headers={"Authorization": basic("alice", "bad")}).status_code, 401)
        response = client.get("/api/account/me", headers={"Authorization": basic("alice", "correct-horse")})
        self.assertNotIn("session_token", response.cookies)


class ControlApiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.alice = Account(id="alice-acct", username="alice", is_admin=False)
        self.runtime = main.UserRuntime(self.alice, Settings(room_id=7), main.auth)
        self.addCleanup(lambda: Path(self.runtime.automation.history.path).unlink(missing_ok=True))
        session = LoginSession("s1", AsyncMock(), AsyncMock(), created_at=1.0, status="authenticated")
        session.owner_account_id = self.alice.id
        session.user_info = {"accNo": "1001", "userName": "Alice", "token": "secret-token"}
        self.session = session
        self.fake_auth = SimpleNamespace(sessions={"s1": session}, refresh_status=AsyncMock())
        self.api = FakeSeatApi([seat_row("A1", 11), seat_row("A2", 12, open_state=0),
                                seat_row("A3", 13, resv=[{"startTime": "2000-01-01 08:00:00"}])])
        for p in (patch.object(main, "auth", self.fake_auth),
                  patch.dict(main.runtimes, {self.alice.id: self.runtime}),
                  patch.object(main, "authenticated_api", AsyncMock(return_value=self.api))):
            p.start()
            self.addCleanup(p.stop)

    def set_submit(self, enabled: bool) -> None:
        self.runtime.settings = self.runtime.settings.model_copy(update={"submit_enabled": enabled})

    def test_resolve_date(self) -> None:
        today = control_api._today()
        self.assertEqual(control_api.resolve_date("today", max_days_ahead=1), today)
        self.assertEqual(control_api.resolve_date((today + timedelta(days=1)).isoformat(), max_days_ahead=1),
                         today + timedelta(days=1))
        for bad in ("dayafter", (today - timedelta(days=1)).isoformat(), "2026/1/1"):
            with self.assertRaises(HTTPException):
                control_api.resolve_date(bad, max_days_ahead=1)

    async def test_overview_reports_session_and_reservations_without_secrets(self) -> None:
        result = await control_api.overview(None, True, account=self.alice)
        self.assertTrue(result["ready"])
        self.assertEqual(result["session"]["session_id"], "s1")
        self.assertEqual(result["session"]["user"]["acc_no"], "1001")
        self.assertEqual(set(result["reservations"]), {"today", "tomorrow"})
        self.assertNotIn("secret-token", str(result))

    async def test_overview_without_login(self) -> None:
        self.fake_auth.sessions.clear()
        result = await control_api.overview(None, True, account=self.alice)
        self.assertFalse(result["ready"])
        self.assertIn("hint", result)
        with self.assertRaises(HTTPException):
            await control_api.overview("other", True, account=self.alice)

    async def test_overview_without_records_still_verifies_credential(self) -> None:
        result = await control_api.overview(None, False, account=self.alice)
        self.assertTrue(result["ready"])
        self.assertTrue(result["library"]["confirmed"])
        self.assertNotIn("reservations", result)
        self.api.my_reservations.assert_awaited_once()
        self.api.close.assert_awaited_once()

    async def test_overview_expired_or_unavailable_is_not_ready(self) -> None:
        for include in (False, True):
            for error, reason in ((RuntimeError("secret-token"), "library_login_expired"),
                                  (OSError("secret-token-url"), "library_unavailable"),
                                  (TimeoutError("secret-token"), "query_timeout")):
                with self.subTest(include=include, reason=reason):
                    self.api.my_reservations.side_effect = error
                    self.api.close.reset_mock()
                    result = await control_api.overview(None, include, account=self.alice)
                    self.assertFalse(result["ready"])
                    self.assertFalse(result["library"]["confirmed"])
                    self.assertEqual(result["library"]["reason"], reason)
                    self.assertNotIn("secret-token", str(result))
                    self.api.close.assert_awaited_once()

    async def test_overview_preserves_partial_results_on_failure(self) -> None:
        self.api.my_reservations.side_effect = [{"code": 0, "data": []}, RuntimeError("expired")]
        result = await control_api.overview(None, True, account=self.alice)
        self.assertFalse(result["ready"])
        self.assertEqual(result["reservations"]["today"]["records"], [])
        self.assertIsNone(result["reservations"]["tomorrow"]["records"])

    async def test_overview_handles_credential_acquisition_failure(self) -> None:
        with patch.object(main, "authenticated_api", AsyncMock(side_effect=HTTPException(401, "secret-token"))):
            result = await control_api.overview(None, True, account=self.alice)
        self.assertFalse(result["ready"])
        self.assertEqual(result["library"]["reason"], "library_login_expired")
        self.assertNotIn("secret-token", str(result))

    async def test_overview_real_timeout_closes_request_client(self) -> None:
        async def stalled(*_):
            await asyncio.sleep(10)

        self.api.my_reservations.side_effect = stalled
        with patch.object(control_api, "OVERVIEW_TIMEOUT_SECONDS", .01):
            result = await control_api.overview(None, False, account=self.alice)
        self.assertFalse(result["ready"])
        self.assertEqual(result["library"]["reason"], "query_timeout")
        self.api.close.assert_awaited_once()

    async def test_overview_library_errors_are_structured_http_200(self) -> None:
        previous = dict(main.app.dependency_overrides)
        main.app.dependency_overrides[control_api.current_account] = lambda: self.alice
        try:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
                for failure, reason in ((RuntimeError("secret-token"), "library_login_expired"),
                                        (httpx.ConnectError("secret-token-url"), "library_unavailable")):
                    self.api.my_reservations.side_effect = failure
                    response = await client.get("/api/overview")
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(response.json()["ready"])
                    self.assertEqual(response.json()["library"]["reason"], reason)
                    self.assertNotIn("secret-token", response.text)
                self.api.my_reservations.side_effect = None
                self.api.my_reservations.return_value = {"code": 1, "message": "secret-token-url"}
                response = await client.get("/api/overview")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["library"]["reason"], "library_response_error")
                self.assertNotIn("secret-token", response.text)
        finally:
            main.app.dependency_overrides.clear()
            main.app.dependency_overrides.update(previous)

    async def test_availability_filters_free_seats(self) -> None:
        result = await control_api.room_availability(7, "s1", "tomorrow", True, account=self.alice)
        self.assertEqual(result["total"], 3)
        self.assertEqual([s["dev_name"] for s in result["seats"]], ["A1"])
        self.assertEqual(result["seats"][0]["free_time_options"][0], {"start": "08:00", "ends": ["09:00", "10:00", "11:00", "12:00"]})

    async def test_free_options_exclude_existing_reservation(self) -> None:
        day = control_api._today()
        seat = {"time_options": [{"start": "08:00", "ends": ["09:00", "10:00"]}, {"start": "10:00", "ends": ["11:00"]}],
                "reservations": [{"startTime": f"{day} 09:00:00", "endTime": f"{day} 10:00:00"}]}
        self.assertEqual(control_api.free_time_options(seat, day),
                         [{"start": "08:00", "ends": ["09:00"]}, {"start": "10:00", "ends": ["11:00"]}])

    async def test_slots_by_name_or_id_with_tips(self) -> None:
        by_id = await control_api.seat_slots(7, "11", "s1", "today", "08:00", account=self.alice)
        self.assertEqual(by_id["seat"]["dev_name"], "A1")
        self.api.device_tips.assert_awaited_once()
        with self.assertRaises(HTTPException) as ctx:
            await control_api.seat_slots(7, "Z9", "s1", "today", None, account=self.alice)
        self.assertEqual(ctx.exception.status_code, 404)

    def req(self, **kw) -> control_api.ReserveRequest:
        base = dict(auth_session_id="s1", seat="A1", date="tomorrow", start="08:00", end="10:00")
        return control_api.ReserveRequest(**{**base, **kw})

    async def test_reserve_requires_submit_enabled_and_confirm(self) -> None:
        with self.assertRaises(HTTPException) as ctx:
            await control_api.reserve_seat(self.req(confirm=True), account=self.alice)
        self.assertEqual(ctx.exception.status_code, 403)
        self.set_submit(True)
        with self.assertRaises(HTTPException) as ctx:
            await control_api.reserve_seat(self.req(), account=self.alice)
        self.assertEqual(ctx.exception.status_code, 400)
        self.api.reserve.assert_not_awaited()

    async def test_dry_run_validates_without_submitting(self) -> None:
        result = await control_api.reserve_seat(self.req(dry_run=True), account=self.alice)
        self.assertTrue(result["would_submit"])
        self.assertTrue(result["begin"].endswith("08:00:00"))
        self.api.reserve.assert_not_awaited()

    async def test_reserve_rejections(self) -> None:
        self.set_submit(True)
        cases = [(dict(seat="A2"), 409), (dict(seat="A3"), 409), (dict(start="08:30"), 409),
                 (dict(start="10:00", end="09:00"), 422), (dict(seat="nope"), 404), (dict(date="dayafter"), 422)]
        for kw, code in cases:
            with self.subTest(kw=kw), self.assertRaises(HTTPException) as ctx:
                await control_api.reserve_seat(self.req(confirm=True, **kw), account=self.alice)
            self.assertEqual(ctx.exception.status_code, code)
        self.api.reserve.assert_not_awaited()

    async def test_reserve_submits_once(self) -> None:
        self.set_submit(True)
        result = await control_api.reserve_seat(self.req(confirm=True), account=self.alice)
        self.assertTrue(result["success"])
        args = self.api.reserve.await_args.args
        self.assertEqual(args[0], 11)
        self.assertTrue(args[1].endswith(" 08:00:00") and args[2].endswith(" 10:00:00"))


class ImportOrderTests(unittest.TestCase):
    def test_fresh_interpreter_supports_both_import_orders(self) -> None:
        root = str(Path(__file__).resolve().parents[1])
        for code in ("import app.control_api; import app.main", "import app.main; import app.control_api"):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, "-c", code], cwd=cwd, timeout=20,
                                        env={**os.environ, "PYTHONPATH": root, "PYTHONDONTWRITEBYTECODE": "1"},
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()

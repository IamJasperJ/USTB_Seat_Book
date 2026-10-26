from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app import main
from app.accounts import AccountStore, SessionTokenStore
from app.auth import LoginSession


def make_session(session_id: str, owner: str, created_at: float = 100.0, status: str = "authenticated") -> LoginSession:
    session = LoginSession(session_id, AsyncMock(), AsyncMock(), created_at=created_at, status=status)
    session.owner_account_id = owner
    session.sso_authenticated = True
    return session


class AdminApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.dir = TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = AccountStore(Path(self.dir.name) / "accounts.sqlite3")
        self.admin = self.store.create("boss", "password123")
        self.alice = self.store.create("alice", "password123", seed_settings={"submit_enabled": True})
        self.bob = self.store.create("bob", "password123")
        self.tokens = SessionTokenStore()
        self.sessions: dict[str, LoginSession] = {}

        async def close_session(session_id: str) -> None:
            self.sessions.pop(session_id, None)

        self.fake_auth = SimpleNamespace(sessions=self.sessions, close_session=AsyncMock(side_effect=close_session),
                                         get=AsyncMock(), refresh_status=AsyncMock())
        self.runtimes: dict = {}
        for patcher in (patch.object(main, "account_store", self.store), patch.object(main, "auth", self.fake_auth),
                        patch.object(main, "session_tokens", self.tokens), patch.dict(main.runtimes, self.runtimes, clear=True)):
            patcher.start()
            self.addCleanup(patcher.stop)
        for account in (self.admin, self.alice, self.bob):
            await main.ensure_runtime(account)
            runtime = main.runtimes[account.id]
            self.addCleanup(lambda rt=runtime: Path(rt.automation.history.path).unlink(missing_ok=True))
            self.addAsyncCleanup(runtime.daily.close)
            self.addAsyncCleanup(runtime.automation.stop)
        self.alice_rt = main.runtimes[self.alice.id]

    async def run_task(self, runtime, session_id: str, mode: str = "once") -> None:
        runtime.automation.session_id = session_id
        runtime.automation.mode = mode
        runtime.automation.task = asyncio.create_task(asyncio.sleep(3600))

    async def test_non_admin_is_refused(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            await main.require_admin(account=self.alice)
        self.assertEqual(caught.exception.status_code, 403)
        self.assertIs(await main.require_admin(account=self.admin), self.admin)

    async def test_list_summarises_every_user_with_own_session_counts(self) -> None:
        self.sessions["a1"] = make_session("a1", self.alice.id)
        self.sessions["b1"] = make_session("b1", self.bob.id, status="waiting_scan")
        self.sessions["b1"].sso_authenticated = False
        result = await main.admin_list_users(admin=self.admin)
        by_name = {u["username"]: u for u in result["users"]}
        self.assertEqual(set(by_name), {"boss", "alice", "bob"})
        self.assertEqual((by_name["alice"]["sessions"], by_name["alice"]["logged_in"]), (1, True))
        self.assertEqual((by_name["bob"]["sessions"], by_name["bob"]["logged_in"]), (1, False))
        self.assertTrue(by_name["alice"]["submit_enabled"])
        self.assertEqual(result["you"], self.admin.id)

    async def test_detail_never_exposes_session_ids_of_the_task_or_credentials(self) -> None:
        self.sessions["a1"] = make_session("a1", self.alice.id)
        self.sessions["a1"].user_info = {"token": "secret-token", "userName": "Alice"}
        await self.run_task(self.alice_rt, "a1")
        detail = await main.admin_user_detail(self.alice.id, admin=self.admin)
        self.assertNotIn("session_id", detail["automation"])
        self.assertNotIn("secret-token", str(detail))
        self.assertTrue(detail["sessions"][0]["used_by_automation"])
        self.assertNotIn("password", str(detail).lower())

    async def test_settings_update_persists_and_validates(self) -> None:
        result = await main.admin_update_settings(
            self.alice.id, main.AdminSettingsUpdate(start_time="09:00", end_time="12:30", seat_names=["F4B001"]),
            admin=self.admin)
        self.assertEqual(result["settings"]["start_time"], "09:00")
        stored = self.store.load_settings(self.alice.id)
        self.assertEqual((stored.start_time, stored.end_time, stored.seat_names), ("09:00", "12:30", ["F4B001"]))
        self.assertEqual(self.alice_rt.automation.settings.start_time, "09:00")
        # Other accounts are untouched.
        self.assertEqual(self.store.load_settings(self.bob.id).start_time, "07:00")
        for bad in ({"start_time": "9am"}, {"auto_trigger_time": "25:00"}, {"target_date": "next week"},
                    {"seat_names": ["  "]}, {"poll_interval_seconds": "x"}):
            with self.subTest(bad=bad), self.assertRaises((HTTPException, ValueError)):
                await main.admin_update_settings(self.alice.id, main.AdminSettingsUpdate(**bad), admin=self.admin)

    async def test_settings_update_rejects_loose_formats_and_degenerate_intervals(self) -> None:
        for bad in ({"start_time": "7:5"}, {"auto_trigger_time": "7:5:1"}, {"auto_trigger_time": "7:5"},
                    {"target_date": "20261016"}, {"target_date": "2026-W42-5"}):
            with self.subTest(bad=bad), self.assertRaises(HTTPException) as ctx:
                await main.admin_update_settings(self.alice.id, main.AdminSettingsUpdate(**bad), admin=self.admin)
            self.assertEqual(ctx.exception.status_code, 422)
        for bad in ({"poll_interval_seconds": "NaN"}, {"poll_interval_seconds": "Infinity"},
                    {"poll_interval_seconds": -1}, {"keepalive_interval_seconds": 0},
                    {"auto_retry_interval_seconds": -1}):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                main.AdminSettingsUpdate(**bad)
        good = await main.admin_update_settings(
            self.alice.id, main.AdminSettingsUpdate(auto_trigger_time="07:05:01", target_date="2026-10-18"),
            admin=self.admin)
        self.assertEqual(good["settings"]["auto_trigger_time"], "07:05:01")

    async def test_admin_edits_auto_extend_settings(self) -> None:
        result = await main.admin_update_settings(self.alice.id, main.AdminSettingsUpdate(
            auto_extend_enabled=True, auto_extend_after="08:30", auto_extend_interval_minutes=3,
            auto_extend_checkin_minutes=15, auto_extend_temp_leave_minutes=25), admin=self.admin)
        self.assertEqual({k: v for k, v in result["settings"].items() if k.startswith("auto_extend")},
                         {"auto_extend_enabled": True, "auto_extend_after": "08:30", "auto_extend_interval_minutes": 3,
                          "auto_extend_checkin_minutes": 15, "auto_extend_temp_leave_minutes": 25})
        self.assertTrue(self.store.load_settings(self.alice.id).auto_extend_enabled)
        with self.assertRaises(HTTPException) as ctx:
            await main.admin_update_settings(self.alice.id, main.AdminSettingsUpdate(auto_extend_interval_minutes=15),
                                             admin=self.admin)
        self.assertEqual(ctx.exception.status_code, 422)
        with self.assertRaises(ValueError):
            main.AdminSettingsUpdate(auto_extend_after="8:30")

    async def test_settings_update_rejects_empty_and_unknown_user(self) -> None:
        with self.assertRaises(HTTPException) as empty:
            await main.admin_update_settings(self.alice.id, main.AdminSettingsUpdate(), admin=self.admin)
        self.assertEqual(empty.exception.status_code, 422)
        with self.assertRaises(HTTPException) as missing:
            await main.admin_update_settings("nope", main.AdminSettingsUpdate(room_name="x"), admin=self.admin)
        self.assertEqual(missing.exception.status_code, 404)

    async def test_disabling_daily_stops_its_running_task(self) -> None:
        await main.admin_update_settings(self.alice.id, main.AdminSettingsUpdate(daily_auto_enabled=True), admin=self.admin)
        await self.run_task(self.alice_rt, "a1", mode="daily")
        await main.admin_update_settings(self.alice.id, main.AdminSettingsUpdate(daily_auto_enabled=False), admin=self.admin)
        self.assertFalse(self.alice_rt.automation.running)
        self.assertFalse(self.store.load_settings(self.alice.id).daily_auto_enabled)

    async def test_stop_automation_also_disables_daily_so_it_is_not_rearmed(self) -> None:
        self.alice_rt.settings.daily_auto_enabled = True
        await self.run_task(self.alice_rt, "a1", mode="daily")
        result = await main.admin_stop_automation(self.alice.id, admin=self.admin)
        self.assertEqual(result, {"stopped": True, "daily_disabled": True})
        self.assertFalse(self.alice_rt.automation.running)
        self.assertFalse(self.store.load_settings(self.alice.id).daily_auto_enabled)

    async def test_close_session_only_reaches_that_users_sessions(self) -> None:
        self.sessions["a1"] = make_session("a1", self.alice.id)
        self.sessions["b1"] = make_session("b1", self.bob.id)
        with self.assertRaises(HTTPException) as caught:
            await main.admin_close_session(self.alice.id, "b1", admin=self.admin)
        self.assertEqual(caught.exception.status_code, 404)
        self.assertIn("b1", self.sessions)
        await self.run_task(self.alice_rt, "a1")
        result = await main.admin_close_session(self.alice.id, "a1", admin=self.admin)
        self.assertTrue(result["automation_stopped"])
        self.assertNotIn("a1", self.sessions)

    async def test_password_reset_changes_login_and_signs_user_out(self) -> None:
        token = self.tokens.issue(self.alice.id)
        other = self.tokens.issue(self.bob.id)
        result = await main.admin_reset_password(self.alice.id, main.PasswordReset(password="brand-new-pass"), admin=self.admin)
        self.assertEqual(result["signed_out"], 1)
        self.assertIsNone(self.tokens.resolve(token))
        self.assertIsNotNone(self.tokens.resolve(other))
        self.assertIsNone(self.store.verify("alice", "password123"))
        self.assertIsNotNone(self.store.verify("alice", "brand-new-pass"))
        with self.assertRaises(HTTPException) as short:
            await main.admin_reset_password(self.alice.id, main.PasswordReset(password="short"), admin=self.admin)
        self.assertEqual(short.exception.status_code, 422)

    async def test_role_changes_but_never_your_own(self) -> None:
        await main.admin_set_role(self.alice.id, main.RoleUpdate(is_admin=True), admin=self.admin)
        self.assertTrue(self.store.get(self.alice.id).is_admin)
        await main.admin_set_role(self.alice.id, main.RoleUpdate(is_admin=False), admin=self.admin)
        self.assertFalse(self.store.get(self.alice.id).is_admin)
        with self.assertRaises(HTTPException) as caught:
            await main.admin_set_role(self.admin.id, main.RoleUpdate(is_admin=False), admin=self.admin)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertTrue(self.store.get(self.admin.id).is_admin)

    async def test_delete_user_removes_everything_but_keeps_history(self) -> None:
        self.sessions["a1"] = make_session("a1", self.alice.id)
        self.sessions["b1"] = make_session("b1", self.bob.id)
        token = self.tokens.issue(self.alice.id)
        await self.run_task(self.alice_rt, "a1", mode="daily")
        history_path = Path(self.alice_rt.automation.history.path)
        self.alice_rt.automation.history.save({"task_id": "t1", "started_at": "2026-10-01T00:00:00+08:00",
                                               "finished_at": "2026-10-01T00:01:00+08:00", "state": "success"})
        archived = history_path.parent / "deleted" / history_path.name
        self.addCleanup(lambda: archived.unlink(missing_ok=True))

        result = await main.admin_delete_user(self.alice.id, admin=self.admin)

        self.assertEqual(result, {"status": "deleted", "history_archived": True})
        self.assertIsNone(self.store.get(self.alice.id))
        self.assertNotIn(self.alice.id, main.runtimes)
        self.assertEqual(set(self.sessions), {"b1"})
        self.assertFalse(self.alice_rt.automation.running)
        self.assertIsNone(self.tokens.resolve(token))
        self.assertTrue(archived.exists() and not history_path.exists())
        with self.assertRaises(HTTPException) as gone:
            await main.current_account(session_token=token)
        self.assertEqual(gone.exception.status_code, 401)
        # Bob and the admin are unaffected.
        self.assertIn(self.bob.id, main.runtimes)
        self.assertIsNotNone(self.store.get(self.bob.id))

    async def test_cannot_delete_yourself(self) -> None:
        with self.assertRaises(HTTPException) as caught:
            await main.admin_delete_user(self.admin.id, admin=self.admin)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIsNotNone(self.store.get(self.admin.id))


if __name__ == "__main__":
    unittest.main()

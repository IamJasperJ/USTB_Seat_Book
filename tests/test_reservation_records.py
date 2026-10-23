from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

import httpx

from app import main
from app.accounts import Account
from app.config import Settings
from app.library import DEFAULT_TIME_RULE, SeatApi, beijing_now, build_room_seat_view
from tests.support.fake_library import DEFAULT_USER, ROOM_ID, FakeLibrary, create_app


class ReservationRecordTests(unittest.TestCase):
    def record(self, status=1, **changes):
        row = {"uuid": "uuid-1", "resvId": 123, "appAccNo": "1001", "logonName": "alice",
               "resvBeginTime": "2026-10-21 08:00:00", "resvEndTime": "2026-10-21 22:00:00",
               "resvStatus": status, "endEarly": False,
               "resvDevInfoList": [{"devId": 116, "devName": "F4B116", "roomId": ROOM_ID,
                                    "roomName": "图西楼四楼北区"}], **changes}
        return main._reservation_records({"code": 0, "data": [row]},
                                         {"accNo": 1001, "logonName": "alice"})[0]

    def test_permissions_follow_status_bits(self):
        # status, site endEarly flag, cancel, end early, temp leave, release action
        cases = [
            (1 | 2, False, True, False, False, "cancel"),
            (1 | 2, True, True, True, False, "cancel"),
            (1 | 4, False, False, False, False, None),
            (1 | 4, True, False, True, False, "end_early"),
            (1 | 4 | 64, True, False, True, True, "end_early"),
            (1 | 4 | 64 | 2048, True, False, True, False, "end_early"),
            (1 | 4 | 64 | 128, False, False, False, False, None),
            # A finished reservation can't be ended early even if the site still sends endEarly.
            (1 | 4 | 64 | 128, True, False, False, False, None),
            (1 | 128, False, False, False, False, None),
        ]
        for status, end_early, cancel, end, leave, action in cases:
            with self.subTest(status=status, endEarly=end_early):
                r = self.record(status, endEarly=end_early)
                self.assertEqual((r["cancellable"], r["end_early_allowed"], r["temp_leave_allowed"],
                                  r["rebook_action"]), (cancel, end, leave, action))
                self.assertEqual(r["signed_in"], bool(status & 64))
                self.assertEqual(r["finished"], bool(status & 128))
                self.assertEqual(r["on_temp_leave"], bool(status & 2048))

    def test_check_in_reminder_follows_the_site(self):
        due = self.record(1 | 4, latestCheckInTime=1792549800000)
        self.assertTrue(due["needs_check_in"])
        self.assertEqual(due["latest_check_in_time"], 1792549800000)
        for status in (1 | 4 | 64, 1 | 4 | 128, 1 | 2):
            with self.subTest(status=status):
                self.assertFalse(self.record(status)["needs_check_in"])

    def test_foreign_account_cannot_act(self):
        for status in (1 | 2, 1 | 4 | 64, 1 | 4 | 64 | 2048):
            for identity in ({"appAccNo": "2002"}, {"appAccNo": None, "logonName": "bob"}):
                with self.subTest(status=status, identity=identity):
                    r = self.record(status, endEarly=True, **identity)
                    self.assertFalse(r["cancellable"])
                    self.assertFalse(r["end_early_allowed"])
                    self.assertFalse(r["temp_leave_allowed"])
                    self.assertIsNone(r["rebook_action"])

    def test_labels_and_existing_fields(self):
        labels = {1: "预约成功", 2: "未生效", 4: "已生效", 8: "待支付", 16: "违约",
                  32: "已支付", 64: "已签到", 128: "已结束", 256: "审核中", 512: "审核未通过",
                  1024: "审核通过", 2048: "暂离", 8192: "待同意"}
        for bit, label in labels.items():
            with self.subTest(bit=bit):
                self.assertEqual(self.record(bit)["status_labels"], [label])
        r = self.record(sum(labels), tempLeaveEndTime="2026-10-21 11:00:00")
        self.assertEqual(r["status_labels"], list(labels.values()))
        self.assertEqual((r["dev_id"], r["room_id"]), (116, ROOM_ID))
        self.assertEqual(r["temp_leave_end_time"], "2026-10-21 11:00:00")
        self.assertEqual({k: r[k] for k in ("uuid", "resv_id", "begin_time", "end_time", "status", "devices")},
                         {"uuid": "uuid-1", "resv_id": 123, "begin_time": "2026-10-21 08:00:00",
                          "end_time": "2026-10-21 22:00:00", "status": sum(labels),
                          "devices": [{"name": "F4B116", "room": "图西楼四楼北区"}]})

    def test_optional_device_fields(self):
        for devices in (None, [], {}, [None], [{"devName": "F4B116"}]):
            with self.subTest(devices=devices):
                r = self.record(resvDevInfoList=devices)
                self.assertIsNone(r["dev_id"])
                self.assertIsNone(r["room_id"])
        self.assertFalse(self.record(uuid=None)["cancellable"])

    def test_runtime_has_distinct_operation_lock(self):
        # Avoid opening automation-history stores for this lock-only assertion.
        with patch.object(main, "AutoReservation"), patch.object(main, "DailyReservation"):
            a = main.UserRuntime(Account(id="a", username="alice", is_admin=False), Settings(), main.auth)
            b = main.UserRuntime(Account(id="b", username="bob", is_admin=False), Settings(), main.auth)
        self.assertIsInstance(a.operation_lock, asyncio.Lock)
        self.assertIsNot(a.operation_lock, b.operation_lock)
        self.assertFalse(a.operation_lock.locked())

    def test_beijing_clock_is_naive_wall_time(self):
        from zoneinfo import ZoneInfo
        before = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        now = beijing_now()
        after = datetime.now(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        self.assertIsNone(now.tzinfo)
        self.assertLessEqual(before, now)
        self.assertLessEqual(now, after)


class FakeLibraryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = FakeLibrary()
        self.state.clock = datetime(2026, 10, 21, 10)
        self.app = create_app(self.state)
        self.api = SeatApi(Settings(base_url="http://fake"), {}, DEFAULT_USER)
        await self.api.client.aclose()
        self.api.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                          base_url="http://fake", headers={"token": DEFAULT_USER["token"]})
        self.addAsyncCleanup(self.api.close)

    async def test_official_actions_use_distinct_identifiers(self):
        row = self.state.seed({})
        result = await self.api.temp_leave(row["resvId"])
        self.assertEqual(result["code"], 0)
        self.assertTrue(row["resvStatus"] & 2048)
        self.assertEqual(row["tempLeaveEndTime"], "2026-10-21 11:00:00")
        self.assertNotEqual((await self.api.temp_leave(row["uuid"]))["code"], 0)
        self.assertNotEqual((await self.api.end_ahead(str(row["resvId"])))["code"], 0)
        result = await self.api.end_ahead(row["uuid"])
        self.assertEqual(result["code"], 0)
        self.assertTrue(row["resvStatus"] & 128)
        self.assertEqual(row["resvEndTime"], "2026-10-21 10:00:00")

    async def test_rules_seats_and_date_filtered_own_rows(self):
        today = self.state.now().date()
        original_rule = deepcopy(DEFAULT_TIME_RULE)
        row = self.state.seed({})
        self.state.seed({"day": "2026-10-22"})
        self.state.user_for("bob")
        self.state.seed({"token": "qa-bob"})
        result = await self.api.my_reservations(today)
        self.assertEqual([r["uuid"] for r in result["data"]], [row["uuid"]])
        seats = build_room_seat_view(await self.api.seats(today))
        self.assertEqual(len(seats), 7)
        self.assertEqual(seats[2]["dev_name"], "F4B116")
        self.assertEqual(seats[2]["time_options"][0]["start"], "07:00")
        self.assertEqual(seats[2]["time_options"][0]["ends"][0], "07:30")
        self.assertEqual(seats[2]["time_options"][0]["ends"][-1], "22:15")
        self.assertEqual(len(seats[2]["reservations"]), 2)
        self.assertEqual(DEFAULT_TIME_RULE, original_rule)
        self.assertEqual((await self.api.areas())["data"][0]["roomId"], ROOM_ID)

    async def test_reserve_rejects_overlap_and_bad_grid_then_cancel(self):
        seat = self.state.seats[2]["devId"]
        begin, end = "2026-10-21 12:00:00", "2026-10-21 22:00:00"
        result = await self.api.reserve(seat, begin, end)
        self.assertEqual(result["code"], 0)
        self.assertNotEqual((await self.api.reserve(seat, begin, end))["code"], 0)
        self.assertNotEqual((await self.api.reserve(seat, "2026-10-21 07:01:00", "2026-10-21 08:00:00"))["code"], 0)
        self.assertEqual((await self.api.cancel_reservation(result["data"]["uuid"]))["code"], 0)
        self.assertFalse(self.state.rows)

    async def test_injected_failures_are_once_and_do_not_mutate(self):
        row = self.state.seed({})
        for action, call in (("tempLeave", lambda: self.api.temp_leave(row["resvId"])),
                             ("endAhaed", lambda: self.api.end_ahead(row["uuid"])),
                             ("cancel", lambda: self.api.cancel_reservation(row["uuid"]))):
            await self.api.client.post("/__fake/failures", json={"action": action, "message": "测试失败"})
            before = deepcopy(row)
            self.assertEqual((await call())["message"], "测试失败")
            self.assertEqual(row, before)
            self.assertNotIn(action, self.state.failures)
        await self.api.client.post("/__fake/failures", json={"action": "reserve", "message": "不能预约"})
        self.assertEqual((await self.api.reserve(self.state.seats[0]["devId"],
                                               "2026-10-21 12:00:00", "2026-10-21 22:00:00"))["message"], "不能预约")
        self.assertEqual(len(self.state.rows), 1)

    async def test_timeout_can_lose_response_after_creation(self):
        await self.api.client.post("/__fake/failures", json={"action": "reserve", "mode": "timeout",
                                                             "seconds": 1, "after": True})
        with self.assertRaises(TimeoutError):
            await asyncio.wait_for(self.api.reserve(self.state.seats[0]["devId"],
                                                    "2026-10-21 12:00:00", "2026-10-21 22:00:00"), .05)
        result = await self.api.my_reservations(self.state.now().date())
        self.assertEqual(len(result["data"]), 1)
        self.assertFalse(self.state.failures)

    async def test_controls_and_token_validation(self):
        response = await self.api.client.get("/ic-web/auth/userInfo")
        self.assertEqual(response.json()["data"], DEFAULT_USER)
        response = await self.api.client.get("/ic-web/auth/userInfo", headers={"token": "wrong"})
        self.assertEqual(response.status_code, 401)
        await self.api.client.post("/__fake/clock", json={"now": "2026-10-22T09:00:00"})
        response = await self.api.client.post("/__fake/seed", json={})
        self.assertTrue(response.json()["data"]["resvBeginTime"].startswith("2026-10-22"))
        await self.api.client.post("/__fake/reset")
        self.assertEqual(self.state.rows, [])
        self.assertIsNone(self.state.clock)


class QAServerSmokeTests(unittest.TestCase):
    def test_fake_login_and_mine_via_real_http(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp, socket.socket() as a, socket.socket() as b:
            a.bind(("127.0.0.1", 0))
            b.bind(("127.0.0.1", 0))
            port, library_port = a.getsockname()[1], b.getsockname()[1]
            a.close()
            b.close()
            workdir = Path(tmp) / "work"
            with (Path(tmp) / "server.log").open("w+") as log:
                process = subprocess.Popen([sys.executable, "-m", "tests.support.qa_server", "--port", str(port),
                                            "--library-port", str(library_port), "--workdir", str(workdir)],
                                           cwd=root, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                                           stdout=log, stderr=subprocess.STDOUT)
                try:
                    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2, trust_env=False) as client:
                        deadline = time.monotonic() + 15
                        while True:
                            if process.poll() is not None:
                                log.seek(0)
                                self.fail(log.read())
                            try:
                                if client.get("/").status_code == 200:
                                    break
                            except httpx.TransportError:
                                pass
                            if time.monotonic() > deadline:
                                log.seek(0)
                                self.fail("QA server did not start: " + log.read())
                            time.sleep(.05)
                        response = client.post("/api/account/register", json={"username": "qa_alice", "password": "correct-horse"})
                        self.assertEqual(response.status_code, 200, response.text)
                        response = client.post("/api/auth/sessions")
                        self.assertEqual(response.status_code, 200, response.text)
                        login = response.json()
                        self.assertEqual(login["status"], "authenticated")
                        qr = client.get(login["qr_url"])
                        self.assertEqual(qr.status_code, 200)
                        self.assertTrue(qr.content.startswith(b"\x89PNG"))
                        with httpx.Client(base_url=f"http://127.0.0.1:{library_port}", trust_env=False) as library:
                            self.assertEqual(library.post("/__fake/seed", json={}).status_code, 200)
                        response = client.get("/api/reservations/mine", params={"auth_session_id": login["session_id"], "day": "today"})
                        self.assertEqual(response.status_code, 200, response.text)
                        records = response.json()["records"]
                        self.assertEqual(len(records), 1)
                        self.assertTrue(records[0]["temp_leave_allowed"])
                        self.assertEqual(records[0]["dev_id"], 102057116)
                        status = client.get(login["status_url"]).json()
                        self.assertFalse(status["keepalive_running"])
                        self.assertFalse(status["keepalive_enabled"])
                        # Script workflow: only Basic auth, no app cookie or SSO browser.
                        client.cookies.clear()
                        client.auth = ("qa_alice", "correct-horse")
                        with httpx.Client(base_url=f"http://127.0.0.1:{library_port}", trust_env=False) as library:
                            library.post("/__fake/clock", json={"now": "2026-10-21T10:26:00"}).raise_for_status()
                        defaults = client.get("/api/time-rule/default", params={"day": "today"})
                        self.assertEqual(defaults.status_code, 200, defaults.text)
                        self.assertEqual(defaults.json()["time_options"][0],
                                         {"start": "10:30", "ends": [f"{m // 60:02d}:{m % 60:02d}" for m in range(660, 1336, 5)]})
                        self.assertEqual(defaults.json()["target_date"], "2026-10-21")
                        self.assertNotIn("session_token", client.cookies)
                        self.assertEqual(client.get("/api/time-rule/default?day=tomorrow").json()["time_options"][0]["start"], "07:00")
                        self.assertEqual(client.get("/api/time-rule/default?day=invalid").status_code, 422)
                        params = {"auth_session_id": login["session_id"], "date": "today"}
                        for path in (f"/api/rooms/{ROOM_ID}/availability", f"/api/rooms/{ROOM_ID}/seats/F4B114/slots"):
                            response = client.get(path, params=params)
                            self.assertEqual(response.status_code, 200, response.text)
                            body = response.json()
                            seat = body.get("seat") or body["seats"][0]
                            self.assertEqual(seat["time_options"][0]["start"], "10:30")
                            free = body.get("free_time_options", seat.get("free_time_options"))
                            self.assertEqual(free[0]["start"], "10:30")
                        request = {"auth_session_id": login["session_id"], "room_id": ROOM_ID,
                                   "seat": "F4B114", "date": "today", "start": "07:00", "end": "12:00", "dry_run": True}
                        self.assertEqual(client.post("/api/reservations", json=request).status_code, 409)
                        request["start"] = "10:30"
                        preview = client.post("/api/reservations", json=request)
                        self.assertEqual(preview.status_code, 200, preview.text)
                        self.assertTrue(preview.json()["would_submit"])
                        self.assertEqual(preview.json()["begin"], "2026-10-21 10:30:00")
                        # Full temporary-leave flow through Basic auth and both HTTP servers.
                        with httpx.Client(base_url=f"http://127.0.0.1:{library_port}", trust_env=False) as library:
                            row = library.post("/__fake/seed", json={}).json()["data"]
                        leave = client.post("/api/reservations/temp-leave", json={
                            "auth_session_id": login["session_id"], "uuid": row["uuid"], "confirm": True})
                        self.assertEqual(leave.status_code, 200, leave.text)
                        self.assertEqual(leave.json()["temp_leave_end_time"], "2026-10-21 11:26:00")
                        self.assertTrue(leave.json()["success"])
                        mine = client.get("/api/reservations/mine", params={
                            "auth_session_id": login["session_id"], "day": "today"}).json()
                        updated = next(r for r in mine["records"] if r["uuid"] == row["uuid"])
                        self.assertTrue(updated["on_temp_leave"])
                        self.assertFalse(updated["temp_leave_allowed"])
                        self.assertFalse(client.cookies)
                        with httpx.Client(base_url=f"http://127.0.0.1:{port}", trust_env=False) as anonymous:
                            self.assertEqual(anonymous.get("/api/time-rule/default").status_code, 401)
                        self.assertTrue((workdir / "config.json").is_file())
                        self.assertTrue((workdir / "logs/accounts.sqlite3").is_file())
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()

"""Automatic booking tries every configured seat, skipping only real conflicts."""
from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import library
from app.automation import AutoReservation
from app.config import Settings
from app.library import SeatApi
from tests.support.fake_library import DEFAULT_USER, FakeLibrary, create_app

OTHER = {"token": "other-token", "accNo": "other", "logonName": "other", "userName": "别人"}
TOMORROW = date(2026, 10, 22)


class CandidateFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.state = FakeLibrary()
        self.state.clock = datetime(2026, 10, 21, 5, 55)
        self.state.users[OTHER["token"]] = dict(OTHER)
        self.settings = Settings(seat_names=["F4B114", "F4B115", "F4B116"], start_time="08:00",
                                 end_time="12:00", target_date="tomorrow", submit_enabled=True)
        self.client = SeatApi(self.settings, {}, DEFAULT_USER)
        await self.client.client.aclose()
        self.client.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(self.state)),
                                               base_url="http://fake", headers={"token": DEFAULT_USER["token"]})
        self.client.close = AsyncMock()
        self.addAsyncCleanup(self.client.client.aclose)
        session = SimpleNamespace(status="authenticated", owner_account_id="acct")
        auth = SimpleNamespace(get=AsyncMock(return_value=session),
                               credentials=AsyncMock(return_value=({}, DEFAULT_USER)))
        self.auto = AutoReservation(self.settings, auth, account_id="acct")
        self.auto.session_id = "s1"
        self.auto.target_date = TOMORROW
        for p in (patch("app.automation.SeatApi", return_value=self.client),
                  patch.object(library, "beijing_now", return_value=self.state.clock)):
            p.start()
            self.addCleanup(p.stop)

    def taken_by_other(self, seat: str, begin: str, end: str) -> None:
        self.state.seed({"token": OTHER["token"], "seat": seat, "day": TOMORROW.isoformat(),
                         "begin": begin, "end": end, "status": 1 | 2})

    def own_bookings(self) -> list[str]:
        return [r["resvDevInfoList"][0]["devName"] for r in self.state.rows
                if r["appAccNo"] == DEFAULT_USER["accNo"]]

    async def test_first_seat_overlapping_falls_through_to_the_next(self) -> None:
        self.taken_by_other("F4B114", "09:00", "10:00")
        self.assertTrue(await self.auto._attempt_once())
        self.assertEqual(self.auto.reserved_seat, "F4B115")
        self.assertEqual(self.own_bookings(), ["F4B115"])

    async def test_a_booking_that_does_not_overlap_is_not_a_reason_to_skip(self) -> None:
        self.taken_by_other("F4B114", "13:00", "15:00")
        self.assertTrue(await self.auto._attempt_once())
        self.assertEqual(self.auto.reserved_seat, "F4B114")

    async def test_library_refusal_for_one_seat_moves_on(self) -> None:
        # The seat list looks free, but the library refuses the first seat (taken a moment ago).
        real_reserve = self.client.reserve

        async def reserve(dev_id, begin, end):
            if not self.own_bookings() and reserve.calls == 0:
                reserve.calls += 1
                return {"code": 1, "message": "座位已被预约"}
            return await real_reserve(dev_id, begin, end)
        reserve.calls = 0
        self.client.reserve = reserve
        self.assertTrue(await self.auto._attempt_once())
        self.assertEqual(self.auto.reserved_seat, "F4B115")

    async def test_all_seats_taken_reports_each_reason(self) -> None:
        for seat in ("F4B114", "F4B115", "F4B116"):
            self.taken_by_other(seat, "07:00", "09:00")
        with self.assertRaises(RuntimeError) as ctx:
            await self.auto._attempt_once()
        message = str(ctx.exception)
        for seat in ("F4B114", "F4B115", "F4B116"):
            self.assertIn(f"{seat}：所选时段已被预约", message)
        self.assertEqual(self.own_bookings(), [])

    async def test_account_level_refusal_stops_without_trying_other_seats(self) -> None:
        self.client.reserve = AsyncMock(return_value={"code": 1, "message": "超出预约数量"})
        with self.assertRaisesRegex(RuntimeError, "超出预约数量"):
            await self.auto._attempt_once()
        self.assertEqual(self.client.reserve.await_count, 1)

    async def test_unanswered_submit_is_checked_before_booking_another_seat(self) -> None:
        real_reserve = self.client.reserve

        async def booked_then_lost(dev_id, begin, end):
            await real_reserve(dev_id, begin, end)
            raise httpx.ReadTimeout("response lost")
        self.client.reserve = booked_then_lost
        with self.assertRaises(httpx.ReadTimeout):
            await self.auto._attempt_once()
        # The next attempt must find the booking instead of taking a second seat.
        self.client.reserve = AsyncMock(side_effect=AssertionError("must not submit again"))
        self.assertTrue(await self.auto._attempt_once())
        self.assertEqual(self.auto.reserved_seat, "F4B114")
        self.assertEqual(self.own_bookings(), ["F4B114"])

    async def test_unanswered_submit_that_did_not_land_retries_normally(self) -> None:
        self.client.reserve, real_reserve = AsyncMock(side_effect=httpx.ReadTimeout("lost")), self.client.reserve
        with self.assertRaises(httpx.ReadTimeout):
            await self.auto._attempt_once()
        self.client.reserve = real_reserve
        self.assertTrue(await self.auto._attempt_once())
        self.assertEqual(self.own_bookings(), ["F4B114"])

    async def test_library_down_before_opening_does_not_touch_seats_or_bookings(self) -> None:
        self.client.seats = AsyncMock(side_effect=httpx.ConnectError("library not up yet"))
        self.client.reserve = AsyncMock()
        with self.assertRaises(httpx.ConnectError):
            await self.auto._attempt_once()
        self.client.reserve.assert_not_awaited()
        # Nothing was submitted, so the next attempt needn't check 我的预约 first.
        self.assertFalse(self.auto._submit_uncertain)

    async def test_pre_opening_refusals_keep_retrying_until_booking_opens(self) -> None:
        # Seats are listed but the library refuses every submit until booking opens.
        real_reserve = self.client.reserve
        attempts = {"n": 0}

        calls = []

        async def reserve(dev_id, begin, end):
            calls.append((attempts["n"], dev_id))
            if attempts["n"] < 3:
                return {"code": 1, "message": "未到预约开放时间"}
            return await real_reserve(dev_id, begin, end)
        self.client.reserve = reserve

        async def next_attempt(_seconds):
            attempts["n"] += 1
        self.auto._wait_for_trigger = AsyncMock()
        self.auto._sleep_in_chunks = next_attempt
        self.auto.mode = "daily"
        await self.auto._run()
        self.assertEqual(self.auto.state, "success")
        self.assertEqual(self.auto.attempts, 4)
        self.assertEqual(self.own_bookings(), ["F4B114"])
        # Before opening: one request per attempt, always the first seat — no fallback.
        self.assertEqual([attempt for attempt, _ in calls], [0, 1, 2, 3])
        self.assertEqual({dev for _, dev in calls}, {self.state.seats[0]["devId"]})

    async def test_unrecognised_refusal_does_not_fall_back_but_next_retry_skips_by_seat_data(self) -> None:
        # Someone books F4B114 while we submit; the library's wording isn't one we recognise.
        real_reserve = self.client.reserve

        async def reserve(dev_id, begin, end):
            if dev_id == self.state.seats[0]["devId"]:
                self.taken_by_other("F4B114", "08:00", "12:00")
                return {"code": 1, "message": "操作失败"}
            return await real_reserve(dev_id, begin, end)
        self.client.reserve = reserve
        with self.assertRaisesRegex(RuntimeError, "操作失败"):
            await self.auto._attempt_once()
        self.assertEqual(self.own_bookings(), [])
        self.assertTrue(await self.auto._attempt_once())
        self.assertEqual(self.auto.reserved_seat, "F4B115")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import unittest
import tempfile
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import control_api, library, main
from app.accounts import Account, AccountStore, RegistrationRateLimiter
from app.automation import AutoReservation
from app.config import Settings
from tests.support.fake_library import DEFAULT_USER, ROOM_ID, FakeLibrary


class TimeOptionTests(unittest.TestCase):
    def options(self, now, day=date(2026, 10, 21), rule=None):
        return library.build_time_options(rule or library.DEFAULT_TIME_RULE, day=day, now=now)

    def test_today_rounding_and_closing(self):
        cases = [(datetime(2026, 10, 21, 10, 26), '10:30'),
                 (datetime(2026, 10, 21, 10, 30), '10:30'),
                 (datetime(2026, 10, 21, 10, 30, 1), '10:35'),
                 (datetime(2026, 10, 21, 10, 30, 0, 1), '10:35'),
                 (datetime(2026, 10, 21, 21, 45), '21:45')]
        for now, first in cases:
            with self.subTest(now=now):
                options = self.options(now)
                self.assertEqual(options[0]['start'], first)
        self.assertEqual(self.options(cases[0][0])[0]['ends'][0], '11:00')
        for hour, minute in [(21, 46), (22, 0), (23, 0)]:
            self.assertEqual(self.options(datetime(2026, 10, 21, hour, minute)), [])

    def test_past_future_and_legacy(self):
        now = datetime(2026, 10, 21, 10, 26)
        unchanged = library.build_time_options(library.DEFAULT_TIME_RULE)
        self.assertEqual(self.options(now, day=now.date() - timedelta(days=1)), [])
        self.assertEqual(self.options(now, day=now.date() + timedelta(days=1)), unchanged)
        self.assertEqual(self.options(now, day=None), unchanged)
        with patch.object(library, 'beijing_now', return_value=now):
            self.assertEqual(library.build_time_options(library.DEFAULT_TIME_RULE, day=now.date()), self.options(now))

    def test_opening_grid_max_duration_and_multiple_windows(self):
        rule = deepcopy(library.DEFAULT_TIME_RULE)
        rule['resvRule'] = {'timeInterval': 5, 'minResvTime': 30, 'maxResvTime': 60}
        rule['openTimes'] = [{'openStartTime': '07:02', 'openEndTime': '12:02'},
                             {'openStartTime': '14:02', 'openEndTime': '18:02'}]
        options = self.options(datetime(2026, 10, 21, 10, 26), rule=rule)
        self.assertEqual(options[0]['start'], '10:27')
        self.assertEqual(options[0]['ends'][0], '10:57')
        self.assertEqual(options[0]['ends'][-1], '11:27')
        self.assertFalse(any('12:02' <= o['start'] < '14:02' for o in options))
        self.assertEqual(options[-1], {'start': '17:32', 'ends': ['18:02']})


class TimeThreadingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 21, 10, 26)
        self.account = Account(id='time-options-account', username='time_test', is_admin=False)
        self.settings = Settings(room_id=ROOM_ID, seat_names=['F4B116'], target_date='today',
                                 start_time='07:00', end_time='12:00', submit_enabled=True)
        self.runtime = main.UserRuntime(self.account, self.settings, main.auth)
        self.addCleanup(lambda: Path(self.runtime.automation.history.path).unlink(missing_ok=True))
        self.client = library.SeatApi(self.settings, {}, DEFAULT_USER)
        self.addAsyncCleanup(self.client.close)
        self.client.seats = AsyncMock(return_value={'code': 0, 'data': FakeLibrary().seats})
        self.client.reserve = AsyncMock(return_value={'code': 0})
        for p in (patch.object(library, 'beijing_now', return_value=self.now),
                  patch.object(main, 'ensure_runtime', AsyncMock(return_value=self.runtime)),
                  patch.object(main, 'authenticated_api', AsyncMock(return_value=self.client))):
            p.start()
            self.addCleanup(p.stop)

    async def test_seat_and_room_queries_filter_configured_day(self):
        for result in (await main.seats('s1', self.account), await main.room_seats(ROOM_ID, 's1', self.account)):
            self.assertEqual(result['target_date'], '2026-10-21')
            self.assertEqual(result['seats'][0]['time_options'][0]['start'], '10:30')
        result = await main.seats('s1', self.account, date_='tomorrow')
        self.assertEqual(result['time_options'][0]['start'], '07:00')
        self.assertEqual(main._reservation_day('today'), self.now.date())
        self.assertEqual(control_api._today(), self.now.date())

    async def test_explicit_and_configured_reserve_refuse_past_start(self):
        request = control_api.ReserveRequest(auth_session_id='s1', seat='F4B116', date='today',
                                             start='07:00', end='12:00', dry_run=True)
        with self.assertRaises(HTTPException) as ctx:
            await control_api.reserve_seat(request, self.account)
        self.assertEqual(ctx.exception.status_code, 409)
        with self.assertRaises(HTTPException) as ctx:
            await main.submit_reservation(main.SubmitRequest(auth_session_id='s1', confirm=True), self.account)
        self.assertEqual(ctx.exception.status_code, 409)
        self.client.reserve.assert_not_awaited()
        preview = await control_api.reserve_seat(request.model_copy(update={'start': '10:30'}), self.account)
        self.assertTrue(preview['would_submit'])

    async def test_automation_today_filters_but_tomorrow_keeps_early_start(self):
        session = SimpleNamespace(status='authenticated', owner_account_id=self.account.id)
        auth = SimpleNamespace(get=AsyncMock(return_value=session), credentials=AsyncMock(return_value=({}, DEFAULT_USER)))
        auto = AutoReservation(self.settings, auth, account_id=self.account.id)
        auto.session_id = 's1'
        with patch('app.automation.SeatApi', return_value=self.client):
            auto.target_date = self.now.date()
            with self.assertRaisesRegex(RuntimeError, '配置时间'):
                await auto._attempt_once()
            self.client.reserve.assert_not_awaited()
            auto.target_date += timedelta(days=1)
            self.assertTrue(await auto._attempt_once())
        self.assertIn('2026-10-22 07:00:00', self.client.reserve.await_args.args)


class DefaultRuleHttpTests(unittest.TestCase):
    def test_basic_only_before_library_login(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccountStore(Path(tmp) / 'accounts.sqlite3')
            store.create('time_user', 'time-password')
            with patch.object(main, 'account_store', store), \
                 patch.object(main, 'ensure_runtime', AsyncMock()), \
                 patch.object(main, 'monitor_auth_rate_limiter', RegistrationRateLimiter(30, 60)), \
                 patch.object(main, 'authenticated_api', AsyncMock()) as library_login, \
                 patch.object(library, 'beijing_now', return_value=datetime(2026, 10, 21, 10, 26)):
                client = TestClient(main.app)
                self.assertEqual(client.get('/api/time-rule/default').status_code, 401)
                response = client.get('/api/time-rule/default?day=today', auth=('time_user', 'time-password'))
                self.assertEqual(response.status_code, 200, response.text)
                result = response.json()
                self.assertEqual(result['resvRule'], library.DEFAULT_TIME_RULE['resvRule'])
                self.assertEqual(result['openTimes'], library.DEFAULT_TIME_RULE['openTimes'])
                self.assertEqual(result['time_options'][0]['start'], '10:30')
                self.assertEqual(result['time_options'][0]['ends'][0], '11:00')
                self.assertEqual(client.get('/api/time-rule/default', auth=('time_user', 'time-password')).json()['day'], 'tomorrow')
                bad = client.get('/api/time-rule/default?day=yesterday', auth=('time_user', 'time-password'))
                self.assertEqual(bad.status_code, 422)
                self.assertEqual(bad.json()['detail'], '日期应为 today 或 tomorrow')
                self.assertFalse(client.cookies)
                library_login.assert_not_awaited()

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app import main
from app.accounts import AccountStore, RegistrationRateLimiter
from app.auth import LoginSession
from app.config import Settings
from app.library import SeatApi
from tests.support.fake_library import DEFAULT_USER, FakeLibrary, create_app


class TempLeaveTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        store = AccountStore(Path(self.tmp.name) / 'accounts.sqlite3')
        self.account = store.create('alice', 'correct-horse')
        self.runtime = SimpleNamespace(operation_lock=asyncio.Lock(), settings=Settings())
        self.state = FakeLibrary()
        self.state.clock = datetime(2026, 10, 21, 10)
        fake = create_app(self.state)
        self.api = SeatApi(Settings(base_url='http://fake'), {}, DEFAULT_USER)
        await self.api.client.aclose()
        self.api.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=fake),
                                          base_url='http://fake', headers={'token': DEFAULT_USER['token']})
        self.addAsyncCleanup(self.api.close)
        session = LoginSession('own', AsyncMock(), None, status='authenticated',
                               owner_account_id=self.account.id, user_info=DEFAULT_USER)
        foreign = LoginSession('foreign', AsyncMock(), None, status='authenticated',
                               owner_account_id='bob', user_info=DEFAULT_USER)
        auth = SimpleNamespace(get=AsyncMock(side_effect=lambda sid: {'own': session, 'foreign': foreign}[sid]),
                               credentials=AsyncMock(return_value=({}, DEFAULT_USER)))
        for p in (patch.object(main, 'account_store', store),
                  patch.object(main, 'monitor_auth_rate_limiter', RegistrationRateLimiter(1000, 60)),
                  patch.object(main, 'ensure_runtime', AsyncMock(return_value=self.runtime)),
                  patch.object(self.api, 'close', AsyncMock()),
                  patch.object(main, 'auth', auth), patch.object(main, 'SeatApi', return_value=self.api),
                  patch.object(main.library, 'beijing_now', return_value=self.state.clock)):
            p.start()
            self.addCleanup(p.stop)
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url='http://app',
                                      auth=('alice', 'correct-horse'))
        self.addAsyncCleanup(self.http.aclose)

    async def post(self, row, **changes):
        payload = {'auth_session_id': 'own', 'uuid': row['uuid'], 'confirm': True, **changes}
        return await self.http.post('/api/reservations/temp-leave', json=payload)

    async def test_allowed_basic_only_and_refreshed_deadline(self):
        row = self.state.seed({})
        with self.assertLogs('ustb-seat-book', level='INFO') as logs:
            response = await self.post(row)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {'success': True, 'message': '操作成功',
                                          'temp_leave_end_time': '2026-10-21 11:00:00'})
        self.assertTrue(row['resvStatus'] & 2048)
        self.assertFalse(self.http.cookies)
        self.assertIn('reservation temp leave: account=alice resv_id=1 result=成功', logs.output[0])
        self.assertNotIn(DEFAULT_USER['token'], ''.join(logs.output))
        self.assertFalse(self.runtime.operation_lock.locked())

    async def test_refusal_reasons(self):
        for status, reason in ((1 | 4, '未签到'), (1 | 64 | 128, '已结束'), (1 | 64 | 2048, '已在暂离中')):
            with self.subTest(reason=reason):
                self.state.rows.clear()
                row = self.state.seed({'status': status})
                response = await self.post(row)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn(reason, response.json()['detail'])
                self.assertEqual(row['resvStatus'], status)

    async def test_foreign_row_returned_by_upstream_is_refused(self):
        row = self.state.seed({})
        foreign = {**row, 'appAccNo': 'bob'}
        with patch.object(self.api, 'my_reservations', AsyncMock(return_value={'code': 0, 'data': [foreign]})):
            response = await self.post(row)
        self.assertEqual(response.status_code, 409)
        self.assertIn('不是本人预约', response.json()['detail'])
        self.assertFalse(row['resvStatus'] & 2048)

    async def test_not_own_uuid_or_other_date_not_found(self):
        self.state.user_for('bob')
        for row in (self.state.seed({'token': 'qa-bob'}), self.state.seed({'day': '2026-10-22'})):
            response = await self.post(row)
            self.assertEqual(response.status_code, 404, response.text)
            self.assertFalse(row['resvStatus'] & 2048)

    async def test_confirm_missing_and_lock_contention(self):
        row = self.state.seed({})
        response = await self.http.post('/api/reservations/temp-leave', json={'auth_session_id': 'own', 'uuid': row['uuid']})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()['detail'], '请明确确认暂离')
        async with self.runtime.operation_lock:
            response = await self.post(row)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail'], '上一个预约操作仍在进行，请稍候')
        self.assertFalse(row['resvStatus'] & 2048)

    async def test_foreign_session_checks_authenticated_api_ownership(self):
        row = self.state.seed({})
        response = await self.post(row, auth_session_id='foreign')
        self.assertEqual(response.status_code, 404)
        main.auth.credentials.assert_not_awaited()
        main.SeatApi.assert_not_called()
        self.assertFalse(row['resvStatus'] & 2048)
        self.assertFalse(self.runtime.operation_lock.locked())

    async def test_library_failure_message(self):
        row = self.state.seed({})
        self.state.failures['tempLeave'] = {'message': '暂离次数已用完'}
        response = await self.post(row)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail'], '暂离次数已用完')
        self.assertFalse(row['resvStatus'] & 2048)

    async def test_timeout_confirmation_both_ways(self):
        original = self.api.temp_leave
        async def timed(resv_id):
            return await asyncio.wait_for(original(resv_id), .02)
        for after in (False, True):
            with self.subTest(after=after):
                self.state.rows.clear()
                row = self.state.seed({})
                self.state.failures['tempLeave'] = {'mode': 'timeout', 'seconds': 1, 'after': after}
                with patch.object(self.api, 'temp_leave', side_effect=timed):
                    response = await self.post(row)
                self.assertEqual(response.status_code, 200 if after else 502, response.text)
                if after:
                    self.assertTrue(response.json()['success'])
                    self.assertEqual(response.json()['temp_leave_end_time'], '2026-10-21 11:00:00')
                else:
                    self.assertEqual(response.json()['detail'], '暂离结果未确认，请刷新我的预约查看')
                self.assertFalse(self.runtime.operation_lock.locked())

    async def test_success_without_deadline_when_followup_fails(self):
        row = self.state.seed({})
        original = self.api.my_reservations
        first = await original(self.state.clock.date())
        with patch.object(self.api, 'my_reservations', AsyncMock(side_effect=[first, RuntimeError('offline')])):
            response = await self.post(row)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(response.json()['temp_leave_end_time'])

    async def test_timeout_and_failed_confirmation_query(self):
        row = self.state.seed({})
        first = await self.api.my_reservations(self.state.clock.date())
        with patch.object(self.api, 'temp_leave', AsyncMock(side_effect=httpx.ReadTimeout('lost'))), \
             patch.object(self.api, 'my_reservations', AsyncMock(side_effect=[first, RuntimeError('offline')])):
            response = await self.post(row)
        self.assertEqual(response.status_code, 502)
        self.assertFalse(row['resvStatus'] & 2048)

    async def test_epoch_millisecond_begin_time_counts_as_today(self):
        # The live site may return resvBeginTime as epoch milliseconds instead of a string.
        row = self.state.seed({})
        self.state.epoch_times = True
        response = await self.post(row)
        self.assertEqual(response.status_code, 200, response.text)

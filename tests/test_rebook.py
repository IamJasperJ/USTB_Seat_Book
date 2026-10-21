from copy import deepcopy
from datetime import datetime
import unittest
from unittest.mock import AsyncMock, patch

from app import library
from app.main import _reservation_records
from app.reservation_ops import plan_rebook
from tests import test_temp_leave as support
from tests.support.fake_library import DEFAULT_USER, FakeLibrary


class PlanRebookTests(unittest.TestCase):
    def test_table(self):
        cases = [
            ('round', datetime(2026, 10, 21, 10, 2), {}, {}, '12:05'),
            ('exact', datetime(2026, 10, 21, 10), {}, {}, '12:00'),
            ('seconds', datetime(2026, 10, 21, 10, 0, 1), {}, {}, '12:05'),
            ('end', datetime(2026, 10, 21, 20), {}, {}, '必须早于'),
            ('short', datetime(2026, 10, 21, 19, 45), {}, {}, '不足'),
            ('before opening', datetime(2026, 10, 21, 4), {}, {}, '开放时间'),
            ('closed', datetime(2026, 10, 21, 21), {}, {}, '开放时间'),
            ('max', datetime(2026, 10, 21, 10), {}, {'maxResvTime': 60}, '超过'),
            ('own original', datetime(2026, 10, 21, 10), {}, {}, '12:00'),
        ]
        for name, now, changes, rule, expected in cases:
            with self.subTest(name=name):
                state = FakeLibrary()
                state.clock = now
                row = state.seed(changes)
                record = _reservation_records({'code': 0, 'data': [row]}, DEFAULT_USER)[0]
                raw = deepcopy(state.seats[2])
                raw['resvRule'].update(rule)
                raw['resvInfo'] = [{**row, 'startTime': row['resvBeginTime'], 'endTime': row['resvEndTime']}]
                seat = library.build_room_seat_view({'data': [raw]})[0]
                plan = plan_rebook(record, seat, now, 120)
                if isinstance(plan, str):
                    self.assertIn(expected, plan)
                else:
                    self.assertIn(expected, plan['new']['begin'])

    def test_seat_occupied_by_the_user_is_fine_but_closed_seat_is_not(self):
        state = FakeLibrary()
        now = datetime(2026, 10, 21, 10)
        state.clock = now
        row = state.seed({})
        record = _reservation_records({'code': 0, 'data': [row]}, DEFAULT_USER)[0]
        raw = deepcopy(state.seats[2])
        raw['resvInfo'] = []
        raw['devStatus'] = 1  # the user is sitting there right now
        seat = library.build_room_seat_view({'data': [raw]})[0]
        self.assertIsInstance(plan_rebook(record, seat, now, 120), dict)
        seat['open_state'] = 0
        self.assertIn('未开放', plan_rebook(record, seat, now, 120))

    def test_grid_anchor_conflicts_and_identifier_fallback(self):
        state = FakeLibrary()
        now = datetime(2026, 10, 21, 10)
        state.clock = now
        row = state.seed({})
        record = _reservation_records({'code': 0, 'data': [row]}, DEFAULT_USER)[0]
        raw = deepcopy(state.seats[2])
        raw['openTimes'][0]['openStartTime'] = '07:02'
        raw['resvInfo'] = []
        seat = library.build_room_seat_view({'data': [raw]})[0]
        # Choose an end on this opening's grid.
        record['end_time'] = '2026-10-21 22:02:00'
        self.assertIn('12:02', plan_rebook(record, seat, now, 120)['new']['begin'])
        seat['reservations'] = [{'startTime': record['begin_time'], 'endTime': record['end_time']}]
        self.assertIsInstance(plan_rebook(record, seat, now, 120), dict)
        seat['reservations'][0]['uuid'] = 'someone-else'
        self.assertIn('冲突', plan_rebook(record, seat, now, 120))
        seat['reservations'] = [{'uuid': 'other', 'startTime': '12:02', 'endTime': '13:02'}]
        self.assertIn('冲突', plan_rebook(record, seat, now, 120))


class RebookTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = support.TempLeaveTests.asyncSetUp

    # Reuse the real app + Basic auth + stateful upstream fixture.
    async def rebook(self, row, **changes):
        return await self.http.post('/api/reservations/rebook', json={
            'auth_session_id': 'own', 'uuid': row['uuid'], 'confirm': True, **changes})

    async def test_rebook_cancel_and_end_early_basic_only_epoch(self):
        self.runtime.settings.submit_enabled = True
        for status, action, epoch in ((3, 'cancel', False), (69, 'end_early', True)):
            with self.subTest(action=action):
                self.state.rows.clear()
                self.state.epoch_times = epoch
                row = self.state.seed({'status': status})
                with self.assertLogs('ustb-seat-book', level='INFO') as logs:
                    response = await self.rebook(row)
                self.assertEqual(response.status_code, 200, response.text)
                data = response.json()
                self.assertEqual(data['state'], 'rebooked')
                self.assertTrue(data['success'])
                self.assertEqual(data['release_action'], action)
                self.assertEqual(data['new']['begin'], '2026-10-21 12:00:00')
                self.assertEqual(data['new']['end'], '2026-10-21 22:00:00')
                self.assertEqual(self.state.rows[-1]['resvBeginTime'], data['new']['begin'])
                self.assertFalse(self.http.cookies)
                self.assertIn(f'action={action} state=rebooked', ''.join(logs.output))

    async def test_rebook_release_failure_stops(self):
        self.runtime.settings.submit_enabled = True
        row = self.state.seed({'status': 3})
        before = deepcopy(self.state.rows)
        self.state.failures['cancel'] = {'message': '拒绝取消'}
        with patch.object(self.api, 'reserve', wraps=self.api.reserve) as reserve:
            response = await self.rebook(row)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail'], '原预约未变更：拒绝取消')
        reserve.assert_not_called()
        self.assertEqual(self.state.rows, before)

    async def test_rebook_reserve_failure_and_timeout_both_ways(self):
        import asyncio
        self.runtime.settings.submit_enabled = True
        original = self.api.reserve
        async def timed(*args):
            return await asyncio.wait_for(original(*args), .02)
        for mode, after, expected in (('fail', False, 'released_not_rebooked'),
                                      ('fail', True, 'rebooked'),
                                      ('timeout', True, 'rebooked'),
                                      ('timeout', False, 'released_not_rebooked')):
            with self.subTest(mode=mode, after=after):
                self.state.rows.clear()
                row = self.state.seed({'status': 3})
                self.state.failures['reserve'] = {'mode': mode, 'after': after, 'seconds': 1, 'message': '已被占用'}
                with patch.object(self.api, 'reserve', side_effect=timed) as reserve:
                    response = await self.rebook(row)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['state'], expected)
                self.assertEqual(reserve.call_count, 1)
                if expected == 'released_not_rebooked':
                    self.assertTrue(response.json()['message'].startswith('原预约已释放，重约失败：'))

    async def test_rebook_unknown_when_confirmation_fails(self):
        self.runtime.settings.submit_enabled = True
        row = self.state.seed({'status': 3})
        first = await self.api.my_reservations(self.state.clock.date())
        with patch.object(self.api, 'reserve', AsyncMock(side_effect=TimeoutError)), \
             patch.object(self.api, 'my_reservations', AsyncMock(side_effect=[first, TimeoutError])):
            response = await self.rebook(row)
        self.assertEqual(response.json()['state'], 'unknown')
        self.assertFalse(response.json()['success'])
        self.assertIn('我的预约', response.json()['message'])

    async def test_rebook_disabled_dry_run_and_lock(self):
        self.runtime.settings.submit_enabled = False
        row = self.state.seed({})
        before = deepcopy(self.state.rows)
        response = await self.rebook(row)
        self.assertEqual(response.status_code, 403)
        response = await self.rebook(row, dry_run=True, confirm=False)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['dry_run'])
        self.assertEqual(self.state.rows, before)
        async with self.runtime.operation_lock:
            response = await self.rebook(row, dry_run=True)
        self.assertEqual(response.status_code, 409)

    async def test_rebook_confirm_bounds_conflict_and_room_fallback(self):
        self.runtime.settings.submit_enabled = True
        row = self.state.seed({'row': {'resvDevInfoList': [{'devId': 102057116, 'devName': 'F4B116'}]}})
        response = await self.rebook(row, confirm=False)
        self.assertEqual(response.status_code, 400)
        for delay in (4, 901, 5.5, '120', True):
            response = await self.rebook(row, delay_minutes=delay)
            self.assertEqual(response.status_code, 422, response.text)
        response = await self.rebook(row, dry_run=True)
        self.assertEqual(response.status_code, 200, response.text)
        self.state.user_for('bob')
        self.state.seed({'token': 'qa-bob', 'begin': '13:00', 'end': '14:00'})
        before = deepcopy(self.state.rows)
        response = await self.rebook(row)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.state.rows, before)
        row['resvDevInfoList'][0]['devName'] = 'unknown'
        response = await self.rebook(row)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail'], '无法确定座位所在区域')

    async def test_rebook_release_exception_and_foreign_record(self):
        self.runtime.settings.submit_enabled = True
        row = self.state.seed({'status': 3})
        before = deepcopy(self.state.rows)
        with patch.object(self.api, 'cancel_reservation', AsyncMock(side_effect=TimeoutError)), \
             patch.object(self.api, 'reserve', AsyncMock()) as reserve:
            response = await self.rebook(row)
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()['detail'].startswith('原预约未变更：'))
        reserve.assert_not_awaited()
        self.assertEqual(self.state.rows, before)
        foreign = {**row, 'appAccNo': 'bob'}
        with patch.object(self.api, 'my_reservations', AsyncMock(return_value={'code': 0, 'data': [foreign]})):
            response = await self.rebook(row)
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.state.rows, before)

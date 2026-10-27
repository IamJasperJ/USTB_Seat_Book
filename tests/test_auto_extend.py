"""Auto-extend rebooks reservations about to miss check-in or a temporary-leave return."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from app import main
from app.auto_extend import AutoExtend
from tests import test_temp_leave as support

NOT_STARTED = 1 | 2
NOT_SIGNED_IN = 1 | 4
ON_TEMP_LEAVE = 1 | 4 | 64 | 2048


class AutoExtendTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await support.TempLeaveTests.asyncSetUp(self)
        self.runtime.account = self.account
        self.runtime.settings.submit_enabled = True
        self.runtime.settings.auto_extend_enabled = True
        self.runtime.auto_extend = AutoExtend(self.runtime)
        own = SimpleNamespace(session_id='own', status='authenticated', owner_account_id=self.account.id,
                              created_at=1)
        main.auth.sessions = {'own': own}
        self.now = self.state.clock

    def seed(self, status, begin='10:00', **row):
        return self.state.seed({'status': status, 'begin': begin, 'end': '22:00', 'row': row})

    def at(self, minutes):
        return (self.now + timedelta(minutes=minutes)).strftime('%Y-%m-%d %H:%M:%S')

    async def check(self, **kwargs):
        return await self.runtime.auto_extend.check('own', now=self.now, **kwargs)

    async def test_not_signed_in_near_start_pushes_the_start_back(self):
        # 10:15-22:00 at 10:00, not signed in: cancel and book 10:35-22:00 (start + 20).
        row = self.seed(NOT_STARTED, begin='10:15')
        result = await self.check()
        [action] = result['actions']
        self.assertEqual((action['kind'], action['state'], action['release_action']), ('check_in', 'rebooked', 'cancel'))
        self.assertEqual((action['new']['begin'], action['new']['end']), ('2026-10-21 10:35:00', '2026-10-21 22:00:00'))
        self.assertNotIn(row, self.state.rows)  # cancelled, not ended early
        self.assertEqual(result['watching'], [])
        # The new booking is 30 minutes away at 10:05: no immediate second rebook.
        self.now = self.now + timedelta(minutes=5)
        with patch.object(main.library, 'beijing_now', return_value=self.now):
            again = await self.check()
        self.assertEqual(again['actions'], [])
        self.assertEqual([(w['kind'], w['minutes_left']) for w in again['watching']], [('check_in', 30)])

    async def test_already_started_without_sign_in(self):
        # Started 10 minutes ago: start + 20 is still ahead, so 09:50 -> 10:10.
        self.seed(NOT_SIGNED_IN, begin='09:50')
        [action] = (await self.check())['actions']
        self.assertEqual((action['release_action'], action['new']['begin']), ('end_early', '2026-10-21 10:10:00'))
        # Started long ago: start + 20 has passed, so the new slot starts 20 minutes from now.
        self.state.rows.clear()
        self.seed(NOT_SIGNED_IN, begin='08:00')
        [action] = (await self.check())['actions']
        self.assertEqual(action['new']['begin'], '2026-10-21 10:20:00')

    async def test_temp_leave_minutes_left_uses_its_own_threshold(self):
        self.runtime.settings.auto_extend_temp_leave_minutes = 30
        self.seed(ON_TEMP_LEAVE, tempLeaveEndTime=25)  # live format: minutes left
        [action] = (await self.check())['actions']
        self.assertEqual((action['kind'], action['minutes_left'], action['delay_minutes']), ('temp_leave', 25, 30))
        self.assertEqual(action['new']['begin'], '2026-10-21 10:30:00')

    async def test_far_from_start_or_deadline_is_only_watched(self):
        self.seed(NOT_STARTED, begin='10:25')
        self.seed(ON_TEMP_LEAVE, tempLeaveEndTime=self.at(60))
        self.seed(1 | 4 | 64)  # signed in and seated: nothing to do
        self.seed(1 | 2 | 8192, begin='10:05')  # awaiting approval: not ours to move
        before = deepcopy(self.state.rows)
        result = await self.check()
        self.assertEqual(result['actions'], [])
        self.assertCountEqual([(w['kind'], w['minutes_left']) for w in result['watching']],
                              [('check_in', 25), ('temp_leave', 60)])
        self.assertEqual(self.state.rows, before)

    async def test_dry_run_endpoint_reports_without_releasing(self):
        self.seed(NOT_STARTED, begin='10:05')
        before = deepcopy(self.state.rows)
        with patch.object(main, 'ensure_runtime', return_value=self.runtime):
            response = await self.http.post('/api/auto-extend/check', json={})
        self.assertEqual(response.status_code, 200, response.text)
        [action] = response.json()['actions']
        self.assertTrue(action['dry_run'])
        self.assertEqual(action['new']['begin'], '2026-10-21 10:25:00')
        self.assertEqual(self.state.rows, before)
        self.assertEqual(self.runtime.auto_extend.status()['actions'], [])
        with patch.object(main, 'ensure_runtime', return_value=self.runtime):
            refused = await self.http.post('/api/auto-extend/check', json={'dry_run': False})
        self.assertEqual(refused.status_code, 400)

    async def test_too_little_time_left_is_refused_and_nothing_released(self):
        self.now = self.state.clock = datetime(2026, 10, 21, 21, 30)
        self.seed(NOT_STARTED, begin='21:35')
        before = deepcopy(self.state.rows)
        with patch.object(main.library, 'beijing_now', return_value=self.now):
            [action] = (await self.check())['actions']
        self.assertEqual(action['state'], 'refused')
        self.assertIn('不足', action['message'])
        self.assertEqual(self.state.rows, before)

    async def test_tick_gating(self):
        ext, settings = self.runtime.auto_extend, self.runtime.settings
        self.seed(NOT_STARTED, begin='10:15')
        settings.auto_extend_after = '12:00'
        await ext.tick(self.now)
        self.assertEqual(ext.state, 'waiting_start')
        settings.auto_extend_after = '07:00'
        settings.submit_enabled = False
        await ext.tick(self.now)
        self.assertEqual(ext.state, 'blocked')
        settings.submit_enabled = True
        main.auth.sessions = {}
        await ext.tick(self.now)
        self.assertEqual(ext.state, 'waiting_login')
        main.auth.sessions = {'own': SimpleNamespace(session_id='own', status='authenticated',
                                                     owner_account_id=self.account.id, created_at=1)}
        await ext.tick(self.now)
        self.assertEqual((ext.state, len(ext.actions)), ('active', 1))
        # Within the interval nothing is queried again.
        with patch.object(ext, 'check') as check:
            await ext.tick(self.now + timedelta(minutes=4))
            check.assert_not_called()
            await ext.tick(self.now + timedelta(minutes=5))
            check.assert_called_once()
        settings.auto_extend_enabled = False
        await ext.tick(self.now)
        self.assertEqual(ext.state, 'disabled')

    async def test_interval_must_be_shorter_than_thresholds(self):
        self.runtime.automation = SimpleNamespace(settings=None)
        with patch.object(main, 'ensure_runtime', return_value=self.runtime), \
                patch.object(main.account_store, 'save_settings'):
            response = await self.http.put('/api/config', json={'auto_extend_interval_minutes': 20})
            self.assertEqual(response.status_code, 422)
            response = await self.http.put('/api/config', json={'auto_extend_after': '7:00'})
            self.assertEqual(response.status_code, 422)
            response = await self.http.put('/api/config', json={'auto_extend_interval_minutes': 10,
                                                                 'auto_extend_after': '08:30'})
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.runtime.settings.auto_extend_after, '08:30')


if __name__ == '__main__':
    unittest.main()

import asyncio
from datetime import datetime,timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock,Mock,patch

import httpx
from app.accounts import Account,AccountStore,SessionTokenStore,RegistrationRateLimiter
from app.auth import LoginSession
from app.config import Settings
from app.monitoring import ReservationMonitor,CHINA,reservation_state,reservation_time


class ReservationMonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.account=Account('A','alice',False);self.settings=Settings()
        self.target=datetime.now(CHINA).date()+timedelta(days=1)
        self.session=LoginSession('session',AsyncMock(),Mock(),status='authenticated',owner_account_id='A',
                                  user_info={'token':'library-secret','accNo':1})
        self.auth=SimpleNamespace(sessions={'session':self.session})
        self.session.context.cookies.return_value=[]
        self.monitor=ReservationMonitor(self.auth)
        self.client=Mock();self.client.close=AsyncMock();self.client.my_reservations=AsyncMock()

    def row(self,status=2,**overrides):
        return {'resvId':12,'classKind':8,'appAccNo':1,'resvStatus':status,
                'resvBeginTime':f'{self.target} 10:00:00','resvEndTime':f'{self.target} 22:00:00',
                'resvDevInfoList':[{'devName':'F4B116','roomName':'四楼北区'}],**overrides}

    async def query(self,rows,count=None):
        self.client.my_reservations.return_value={'code':0,'data':rows,'count':len(rows) if count is None else count}
        with patch('app.monitoring.SeatApi',return_value=self.client):
            return await self.monitor.day(self.account,self.settings,self.target)

    async def test_real_list_not_local_history_and_stable_event(self):
        first=await self.query([self.row()])
        self.assertTrue(first['has_reservation']);self.assertTrue(first['confirmed'])
        self.assertEqual(first['records'][0]['devices'][0]['name'],'F4B116')
        self.assertNotIn('library-secret',str(first))
        again=await self.query([self.row()])
        self.assertTrue(again['cached']);self.client.my_reservations.assert_awaited_once()
        self.assertEqual(first['records'][0]['event_id'],again['records'][0]['event_id'])

    async def test_empty_is_false_only_when_confirmed(self):
        result=await self.query([])
        self.assertIs(result['has_reservation'],False);self.assertTrue(result['confirmed'])

    async def test_no_login_cannot_use_foreign_session(self):
        self.session.owner_account_id='B'
        result=await self.query([self.row()])
        self.assertIsNone(result['has_reservation']);self.assertFalse(result['confirmed'])
        self.client.my_reservations.assert_not_awaited()

    async def test_expired_and_remote_failure_are_unknown(self):
        self.client.my_reservations.side_effect=RuntimeError('secret-token-url')
        with patch('app.monitoring.SeatApi',return_value=self.client):
            result=await self.monitor.day(self.account,self.settings,self.target)
        self.assertIsNone(result['has_reservation'])
        self.assertNotIn('secret-token-url',str(result));self.client.close.assert_awaited_once()

    async def test_foreign_or_wrong_date_is_not_success(self):
        result=await self.query([self.row(appAccNo=2)])
        self.assertIsNone(result['has_reservation'])
        self.monitor.clear('A')
        result=await self.query([self.row(resvBeginTime='2020-01-01 10:00:00')])
        self.assertIsNone(result['has_reservation'])

    async def test_terminal_pending_and_unknown_statuses(self):
        for status,expected in [(129,'not_booked'),(513,'not_booked'),(259,'pending'),(8193,'pending'),(0,'unknown')]:
            self.monitor.clear('A')
            result=await self.query([self.row(status)])
            self.assertEqual(result['booking_status'],expected)

    async def test_pagination_checks_later_page_and_never_false_on_inconsistent_list(self):
        self.client.my_reservations.side_effect=[
            {'code':0,'data':[self.row(129)]*20,'count':21},
            {'code':0,'data':[self.row(2)],'count':21}]
        with patch('app.monitoring.SeatApi',return_value=self.client):
            result=await self.monitor.day(self.account,self.settings,self.target)
        self.assertTrue(result['has_reservation']);self.assertEqual(self.client.my_reservations.await_count,2)
        self.monitor.clear('A');self.client.my_reservations.side_effect=None
        result=await self.query([],count=1)
        self.assertIsNone(result['has_reservation'])

    async def test_concurrent_pollers_share_query(self):
        async def delayed(*_):
            await asyncio.sleep(.01);return {'code':0,'data':[self.row()],'count':1}
        self.client.my_reservations.side_effect=delayed
        with patch('app.monitoring.SeatApi',return_value=self.client):
            results=await asyncio.gather(*(self.monitor.day(self.account,self.settings,self.target) for _ in range(4)))
        self.assertTrue(all(r['has_reservation'] for r in results))
        self.client.my_reservations.assert_awaited_once()

    async def test_timeout_is_unknown_and_client_closed(self):
        self.monitor.timeout=.01
        async def delayed(*_):await asyncio.sleep(1)
        self.client.my_reservations.side_effect=delayed
        with patch('app.monitoring.SeatApi',return_value=self.client):
            result=await self.monitor.day(self.account,self.settings,self.target)
        self.assertIsNone(result['has_reservation']);self.client.close.assert_awaited_once()

    def test_time_formats_and_status_bits(self):
        stamp=datetime(2026,10,21,10,tzinfo=CHINA)
        self.assertEqual(reservation_time(stamp.timestamp()*1000),stamp)
        self.assertEqual(reservation_time('2026-10-21T02:00:00Z'),stamp)
        self.assertEqual(reservation_state(3),'booked')

    async def test_existing_login_authentication_and_account_isolation(self):
        from app import main
        with TemporaryDirectory() as tmp:
            store=AccountStore(Path(tmp)/'accounts.sqlite3');a=store.create('alice','password123')
            b=store.create('bob','password123')
            tokens=SessionTokenStore();token=tokens.issue(a.id)
            monitor=Mock();monitor.snapshot=AsyncMock(return_value={'has_reservation':None,'days':{}})
            runtime=SimpleNamespace(settings=Settings(),automation=Mock())
            runtime.automation.status.return_value={'state':'idle','session_id':'never-expose'}
            with patch.object(main,'account_store',store),patch.object(main,'reservation_monitor',monitor), \
                 patch.object(main,'session_tokens',tokens), \
                 patch.object(main,'ensure_runtime',AsyncMock(return_value=runtime)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
                    self.assertEqual((await client.get('/api/monitor/status')).status_code,401)
                    client.headers['Authorization']='Bearer ustb_mon_removed'
                    self.assertEqual((await client.get('/api/monitor/status')).status_code,401)
                    client.headers.pop('Authorization')
                    client.cookies.set('session_token',token)
                    response=await client.get('/api/monitor/status?day=tomorrow')
                    self.assertEqual(response.status_code,200)
                    self.assertEqual(monitor.snapshot.await_args.args[0].id,a.id)
                    self.assertNotIn('never-expose',response.text)
                    self.assertEqual((await client.post('/api/monitor/token')).status_code,404)
                    self.assertEqual((await client.get('/api/monitor/token')).status_code,404)
                    self.assertEqual((await client.get('/api/monitor/status?day=2026-12-31')).status_code,422)
                    client.cookies.set('session_token',tokens.issue(b.id))
                    self.assertEqual((await client.get('/api/monitor/status?day=today')).status_code,200)
                    self.assertEqual(monitor.snapshot.await_args.args[0].id,b.id)
                    tokens.revoke_account(b.id)
                    self.assertEqual((await client.get('/api/monitor/status')).status_code,401)

    async def test_password_query_without_cookie_and_no_login_side_effects(self):
        from app import main
        with TemporaryDirectory() as tmp:
            store=AccountStore(Path(tmp)/'accounts.sqlite3')
            a=store.create('alice','password123');b=store.create('bob','password456')
            tokens=SessionTokenStore();tokens.issue=Mock(side_effect=AssertionError('must not log in'))
            monitor=Mock();monitor.snapshot=AsyncMock(return_value={'has_reservation':None,'days':{}})
            runtime=SimpleNamespace(settings=Settings(),automation=Mock())
            runtime.automation.status.return_value={'state':'idle'}
            limiter=RegistrationRateLimiter(max_per_window=30,window_seconds=60)
            with patch.object(main,'account_store',store),patch.object(main,'session_tokens',tokens), \
                 patch.object(main,'reservation_monitor',monitor),patch.object(main,'monitor_auth_rate_limiter',limiter), \
                 patch.object(main,'ensure_runtime',AsyncMock(return_value=runtime)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),base_url='http://test') as client:
                    payload={'username':'alice','password':'password123','day':'tomorrow'}
                    result=await client.post('/api/monitor/status',json=payload)
                    self.assertEqual(result.status_code,200)
                    self.assertNotIn('set-cookie',result.headers)
                    self.assertEqual(result.headers['cache-control'],'no-store')
                    self.assertEqual(monitor.snapshot.await_args.args[0].id,a.id)
                    self.assertEqual(monitor.snapshot.await_args.args[2],'tomorrow')
                    self.assertNotIn('password123',result.text)
                    client.cookies.set('session_token','irrelevant-cookie')
                    result=await client.post('/api/monitor/status',json={'username':'bob','password':'password456'})
                    self.assertEqual(result.status_code,200)
                    self.assertEqual(monitor.snapshot.await_args.args[0].id,b.id)
                    self.assertEqual(monitor.snapshot.await_args.args[2],'both')
                    monitor.snapshot.reset_mock()
                    for password in ['wrong-password','']:
                        result=await client.post('/api/monitor/status',json={**payload,'password':password})
                        self.assertEqual(result.status_code,401)
                        self.assertNotIn('wrong-password',result.text)
                    monitor.snapshot.assert_not_awaited()
                    self.assertEqual((await client.post('/api/monitor/status',json={**payload,'day':'yesterday'})).status_code,422)
                    self.assertEqual((await client.post('/api/monitor/status',json={})).status_code,422)
                    with patch.object(limiter,'allow',return_value=False):
                        result=await client.post('/api/monitor/status',json=payload)
                        self.assertEqual(result.status_code,429)
                        self.assertEqual(result.headers['retry-after'],'60')

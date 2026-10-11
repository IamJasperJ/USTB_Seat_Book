import unittest
from unittest.mock import AsyncMock, Mock, patch
import httpx

from app.auth import AuthManager, LoginSession
from app.config import Settings
from app.runtime import MemoryMonitor


class ResourceOptimizationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = AuthManager(Settings(keepalive_enabled=False))
        self.page = AsyncMock()
        self.page.url = 'https://libzw.ustb.edu.cn/'
        self.session = LoginSession('test', AsyncMock(), self.page, status='sso_authenticated', sso_authenticated=True)
        self.manager._library_user_info = AsyncMock(return_value=None)

    async def test_sso_response_disposed_on_success_and_503(self):
        response = AsyncMock()
        response.status = 200
        response.url = 'https://jwgl.ustb.edu.cn/'
        self.session.context.request.get.return_value = response
        self.assertTrue(await self.manager._probe_sso(self.session))
        response.dispose.assert_awaited_once()
        response.reset_mock()
        response.status = 503
        with self.assertRaises(RuntimeError):
            await self.manager._probe_sso(self.session)
        response.dispose.assert_awaited_once()

    async def test_qr_response_disposed_before_return_and_on_body_failure(self):
        self.manager.refresh_status = AsyncMock()
        image = AsyncMock()
        image.count.return_value = 1
        image.is_visible.return_value = True
        image.get_attribute.return_value = '/connect/qrimg'
        image.screenshot.return_value = b'fallback'
        frame = Mock(url='https://sso.ustb.edu.cn/connect/qrpage')
        frame.locator.return_value.first = image
        self.page.frames = [frame]
        response = AsyncMock()
        response.ok = True
        response.body.return_value = b'\x89PNG\r\n\x1a\noriginal'
        self.session.context.request.get.return_value = response
        self.assertEqual(await self.manager.qr(self.session), response.body.return_value)
        response.dispose.assert_awaited_once()
        response.reset_mock()
        response.body.side_effect = RuntimeError('body unavailable')
        self.assertEqual(await self.manager.qr(self.session), b'fallback')
        response.dispose.assert_awaited_once()

    async def test_503_probe_never_navigates_or_clears_page(self):
        request = httpx.Request('GET', 'https://libzw.ustb.edu.cn/ic-web/auth/address')
        response = httpx.Response(503, request=request)
        self.manager._login_url = AsyncMock(side_effect=httpx.HTTPStatusError('503', request=request, response=response))
        for _ in range(3):
            await self.manager._try_library_locked(self.session, force=True)
        self.assertEqual(self.manager._login_url.await_count, 3)
        self.page.goto.assert_not_awaited()
        self.page.evaluate.assert_not_awaited()
        self.assertTrue(self.session.library_probe_unavailable)
        self.assertEqual(self.session.status, 'sso_authenticated')

    async def test_recovery_bypasses_old_navigation_cooldown_once(self):
        self.session.library_probe_unavailable = True
        self.session.next_library_navigation = float('inf')
        self.manager._login_url = AsyncMock(return_value='https://sso.ustb.edu.cn/cas/login')
        self.page.goto.return_value = Mock(status=200)
        self.page.evaluate.return_value = False
        await self.manager._try_library_locked(self.session, force=True)
        self.page.goto.assert_awaited_once()
        await self.manager._try_library_locked(self.session, force=True)
        self.assertEqual(self.manager._login_url.await_count, 2)
        self.page.goto.assert_awaited_once()

    async def test_parallel_requests_share_token_refresh(self):
        import asyncio
        self.manager._login_url = AsyncMock(return_value='https://sso.ustb.edu.cn/cas/login')
        self.page.goto.return_value = Mock(status=200)
        self.manager._library_user_info = AsyncMock(side_effect=[None, {'token': 'test-token'}])
        result = await asyncio.gather(self.manager.ensure_library_session(self.session, force=True),
                                      self.manager.ensure_library_session(self.session, force=True))
        self.assertEqual(result, [True, True])
        self.page.goto.assert_awaited_once()

    async def test_io_deltas_and_detail_size_limit(self):
        def sample(total):
            return {'cgroup_read_bytes': total, 'cgroup_write_bytes': 10,
                    'host_major_faults': total, 'host_reclaim_scanned': total,
                    'processes': [{'pid': p, 'started': '1', 'name': 'chromium', 'state': 'S', 'rss_mib': p,
                                   'read_bytes': total, 'write_bytes': 10, 'read_chars': total, 'write_chars': 10}
                                  for p in range(40)]}
        monitor = MemoryMonitor(self.manager.settings, self.manager)
        with patch('app.runtime.memory_snapshot', side_effect=[sample(100), sample(130)]):
            await monitor.sample()
            self.assertIsNone(monitor.latest['cgroup_read_bytes_delta'])
            await monitor.sample()
        self.assertEqual(monitor.latest['cgroup_read_bytes_delta'], 30)
        self.assertEqual(monitor.latest['process_io_read_bytes_delta'], 1200)
        self.assertLessEqual(len(monitor.latest['top_processes']), 12)

    async def test_log_rotation_limits(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from app.runtime import configure_logging
        with TemporaryDirectory() as directory, patch('app.runtime.logging.getLogger') as get_logger:
            configure_logging(str(Path(directory) / 'test.log'))
            handlers = [call.args[0] for call in get_logger.return_value.addHandler.call_args_list]
            disk = next(h for h in handlers if hasattr(h, 'maxBytes'))
            self.assertEqual(disk.maxBytes, 5 * 1024 * 1024)
            self.assertEqual(disk.backupCount, 5)
            for handler in handlers:
                handler.close()

import json
import os
import unittest
from pathlib import Path
from urllib.parse import urlsplit
from playwright.async_api import async_playwright
from app.config import Settings


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS') == '1', 'opt-in local browser test')
class SessionRestoreBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_restore_without_new_login_and_select_multiple(self):
        root = Path(__file__).resolve().parents[1] / 'app/web'
        sessions = [{'session_id': 'existing', 'status': 'authenticated'}]
        task = {'running': False}
        requests, errors = [], []
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                async def route_request(route):
                    path = urlsplit(route.request.url).path
                    requests.append((route.request.method, path))
                    if path == '/':
                        return await route.fulfill(path=str(root / 'index.html'), content_type='text/html')
                    if path.startswith('/web/'):
                        return await route.fulfill(path=str(root / path.removeprefix('/web/')))
                    payload = {}
                    if path == '/api/account/me':
                        payload = {'username': 'tester', 'is_admin': True}
                    elif path == '/api/config':
                        payload = Settings().model_dump()
                    elif path == '/api/status':
                        payload = {'service': 'ok', 'auth_sessions': len(sessions), 'sessions': sessions, 'automation': task}
                    elif path.startswith('/api/auth/sessions/'):
                        identifier = path.rsplit('/', 1)[-1]
                        payload = {'status': next(s['status'] for s in sessions if s['session_id'] == identifier),
                                   'user': {'user_name': 'Test user', 'acc_no': 'test'}}
                    elif path == '/api/automation':
                        payload = {'running': False, 'state': 'idle'}
                    elif path == '/api/automation/history':
                        payload = {'records': [], 'total': 0, 'page': 1, 'page_size': 10}
                    elif path == '/api/logs':
                        payload = {'logs': []}
                    elif path == '/api/areas':
                        payload = {'areas': []}
                    await route.fulfill(content_type='application/json', body=json.dumps(payload))
                await page.route('http://app.test/**', route_request)
                await page.goto('http://app.test/')
                await page.wait_for_function("document.getElementById('auth-badge').textContent === '已登录'")
                self.assertEqual(await page.locator('#session-id').inner_text(), 'existing')
                self.assertTrue(await page.locator('#backend-session-picker').is_hidden())
                self.assertEqual(await page.locator('#user-name').inner_text(), 'Test user')
                self.assertFalse(await page.locator('#query-seats').is_disabled())

                # A stale tab-local ID must not conceal the backend login.
                await page.evaluate("sessionStorage.setItem('ustb_auth_session_id', 'stale')")
                await page.reload()
                await page.wait_for_function("document.getElementById('auth-badge').textContent === '已登录'")
                self.assertEqual(await page.locator('#session-id').inner_text(), 'existing')

                # A pending, intentionally selected QR must remain selected.
                sessions.append({'session_id': 'pending', 'status': 'waiting_scan'})
                await page.evaluate("sessionStorage.setItem('ustb_auth_session_id', 'pending')")
                await page.reload()
                await page.wait_for_function("document.getElementById('auth-badge').textContent === '等待扫码'")
                self.assertEqual(await page.locator('#session-id').inner_text(), 'pending')

                sessions[:] = [{'session_id': 'first', 'status': 'authenticated'}, {'session_id': 'second', 'status': 'authenticated'}]
                await page.evaluate('sessionStorage.clear()')
                await page.reload()
                await page.locator('#backend-session-picker').wait_for(state='visible')
                await page.locator('#backend-session-select').select_option('second')
                await page.wait_for_function("document.getElementById('auth-badge').textContent === '已登录'")
                self.assertEqual(await page.locator('#session-id').inner_text(), 'second')
                self.assertTrue(await page.locator('#backend-session-picker').is_hidden())

                task.update(running=True, session_id='first')
                await page.evaluate('sessionStorage.clear()')
                await page.reload()
                await page.wait_for_function("document.getElementById('auth-badge').textContent === '已登录'")
                self.assertEqual(await page.locator('#session-id').inner_text(), 'first')
                self.assertFalse(any(method != 'GET' for method, _ in requests), requests)
                self.assertFalse(errors, errors)
            finally:
                await browser.close()

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from urllib.parse import urlsplit, parse_qs

from playwright.async_api import async_playwright

from app.config import Settings
from app.library import DEFAULT_TIME_RULE, build_time_options


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "opt-in local browser test")
class AutomationWebTests(unittest.IsolatedAsyncioTestCase):
    async def test_current_task_history_paging_and_safe_errors(self) -> None:
        record = {"task_id": "test-task", "state": "waiting_trigger", "running": True,
                  "target_date": "2026-09-30", "room_name": "图西楼四楼北区",
                  "seat_names": ["F4B114"], "start_time": "07:00", "end_time": "09:00",
                  "next_trigger_at": "2026-09-29T06:10:30+08:00", "attempts": 0,
                  "retry_interval_seconds": 5, "started_at": "2026-09-28T10:00:00+08:00"}
        history = {**record, "running": False, "state": "auth_required", "attempts": 127,
                   "last_error": '<img src=x onerror="window.badError=true">需要扫码',
                   "finished_at": "2026-09-28T08:26:50+08:00"}
        root = Path(__file__).resolve().parents[1] / "app" / "web"
        errors = []
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            try:
                page = await browser.new_page(viewport={"width": 1280, "height": 900})
                page.on("pageerror", lambda error: errors.append(str(error)))
                async def route_request(route) -> None:
                    url = urlsplit(route.request.url)
                    if url.path == "/":
                        await route.fulfill(path=str(root / "index.html"), content_type="text/html")
                        return
                    if url.path.startswith("/web/"):
                        await route.fulfill(path=str(root / url.path.removeprefix("/web/")))
                        return
                    payload = {}
                    if url.path == "/api/account/me":
                        payload = {"username": "tester", "is_admin": True}
                    elif url.path == "/api/config":
                        payload = Settings(target_date="2026-09-30").model_dump()
                    elif url.path == "/api/time-rule/default":
                        payload = {**DEFAULT_TIME_RULE, "now": "2026-09-28T10:00:00", "target_date": "2026-09-29",
                                   "time_options": build_time_options(DEFAULT_TIME_RULE)}
                    elif url.path == "/api/status":
                        payload = {"service": "ok", "auth_sessions": 1, "submit_enabled": True, "keepalive_enabled": True}
                    elif url.path == "/api/logs":
                        payload = {"logs": []}
                    elif url.path == "/api/automation":
                        payload = record
                    elif url.path == "/api/automation/history":
                        p = int(parse_qs(url.query)["page"][0])
                        payload = {"records": [history if p == 1 else {**history, "task_id": "older-task"}], "total": 11, "page": p, "page_size": 10}
                    await route.fulfill(content_type="application/json", body=json.dumps(payload))
                await page.route("http://app.test/**", route_request)
                await page.goto("http://app.test/")
                await page.locator("#automation-current .task-record").wait_for()
                self.assertIn("F4B114", await page.locator("#automation-current").inner_text())
                self.assertIn("2026/9/29 06:10:30", await page.locator("#automation-current").inner_text())
                await page.locator("#automation-history details summary").click()
                self.assertIn("需要扫码", await page.locator("#automation-history").inner_text())
                self.assertEqual(await page.locator("#automation-history img").count(), 0)
                self.assertEqual(await page.locator('#issue-monitor-token, #revoke-monitor-token').count(), 0)
                await page.locator("#automation-history-next").click()
                await page.wait_for_function("() => document.getElementById('automation-history').textContent.includes('older-task')")
                self.assertIn("第 2 / 2 页", await page.locator("#automation-history-page").inner_text())
                self.assertFalse(errors, errors)
                # Saved values survive rule updates; a deliberate start change
                # selects the nearest valid end instead of jumping to the first.
                await page.evaluate("""() => {
                    state.selectedTargetDate = '2026-09-28';
                    state.timeNow = '2026-09-28T10:26:00';
                    updateTimeSelects([{start: '10:30', ends: ['11:00', '12:00']}], '07:00', '20:00');
                }""")
                start = page.locator('[name="start_time"]').first
                end = page.locator('[name="end_time"]').first
                self.assertEqual(await start.input_value(), '07:00')
                self.assertIn('已过', await start.locator('option:checked').inner_text())
                self.assertEqual(await end.input_value(), '20:00')
                self.assertIn('不在可选范围', await end.locator('option:checked').inner_text())
                await start.select_option('10:30')
                self.assertEqual(await end.input_value(), '12:00')
                await page.evaluate("""() => updateTimeSelects(
                    [{start: '10:30', ends: ['11:00', '12:00']}], '10:30', '11:00')""")
                self.assertEqual(await end.input_value(), '11:00')
                await page.set_viewport_size({"width": 390, "height": 844})
                self.assertTrue(await page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
            finally:
                await browser.close()

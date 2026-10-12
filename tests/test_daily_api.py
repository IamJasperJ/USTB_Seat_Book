from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import AsyncMock, patch
import unittest

from app import main
from app.accounts import Account, AccountStore
from app.auth import LoginSession
from app.automation import AutoReservation
from app.config import Settings
from app.daily import DailyReservation


class DailyApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_enable_disable_persist_and_do_not_require_fixed_date(self) -> None:
        with TemporaryDirectory() as directory:
            store = AccountStore(Path(directory) / "accounts.sqlite3")
            account = store.create(
                "tester", "password123",
                seed_settings={"submit_enabled": True, "target_date": "2020-01-01"},
            )
            auth = AsyncMock()
            session = LoginSession("scan", AsyncMock(), AsyncMock())
            session.status = "authenticated"
            session.sso_authenticated = True
            session.owner_account_id = account.id
            auth.sessions = {session.session_id: session}
            auth.get.return_value = session
            runtime = main.UserRuntime(account, store.load_settings(account.id), auth)
            self.addCleanup(lambda: Path(runtime.automation.history.path).unlink(missing_ok=True))
            with patch.object(main, "auth", auth), patch.dict(main.runtimes, {account.id: runtime}), \
                    patch.object(main, "account_store", store):
                enabled = await main.start_daily_automation(main.AutomationRequest(auth_session_id="scan"), account=account)
                self.assertTrue(enabled["enabled"])
                self.assertTrue(runtime.automation.running)
                self.assertNotEqual(runtime.automation.target_date.isoformat(), "2020-01-01")
                self.assertTrue(store.load_settings(account.id).daily_auto_enabled)
                # A service restart reads the persisted switch; live cookies
                # are still required and it must wait rather than submit.
                restored_settings = store.load_settings(account.id)
                restored_auth = AsyncMock()
                restored_auth.sessions = {}
                restored_auto = AutoReservation(restored_settings, restored_auth, account_id=account.id)
                restored_daily = DailyReservation(restored_auto)
                await restored_daily.tick()
                self.assertEqual(restored_daily.state, "waiting_login")
                await main.stop_automation(account=account)
                self.assertFalse(runtime.automation.running)
                self.assertFalse(store.load_settings(account.id).daily_auto_enabled)

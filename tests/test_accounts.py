from __future__ import annotations

import time
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from app.accounts import AccountStore, RegistrationRateLimiter, SessionTokenStore
from app.config import Settings


class AccountStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = AccountStore(Path(self.temp.name) / "accounts.sqlite3")

    def test_first_account_is_admin_and_later_ones_are_not(self) -> None:
        first = self.store.create("alice", "password123")
        second = self.store.create("bob", "password123")
        self.assertTrue(first.is_admin)
        self.assertFalse(second.is_admin)

    def test_duplicate_username_rejected(self) -> None:
        self.store.create("alice", "password123")
        with self.assertRaises(ValueError):
            self.store.create("alice", "another-password")

    def test_username_and_password_validation(self) -> None:
        with self.assertRaises(ValueError):
            self.store.create("al", "password123")  # too short
        with self.assertRaises(ValueError):
            self.store.create("has space", "password123")
        with self.assertRaises(ValueError):
            self.store.create("alice", "short")  # too short password

    def test_verify_succeeds_only_with_correct_password(self) -> None:
        self.store.create("alice", "password123")
        self.assertIsNotNone(self.store.verify("alice", "password123"))
        self.assertIsNone(self.store.verify("alice", "wrong-password"))
        self.assertIsNone(self.store.verify("nobody", "password123"))

    def test_settings_round_trip_and_seeding(self) -> None:
        account = self.store.create("alice", "password123", seed_settings={"room_id": 42, "submit_enabled": True})
        settings = self.store.load_settings(account.id)
        self.assertEqual(settings.room_id, 42)
        self.assertTrue(settings.submit_enabled)

        settings.room_id = 99
        self.store.save_settings(account.id, settings)
        self.assertEqual(self.store.load_settings(account.id).room_id, 99)

    def test_get_by_id_returns_none_when_missing(self) -> None:
        self.assertIsNone(self.store.get("does-not-exist"))


class SessionTokenStoreTests(unittest.TestCase):
    def test_issue_and_resolve_round_trip(self) -> None:
        store = SessionTokenStore()
        token = store.issue("account-1")
        self.assertEqual(store.resolve(token), "account-1")

    def test_revoke_invalidates_token(self) -> None:
        store = SessionTokenStore()
        token = store.issue("account-1")
        store.revoke(token)
        self.assertIsNone(store.resolve(token))

    def test_unknown_token_resolves_to_none(self) -> None:
        store = SessionTokenStore()
        self.assertIsNone(store.resolve("not-a-real-token"))

    def test_expired_token_resolves_to_none(self) -> None:
        store = SessionTokenStore(ttl_seconds=0)
        token = store.issue("account-1")
        time.sleep(0.01)
        self.assertIsNone(store.resolve(token))


class RegistrationRateLimiterTests(unittest.TestCase):
    def test_allows_up_to_the_limit_then_blocks(self) -> None:
        limiter = RegistrationRateLimiter(max_per_window=2, window_seconds=3600)
        self.assertTrue(limiter.allow("1.2.3.4"))
        self.assertTrue(limiter.allow("1.2.3.4"))
        self.assertFalse(limiter.allow("1.2.3.4"))

    def test_separate_keys_are_independent(self) -> None:
        limiter = RegistrationRateLimiter(max_per_window=1, window_seconds=3600)
        self.assertTrue(limiter.allow("1.2.3.4"))
        self.assertTrue(limiter.allow("5.6.7.8"))


if __name__ == "__main__":
    unittest.main()

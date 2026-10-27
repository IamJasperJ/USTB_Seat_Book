"""Recurring auth warnings are logged once per cause, with a recovery summary."""
import unittest
from unittest.mock import AsyncMock

from app.auth import AuthManager, LoginSession
from app.config import GlobalSettings


class RepeatedWarningTests(unittest.TestCase):
    def test_repeats_are_counted_not_logged_and_recovery_summarises(self):
        manager = AuthManager(GlobalSettings())
        session = LoginSession("s1", AsyncMock(), None)
        with self.assertLogs("ustb-seat-book.auth", level="INFO") as logs:
            for _ in range(5):
                manager._warn_repeated(session, "bootstrap", "HTTP 503", "bootstrap failed: %s", "HTTP 503")
            manager._warn_repeated(session, "bootstrap", "timeout", "bootstrap failed: %s", "timeout")
            manager._warn_repeated(session, "bootstrap", "timeout", "bootstrap failed: %s", "timeout")
            manager._warning_resolved(session, "bootstrap", "Library session bootstrap")
        self.assertEqual([r.getMessage() for r in logs.records], [
            "bootstrap failed: HTTP 503",
            "bootstrap failed: timeout",
            "Library session bootstrap recovered after 2 consecutive failures: session_id=s1",
        ])
        self.assertEqual(session.repeated_warnings, {})

    def test_single_failure_recovers_silently(self):
        manager = AuthManager(GlobalSettings())
        session = LoginSession("s1", AsyncMock(), None)
        with self.assertLogs("ustb-seat-book.auth", level="INFO") as logs:
            manager._warn_repeated(session, "keepalive", (503, None), "inconclusive")
            manager._warning_resolved(session, "keepalive", "Library keepalive")
        self.assertEqual(len(logs.records), 1)


if __name__ == "__main__":
    unittest.main()

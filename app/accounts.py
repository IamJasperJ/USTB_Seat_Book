from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import Settings

PBKDF2_ITERATIONS = 200_000
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")


@dataclass
class Account:
    id: str
    username: str
    is_admin: bool


def _hash_password(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS).hex()


class AccountStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS accounts ("
                "id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL, salt TEXT NOT NULL, "
                "password_hash TEXT NOT NULL, is_admin INTEGER NOT NULL DEFAULT 0, "
                "settings_json TEXT NOT NULL, created_at TEXT NOT NULL)"
            )

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def count(self) -> int:
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]

    def list_ids(self) -> list[str]:
        with self._connect() as db:
            return [row[0] for row in db.execute("SELECT id FROM accounts").fetchall()]

    def list_accounts(self) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, username, is_admin, created_at FROM accounts ORDER BY created_at, rowid"
            ).fetchall()
        return [{"id": r[0], "username": r[1], "is_admin": bool(r[2]), "created_at": r[3]} for r in rows]

    def admin_count(self) -> int:
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM accounts WHERE is_admin = 1").fetchone()[0]

    def set_admin(self, account_id: str, is_admin: bool) -> None:
        with self._connect() as db:
            db.execute("UPDATE accounts SET is_admin = ? WHERE id = ?", (int(is_admin), account_id))

    def set_password(self, account_id: str, password: str) -> None:
        if len(password) < 8:
            raise ValueError("密码至少 8 位")
        salt = secrets.token_bytes(16)
        with self._connect() as db:
            db.execute(
                "UPDATE accounts SET salt = ?, password_hash = ? WHERE id = ?",
                (salt.hex(), _hash_password(password, salt), account_id),
            )

    def delete(self, account_id: str) -> None:
        with self._connect() as db:
            db.execute("DELETE FROM accounts WHERE id = ?", (account_id,))

    def create(self, username: str, password: str, seed_settings: dict[str, Any] | None = None) -> Account:
        username = username.strip()
        if not USERNAME_RE.match(username):
            raise ValueError("用户名应为 3-32 位字母、数字或下划线")
        if len(password) < 8:
            raise ValueError("密码至少 8 位")
        salt = secrets.token_bytes(16)
        account_id = secrets.token_urlsafe(12)
        is_admin = self.count() == 0
        settings = Settings(**(seed_settings or {}))
        try:
            with self._connect() as db:
                db.execute(
                    "INSERT INTO accounts VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        account_id, username, salt.hex(), _hash_password(password, salt),
                        int(is_admin), settings.model_dump_json(),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError("用户名已被占用") from exc
        return Account(id=account_id, username=username, is_admin=is_admin)

    def verify(self, username: str, password: str) -> Account | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, salt, password_hash, is_admin FROM accounts WHERE username = ?", (username,)
            ).fetchone()
        if not row:
            return None
        account_id, salt_hex, password_hash, is_admin = row
        if not hmac.compare_digest(_hash_password(password, bytes.fromhex(salt_hex)), password_hash):
            return None
        return Account(id=account_id, username=username, is_admin=bool(is_admin))

    def get(self, account_id: str) -> Account | None:
        with self._connect() as db:
            row = db.execute(
                "SELECT id, username, is_admin FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        return Account(id=row[0], username=row[1], is_admin=bool(row[2])) if row else None

    def load_settings(self, account_id: str) -> Settings:
        with self._connect() as db:
            row = db.execute("SELECT settings_json FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not row:
            raise KeyError(account_id)
        return Settings.model_validate_json(row[0])

    def save_settings(self, account_id: str, settings: Settings) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE accounts SET settings_json = ? WHERE id = ?",
                (settings.model_dump_json(), account_id),
            )


class SessionTokenStore:
    """In-memory login-session tokens; a service restart logs every account out.

    Consistent with AuthManager.sessions already being reset on restart.
    """

    def __init__(self, ttl_seconds: int = 30 * 24 * 3600):
        self.ttl_seconds = ttl_seconds
        self._tokens: dict[str, tuple[str, float]] = {}

    def issue(self, account_id: str) -> str:
        token = secrets.token_urlsafe(32)
        self._tokens[token] = (account_id, time.monotonic() + self.ttl_seconds)
        return token

    def resolve(self, token: str) -> str | None:
        entry = self._tokens.get(token)
        if not entry:
            return None
        account_id, expires_at = entry
        if time.monotonic() > expires_at:
            del self._tokens[token]
            return None
        return account_id

    def revoke(self, token: str) -> None:
        self._tokens.pop(token, None)

    def revoke_account(self, account_id: str) -> int:
        """Sign an account out everywhere (after a password reset or deletion)."""
        stale = [t for t, (owner, _) in self._tokens.items() if owner == account_id]
        for token in stale:
            del self._tokens[token]
        return len(stale)


class RegistrationRateLimiter:
    """Minimal per-IP guard against trivial self-registration spam."""

    def __init__(self, max_per_window: int = 5, window_seconds: int = 3600):
        self.max_per_window = max_per_window
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        hits = [t for t in self._hits.get(key, []) if now - t < self.window_seconds]
        if len(hits) >= self.max_per_window:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True

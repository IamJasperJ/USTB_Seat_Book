"""Small persistent task journal; credentials and account cookies are never stored."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class AutomationHistory:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT, payload TEXT NOT NULL)")

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, record: dict[str, Any]) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT INTO tasks VALUES (?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET finished_at=excluded.finished_at, payload=excluded.payload",
                (record["task_id"], record["started_at"], record.get("finished_at"), json.dumps(record, ensure_ascii=False)),
            )

    def interrupt_unfinished(self, finished_at: str) -> None:
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM tasks WHERE finished_at IS NULL").fetchall()
        for (payload,) in rows:
            record = json.loads(payload)
            record.update(state="interrupted", running=False, finished_at=finished_at,
                          last_error="服务退出或重启，任务未自动恢复；请重新登录并启动")
            self.save(record)

    def list(self, page: int = 1, page_size: int = 10) -> dict[str, Any]:
        with self._connect() as db:
            total = db.execute("SELECT COUNT(*) FROM tasks WHERE finished_at IS NOT NULL").fetchone()[0]
            rows = db.execute(
                "SELECT payload FROM tasks WHERE finished_at IS NOT NULL ORDER BY started_at DESC, rowid DESC LIMIT ? OFFSET ?",
                (page_size, (page - 1) * page_size),
            ).fetchall()
        return {"records": [json.loads(row[0]) for row in rows], "total": total, "page": page, "page_size": page_size}

    def daily_finished(self, trigger_date: str) -> bool:
        with self._connect() as db:
            rows = db.execute("SELECT payload FROM tasks WHERE finished_at IS NOT NULL").fetchall()
        return any(
            (r := json.loads(payload)).get("mode") == "daily"
            and (r.get("next_trigger_at") or "").startswith(trigger_date + "T")
            and r.get("state") != "auth_required"
            and not (r.get("state") == "interrupted" and r.get("attempts", 0) == 0)
            and not (r.get("state") == "stopped" and r.get("attempts", 0) == 0)
            for (payload,) in rows
        )

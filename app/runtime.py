from __future__ import annotations

import logging
import asyncio
import json
import os
from logging.handlers import RotatingFileHandler
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class RingLogHandler(logging.Handler):
    def __init__(self, max_records: int = 500):
        super().__init__()
        self.records: deque[dict[str, Any]] = deque(maxlen=max_records)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(
            {
                "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": self.format(record),
            }
        )


def configure_logging(log_file: str = "logs/ustb-seat-book.log") -> RingLogHandler:
    ring = RingLogHandler()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    ring.setFormatter(formatter)

    path = Path(log_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = RotatingFileHandler(path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(ring)
    root.addHandler(file_handler)
    return ring


def memory_snapshot() -> dict[str, Any]:
    """Read Linux counters without launching commands or browser processes.

    RSS sums can double-count shared pages; cgroup totals include file cache.
    Missing counters are null, never reported as zero usage.
    """
    def counters(path: Path) -> dict[str, int]:
        values = {}
        try:
            for line in path.read_text().splitlines():
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    values[parts[0].rstrip(":")] = int(parts[1])
        except (OSError, ValueError):
            pass
        return values

    def number(path: Path) -> int | None:
        try:
            value = int(path.read_text().strip())
            return value if value < (1 << 60) else None
        except (OSError, ValueError):
            return None

    mib = lambda value: round(value / 1048576, 2) if value is not None else None
    proc = Path("/proc")
    host = counters(proc / "meminfo")
    processes = {}
    if proc.is_dir():
        for entry in proc.iterdir():
            if entry.name.isdigit():
                fields = counters(entry / "status")
                if "PPid" in fields:
                    processes[int(entry.name)] = fields
    descendants = {os.getpid()}
    while True:
        found = {pid for pid, fields in processes.items() if fields["PPid"] in descendants}
        if found <= descendants:
            break
        descendants.update(found)
    rss = sum(processes[pid].get("VmRSS", 0) * 1024 for pid in descendants if pid in processes) if processes else None
    details = []
    for pid in sorted(descendants & processes.keys()):
        fields = processes[pid]
        io = counters(proc / str(pid) / "io")
        try:
            name = (proc / str(pid) / "comm").read_text().strip()[:64]
            stat = (proc / str(pid) / "stat").read_text().rsplit(")", 1)[1].split()
            started = stat[19]
            process_state = stat[0]
        except (OSError, IndexError):
            name, started, process_state = "unknown", None, None
        details.append({"pid": pid, "started": started, "name": name, "state": process_state,
                        "rss_mib": mib(fields.get("VmRSS", 0) * 1024),
                        "read_bytes": io.get("read_bytes"), "write_bytes": io.get("write_bytes"),
                        "read_chars": io.get("rchar"), "write_chars": io.get("wchar")})
    cgroup = Path("/sys/fs/cgroup")
    if (cgroup / "memory.current").exists():
        usage = number(cgroup / "memory.current")
        peak = number(cgroup / "memory.peak")
        limit = number(cgroup / "memory.max")
        stats = counters(cgroup / "memory.stat")
        cache = stats.get("file")
    else:
        root = cgroup / "memory"
        # Also support uv on the host, where memory controller files are nested.
        if not (root / "memory.usage_in_bytes").exists():
            try:
                for line in (proc / "self/cgroup").read_text().splitlines():
                    _, controllers, relative = line.split(":", 2)
                    if "memory" in controllers.split(","):
                        root = root / relative.lstrip("/")
            except (OSError, ValueError):
                pass
        usage = number(root / "memory.usage_in_bytes")
        peak = number(root / "memory.max_usage_in_bytes")
        limit = number(root / "memory.limit_in_bytes")
        cache = counters(root / "memory.stat").get("cache")
    block_io: dict[str, int] = {}
    try:
        if (cgroup / "io.stat").exists():
            for line in (cgroup / "io.stat").read_text().splitlines():
                for part in line.split()[1:]:
                    key, value = part.split("=", 1)
                    if key in {"rbytes", "wbytes"}:
                        block_io[key] = block_io.get(key, 0) + int(value)
        else:
            root_io = cgroup / "blkio"
            for candidate in ("blkio.throttle.io_service_bytes", "blkio.io_service_bytes"):
                path = root_io / candidate
                if path.exists():
                    for line in path.read_text().splitlines():
                        parts = line.split()
                        if len(parts) == 3 and parts[1] in {"Read", "Write"}:
                            key = "rbytes" if parts[1] == "Read" else "wbytes"
                            block_io[key] = block_io.get(key, 0) + int(parts[2])
                    break
    except (OSError, ValueError):
        pass
    vm = counters(proc / "vmstat")
    cpu_total = cpu_wait = None
    try:
        values = [int(value) for value in (proc / "stat").read_text().splitlines()[0].split()[1:9]]
        cpu_total, cpu_wait = sum(values), values[4]
    except (OSError, ValueError, IndexError):
        pass
    return {
        "process_tree_rss_mib": mib(rss),
        "process_count": len(descendants & processes.keys()) if processes else None,
        "cgroup_usage_mib": mib(usage), "cgroup_peak_mib": mib(peak),
        "cgroup_cache_mib": mib(cache), "cgroup_limit_mib": mib(limit),
        "host_total_mib": mib(host["MemTotal"] * 1024) if "MemTotal" in host else None,
        "host_available_mib": mib(host["MemAvailable"] * 1024) if "MemAvailable" in host else None,
        "host_swap_used_mib": mib((host["SwapTotal"] - host["SwapFree"]) * 1024) if {"SwapTotal", "SwapFree"} <= host.keys() else None,
        "processes": details,
        "cgroup_read_bytes": block_io.get("rbytes"), "cgroup_write_bytes": block_io.get("wbytes"),
        "host_major_faults": vm.get("pgmajfault"),
        "host_reclaim_scanned": sum(v for k, v in vm.items() if k.startswith("pgscan_")) if vm else None,
        "host_cpu_ticks": cpu_total, "host_iowait_ticks": cpu_wait,
    }


class MemoryMonitor:
    def __init__(self, settings, auth):
        self.settings, self.auth = settings, auth
        self.task = None
        self.latest: dict[str, Any] = {}
        self._previous_processes: dict[tuple, dict] = {}
        self._previous_counters: dict = {}
        self._previous_time: float | None = None

    async def start(self):
        self.task = asyncio.create_task(self._loop())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def sample(self):
        snapshot = await asyncio.to_thread(memory_snapshot)
        import time
        now = time.monotonic()
        elapsed = now - self._previous_time if self._previous_time is not None else None
        details = snapshot.pop("processes", [])
        current = {(p["pid"], p["started"]): p for p in details}
        def delta(value, previous):
            if value is None or previous is None or value < previous:
                return None
            return value - previous
        for p in details:
            previous = self._previous_processes.get((p["pid"], p["started"]), {})
            for key in ("read_bytes", "write_bytes", "read_chars", "write_chars"):
                p[key + "_delta"] = delta(p[key], previous.get(key, 0) if elapsed is not None else None)
        for key in ("cgroup_read_bytes", "cgroup_write_bytes", "host_major_faults", "host_reclaim_scanned", "host_cpu_ticks", "host_iowait_ticks"):
            snapshot[key + "_delta"] = delta(snapshot.get(key), self._previous_counters.get(key))
        cpu_delta = snapshot["host_cpu_ticks_delta"]
        wait_delta = snapshot["host_iowait_ticks_delta"]
        snapshot["host_iowait_percent"] = round(100 * wait_delta / cpu_delta, 2) if cpu_delta and wait_delta is not None else None
        # Bound per-sample payload even if a renderer spawns many children.
        top_memory = sorted(details, key=lambda p: p["rss_mib"], reverse=True)[:6]
        top_io = sorted(details, key=lambda p: (p["read_bytes_delta"] or 0) + (p["write_bytes_delta"] or 0), reverse=True)[:6]
        selected = {p["pid"]: p for p in top_memory + top_io}
        snapshot["io_sample_seconds"] = round(elapsed, 2) if elapsed is not None else None
        snapshot["process_io_read_bytes_delta"] = sum(p["read_bytes_delta"] or 0 for p in details) if elapsed is not None and any(p["read_bytes_delta"] is not None for p in details) else None
        snapshot["process_io_write_bytes_delta"] = sum(p["write_bytes_delta"] or 0 for p in details) if elapsed is not None and any(p["write_bytes_delta"] is not None for p in details) else None
        snapshot["top_processes"] = list(selected.values())
        self._previous_processes = current
        self._previous_counters = snapshot.copy()
        self._previous_time = now
        counts: dict[str, int] = {}
        for session in self.auth.sessions.values():
            counts[session.status] = counts.get(session.status, 0) + 1
        self.latest = {"time": datetime.now(timezone.utc).isoformat(), **snapshot,
                       "browser_sessions": len(self.auth.sessions), "session_states": counts,
                       "max_browser_sessions": self.settings.max_browser_sessions}
        logging.getLogger("ustb-seat-book.memory").info("Memory usage: %s", json.dumps(self.latest, ensure_ascii=False))

    async def _loop(self):
        while True:
            try:
                await self.sample()
            except asyncio.CancelledError:
                raise
            except Exception:
                logging.getLogger("ustb-seat-book.memory").warning("Memory sampling temporarily unavailable")
            await asyncio.sleep(self.settings.memory_log_interval_seconds)

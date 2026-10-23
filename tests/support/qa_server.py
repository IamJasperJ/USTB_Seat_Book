"""Run browser-free QA: python -m tests.support.qa_server --port 8130 --library-port 8131.

/__fake/seed accepts {seat, day, begin, end, status, endEarly, token, row}.
The default seed is today's signed-in F4B116 08:00–22:00 for the latest login.
/__fake/clock accepts {now: "2026-10-21T10:00:00"} (null restores real time).
/__fake/failures accepts {action: "reserve"|"cancel"|"endAhaed"|"tempLeave",
mode: "fail"|"timeout", message, seconds: 30, after: false}.
after=true applies the operation before simulating a failed/lost response.
/__fake/state shows users (including QA tokens), seats and reservations.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import sys
import tempfile
from uuid import uuid4

import uvicorn

from .fake_library import FakeLibrary, create_app


def prepare_apps(workdir: Path, library_url: str):
    """Import the production app only after isolating all relative storage paths."""
    workdir = workdir.resolve()
    repo = Path(__file__).resolve().parents[2]
    if workdir.is_relative_to(repo):
        raise ValueError("QA 工作目录必须在仓库之外")
    workdir.mkdir(parents=True, exist_ok=True)
    if any(workdir.iterdir()):
        raise ValueError("QA 工作目录必须为空，请使用新的临时目录")
    # Existing imports have already opened relative stores; do not silently reuse them.
    if "app.main" in sys.modules:
        raise RuntimeError("请在独立进程中启动 QA 服务")
    (workdir / "config.json").write_text(json.dumps({"base_url": library_url}), encoding="utf-8")
    os.chdir(workdir)

    from app import main
    from app.auth import AuthManager, LoginSession

    state = FakeLibrary()
    # The app and fake upstream share the same controllable wall clock.
    from app import library
    library.beijing_now = state.now

    class FakePage:
        """Stands in for the login page so status/health summaries can inspect it."""
        url = "about:blank"

        def is_closed(self):
            return False

    class FakeContext:
        async def cookies(self):
            return []

    class FakeAuth(AuthManager):
        async def start(self):
            pass

        async def close(self):
            pass

        async def create_session(self, owner_account_id="", login_source="jwgl"):
            user = state.user_for(owner_account_id)
            session = LoginSession(str(uuid4()), FakeContext(), FakePage(), status="authenticated",
                                   owner_account_id=owner_account_id, user_info=dict(user),
                                   sso_authenticated=True)
            for old in list(self.sessions.values()):
                if old.owner_account_id == owner_account_id:
                    self.on_session_superseded(old.session_id, session)
                    await self.close_session(old.session_id)
            self.sessions[session.session_id] = session
            runtime = main.runtimes[owner_account_id]
            runtime.settings.keepalive_enabled = False
            return session

        async def qr(self, session):
            # The existing UI requests this URL even for an authenticated session.
            return base64.b64decode(
                "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aF9sAAAAASUVORK5CYII="
            )

        async def refresh_status(self, session):
            pass

        async def close_session(self, session_id):
            session = self.sessions.pop(session_id, None)
            if session:
                session.status = "closed"

    auth = FakeAuth(main.global_settings)
    auth.protected_session_ids = main.auth.protected_session_ids
    auth.account_settings_for = main.auth.account_settings_for
    auth.on_session_superseded = main.rebind_superseded_session
    main.auth = auth
    main.memory_monitor.auth = auth
    main.browser_health.auth = auth
    main.reservation_monitor.auth = auth

    async def noop():
        pass

    # Browser health probes require real pages; this harness intentionally has none.
    main.browser_health.start = noop
    main.browser_health.close = noop
    return main.app, create_app(state)


class QAServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        # Both servers share one shutdown handler in run_servers.
        yield


async def run_servers(app, library, port: int, library_port: int, host: str = "127.0.0.1"):
    servers = [QAServer(uvicorn.Config(library, host=host, port=library_port)),
               QAServer(uvicorn.Config(app, host=host, port=port))]
    loop = asyncio.get_running_loop()

    def shutdown():
        for server in servers:
            server.should_exit = True

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown)
    tasks = [asyncio.create_task(server.serve()) for server in servers]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        shutdown()
        await asyncio.gather(*tasks, return_exceptions=True)
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)


def cli():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8130)
    parser.add_argument("--library-port", type=int, default=8131)
    parser.add_argument("--host", default="127.0.0.1", help="监听地址；容器内 QA 用 0.0.0.0")
    parser.add_argument("--workdir", type=Path, help="仓库外的空临时目录；默认自动创建")
    args = parser.parse_args()
    if args.port == args.library_port:
        parser.error("两个服务必须使用不同端口")
    workdir = (args.workdir or Path(tempfile.mkdtemp(prefix="ustb-seat-qa-"))).resolve()
    url = f"http://127.0.0.1:{args.library_port}"
    app, library = prepare_apps(workdir, url)
    print(f"QA app: http://127.0.0.1:{args.port}\nFake library: {url}\nWorkdir: {workdir.resolve()}", flush=True)
    asyncio.run(run_servers(app, library, args.port, args.library_port, args.host))


if __name__ == "__main__":
    cli()

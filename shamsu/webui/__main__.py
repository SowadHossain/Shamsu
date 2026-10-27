"""Standalone service: python -m shamsu.webui. No interactive CLI required."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from shamsu.control.runner import QueuedRunner
from shamsu.runtime.home import shamsu_home
from shamsu.webui.cli import serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the standalone SHAMSU web application")
    parser.add_argument("--host", default=os.environ.get("SHAMSU_WEB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SHAMSU_WEB_PORT", "8765")))
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--scan", action="append", default=[])
    args = parser.parse_args(argv)
    root = os.environ.get("SHAMSU_WORKSPACE_ROOT", "")
    if root:
        Path(root).mkdir(parents=True, exist_ok=True)
    token = os.environ.get("SHAMSU_WEB_AUTH_TOKEN", "")
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not token:
        import secrets

        token_file = shamsu_home() / "web-token"
        token_file.parent.mkdir(parents=True, exist_ok=True)
        if not token_file.exists():
            token_file.write_text(secrets.token_urlsafe(32), encoding="utf-8")
            token_file.chmod(0o600)
        token = token_file.read_text(encoding="utf-8").strip()
    runner = QueuedRunner(surface="web")
    runner.start(recover_own_pid=True)
    try:
        return serve(
            args.workspace, port=args.port, scan=args.scan or ([root] if root else []),
            host=args.host, token=token or None, runner=runner,
        )
    finally:
        runner.stop()


if __name__ == "__main__":
    raise SystemExit(main())

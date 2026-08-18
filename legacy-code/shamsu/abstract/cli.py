"""`python -m shamsu.abstract.cli` - manual setup/status/repair for Codebase-Memory MCP.

Used by the installer scripts and by SHAMSU's `/abstract` slash commands.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from shamsu.abstract.service import AbstractService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m shamsu.abstract.cli")
    parser.add_argument("command", choices=["status", "setup", "repair", "build", "refresh"])
    parser.add_argument("--workspace", default=str(Path.cwd()))
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    service = AbstractService(Path(args.workspace))
    if args.command == "status":
        payload = service.status().to_dict()
        ok = payload["normal_mode_allowed"]
    elif args.command == "setup":
        payload = service.setup()
        ok = bool(payload.get("ok"))
    elif args.command == "build":
        payload = service.build()
        ok = bool(payload.get("ok"))
    elif args.command == "refresh":
        payload = service.refresh()
        ok = bool(payload.get("ok"))
    else:
        payload = service.repair()
        ok = bool(payload.get("ok"))

    if args.as_json:
        print(json.dumps(payload, indent=2))
    else:
        print(payload.get("message") or json.dumps(payload))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

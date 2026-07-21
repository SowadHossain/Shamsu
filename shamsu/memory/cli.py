"""CLI entrypoint for SHAMSU Graphiti memory setup/status/repair."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from shamsu.memory.service import MemoryService


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m shamsu.memory.cli")
    parser.add_argument("command", choices=("status", "setup", "repair"))
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    service = MemoryService(workspace)
    if args.command == "status":
        status = service.status().to_dict()
        if args.as_json:
            print(json.dumps(status, indent=2))
        else:
            print(f"available={status['health']['available']} message={status['health']['message']}")
        return 0 if status["normal_mode_allowed"] else 1
    if args.command == "setup":
        result = service.setup()
    else:
        result = service.repair()
    if args.as_json:
        print(json.dumps(result, indent=2))
    else:
        print(result.get("error") or result.get("manual_steps") or result.get("message") or result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Reusable Definition-of-Done check primitives."""
from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import urlparse

from shamsu.tools.executor import CommandRunner

CheckReturn = tuple[bool, str]


def file_exists(target_dir: Path, path: str, **_kwargs) -> CheckReturn:
    candidate = (target_dir / path).resolve()
    return candidate.exists(), f"{path} exists" if candidate.exists() else f"{path} is missing"


def command_succeeds(
    target_dir: Path,
    command: str,
    runner: CommandRunner | None = None,
    **_kwargs,
) -> CheckReturn:
    runner = runner or CommandRunner(target_dir, approval_func=lambda _request: True)
    exit_code, stdout, stderr = runner.run(command, target_dir)
    detail = "\n".join(part for part in (stdout, stderr) if part).strip()
    if not detail:
        detail = f"exit code {exit_code}"
    return exit_code == 0, detail


def build_succeeds(target_dir: Path, command: str = "npm run build", **kwargs) -> CheckReturn:
    return command_succeeds(target_dir, command, **kwargs)


def element_present(target_dir: Path, selector: str, file: str | None = None, **_kwargs) -> CheckReturn:
    if file is None:
        return False, "element_present requires a source file in offline DoD mode"
    content = _read(target_dir / file)
    found = _selector_in_text(selector, content)
    return found, f"{selector} found in {file}" if found else f"{selector} missing from {file}"


def websocket_opens(target_dir: Path, file: str = "src/net/room.ts", **_kwargs) -> CheckReturn:
    content = _read(target_dir / file)
    found = (
        "WebSocket" in content
        or "new WebSocket" in content
        or "colyseus.js" in content
        or "new Client(" in content
        or "joinOrCreate" in content
    )
    return found, f"multiplayer relay client found in {file}" if found else f"No relay client in {file}"


def two_clients_see_two_players(
    target_dir: Path,
    file: str = "src/App.tsx",
    players: int = 2,
    **_kwargs,
) -> CheckReturn:
    content = _read(target_dir / file)
    local = _selector_in_text("[data-testid=local-player]", content) or _react_prop_in_text(
        "testId", "local-player", content
    )
    remote = _selector_in_text("[data-testid=remote-player]", content) or _react_prop_in_text(
        "testId", "remote-player", content
    )
    found = local and remote and players >= 2
    if found:
        return True, "local and remote player render targets are present"
    return False, "local/remote player render targets are missing"


def state_advances(target_dir: Path, file: str = "src/game/rules.ts", **_kwargs) -> CheckReturn:
    content = _read(target_dir / file)
    markers = ("requestAnimationFrame", "setInterval", "updateGameState", "dt")
    found = any(marker in content for marker in markers)
    return found, "game loop/state update marker found" if found else "no state advancement marker found"


def end_state_reachable(target_dir: Path, file: str = "src/game/rules.ts", **_kwargs) -> CheckReturn:
    content = _read(target_dir / file)
    found = bool(re.search(r"game[-_ ]?over|endState|winner|lose|win", content, re.IGNORECASE))
    return found, "end condition marker found" if found else "no reachable end condition marker found"


CHECKS = {
    "file_exists": file_exists,
    "command_succeeds": command_succeeds,
    "build_succeeds": build_succeeds,
    "element_present": element_present,
    "websocket_opens": websocket_opens,
    "two_clients_see_two_players": two_clients_see_two_players,
    "state_advances": state_advances,
    "end_state_reachable": end_state_reachable,
}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _selector_in_text(selector: str, content: str) -> bool:
    test_id = _test_id_from_selector(selector)
    if test_id:
        return (
            f'data-testid="{test_id}"' in content
            or f"data-testid='{test_id}'" in content
            or f"data-testid={{{repr(test_id)}}}" in content
            or f"data-testid={{`{test_id}`}}" in content
        )
    return selector in content


def _test_id_from_selector(selector: str) -> str:
    match = re.fullmatch(r"\[data-testid=['\"]?([^'\"]+)['\"]?\]", selector.strip())
    return match.group(1) if match else ""


def _react_prop_in_text(prop: str, value: str, content: str) -> bool:
    return (
        f'{prop}="{value}"' in content
        or f"{prop}='{value}'" in content
        or f"{prop}={{'{value}'}}" in content
        or f'{prop}={{{repr(value)}}}' in content
    )


def is_local_url(url: str) -> bool:
    host = urlparse(url).hostname
    return host in {"localhost", "127.0.0.1", "::1"}

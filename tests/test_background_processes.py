"""Background servers survive the session that started them. They shouldn't.

Measured 2026-08-31 in `F:\\voice-demo`: `python -m http.server 8000`, started at
06:19 by a session that ended at 01:59, was still listening on port 8000 at
08:20 - two hours later, two processes, one port held.

The cleanup existed and was correctly written: a `_DETACHED` registry and a
`_reap_detached` that kills the whole tree. It had two holes.

* **`atexit` was its only trigger.** That runs on a clean interpreter exit. It
  does not run when a console window is closed, when the process is killed, or
  when it crashes - which are the ordinary ways a terminal session ends.
* **The registry was in memory.** Nothing was written down, so once the process
  was gone the server could never be found again: the only trace on disk was a
  log named `<timestamp>-<hash>.log`, which does not name the process.

`dev_server.py` has kept a proper on-disk record all along
(`.shamsu/dev-servers.json`, with liveness checks). It is simply not on the path
`run_command` takes, which is the path every `npm run dev` actually goes down.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from shamsu.tools import executor
from shamsu.tools.executor import (
    CommandRunner,
    _port_of,
    background_processes,
    pid_alive,
    stop_all_background_processes,
    stop_background_process,
)


@pytest.fixture
def workspace(tmp_path):
    yield tmp_path
    # Never leave one of these behind in the test suite either.
    stop_all_background_processes(tmp_path)


def _runner(workspace: Path) -> CommandRunner:
    return CommandRunner(workspace, approval_func=lambda _r: True, timeout_seconds=30)


def _launch(workspace: Path, port: int) -> dict:
    code, _out, _err = _runner(workspace).run(f"python -m http.server {port}", workspace)
    assert code == 0
    live = background_processes(workspace)
    assert live, "a detached server should be recorded"
    return live[0]


# -- the record exists, and outlives this interpreter's memory of it ---------



def test_a_background_server_is_written_down(workspace):
    record = _launch(workspace, 8941)
    assert record["port"] == 8941
    assert "http.server" in record["command"]
    assert Path(record["log"]).name.endswith(".log")
    # Named by pid, so a sweep is a directory listing and two processes can
    # never race on one index file.
    assert (workspace / ".shamsu" / "processes" / f"{record['pid']}.json").is_file()



def test_a_stranded_server_is_still_findable_after_the_session_dies(workspace, monkeypatch):
    """THE regression. Wipe the in-memory registry - the atexit hook never ran,
    there is no Popen handle - and it must still be findable and stoppable."""
    record = _launch(workspace, 8942)
    pid = record["pid"]

    monkeypatch.setattr(executor, "_DETACHED", {})
    monkeypatch.setattr(executor, "_DETACHED_WORKSPACES", set())

    found = background_processes(workspace)
    assert [r["pid"] for r in found] == [pid]
    assert stop_background_process(workspace, pid) is True
    for _ in range(20):
        if not pid_alive(pid):
            break
        time.sleep(0.25)
    assert not pid_alive(pid)
    assert background_processes(workspace) == []



def test_stopping_everything_clears_the_directory(workspace):
    _launch(workspace, 8943)
    stopped = stop_all_background_processes(workspace)
    assert len(stopped) == 1
    assert background_processes(workspace) == []
    assert not list((workspace / ".shamsu" / "processes").glob("*.json"))


# -- the sweep forgets what is no longer there -------------------------------


def test_a_record_for_a_dead_pid_is_dropped_on_sight(workspace):
    """Otherwise the directory becomes a graveyard and every start reports
    servers that stopped weeks ago."""
    directory = workspace / ".shamsu" / "processes"
    directory.mkdir(parents=True)
    # A pid that cannot be alive.
    (directory / "999999.json").write_text(
        json.dumps({"pid": 999999, "command": "npm run dev", "started": 0}),
        encoding="utf-8",
    )
    assert background_processes(workspace) == []
    assert not (directory / "999999.json").exists()


def test_an_unreadable_record_is_dropped_rather_than_raising(workspace):
    directory = workspace / ".shamsu" / "processes"
    directory.mkdir(parents=True)
    (directory / "123.json").write_text("{not json", encoding="utf-8")
    assert background_processes(workspace) == []
    assert not (directory / "123.json").exists()


def test_a_workspace_that_never_started_anything_is_quiet(workspace):
    assert background_processes(workspace) == []
    assert stop_all_background_processes(workspace) == []


def test_stopping_a_pid_that_is_already_gone_is_not_an_error(workspace):
    assert stop_background_process(workspace, 999999) is False


# -- a command that dies is not a started server -----------------------------


def test_a_command_that_exits_immediately_is_not_recorded_as_running(workspace):
    """Live 2026-08-31: `cd /workspace && python -m http.server 8000` exited 1
    on a path that does not exist on Windows, and the session log recorded
    "Started in the background". Two servers appeared on port 8000 in the log
    when there had only ever been one."""
    code, _out, _err = _runner(workspace).run(
        "cd /definitely-not-here && python -m http.server 8996", workspace
    )
    assert code != 0
    assert background_processes(workspace) == []


def test_the_detached_launcher_says_whether_it_stayed_up(workspace, tmp_path):
    """The caller could not tell a detached server from one that died, so it
    logged `command.detached` for both."""
    from shamsu.tools.executor import _run_command_detached

    log = tmp_path / "out.log"
    _code, _out, _err, still_running = _run_command_detached(
        "cd /definitely-not-here && python -m http.server 8997",
        workspace,
        log,
        0,
        workspace,
    )
    assert still_running is False


# -- the port, which is the useful half of the listing -----------------------


@pytest.mark.parametrize(
    ("command", "port"),
    [
        ("python -m http.server 8931", 8931),
        ("npm run dev -- --port 5173", 5173),
        ("vite --port=3000", 3000),
        ("npx serve 8080", 8080),
        ("PORT=4000 npm start", 4000),
        ("uvicorn app:app --port 8000", 8000),
        ("php -S localhost:8000", 8000),
        ("next dev -p 3001", 3001),
    ],
)
def test_the_port_is_recognised(command, port):
    assert _port_of(command) == port


@pytest.mark.parametrize(
    "command",
    [
        "npm run dev",
        "python -m http.server",
        # The reason this errs towards silence: a wrong port is worse than none.
        "node --max-old-space-size=4096 server.js",
    ],
)
def test_no_port_is_claimed_when_there_is_none_to_read(command):
    assert _port_of(command) is None

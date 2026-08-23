"""The local web portal, read-only slice.

What this is: every workspace the portal has seen, every thread in them, and a
live turn streaming in as it happens - read from the same `activity.jsonl` the
CLI and the phone render from, so all three agree by construction.

What it deliberately is NOT, yet: an input box, a Stop button, or approvals.
`run_control._RUNS` is a module-level dict, so a portal in another process
cannot see or control a run; a Stop button would silently do nothing and a
prompt box would lie about where the work is happening. Those wait for the
control plane. A surface that shows you a control it cannot honour is worse
than one that admits it has none - so the write routes answer 501 and say why.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request

import pytest

from shamsu.runtime.turn_stream import TurnEvent, TurnStream
from shamsu.session.manager import SessionManager
from shamsu.runtime import workspaces as registry
from shamsu.webui import api
from shamsu.webui.server import WebPortal


@pytest.fixture
def portal(tmp_path, monkeypatch):
    """A live portal on a free loopback port, torn down after the test."""
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "project"
    workspace.mkdir()
    server = WebPortal(workspace, port=0)
    server.start()
    yield server
    server.stop()


def _get(portal: WebPortal, path: str, *, token: str | None = "", **headers):
    url = f"{portal.base_url}{path}"
    request = urllib.request.Request(url)
    supplied = portal.token if token == "" else token
    if supplied:
        request.add_header("X-Shamsu-Token", supplied)
    for key, value in headers.items():
        request.add_header(key.replace("_", "-"), value)
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status, response.read().decode("utf-8"), response.headers


def _wsid(portal: WebPortal) -> str:
    return api.workspace_id(portal.workspace)


def _json(portal: WebPortal, path: str, **kwargs):
    _status, body, _headers = _get(portal, path, **kwargs)
    return json.loads(body)


# --- security -------------------------------------------------------------


def test_the_portal_binds_loopback_only(portal):
    """Never 0.0.0.0. This machine's agent is not a service on the network."""
    assert portal.host == "127.0.0.1"
    assert portal.base_url.startswith("http://127.0.0.1:")


def test_a_request_without_a_token_is_served_on_loopback(portal):
    """Changed deliberately 2026-08-23. The shell has to be served without a
    token - the browser has nowhere to put one before the page loads - so an
    API that demanded one gave a complete-looking application that then failed
    every request with a 401. See `WebPortal.requires_token` for what still
    guards a loopback bind, and `tests/test_web_access.py` for the policy."""
    assert not portal.requires_token
    assert _get(portal, "/api/workspaces", token=None)


def test_a_wrong_token_is_ignored_rather_than_refused_on_loopback(portal):
    """Nothing is being checked, so a wrong one is not a failure - it is a
    query parameter nobody read."""
    assert _get(portal, "/api/workspaces", token="not-the-token")


def test_the_token_comes_back_when_it_is_demanded(portal, monkeypatch):
    from shamsu.webui.server import TOKEN_ENV

    monkeypatch.setenv(TOKEN_ENV, "1")
    for wrong in (None, "not-the-token"):
        with pytest.raises(urllib.error.HTTPError) as caught:
            _get(portal, "/api/workspaces", token=wrong)
        assert caught.value.code == 401
    assert _get(portal, "/api/workspaces", token=portal.token)


def test_the_token_is_long_enough_to_be_worth_having(portal):
    assert len(portal.token) >= 32


def test_a_foreign_origin_is_refused(portal):
    """DNS-rebinding defence: a page on another origin must not read this."""
    with pytest.raises(urllib.error.HTTPError) as caught:
        _get(portal, "/api/workspaces", Origin="http://evil.example")
    assert caught.value.code == 403


def test_no_cors_headers_are_ever_sent(portal):
    _status, _body, headers = _get(portal, "/api/workspaces")
    assert "Access-Control-Allow-Origin" not in headers


def test_a_write_route_is_not_reachable_by_GET(portal):
    """The portal takes prompts now, but only as a POST with a body."""
    with pytest.raises(urllib.error.HTTPError) as caught:
        _get(portal, "/api/workspaces/x/sessions/y/prompt")
    assert caught.value.code == 404


def test_an_unknown_path_is_a_plain_404(portal):
    with pytest.raises(urllib.error.HTTPError) as caught:
        _get(portal, "/api/nonsense")
    assert caught.value.code == 404


def test_a_session_id_is_never_treated_as_a_path(portal):
    """The server takes ids, never filesystem paths."""
    workspace_id = _json(portal, "/api/workspaces")["workspaces"][0]["id"]
    with pytest.raises(urllib.error.HTTPError) as caught:
        _get(
            portal,
            f"/api/workspaces/{workspace_id}/sessions/..%2F..%2Fetc%2Fpasswd/messages",
        )
    assert caught.value.code in (400, 404)


# --- the shell ------------------------------------------------------------


def test_the_app_shell_loads_without_a_token(portal):
    """The shell has to load in order to ask for the token. It carries no data."""
    status, body, _headers = _get(portal, "/", token=None)
    assert status == 200
    assert "<title>" in body.lower()
    assert portal.token not in body


def test_the_shell_is_self_contained(portal):
    """Zero new dependencies means zero CDN links too."""
    _status, body, _headers = _get(portal, "/", token=None)
    assert "http://" not in body.replace("http://www.w3.org", "")
    assert "cdn" not in body.lower()


# --- reading ---------------------------------------------------------------


def test_workspaces_and_their_session_counts_are_listed(portal, tmp_path):
    manager = SessionManager(portal.workspace)
    manager.create_session("first")
    manager.create_session("second")

    payload = _json(portal, "/api/workspaces")
    workspaces = payload["workspaces"]
    assert len(workspaces) == 1
    assert workspaces[0]["path"] == str(portal.workspace)
    assert workspaces[0]["session_count"] == 2


def test_a_second_workspace_appears_once_the_portal_has_seen_it(portal, tmp_path):
    """There is no workspace registry yet (that is P3), so the portal keeps a
    small install-level note of the workspaces it has been opened in."""
    other = tmp_path / "other-project"
    other.mkdir()
    registry.remember_workspace(other)

    paths = {item["path"] for item in _json(portal, "/api/workspaces")["workspaces"]}
    assert str(other.resolve()) in paths
    assert str(portal.workspace) in paths


def test_a_workspace_that_has_been_deleted_is_dropped_not_crashed_on(portal, tmp_path):
    gone = tmp_path / "deleted-project"
    gone.mkdir()
    registry.remember_workspace(gone)
    for item in gone.iterdir():
        item.unlink()
    gone.rmdir()

    paths = {item["path"] for item in _json(portal, "/api/workspaces")["workspaces"]}
    assert str(gone.resolve()) not in paths


def test_sessions_are_listed_newest_first_with_titles(portal):
    manager = SessionManager(portal.workspace)
    manager.create_session("older")
    manager.create_session("newer")

    workspace_id = _json(portal, "/api/workspaces")["workspaces"][0]["id"]
    sessions = _json(portal, f"/api/workspaces/{workspace_id}/sessions")["sessions"]
    assert [item["title"] for item in sessions] == ["newer", "older"]
    assert all("session_id" in item for item in sessions)


def test_the_transcript_is_served_from_messages_jsonl(portal):
    logger = SessionManager(portal.workspace).create_session("chat")
    logger.append_message("user", "add a pause menu")
    logger.append_message("assistant", "Done.")

    payload = _json(portal, f"/api/workspaces/{_wsid(portal)}/sessions/{logger.session_id}/messages")
    assert [item["role"] for item in payload["messages"]] == ["user", "assistant"]
    assert payload["messages"][0]["content"] == "add a pause menu"


def test_a_transcript_can_be_fetched_incrementally(portal):
    logger = SessionManager(portal.workspace).create_session("chat")
    for index in range(5):
        logger.append_message("user", f"line {index}")

    payload = _json(portal, f"/api/workspaces/{_wsid(portal)}/sessions/{logger.session_id}/messages?after=3")
    assert [item["content"] for item in payload["messages"]] == ["line 3", "line 4"]


def test_a_past_turn_is_replayed_from_activity_jsonl(portal):
    logger = SessionManager(portal.workspace).create_session("chat")
    stream = TurnStream(portal.workspace, logger.session_id)
    for index, (kind, text) in enumerate(
        [("turn.start", "go"), ("tool.call", "read_file a.py"), ("turn.end", "done in 3s")], 1
    ):
        stream.publish(
            TurnEvent(seq=index, kind=kind, text=text, turn_id="turn-1", source="cli")
        )

    payload = _json(portal, f"/api/workspaces/{_wsid(portal)}/sessions/{logger.session_id}/activity")
    assert [item["kind"] for item in payload["events"]] == [
        "turn.start",
        "tool.call",
        "turn.end",
    ]


def test_health_reports_what_it_actually_knows(portal):
    payload = _json(portal, "/api/health")
    assert payload["ok"] is True
    assert "model" in payload
    # The portal can drive a run now, so a client that finds a prompt box
    # should be told the box is real.
    assert payload["read_only"] is False


# --- the live stream ------------------------------------------------------


def test_the_stream_replays_the_turn_then_tails_it_live(portal):
    logger = SessionManager(portal.workspace).create_session("live")
    stream = TurnStream(portal.workspace, logger.session_id)
    stream.publish(TurnEvent(seq=1, kind="turn.start", text="go", turn_id="t1"))

    received: list[dict] = []
    done = threading.Event()

    def read_stream() -> None:
        request = urllib.request.Request(
            f"{portal.base_url}/api/workspaces/{_wsid(portal)}/sessions/{logger.session_id}/stream"
        )
        request.add_header("X-Shamsu-Token", portal.token)
        with urllib.request.urlopen(request, timeout=10) as response:
            for raw in response:
                line = raw.decode("utf-8").strip()
                if line.startswith("data:"):
                    received.append(json.loads(line[5:].strip()))
                    if len(received) >= 3:
                        done.set()
                        return

    reader = threading.Thread(target=read_stream, daemon=True)
    reader.start()
    # Wait for the OPENING event to arrive before publishing the rest.
    #
    # Without this the test races its own reader: it published `turn.end`
    # before the HTTP request had been made, so by the time the stream opened
    # the turn was already closed. A fresh subscriber resumes from the start of
    # the turn still in flight and from the END of the file when every turn has
    # finished - see `sse.resume_line` - so a closed turn is correctly not
    # replayed, and the test was asserting a replay that must not happen.
    #
    # It passed before only because the stream used to replay the ENTIRE log to
    # every new reader, which is the duplication this pair of behaviours
    # exists to stop.
    for _ in range(150):
        if received:
            break
        time.sleep(0.05)
    assert received, "the in-flight turn's opening event was not replayed"
    for index, (kind, text) in enumerate([("activity", "working"), ("turn.end", "done")], 2):
        stream.publish(TurnEvent(seq=index, kind=kind, text=text, turn_id="t1"))
    assert done.wait(timeout=15), f"stream delivered only {received}"
    assert [item["kind"] for item in received] == ["turn.start", "activity", "turn.end"]


def test_the_stream_resumes_from_last_event_id_without_gaps_or_repeats(portal):
    """§8.7 criterion 3: hard-refresh mid-turn loses and duplicates nothing."""
    logger = SessionManager(portal.workspace).create_session("resume")
    stream = TurnStream(portal.workspace, logger.session_id)
    for index, text in enumerate(["one", "two", "three"], 1):
        stream.publish(TurnEvent(seq=index, kind="activity", text=text, turn_id="t1"))

    received: list[dict] = []
    done = threading.Event()

    def read_stream() -> None:
        request = urllib.request.Request(
            f"{portal.base_url}/api/workspaces/{_wsid(portal)}/sessions/{logger.session_id}/stream"
        )
        request.add_header("X-Shamsu-Token", portal.token)
        request.add_header("Last-Event-ID", "2")
        with urllib.request.urlopen(request, timeout=10) as response:
            for raw in response:
                line = raw.decode("utf-8").strip()
                if line.startswith("data:"):
                    received.append(json.loads(line[5:].strip()))
                    if len(received) >= 2:
                        done.set()
                        return

    reader = threading.Thread(target=read_stream, daemon=True)
    reader.start()
    stream.publish(TurnEvent(seq=4, kind="turn.end", text="done", turn_id="t1"))
    assert done.wait(timeout=15), f"stream delivered only {received}"
    assert [item["text"] for item in received] == ["three", "done"]


# --- the pure layer, without HTTP -----------------------------------------


def test_redaction_happens_before_anything_leaves_the_process(tmp_path):
    logger = SessionManager(tmp_path).create_session("secret")
    logger.append_message("user", "export AWS_SECRET_ACCESS_KEY=AKIAIOSFODNN7EXAMPLE")

    payload = api.session_messages(tmp_path, logger.session_id)
    body = json.dumps(payload)
    assert "AKIAIOSFODNN7EXAMPLE" not in body


def test_an_unknown_session_is_reported_not_guessed(tmp_path):
    with pytest.raises(api.NotFound):
        api.session_messages(tmp_path, "no-such-session")


def test_a_workspace_id_round_trips(tmp_path):
    workspace = tmp_path / "proj"
    workspace.mkdir()
    workspace_id = api.workspace_id(workspace)
    assert api.workspace_for_id(workspace_id, [workspace]) == workspace.resolve()


def test_an_unknown_workspace_id_is_rejected_rather_than_resolved(tmp_path):
    """The id is a handle into a known list, never a path the caller supplies."""
    with pytest.raises(api.NotFound):
        api.workspace_for_id("deadbeef", [tmp_path])


def test_the_registry_deduplicates_and_survives_a_bad_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "proj"
    workspace.mkdir()
    registry.remember_workspace(workspace)
    registry.remember_workspace(workspace)
    assert registry.known_workspaces() == [workspace.resolve()]

    registry.registry_path().write_text("{not json", encoding="utf-8")
    assert registry.known_workspaces() == []
    registry.remember_workspace(workspace)
    assert registry.known_workspaces() == [workspace.resolve()]


# --- the REPL command -----------------------------------------------------


class _RecordingConsole:
    def __init__(self) -> None:
        self.printed: list[str] = []

    def print(self, *args, **kwargs) -> None:
        for arg in args:
            self.printed.append(str(getattr(arg, "renderable", arg)))

    @property
    def text(self) -> str:
        return "\n".join(self.printed)


def test_the_web_command_starts_prints_a_link_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.webui import local

    console = _RecordingConsole()
    try:
        local.handle_web_command("/web 0", tmp_path, console)
        portal = local._MANAGER.running
        assert portal is not None
        assert portal.url in console.text
        # NOT "read-only": `POST .../prompt` has always started a turn, and
        # the panel said otherwise until 2026-08-23.
        assert "can start one of its own" in console.text
        assert "no token" in console.text
    finally:
        assert local.stop_web_portal() is True


def test_starting_twice_does_not_mint_a_second_portal(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.webui import local

    try:
        local.handle_web_command("/web 0", tmp_path, _RecordingConsole())
        first = local._MANAGER.running
        second_console = _RecordingConsole()
        local.handle_web_command("/web 0", tmp_path, second_console)
        assert local._MANAGER.running is first
        assert "Already running" in second_console.text
    finally:
        local.stop_web_portal()


def test_status_never_reprints_the_token(tmp_path, monkeypatch):
    """The token is a credential; it is shown once, with the URL, and no more."""
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.webui import local

    try:
        local.handle_web_command("/web 0", tmp_path, _RecordingConsole())
        token = local._MANAGER.running.token
        console = _RecordingConsole()
        local.handle_web_command("/web status", tmp_path, console)
        assert token not in console.text
        assert "Running at" in console.text
    finally:
        local.stop_web_portal()


def test_stopping_when_nothing_runs_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.webui import local

    console = _RecordingConsole()
    local.handle_web_command("/web stop", tmp_path, console)
    assert "not running" in console.text.lower()


# --- the command line -----------------------------------------------------


def test_every_spelling_of_the_web_command_is_accepted():
    """`-web` is what people type, so it works alongside the tidy spellings."""
    from shamsu.cli.arguments import parse_args

    for argv in (["web"], ["--web"], ["-web"]):
        assert parse_args(argv).command == "web", argv


def test_the_web_command_takes_a_port():
    from shamsu.cli.arguments import parse_args

    assert parse_args(["web", "--port", "9000"]).port == 9000
    assert parse_args(["-web", "--port", "0"]).port == 0
    assert parse_args(["web"]).port is None


def test_a_bare_invocation_still_opens_the_repl():
    from shamsu.cli.arguments import parse_args

    assert parse_args([]).command is None


def test_port_is_refused_where_it_would_mean_nothing():
    from shamsu.cli.arguments import parse_args

    with pytest.raises(SystemExit):
        parse_args(["run", "--prompt", "hi", "--port", "8765"])


def test_the_web_flag_cannot_contradict_the_run_command():
    from shamsu.cli.arguments import parse_args

    with pytest.raises(SystemExit):
        parse_args(["run", "--prompt", "hi", "--web"])


def test_serving_prints_the_link_and_stops_cleanly(tmp_path, monkeypatch):
    """The headless path: start, hand over a URL, shut down on request."""
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.webui import cli as webui_cli

    console = _RecordingConsole()
    captured = {}

    real_portal = webui_cli.WebPortal

    class CapturingPortal(real_portal):
        def __init__(self, workspace, *, port=0):
            super().__init__(workspace, port=0)
            captured["portal"] = self

    monkeypatch.setattr(webui_cli, "WebPortal", CapturingPortal)
    # Already set, so the serve loop hands back control at once instead of
    # blocking. Injecting the event beats patching threading, which reaches
    # into the HTTP server's own internals.
    already_stopped = threading.Event()
    already_stopped.set()

    exit_code = webui_cli.serve(tmp_path, port=0, console=console, stop=already_stopped)

    assert exit_code == 0
    assert captured["portal"].url in console.text
    assert "can start one" in console.text.lower()
    assert "Ctrl-C" in console.text
    # And the server really is down afterwards.
    assert captured["portal"]._server is None


def test_a_port_already_in_use_explains_itself(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.webui import cli as webui_cli

    class RefusingPortal(webui_cli.WebPortal):
        def start(self):
            raise OSError("address already in use")

    monkeypatch.setattr(webui_cli, "WebPortal", RefusingPortal)
    console = _RecordingConsole()

    assert webui_cli.serve(tmp_path, port=8765, console=console) == 2
    assert "--port 8766" in console.text


# --- system-wide, not workspace-bound --------------------------------------


def test_a_thread_in_another_workspace_actually_opens(tmp_path, monkeypatch):
    """The bug that made the portal useless: it listed what it could not read.

    Every session was resolved against the portal's OWN workspace, so a thread
    in any other project 404'd the moment you clicked it.
    """
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.runtime import workspaces

    other = tmp_path / "other-project"
    other.mkdir()
    logger = SessionManager(other).create_session("elsewhere")
    logger.append_message("user", "hello from the other project")
    workspaces.remember_workspace(other)

    here = tmp_path / "here"
    here.mkdir()
    portal = WebPortal(here, port=0)
    portal.start()
    try:
        listed = _json(portal, "/api/workspaces")["workspaces"]
        target = next(item for item in listed if item["path"] == str(other.resolve()))
        payload = _json(
            portal,
            f"/api/workspaces/{target['id']}/sessions/{logger.session_id}/messages",
        )
        assert payload["messages"][0]["content"] == "hello from the other project"
    finally:
        portal.stop()


def test_the_portal_needs_no_workspace_at_all(tmp_path, monkeypatch):
    """`shamsu -web` is a view over the machine, not a window onto one project."""
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.runtime import workspaces

    project = tmp_path / "recorded"
    project.mkdir()
    SessionManager(project).create_session("thread")
    workspaces.remember_workspace(project)

    portal = WebPortal(port=0)
    portal.start()
    try:
        assert portal.workspace is None
        listed = _json(portal, "/api/workspaces")["workspaces"]
        assert [item["path"] for item in listed] == [str(project.resolve())]
        assert _json(portal, "/api/health")["workspace"] == ""
    finally:
        portal.stop()


# --- discovery -------------------------------------------------------------


def test_scanning_finds_workspaces_that_have_real_threads(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.runtime import workspaces

    root = tmp_path / "projects"
    (root / "alpha").mkdir(parents=True)
    (root / "nested" / "beta").mkdir(parents=True)
    (root / "not-a-workspace").mkdir()
    SessionManager(root / "alpha").create_session("a")
    SessionManager(root / "nested" / "beta").create_session("b")

    found = {path.name for path in workspaces.discover_workspaces(root)}
    assert found == {"alpha", "beta"}


def test_scanning_skips_the_directories_that_would_make_it_slow(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.runtime import workspaces

    root = tmp_path / "projects"
    buried = root / "node_modules" / "pkg"
    buried.mkdir(parents=True)
    SessionManager(buried).create_session("noise")

    assert workspaces.discover_workspaces(root) == []


def test_scanning_respects_its_depth_bound(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.runtime import workspaces

    deep = tmp_path / "a" / "b" / "c" / "d" / "e" / "f"
    deep.mkdir(parents=True)
    SessionManager(deep).create_session("too-deep")

    assert workspaces.discover_workspaces(tmp_path, max_depth=2) == []
    assert workspaces.discover_workspaces(tmp_path, max_depth=8) == [deep.resolve()]


def test_scanning_a_path_that_is_not_there_is_not_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.runtime import workspaces

    assert workspaces.discover_workspaces(tmp_path / "nope") == []


def test_a_directory_with_a_bare_dot_shamsu_is_not_a_workspace(tmp_path, monkeypatch):
    """`.shamsu` appears for an index or a config; a THREAD is what counts."""
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.runtime import workspaces

    (tmp_path / "indexed" / ".shamsu").mkdir(parents=True)
    assert workspaces.discover_workspaces(tmp_path) == []


def test_the_scan_flag_reaches_the_server(tmp_path, monkeypatch):
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.cli.arguments import parse_args
    from shamsu.webui import cli as webui_cli

    assert parse_args(["-web", "--scan", "/a", "--scan", "/b"]).scan == ["/a", "/b"]

    root = tmp_path / "projects"
    (root / "gamma").mkdir(parents=True)
    SessionManager(root / "gamma").create_session("g")

    stop = threading.Event()
    stop.set()
    console = _RecordingConsole()
    assert webui_cli.serve(None, port=0, console=console, stop=stop, scan=[str(root)]) == 0
    assert "found 1 workspace" in console.text

    from shamsu.runtime import workspaces

    assert (root / "gamma").resolve() in workspaces.known_workspaces()


# --- chatting from the browser --------------------------------------------


def _post(portal, path, body, *, token=""):
    import json as _json

    url = f"{portal.base_url}{path}"
    data = _json.dumps(body).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    supplied = portal.token if token == "" else token
    if supplied:
        request.add_header("X-Shamsu-Token", supplied)
    with urllib.request.urlopen(request, timeout=15) as response:
        return response.status, _json.loads(response.read().decode("utf-8"))


class _SlowClient:
    def __init__(self, delay=0.4):
        self.delay = delay
        self.calls = 0

    async def chat(self, **kwargs):
        import asyncio as _asyncio

        self.calls += 1
        await _asyncio.sleep(self.delay)
        return {"message": {"content": "done", "tool_calls": []}}


@pytest.fixture
def chat_portal(tmp_path, monkeypatch):
    """A portal wired to its own control store and a scripted model."""
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("SHAMSU_LEGACY_ROUTING", raising=False)
    from shamsu.control.runner import QueuedRunner
    from shamsu.control.store import ControlStore

    client = _SlowClient()
    monkeypatch.setattr(
        "shamsu.agents.chat_loop._default_ollama_client", lambda *a, **k: client
    )
    workspace = tmp_path / "project"
    workspace.mkdir()
    logger = SessionManager(workspace).create_session("chat")
    store = ControlStore(tmp_path / "control.db")
    runner = QueuedRunner(store, surface="web")
    portal = WebPortal(workspace, port=0, runner=runner)
    portal.start()
    try:
        yield portal, logger.session_id, client, store, runner
    finally:
        runner.stop()
        portal.stop()


def test_a_prompt_sent_from_the_browser_runs_the_agent(chat_portal):
    portal, session_id, client, _store, runner = chat_portal
    base = f"/api/workspaces/{_wsid(portal)}/sessions/{session_id}"

    status, payload = _post(portal, f"{base}/prompt", {"text": "add a pause menu"})
    assert status == 202
    assert payload["accepted"] is True
    assert payload["queued"] is False

    deadline = time.monotonic() + 40
    while runner.busy() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert client.calls == 1

    messages = _json(portal, f"{base}/messages")["messages"]
    assert any("pause menu" in item["content"] for item in messages)


def test_a_second_prompt_queues_and_is_visible(chat_portal):
    """Queue depth has to be visible, or waiting looks like hanging."""
    portal, session_id, _client, _store, runner = chat_portal
    base = f"/api/workspaces/{_wsid(portal)}/sessions/{session_id}"

    _post(portal, f"{base}/prompt", {"text": "first"})
    _status, second = _post(portal, f"{base}/prompt", {"text": "second"})
    assert second["queued"] is True
    assert "waiting" in second["reason"]

    queue = _json(portal, f"{base}/queue")
    assert [item["text"] for item in queue["queued"]] == ["second"]
    assert queue["running_on"] == "web"

    deadline = time.monotonic() + 60
    while runner.busy() and time.monotonic() < deadline:
        time.sleep(0.05)


def test_a_queued_prompt_can_be_dropped(chat_portal):
    portal, session_id, _client, _store, runner = chat_portal
    base = f"/api/workspaces/{_wsid(portal)}/sessions/{session_id}"

    _post(portal, f"{base}/prompt", {"text": "first"})
    _status, second = _post(portal, f"{base}/prompt", {"text": "drop me"})
    _status, result = _post(portal, f"{base}/cancel", {"queue_id": second["queue_id"]})
    assert result["cancelled"] is True
    assert _json(portal, f"{base}/queue")["queued"] == []

    deadline = time.monotonic() + 60
    while runner.busy() and time.monotonic() < deadline:
        time.sleep(0.05)


def test_an_empty_prompt_is_refused(chat_portal):
    portal, session_id, _client, _store, _runner = chat_portal
    base = f"/api/workspaces/{_wsid(portal)}/sessions/{session_id}"
    with pytest.raises(urllib.error.HTTPError) as caught:
        _post(portal, f"{base}/prompt", {"text": "   "})
    assert caught.value.code == 400


def test_a_prompt_without_a_token_is_accepted_on_loopback(chat_portal):
    """The plain link has to be able to send one; that is the whole point of
    dropping the token. The foreign-Origin refusal below is what stops a page
    on another site doing the same thing."""
    portal, session_id, _client, _store, _runner = chat_portal
    base = f"/api/workspaces/{_wsid(portal)}/sessions/{session_id}"
    assert _post(portal, f"{base}/prompt", {"text": "hi"}, token=None)


def test_a_prompt_is_refused_when_the_token_is_demanded(chat_portal, monkeypatch):
    from shamsu.webui.server import TOKEN_ENV

    portal, session_id, _client, _store, _runner = chat_portal
    monkeypatch.setenv(TOKEN_ENV, "1")
    base = f"/api/workspaces/{_wsid(portal)}/sessions/{session_id}"
    with pytest.raises(urllib.error.HTTPError) as caught:
        _post(portal, f"{base}/prompt", {"text": "hi"}, token=None)
    assert caught.value.code == 401


def test_a_prompt_from_a_foreign_origin_is_refused(chat_portal):
    """A POST is exactly what DNS rebinding would want to reach."""
    import json as _json

    portal, session_id, _client, _store, _runner = chat_portal
    base = f"/api/workspaces/{_wsid(portal)}/sessions/{session_id}"
    request = urllib.request.Request(
        f"{portal.base_url}{base}/prompt",
        data=_json.dumps({"text": "hi"}).encode("utf-8"),
        method="POST",
    )
    request.add_header("X-Shamsu-Token", portal.token)
    request.add_header("Origin", "http://evil.example")
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=15)
    assert caught.value.code == 403


# --- approvals, answerable from here --------------------------------------


def test_a_pending_approval_is_listed_and_can_be_allowed(chat_portal):
    portal, session_id, _client, store, _runner = chat_portal
    approval_id = store.raise_approval(
        workspace=portal.workspace,
        session_id=session_id,
        description="run pytest -q",
        risk_level="medium",
        action_type="run_command",
    )

    listed = _json(portal, "/api/approvals")["approvals"]
    assert [item["approval_id"] for item in listed] == [approval_id]
    assert listed[0]["description"] == "run pytest -q"

    _status, result = _post(portal, f"/api/approvals/{approval_id}", {"decision": "allow"})
    assert result["resolved"] is True
    assert store.approval(approval_id).decision == "allow"
    assert store.approval(approval_id).decided_by == "web"
    assert _json(portal, "/api/approvals")["approvals"] == []


def test_answering_something_already_answered_says_so(chat_portal):
    """Two people, two surfaces, one question. The second is told, not errored."""
    portal, session_id, _client, store, _runner = chat_portal
    approval_id = store.raise_approval(workspace=portal.workspace, session_id=session_id)
    store.resolve_approval(approval_id, "allow", "telegram")

    _status, result = _post(portal, f"/api/approvals/{approval_id}", {"decision": "deny"})
    assert result["resolved"] is False
    assert store.approval(approval_id).decided_by == "telegram"


def test_a_nonsense_decision_is_rejected(chat_portal):
    portal, session_id, _client, store, _runner = chat_portal
    approval_id = store.raise_approval(workspace=portal.workspace, session_id=session_id)
    with pytest.raises(urllib.error.HTTPError) as caught:
        _post(portal, f"/api/approvals/{approval_id}", {"decision": "maybe"})
    assert caught.value.code == 400


def test_approvals_are_listed_across_every_workspace(chat_portal, tmp_path):
    """You should not have to be looking at the right project to be asked."""
    portal, session_id, _client, store, _runner = chat_portal
    other = tmp_path / "elsewhere"
    other.mkdir()
    store.raise_approval(workspace=other, session_id="s-other", description="rm -rf")

    descriptions = {item["description"] for item in _json(portal, "/api/approvals")["approvals"]}
    assert "rm -rf" in descriptions


def test_a_body_that_is_not_json_is_refused(chat_portal):
    portal, session_id, _client, _store, _runner = chat_portal
    request = urllib.request.Request(
        f"{portal.base_url}/api/approvals/whatever",
        data=b"not json at all",
        method="POST",
    )
    request.add_header("X-Shamsu-Token", portal.token)
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=15)
    assert caught.value.code == 400


# --- the sidebar: most recent first, loaded only when opened --------------


def test_workspaces_come_back_most_recently_used_first(tmp_path, monkeypatch):
    """You almost always want the project you just left, so it goes on top."""
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.runtime import workspaces as registry

    stale = tmp_path / "stale"
    fresh = tmp_path / "fresh"
    stale.mkdir()
    fresh.mkdir()
    SessionManager(stale).create_session("old work")
    time.sleep(0.02)
    SessionManager(fresh).create_session("new work")

    # Registered in the WRONG order on purpose: registry order is when a
    # workspace was first seen, which says nothing about recency.
    registry.remember_workspace(stale)
    registry.remember_workspace(fresh)

    payload = api.workspaces_payload(registry.known_workspaces())
    assert [item["name"] for item in payload["workspaces"]] == ["fresh", "stale"]


def test_a_workspace_with_no_threads_sinks_to_the_bottom(tmp_path, monkeypatch):
    """It has no activity to sort by; floating it on an empty string is worse."""
    monkeypatch.setenv("SHAMSU_HOME", str(tmp_path / "home"))
    from shamsu.runtime import workspaces as registry

    empty = tmp_path / "empty"
    used = tmp_path / "used"
    empty.mkdir()
    used.mkdir()
    SessionManager(used).create_session("real work")
    registry.remember_workspace(empty)
    registry.remember_workspace(used)

    names = [item["name"] for item in api.workspaces_payload(registry.known_workspaces())["workspaces"]]
    assert names == ["used", "empty"]


def test_threads_within_a_workspace_are_newest_first(portal):
    manager = SessionManager(portal.workspace)
    manager.create_session("first")
    time.sleep(0.02)
    manager.create_session("second")
    time.sleep(0.02)
    manager.create_session("third")

    sessions = _json(portal, f"/api/workspaces/{_wsid(portal)}/sessions")["sessions"]
    assert [item["title"] for item in sessions] == ["third", "second", "first"]


def test_each_thread_carries_the_time_the_sidebar_shows(portal):
    """The relative stamp is rendered client-side, so the field has to be there."""
    logger = SessionManager(portal.workspace).create_session("dated")
    sessions = _json(portal, f"/api/workspaces/{_wsid(portal)}/sessions")["sessions"]
    row = next(item for item in sessions if item["session_id"] == logger.session_id)
    assert row["updated_at"]


def test_listing_workspaces_does_not_read_every_thread(portal, monkeypatch):
    """The point of collapsing: opening the portal must not load 126 threads.

    The workspace list needs a count, not the sessions themselves - and the
    browser only asks for a group's sessions when you open it.
    """
    manager = SessionManager(portal.workspace)
    for index in range(5):
        manager.create_session(f"thread {index}")

    payload = _json(portal, "/api/workspaces")
    summary = payload["workspaces"][0]
    assert summary["session_count"] == 5
    # A count and a timestamp, not a session list.
    assert "sessions" not in summary

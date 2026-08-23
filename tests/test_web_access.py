"""The plain link, and prompts that cannot silently vanish.

Both from a live report: opening `http://127.0.0.1:8765/` gave a page that
rendered completely and then failed every request with a 401, and a prompt for
a session id of `undefined` came back `202 accepted` and was never seen again.
"""
from __future__ import annotations

import json
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from shamsu.webui.server import TOKEN_ENV, WebPortal, is_loopback


@pytest.fixture
def portal():
    # `port=0` so the OS picks a free one. The default is a FIXED 8765, which
    # is the port a real SHAMSU serves on - so with the portal running these
    # tests bound nothing, sent their requests to the LIVE instance, and asked
    # it questions about a workspace it had never heard of. That is a false
    # failure at best and a test suite driving your running agent at worst.
    started = WebPortal(Path(tempfile.mkdtemp(prefix="shamsu-web-test-")), port=0)
    started.start()
    try:
        yield started
    finally:
        started.stop()


def _call(portal, path, method="GET", body=None, token=None, origin=None):
    url = f"{portal.base_url}{path}"
    if token:
        url += ("&" if "?" in url else "?") + f"t={token}"
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    request.add_header("Origin", origin or portal.base_url)
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


# -- phase 1: the link is the whole thing ----------------------------------


def test_a_loopback_portal_needs_no_token(portal):
    assert not portal.requires_token
    assert portal.url == portal.base_url, "the link still carries a token"


def test_the_plain_link_drives_the_whole_api(portal):
    """The shell has to be served without a token - the browser has nowhere to
    put one before the page loads - so an API that demanded one gave a
    complete-looking application that 401'd on every request."""
    assert _call(portal, "/")[0] == 200
    assert _call(portal, "/api/health")[0] == 200
    assert _call(portal, "/api/workspaces")[0] == 200


def test_a_bind_beyond_this_machine_still_demands_the_token(portal):
    """At that point anything on the LAN could write files and run commands."""
    portal.host = "0.0.0.0"
    assert portal.requires_token
    assert f"t={portal.token}" in portal.url


def test_the_token_can_be_demanded_back(portal, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "1")
    assert portal.requires_token
    assert _call(portal, "/api/health")[0] == 401
    assert _call(portal, "/api/health", token=portal.token)[0] == 200


def test_only_loopback_addresses_count_as_loopback():
    assert is_loopback("127.0.0.1")
    assert is_loopback("::1")
    assert is_loopback("localhost")
    assert not is_loopback("0.0.0.0")
    assert not is_loopback("192.168.1.10")


def test_a_foreign_origin_is_still_refused(portal):
    """The DNS-rebinding defence is what actually guards a loopback portal, and
    it must not have gone away with the token."""
    status, _body = _call(portal, "/api/health", origin="http://evil.example")
    assert status == 403


# -- phase 2: a prompt cannot vanish ---------------------------------------


def _workspace_id(portal):
    _status, body = _call(portal, "/api/workspaces")
    return json.loads(body)["workspaces"][0]["id"]


@pytest.mark.parametrize("bogus", ["undefined", "None", "20990101-000000-zzzz"])
def test_a_prompt_for_a_session_that_does_not_exist_is_refused(portal, bogus):
    """It used to come back `202 accepted` with a queue id, then run against a
    thread nobody could open: no answer, no error, no trace."""
    workspace_id = _workspace_id(portal)
    status, body = _call(
        portal,
        f"/api/workspaces/{workspace_id}/sessions/{bogus}/prompt",
        method="POST",
        body={"text": "hello"},
    )
    assert status == 404, body
    assert bogus in body.decode()


def test_session_exists_is_honest_about_both_answers(tmp_path):
    from shamsu.session.manager import SessionManager
    from shamsu.webui.api import session_exists

    assert not session_exists(tmp_path, "undefined")
    assert not session_exists(tmp_path, "")

    logger = SessionManager(tmp_path).create_session("probe")
    assert session_exists(tmp_path, logger.session_id)


def test_a_failed_turn_ends_visibly_on_the_stream(tmp_path):
    """Surfaces render from the turn stream. A worker that only marked its
    queue row cancelled left the browser on `turn.start` for ever - a bubble
    that never resolves, which is what "out of sync" looks like."""
    from shamsu.control.runner import QueuedRunner
    from shamsu.runtime.turn_stream import TurnStream

    QueuedRunner(surface="web")._announce_failure(
        tmp_path, "sess-x", RuntimeError("the model did not respond within 600s")
    )

    events = TurnStream(tmp_path, "sess-x", persist=False).replay(-1)
    kinds = [event.kind for event in events]
    assert "error" in kinds, "the failure was never announced"
    assert kinds[-1] == "turn.end", "the turn never ended"
    assert events[-1].data.get("status") == "failed"
    assert "did not respond" in events[0].text
    assert events[-1].source == "web", "the surface that failed is not recorded"


def test_announcing_a_failure_never_raises_a_second_one(tmp_path, monkeypatch):
    from shamsu.control import runner as runner_module

    monkeypatch.setattr(
        runner_module.QueuedRunner, "surface", "web", raising=False
    )
    broken = runner_module.QueuedRunner(surface="web")
    # An unwritable stream must not turn a failed turn into a crashed worker.
    broken._announce_failure(Path("\\\\?\\nonexistent"), "sess", RuntimeError("x"))

"""The check the model invented because it did not exist.

Live 2026-08-31 in `F:\\voice-demo`, building a browser game, the model called
`verify_web_app` repeatedly - a tool that has never existed here - got "There is
no tool called verify_web_app" every time, and reported to the user that
"verify_web_app keeps reporting 'no canvas found'... an environment limitation
where the browser tool cannot properly detect WebGL canvases". None of that
happened; it then skipped twelve contract assertions on the strength of it.

`BrowserTool` existed the whole time, with a passing test that drives real
Chromium, and had zero `_tool_schema` entries - so for a browser project the
contract's `BY_RUN` evidence was unreachable and `contract_assert_skip` was the
only exit.

These drive a real browser against real files. There is no point mocking the
thing whose entire job is to tell you what a real browser did.
"""
from __future__ import annotations

import json
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from shamsu.tools.page_check import check_page, is_local_url


@pytest.fixture(scope="module")
def browser_available():
    from shamsu.tools.browser import BrowserTool

    status = BrowserTool(Path.cwd()).status()
    if not status.available:
        pytest.skip(f"no browser: {status.state}")
    return True


@pytest.fixture
def serve(tmp_path):
    """Serve *tmp_path* on a free port for one test."""
    handler = partial(SimpleHTTPRequestHandler, directory=str(tmp_path))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def write(name: str, body: str) -> str:
        (tmp_path / name).write_text(body, encoding="utf-8")
        return f"http://127.0.0.1:{server.server_address[1]}/{name}"

    try:
        yield write
    finally:
        server.shutdown()
        server.server_close()


@pytest.fixture
def report(browser_available):
    from shamsu.tools.browser import BrowserTool

    made = []

    def run(url: str) -> dict:
        browser = BrowserTool(Path.cwd())
        made.append(browser)
        return json.loads(check_page(browser, url).render())

    try:
        yield run
    finally:
        for browser in made:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass


# -- what "it works" looks like ---------------------------------------------


def test_a_page_that_draws_is_reported_as_working(serve, report):
    url = serve("ok.html", "<h1>Hello</h1><canvas width=300 height=150></canvas>")
    result = report(url)
    assert result["ok"] is True
    assert "drew something" in result["message"]
    assert result["data"]["elements"]["canvas"] == 1
    assert result["data"]["canvas_drawn"] is True
    assert result["data"]["screenshot"]


# -- the three failures it exists to catch ------------------------------------


def test_a_canvas_with_no_drawing_surface_is_caught(serve, report):
    """The exact shape of "the game runs but nothing is on screen": the element
    is in the DOM, styled and present, and its buffer is 0x0."""
    url = serve(
        "blank.html",
        "<canvas id=c width=0 height=0></canvas>",
    )
    result = report(url)
    assert result["ok"] is False
    assert "0x0" in result["message"]
    assert result["data"]["elements"]["canvas"] == 1
    assert result["data"]["canvas_drawn"] is False


def test_a_script_that_throws_is_reported_with_the_error(serve, report):
    url = serve(
        "boom.html",
        "<h1>Game</h1><script>throw new Error('SoundManager is not defined');</script>",
    )
    result = report(url)
    assert result["ok"] is False
    assert "console error" in result["message"]
    assert any("SoundManager" in e for e in result["data"]["console_errors"])


def test_a_page_that_renders_nothing_at_all_is_caught(serve, report):
    url = serve("empty.html", "<html><body></body></html>")
    result = report(url)
    assert result["ok"] is False
    assert "no text and no canvas" in result["message"]


def test_a_url_that_does_not_serve_is_a_clean_failure(report):
    result = report("http://127.0.0.1:9/nothing-here")
    assert result["ok"] is False
    assert result["message"]


# -- a server that is still binding is not a broken page ----------------------


@pytest.mark.parametrize(
    "message",
    [
        "Page.goto: net::ERR_EMPTY_RESPONSE at http://localhost:8000/",
        "net::ERR_CONNECTION_REFUSED",
        "net::ERR_CONNECTION_RESET at http://localhost:3000/",
    ],
)
def test_a_server_still_coming_up_is_retried(message):
    """Live 2026-08-31: the agent started a server, called this seconds later,
    got ERR_EMPTY_RESPONSE - and `curl` fetched the same URL ten seconds on.
    The check was wrong, and it sent the model to fix working code."""
    from shamsu.tools.page_check import _still_coming_up

    assert _still_coming_up(message)


@pytest.mark.parametrize(
    "message",
    [
        "Page.goto: net::ERR_NAME_NOT_RESOLVED at http://nope/",
        "Timeout 30000ms exceeded",
        "Browser access denied by user.",
    ],
)
def test_a_real_failure_is_not_retried(message):
    from shamsu.tools.page_check import _still_coming_up

    assert not _still_coming_up(message)


def test_a_slow_server_is_waited_for(serve, browser_available, monkeypatch):
    """The whole point: the first navigation fails and the check still passes."""
    import json as _json

    from shamsu.tools import page_check
    from shamsu.tools.browser import BrowserTool

    monkeypatch.setattr(page_check, "RETRY_SECONDS", 0.05)
    url = serve("late.html", "<h1>Up now</h1>")
    browser = BrowserTool(Path.cwd())
    real_open = browser.open
    calls = {"n": 0}

    def flaky(target, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return type(
                "R", (), {"ok": False, "message": "net::ERR_EMPTY_RESPONSE"}
            )()
        return real_open(target, **kwargs)

    browser.open = flaky
    try:
        result = _json.loads(page_check.check_page(browser, url).render())
    finally:
        browser.close()
    assert calls["n"] == 2
    assert result["ok"] is True


def test_the_user_is_asked_once_not_once_per_retry(monkeypatch):
    """A retry is the same decision. Prompting three times for one check is how
    a capability becomes unusable."""
    from shamsu.tools import page_check

    monkeypatch.setattr(page_check, "RETRY_SECONDS", 0.0)
    asked = []

    class _Browser:
        def open(self, _url, reason="", require_approval=True):
            asked.append(require_approval)
            return type("R", (), {"ok": False, "message": "net::ERR_EMPTY_RESPONSE"})()

    page_check.check_page(_Browser(), "https://example.com/remote")
    assert asked[0] is True
    assert not any(asked[1:])


# -- loading a page and USING one are different questions ---------------------

#: The page that made this necessary: a start button, a canvas, and a loop that
#: draws a couple of dots and nothing else. It passes every load-time check.
_QUIET_GAME = """
<canvas id=c width=400 height=300></canvas>
<button id=start>START</button>
<script>
  const g = document.getElementById('c').getContext('2d');
  g.fillStyle = '#000'; g.fillRect(0, 0, 400, 300);
  document.getElementById('start').onclick = () => {
    setInterval(() => {
      g.fillStyle = '#000'; g.fillRect(0, 0, 400, 300);
      g.fillStyle = '#fff'; g.fillRect(5, 5, 2, 2);
    }, 30);
  };
</script>
"""

#: The same page with a loop that actually fills the screen.
_BUSY_GAME = _QUIET_GAME.replace(
    "g.fillStyle = '#fff'; g.fillRect(5, 5, 2, 2);",
    "g.fillStyle = '#fff'; for (let i=0;i<300;i++) "
    "g.fillRect(Math.random()*400, Math.random()*300, 12, 12);",
)


def _checked(url, **kwargs):
    import json as _json

    from shamsu.tools.browser import BrowserTool
    from shamsu.tools.page_check import check_page

    browser = BrowserTool(Path.cwd())
    try:
        return _json.loads(check_page(browser, url, **kwargs).render())
    finally:
        browser.close()


def test_a_canvas_that_draws_almost_nothing_is_reported(serve, browser_available):
    """Live 2026-08-31: clicking START on the asteroid game moved the canvas
    from 1.22% covered to 1.23% - the stars and the ship, and not one asteroid.
    Every load-time check passed."""
    url = serve("quiet.html", _QUIET_GAME)
    result = _checked(url, click="#start", wait_seconds=1.5)
    assert result["data"]["clicked"] == "#start"
    covered = result["data"]["canvas_covered_pct"]
    assert covered is not None and covered < 1.0
    # The NUMBER reaches the model, whatever the verdict - hiding it behind
    # "the page loaded and drew something" is the failure this closes.
    assert "canvas is drawn on" in result["message"]


def test_a_busy_canvas_measures_as_busy(serve, browser_available):
    url = serve("busy.html", _BUSY_GAME)
    result = _checked(url, click="#start", wait_seconds=1.5)
    assert result["data"]["canvas_covered_pct"] > 5.0


def test_motion_is_measured_between_two_samples(serve, browser_available):
    url = serve("busy2.html", _BUSY_GAME)
    result = _checked(url, click="#start", wait_seconds=1.5)
    assert result["data"]["canvas_changed_pct"] > 1.0


def test_a_selector_that_matches_nothing_is_reported_not_raised(serve, browser_available):
    url = serve("nobutton.html", "<h1>Hi</h1>")
    result = _checked(url, click="#missing")
    assert result["ok"] is False
    assert "could not click '#missing'" in result["message"]


def test_the_selector_is_echoed_back_exactly(serve, browser_available):
    """`.capitalize()` turned `#startBtn` into `#startbtn`, which is a different
    selector and a wrong thing to hand back to a model."""
    url = serve("case.html", "<button id=startBtn>go</button><h1>x</h1>")
    result = _checked(url, click="#startBtn")
    assert "#startBtn" in result["message"]


def test_a_wait_is_capped(serve, browser_available):
    """A model must not be able to spend its turn budget watching a page."""
    import time as _time

    from shamsu.tools.page_check import MAX_WAIT_SECONDS

    url = serve("wait.html", "<h1>Hi</h1>")
    started = _time.perf_counter()
    _checked(url, wait_seconds=600)
    assert _time.perf_counter() - started < MAX_WAIT_SECONDS + 20


def test_no_canvas_means_no_canvas_numbers(serve, browser_available):
    """Reporting 0% for a page with no canvas would be a lie about it."""
    url = serve("plain.html", "<h1>Just text</h1><p>and more</p>")
    result = _checked(url)
    assert result["data"]["canvas_covered_pct"] is None


# -- evidence, not a page dump ------------------------------------------------


def test_the_body_text_is_bounded(serve, report):
    from shamsu.tools.page_check import MAX_TEXT_CHARS

    url = serve("long.html", "<p>" + ("word " * 6000) + "</p>")
    result = report(url)
    assert len(result["data"]["visible_text"]) <= MAX_TEXT_CHARS


def test_errors_are_capped_so_one_broken_page_cannot_fill_the_window(serve, report):
    url = serve(
        "many.html",
        "<h1>x</h1><script>for (let i=0;i<40;i++) console.error('boom '+i);</script>",
    )
    result = report(url)
    assert len(result["data"]["console_errors"]) <= 10


# -- approval: local is not egress -------------------------------------------


@pytest.mark.parametrize(
    "url",
    ["http://localhost:8000", "http://127.0.0.1:3000/game", "file:///c:/tmp/a.html"],
)
def test_a_local_page_is_not_an_egress_decision(url):
    """A prompt per check is what made this capability unusable in the one
    place it was needed - and loading a page the agent just served, headlessly,
    on this machine, is the same kind of act as reading a file it just wrote."""
    assert is_local_url(url) is True


@pytest.mark.parametrize(
    "url",
    ["https://example.com", "http://192.168.1.5:8000", "http://10.0.0.2/", "ftp://x/y"],
)
def test_anything_else_still_asks(url):
    assert is_local_url(url) is False


# -- and it is actually reachable from the agent ------------------------------


def test_the_tool_is_offered_to_the_model():
    """The whole defect was that the capability existed and had no schema."""
    from shamsu.agents.simple_chat import SIMPLE_TOOLS, SIMPLE_TOOL_SCHEMAS

    assert "check_page" in SIMPLE_TOOLS
    schema = next(
        s for s in SIMPLE_TOOL_SCHEMAS if s["function"]["name"] == "check_page"
    )
    assert "url" in schema["function"]["parameters"]["properties"]
    # It has to say that writing the file is not evidence, or a model will keep
    # treating a successful write as a successful page.
    assert "not evidence" in schema["function"]["description"]

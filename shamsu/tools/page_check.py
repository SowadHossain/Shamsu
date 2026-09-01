"""Load a page in a real browser and report what actually happened.

The gap this fills, in the words of the model that hit it. Live 2026-08-31,
`F:\\voice-demo`, building a Three.js-shaped asteroid game: it called
`verify_web_app` - a tool that has never existed anywhere in this codebase -
repeatedly, got "There is no tool called verify_web_app" every time, and told the
user:

    "The issue is that verify_web_app keeps reporting 'no canvas found' even
     though the game runs successfully... This appears to be an environment
     limitation where the browser tool cannot properly detect WebGL canvases."

None of that happened. It invented the tool, invented its output, and then
skipped twelve contract assertions on the strength of the invention.

It was reaching for something real. `BrowserTool` exists, has a passing test that
drives actual Chromium and captures console output and a screenshot, and had
**zero** `_tool_schema` entries - it was never made into an agent tool at all.
So for a browser project the contract's `BY_RUN` evidence was unreachable by
construction, and `contract_assert_skip` was the only exit the model had.

**One tool, not six.** `open`/`click`/`type_text`/`read`/`screenshot` is a
driving API for something with a plan. What a coding agent needs after writing a
page is one question - *did it load, did it draw, did it throw* - and one answer
it can put in a contract. Clicking through a flow is a different job and can have
its own tools when something needs them.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["PageReport", "check_page"]

#: How long to wait after `domcontentloaded` before deciding what is on the
#: page. A canvas game draws on its first `requestAnimationFrame`, which is
#: after DOM ready and before any load event; a second is far more than one
#: frame and far less than a timeout anybody notices.
SETTLE_SECONDS = 1.0

#: Navigation failures that mean "the server is not up YET" rather than "the
#: page is broken". A dev server the agent started moments ago is the normal
#: case here, and reporting a page as broken because its server was still
#: binding is the most expensive answer this tool can give: it tells the model
#: to go and fix working code.
#:
#: Live 2026-08-31 in `F:oice-demo`. The agent started `python -m http.server
#: 8000`, called `check_page` seconds later and got `net::ERR_EMPTY_RESPONSE` -
#: then ran `curl` against the same URL ten seconds on and got the page. The
#: check was wrong and the model fell back to curl, which can tell you a page
#: was served but nothing about whether it renders.
_NOT_UP_YET = (
    "ERR_EMPTY_RESPONSE",
    "ERR_CONNECTION_REFUSED",
    "ERR_CONNECTION_RESET",
    "ERR_CONNECTION_CLOSED",
    "ERR_SOCKET_NOT_CONNECTED",
    "ERR_ABORTED",
)

#: Tries, and the pause between them. Three tries over ~3s covers a server
#: binding a port; it does not paper over a server that is genuinely down,
#: which still fails - just three seconds later.
NAVIGATION_ATTEMPTS = 3
RETRY_SECONDS = 1.5


def _still_coming_up(message: str) -> bool:
    return any(code in (message or "") for code in _NOT_UP_YET)


#: Text longer than this is a page dump, not evidence. The model asked whether
#: the page works, and eight thousand characters of body text answers a
#: different question at the cost of a quarter of the window.
MAX_TEXT_CHARS = 1200

#: Longest a check may sit watching. Enough to see a game get going; short
#: enough that a model cannot spend its turn budget waiting for one.
MAX_WAIT_SECONDS = 10.0


@dataclass(frozen=True)
class PageReport:
    """What one page load actually did."""

    ok: bool
    url: str = ""
    title: str = ""
    #: Console errors and uncaught exceptions, in order.
    errors: tuple[str, ...] = ()
    #: Elements that decide whether anything was DRAWN, by tag.
    counts: dict[str, int] | None = None
    #: True when a canvas exists AND has a non-zero drawing surface. A canvas
    #: sized 0x0 is present in the DOM and invisible on screen, which is one of
    #: the ways a game "runs" and shows nothing.
    canvas_drawn: bool = False
    text: str = ""
    screenshot: str = ""
    message: str = ""
    #: Whether the page was clicked or waited on, rather than merely loaded.
    #: A blank canvas means different things in those two cases.
    watched: bool = False
    #: What was clicked, and whether the click landed.
    clicked: str = ""
    click_failed: str = ""
    #: Share of the canvas that is not the background colour, 0..1, and the
    #: share that CHANGED between two samples a moment apart. `None` when there
    #: is no readable 2d canvas - a WebGL context cannot be read this way, and
    #: reporting 0 for it would be a lie.
    canvas_ink: float | None = None
    canvas_motion: float | None = None

    def render(self) -> str:
        """The tool result, as the model reads it."""
        if not self.ok:
            return json.dumps(
                {"ok": False, "message": self.message or "The page could not be loaded."},
                ensure_ascii=True,
            )
        counts = self.counts or {}
        # The VERDICT first and in words, because that is the sentence the model
        # will copy into a contract assertion. Numbers after it, so the claim
        # and its evidence arrive together.
        problems: list[str] = []
        if self.click_failed:
            problems.append(self.click_failed)
        if self.errors:
            problems.append(f"{len(self.errors)} console error(s)")
        if counts.get("canvas") and not self.canvas_drawn:
            problems.append("a canvas element exists but its drawing surface is 0x0")
        if not counts.get("canvas") and not self.text.strip():
            problems.append("the page rendered no text and no canvas")
        # The two that only mean something once something has been DRAWN. Stated
        # as what was seen, not as a verdict on the design: a sparse screen is
        # legitimate, and "1% covered and not changing" is a fact the model can
        # weigh. It is also the fact that would have caught the asteroid game.
        # Only once the page has been GIVEN a chance. A canvas that is blank the
        # instant it loads is a canvas nothing has drawn to yet, which is
        # ordinary; a canvas still blank after a click and a wait is a game that
        # is not running. The number is reported either way - this decides only
        # whether to call it a problem.
        if self.watched and self.canvas_ink is not None and self.canvas_ink < BLANK_CANVAS_INK:
            problems.append(
                f"the canvas is only {100 * self.canvas_ink:.1f}% covered - it is "
                "drawing almost nothing"
            )
        elif (
            self.canvas_motion is not None
            and self.canvas_motion < STILL_CANVAS_CHANGE
            and self.clicked
        ):
            problems.append(
                f"nothing on the canvas changed after clicking {self.clicked} - "
                "it is not animating"
            )
        summary = (
            "The page loaded and drew something."
            if not problems
            else "The page loaded but " + "; ".join(problems) + "."
        )
        # The measurements go in the SENTENCE, not only in the data. A canvas
        # 1.3% covered that changed 0.3% in three seconds passes every threshold
        # here and is still a game in which nothing is happening - and the only
        # thing that can judge that is the model, which knows what it was trying
        # to build. Hiding the number behind a verdict is how "The page loaded
        # and drew something" came to be the whole account of an asteroid game
        # with no asteroids in it.
        if self.clicked or self.canvas_ink is not None:
            observed: list[str] = []
            if self.clicked:
                observed.append(f"clicked {self.clicked}")
            if self.canvas_ink is not None:
                observed.append(f"{100 * self.canvas_ink:.1f}% of the canvas is drawn on")
            if self.canvas_motion is not None:
                observed.append(f"{100 * self.canvas_motion:.1f}% of it changed while watching")
            summary += " " + "; ".join(observed) + "."
            summary += (
                " Compare that with what you meant to build: a screen that should"
                " be busy and is barely covered is not working, whatever loaded."
            )
        return json.dumps(
            {
                "ok": not problems,
                "message": summary,
                "data": {
                    "url": self.url,
                    "title": self.title,
                    "console_errors": list(self.errors[:10]),
                    "elements": counts,
                    "canvas_drawn": self.canvas_drawn,
                    "clicked": self.clicked,
                    "canvas_covered_pct": (
                        None if self.canvas_ink is None
                        else round(100 * self.canvas_ink, 2)
                    ),
                    "canvas_changed_pct": (
                        None if self.canvas_motion is None
                        else round(100 * self.canvas_motion, 2)
                    ),
                    "visible_text": self.text,
                    "screenshot": self.screenshot,
                },
            },
            ensure_ascii=True,
        )


#: How much of the canvas is being DRAWN on, and whether that is changing.
#:
#: The measurement that makes this tool useful for a game, and it needs no
#: vision model: the browser reads its own pixels and returns two numbers, which
#: the harness turns into a sentence. The model never sees an image.
#:
#: Live 2026-08-31, the asteroid game in `F:oice-demo`. It loaded with no
#: console errors, a canvas with a real drawing surface, and every element the
#: page promised - so every check this tool had said it was fine. Clicking START
#: and playing for three seconds moved the ink from **1.22% to 1.23%**: the
#: stars and the ship, and not one asteroid. The bug was that half of them spawn
#: moving away from the screen and are never cleaned up, which took reading the
#: source to find and which those two numbers state outright.
#:
#: Sampled every 17th pixel - a prime stride, so it cannot land in step with a
#: repeating pattern - which is ~28k samples on an 800x600 canvas and runs in
#: single-digit milliseconds.
_CANVAS_INK = """() => {
  const c = document.querySelector('canvas');
  if (!c || !c.width || !c.height) return null;
  let g; try { g = c.getContext('2d'); } catch (e) { return null; }
  if (!g) return null;
  let d; try { d = g.getImageData(0, 0, c.width, c.height).data; } catch (e) { return null; }
  const counts = new Map();
  const seen = [];
  let n = 0;
  for (let i = 0; i < d.length; i += 4 * 17) {
    const k = (d[i] << 16) | (d[i + 1] << 8) | d[i + 2];
    counts.set(k, (counts.get(k) || 0) + 1);
    seen.push(k);
    n++;
  }
  let background = 0, best = 0;
  for (const [k, v] of counts) if (v > best) { best = v; background = k; }
  return { sampled: n, distinct: counts.size,
           ink: n ? (n - best) / n : 0, pixels: seen };
}"""

#: Below this a canvas is drawing essentially nothing - a background, and maybe
#: a cursor. Deliberately low: a sparse game screen is legitimate, and the
#: number is reported either way. This only decides whether to SAY something.
BLANK_CANVAS_INK = 0.005

#: How much of the canvas has to differ between two samples for it to count as
#: animating. One moving sprite on a large canvas is a fraction of a percent.
STILL_CANVAS_CHANGE = 0.001


#: Counted because their presence or absence is what "did it render" means for
#: the pages this harness builds. Kept short deliberately - a census of every
#: tag is a page dump by another name.
_COUNTED = ("canvas", "svg", "img", "button", "input", "form", "table", "h1")


#: Hosts a page check may open without asking. Loading a page the agent just
#: served, headlessly, on this machine, is the same kind of act as reading a
#: file it just wrote - the sandbox equivalent already applies, and a prompt per
#: check is what made this capability unusable in the one place it was needed.
#: Anything else is network egress, which this project asks about on purpose:
#: see `_web_is_reachable`, where the web tools are withheld until someone opts
#: in.
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "[::1]", "0.0.0.0")


def is_local_url(url: str) -> bool:
    """Is this a page on this machine?"""
    from urllib.parse import urlparse

    try:
        parsed = urlparse((url or "").strip())
    except ValueError:
        return False
    if parsed.scheme == "file":
        return True
    if parsed.scheme not in {"http", "https", ""}:
        return False
    host = (parsed.hostname or "").lower()
    return host in {h.strip("[]") for h in _LOCAL_HOSTS}


def check_page(
    browser: Any,
    url: str,
    *,
    settle_seconds: float = SETTLE_SECONDS,
    click: str = "",
    wait_seconds: float = 0.0,
) -> PageReport:
    """Open *url*, let it settle, and report what is there.

    Takes the `BrowserTool` rather than making one, so approval, logging and the
    single shared Chromium all stay where they are.

    A page that throws is still a REPORT, not an error: "loaded, and here are the
    four exceptions it threw" is the most useful answer this can give, and
    returning a failure would lose the errors that are the whole point.
    """
    import time as _time

    approval_needed = not is_local_url(url)
    for attempt in range(NAVIGATION_ATTEMPTS):
        opened = browser.open(
            url,
            reason="Check that the page loads and renders.",
            # Asked ONCE. A retry is the same decision, and prompting three
            # times for one check is how a capability becomes unusable.
            require_approval=approval_needed and attempt == 0,
        )
        if getattr(opened, "ok", False):
            break
        message = str(getattr(opened, "message", "") or "")
        if attempt == NAVIGATION_ATTEMPTS - 1 or not _still_coming_up(message):
            return PageReport(ok=False, message=message)
        _time.sleep(RETRY_SECONDS)
    else:  # pragma: no cover - the loop always breaks or returns
        return PageReport(ok=False, message="The page could not be loaded.")

    page = getattr(browser, "_page", None)
    counts: dict[str, int] = {}
    canvas_drawn = False
    if page is not None:
        try:
            page.wait_for_timeout(int(settle_seconds * 1000))
        except Exception:  # noqa: BLE001 - a settle that fails is not a failure
            pass
        for tag in _COUNTED:
            try:
                found = page.locator(tag).count()
            except Exception:  # noqa: BLE001 - one bad selector must not end the check
                continue
            if found:
                counts[tag] = found
        if counts.get("canvas"):
            try:
                # The DOM says a canvas exists; this asks whether it has any
                # surface to draw on. `width`/`height` are the drawing buffer,
                # which is what a 0x0 canvas gets wrong while still being
                # present, styled, and completely invisible.
                canvas_drawn = bool(
                    page.evaluate(
                        "() => Array.from(document.querySelectorAll('canvas'))"
                        ".some(c => c.width > 0 && c.height > 0)"
                    )
                )
            except Exception:  # noqa: BLE001
                canvas_drawn = False

    # Interact, then look again. A page that LOADS is a different question from
    # a page that WORKS, and for anything with a start button they have
    # different answers - which is how a game that draws nothing passed every
    # check this tool had.
    clicked = ""
    click_failed = ""
    ink = motion = None
    if page is not None:
        before = None
        if click:
            try:
                page.click(click, timeout=4000)
                clicked = click
            except Exception as exc:  # noqa: BLE001 - a miss is a REPORT
                first = str(exc).strip().splitlines()[0][:160]
                click_failed = f"could not click {click!r}: {first}"
            if clicked:
                try:
                    before = page.evaluate(_CANVAS_INK)
                except Exception:  # noqa: BLE001
                    before = None
        if wait_seconds > 0:
            try:
                page.wait_for_timeout(int(min(wait_seconds, MAX_WAIT_SECONDS) * 1000))
            except Exception:  # noqa: BLE001
                pass
        try:
            after = page.evaluate(_CANVAS_INK)
        except Exception:  # noqa: BLE001
            after = None
        if after:
            ink = float(after.get("ink") or 0.0)
            if before and before.get("sampled") == after.get("sampled"):
                # Compared pixel for pixel at the same stride, so this is the
                # share of the canvas that actually moved - not a difference in
                # how much was sampled.
                a, b = before.get("pixels") or [], after.get("pixels") or []
                if a and len(a) == len(b):
                    motion = sum(1 for x, y in zip(a, b) if x != y) / len(a)

    shot = ""
    try:
        captured = browser.screenshot()
        if getattr(captured, "ok", False):
            shot = str(getattr(captured, "screenshot_path", "") or "")
    except Exception:  # noqa: BLE001 - a screenshot is evidence, not the check
        pass

    # Re-read AFTER settling: `open` sampled the text at domcontentloaded, and a
    # page that renders on its first frame had nothing in it yet.
    errors = tuple(getattr(opened, "console_errors", ()) or ())
    text = str(getattr(opened, "visible_text", "") or "")
    try:
        reread = browser.read()
        if getattr(reread, "ok", False):
            errors = tuple(getattr(reread, "console_errors", ()) or errors)
            text = str(getattr(reread, "visible_text", "") or text)
    except Exception:  # noqa: BLE001
        pass

    return PageReport(
        ok=True,
        url=str(getattr(opened, "url", "") or url),
        title=str(getattr(opened, "title", "") or ""),
        errors=errors,
        counts=counts,
        canvas_drawn=canvas_drawn,
        text=text[:MAX_TEXT_CHARS],
        screenshot=shot,
        watched=bool(click or wait_seconds > 0),
        clicked=clicked,
        click_failed=click_failed,
        canvas_ink=ink,
        canvas_motion=motion,
    )

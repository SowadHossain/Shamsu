"""One prompt line, whichever surface a turn came from.

    shamsu (Review Project) cli>
    shamsu (Review Project) web>
    shamsu (Review Project) telegram>

Two labels used to compete for one slot. The REPL printed the session title
(`shamsu (asteroids)> `) and the Telegram mirror printed the surface
(`shamsu (remote-telegram)> `), so a remote turn LOST the thread name in order
to gain a source name - and the two never appeared together, which is exactly
when you need both.

The surface is always shown, including for a local turn. That is deliberate: a
single scrollback now interleaves turns from three places, and the word before
`>` is the discriminator. Printing it only for remote turns would make the
difference one word buried mid-string, easy to miss when reading back what ran
overnight - whereas a fixed column is scannable.

The title is context, not content, so it is truncated hard and on a word
boundary. `shamsu (Review Project) cli>` reads; `shamsu (Review Project And
See W...) cli>` does not, and the full title was never the point.
"""
from __future__ import annotations

from typing import Any

SURFACE_CLI = "cli"
SURFACE_WEB = "web"
SURFACE_TELEGRAM = "telegram"

#: Long enough for two or three words of an auto-generated title, short enough
#: that the prompt does not dominate the line it precedes.
TITLE_MAX_CHARS = 18

#: Titles that carry no information. Showing "(Untitled Session)" costs width
#: and tells you nothing you did not already know.
EMPTY_TITLES = frozenset({"shamsu session", "session", "untitled", "untitled session"})


def prompt_label(
    title: str = "",
    surface: str = SURFACE_CLI,
    *,
    max_chars: int = TITLE_MAX_CHARS,
    trailing_space: bool = True,
) -> str:
    """The prompt string for *surface*, naming *title* when there is one."""
    clean = _short_title(title, max_chars)
    name = (surface or SURFACE_CLI).strip() or SURFACE_CLI
    label = f"shamsu ({clean}) {name}>" if clean else f"shamsu {name}>"
    return f"{label} " if trailing_space else label


def session_prompt_label(
    session_logger: Any, surface: str = SURFACE_CLI, *, max_chars: int = TITLE_MAX_CHARS
) -> str:
    """`prompt_label` for a session object, tolerating a broken one.

    A decorative label must never be the reason the REPL stops accepting input,
    so every lookup here is best-effort.
    """
    try:
        title = str(getattr(getattr(session_logger, "metadata", None), "title", "") or "")
    except Exception:  # noqa: BLE001
        title = ""
    return prompt_label(title, surface, max_chars=max_chars)


def _short_title(title: str, max_chars: int) -> str:
    clean = " ".join(str(title or "").split())
    if not clean or clean.lower() in EMPTY_TITLES:
        return ""
    if len(clean) <= max_chars:
        return clean
    # Cut on a word boundary, so the label ends on a word rather than mid-one.
    cut = clean[:max_chars].rsplit(" ", 1)[0].rstrip(" ,.:;-")
    return cut or clean[:max_chars].rstrip()

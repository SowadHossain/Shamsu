"""PRDContract: the structured, source-of-truth view of a PRD.

The legacy extractor only pulled Django entities/fields; game mechanics,
controls, screens, acceptance criteria, and constraints were dropped (see
`feature_requests`, which was extracted and then read nowhere). `PRDContract`
captures the whole intent deterministically so every downstream generator
(template hole-fill or template-free) writes code the PRD actually asked for,
and so acceptance criteria can be tracked and reported.

Extraction is deterministic keyword/section parsing on purpose: a local 7B
model is unreliable at this, and a stable contract is what grounds it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from shamsu.types import ParsedPRD

# --- vocabulary ---------------------------------------------------------------

# Specific game types we can recognize by name. The first hit wins.
_GAME_TYPES: list[tuple[str, tuple[str, ...]]] = [
    ("pong", ("pong",)),
    ("snake", ("snake",)),
    ("breakout", ("breakout", "brick breaker", "arkanoid")),
    ("tetris", ("tetris",)),
    ("flappy", ("flappy",)),
    ("space-invaders", ("space invaders", "space-invaders", "invaders")),
    ("asteroids", ("asteroids",)),
    ("2048", ("2048",)),
    ("minesweeper", ("minesweeper",)),
    ("tic-tac-toe", ("tic tac toe", "tic-tac-toe", "noughts and crosses")),
    ("platformer", ("platformer", "jump and run", "side-scroller", "side scroller")),
    ("shooter", ("shooter", "shmup", "bullet hell")),
    ("racing", ("racing", "racer")),
    ("puzzle", ("puzzle",)),
    ("rpg", ("rpg", "role-playing", "role playing")),
]

_MULTIPLAYER_HINTS = (
    "multiplayer", "multi-player", "online", "lobby", "matchmaking", "pvp",
    "co-op", "coop", "rooms", "netcode", "networked", "server-authoritative",
    "players connect", "join a room", "real-time multiplayer",
)
_SINGLE_LOCAL_HINTS = (
    "single player", "single-player", "singleplayer", "local", "offline",
    "1 player", "one player", "two-player local", "hotseat", "couch",
)
_3D_HINTS = ("3d", "three.js", "webgl", "r3f", "react-three", "voxel")

# Heading matchers -> which contract bucket the section's lines belong to.
_SECTION_BUCKETS: list[tuple[str, tuple[str, ...]]] = [
    ("controls", ("control", "input", "keyboard", "keys", "keybinding", "gamepad")),
    ("acceptance_criteria",
     ("acceptance", "criteria", "definition of done", "success criteria", "must have", "must-have")),
    ("constraints",
     ("constraint", "non-functional", "nonfunctional", "non functional", "performance",
      "limitation", "tech stack", "technology", "stack", "requirements: technical")),
    ("screens", ("screen", "page", "view", "menu", "ui", "interface", "hud")),
    ("mechanics",
     ("mechanic", "gameplay", "rule", "behavior", "behaviour", "feature", "requirement", "scope")),
]

_CONTROL_TOKENS = (
    "arrow key", "arrow keys", "wasd", "space bar", "spacebar", "space key",
    "mouse", "click", "tap", "swipe", "up/down", "left/right", "enter key",
    "w/s", "a/d", "touch", "drag",
)

_STACK_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("django", ("django",)),
    ("python", ("flask", "fastapi", "python", "pytest")),
    ("node", ("react", "vue", "svelte", "next.js", "nextjs", "node", "express",
              "vite", "typescript", "javascript", "canvas", "phaser")),
    ("go", ("golang", " go ", "go module")),
    ("rust", ("rust", "cargo")),
]


@dataclass
class PRDContract:
    """Structured, source-of-truth summary of a PRD."""
    title: str = ""
    project_kind: str = "unknown"   # game | web_app | api | cms | cli | service | unknown
    game_type: str = ""             # pong | snake | ... | "" when not a game
    is_multiplayer: bool = False
    is_3d: bool = False
    mechanics: list[str] = field(default_factory=list)
    controls: list[str] = field(default_factory=list)
    screens: list[str] = field(default_factory=list)
    acceptance_criteria: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    stack_hint: str = ""            # django | python | node | go | rust | ""

    @property
    def is_game(self) -> bool:
        return self.project_kind == "game"

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "project_kind": self.project_kind,
            "game_type": self.game_type,
            "is_multiplayer": self.is_multiplayer,
            "is_3d": self.is_3d,
            "mechanics": list(self.mechanics),
            "controls": list(self.controls),
            "screens": list(self.screens),
            "acceptance_criteria": list(self.acceptance_criteria),
            "constraints": list(self.constraints),
            "features": list(self.features),
            "stack_hint": self.stack_hint,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PRDContract":
        return cls(
            title=str(data.get("title", "")),
            project_kind=str(data.get("project_kind", "unknown")),
            game_type=str(data.get("game_type", "")),
            is_multiplayer=bool(data.get("is_multiplayer", False)),
            is_3d=bool(data.get("is_3d", False)),
            mechanics=list(data.get("mechanics", [])),
            controls=list(data.get("controls", [])),
            screens=list(data.get("screens", [])),
            acceptance_criteria=list(data.get("acceptance_criteria", [])),
            constraints=list(data.get("constraints", [])),
            features=list(data.get("features", [])),
            stack_hint=str(data.get("stack_hint", "")),
        )

    def render_brief(self) -> str:
        """Compact block a local model can read before generating code."""
        lines = [f"# PRD contract: {self.title or 'Untitled'}",
                 f"kind: {self.project_kind}"]
        if self.game_type:
            flavor = "multiplayer" if self.is_multiplayer else "single/local"
            dims = "3D" if self.is_3d else "2D"
            lines.append(f"game: {self.game_type} ({dims}, {flavor})")
        if self.stack_hint:
            lines.append(f"stack: {self.stack_hint}")
        for label, items in (
            ("mechanics", self.mechanics),
            ("controls", self.controls),
            ("screens", self.screens),
            ("acceptance criteria", self.acceptance_criteria),
            ("constraints", self.constraints),
        ):
            if items:
                lines.append(f"{label}:")
                lines.extend(f"  - {item}" for item in items[:12])
        return "\n".join(lines)


def extract_contract(parsed: ParsedPRD) -> PRDContract:
    raw = parsed.raw_text or ""
    lowered = raw.lower()

    game_type = _detect_game_type(lowered)
    is_multiplayer = _any(lowered, _MULTIPLAYER_HINTS)
    is_3d = _any(lowered, _3D_HINTS)
    project_kind = _detect_kind(lowered, game_type)
    stack_hint = _detect_stack(lowered)

    buckets: dict[str, list[str]] = {
        "controls": [], "acceptance_criteria": [], "constraints": [],
        "screens": [], "mechanics": [],
    }
    for heading, lines in parsed.sections.items():
        bucket = _bucket_for_heading(heading)
        if bucket is None:
            continue
        for line in lines:
            cleaned = line.strip("- ").strip()
            if cleaned and cleaned not in buckets[bucket]:
                buckets[bucket].append(cleaned)

    controls = buckets["controls"] or _scan_controls(lowered)
    features = _dedupe(buckets["mechanics"])

    return PRDContract(
        title=parsed.title,
        project_kind=project_kind,
        game_type=game_type,
        is_multiplayer=is_multiplayer,
        is_3d=is_3d,
        mechanics=_dedupe(buckets["mechanics"])[:20],
        controls=_dedupe(controls)[:12],
        screens=_dedupe(buckets["screens"])[:12],
        acceptance_criteria=_dedupe(buckets["acceptance_criteria"])[:20],
        constraints=_dedupe(buckets["constraints"])[:12],
        features=features[:20],
        stack_hint=stack_hint,
    )


# --- helpers ------------------------------------------------------------------

def _any(text: str, needles: tuple[str, ...]) -> bool:
    return any(needle in text for needle in needles)


def _detect_game_type(lowered: str) -> str:
    for name, needles in _GAME_TYPES:
        if _any(lowered, needles):
            return name
    return ""


def _detect_kind(lowered: str, game_type: str) -> str:
    if game_type or re.search(r"\bgame\b", lowered):
        return "game"
    if _any(lowered, ("cms", "content management", "headless cms", "blog engine", "wiki")):
        return "cms"
    if _any(lowered, ("rest api", "restful", "graphql", "api endpoint", "json api", "web service")):
        return "api"
    if _any(lowered, ("cli", "command-line", "command line", "terminal tool")):
        return "cli"
    if _any(lowered, ("microservice", "worker", "daemon", "background service", "cron")):
        return "service"
    if _any(lowered, ("website", "web app", "web application", "dashboard", "portal", "page")):
        return "web_app"
    return "unknown"


def _detect_stack(lowered: str) -> str:
    for name, needles in _STACK_HINTS:
        if _any(lowered, needles):
            return name
    return ""


def _bucket_for_heading(heading: str) -> str | None:
    lowered = heading.lower()
    for bucket, needles in _SECTION_BUCKETS:
        if any(needle in lowered for needle in needles):
            return bucket
    return None


def _scan_controls(lowered: str) -> list[str]:
    found = [token for token in _CONTROL_TOKENS if token in lowered]
    # Normalize a couple of common pairs so the list is readable.
    return _dedupe(found)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        key = item.lower().strip()
        if key and key not in seen:
            seen.add(key)
            result.append(item)
    return result

"""PRD archetype classification for the v2.2 generation registry."""
from __future__ import annotations

import json
from dataclasses import dataclass

from shamsu.llm.manager import LLMManager
from shamsu.types import Archetype, ParsedPRD


@dataclass(frozen=True)
class ArchetypeDecision:
    archetype: Archetype
    confidence: float
    reason: str = ""


ARCHETYPE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "archetype": {
            "type": "string",
            "enum": [item.value for item in Archetype],
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "reason": {"type": "string"},
    },
    "required": ["archetype", "confidence"],
}


def classify_archetype(parsed: ParsedPRD) -> ArchetypeDecision:
    """Classify without a model first.

    The first v2.2 slice keeps project planning offline/deterministic by
    default. Low-confidence prompts can use ``classify_archetype_with_llm`` in
    orchestration code without changing the stable ``build_project_spec`` API.
    """
    text = f"{parsed.title}\n{parsed.raw_text}\n{_sections_text(parsed)}".lower()
    scores = {
        Archetype.REALTIME_3D_GAME: _score(
            text,
            "game",
            "3d",
            "three.js",
            "unity",
            "multiplayer",
            "realtime",
            "physics",
            "player",
            "level",
            "score",
        ),
        Archetype.SAAS_FULLSTACK: _score(
            text,
            "saas",
            "subscription",
            "billing",
            "tenant",
            "multi-tenant",
            "stripe",
            "team",
            "organization",
        ),
        Archetype.REST_API: _score(
            text,
            "rest api",
            "api only",
            "backend service",
            "microservice",
            "endpoint",
            "json api",
            "webhook",
        ),
        Archetype.WEB_CRUD: _score(
            text,
            "crud",
            "dashboard",
            "admin",
            "form",
            "list",
            "detail",
            "entity",
            "model",
            "record",
        ),
    }
    section_names = {name.lower() for name in parsed.sections}
    # Substring matching, because exact names miss the obvious variants: this
    # required the literal "data models" and so scored nothing for a PRD whose
    # section is "Data Model" (singular) or "Database Schema". A declared data
    # model is the STRUCTURAL signal for an entity-backed product, and it has to
    # carry the weight now that the framework keyword bonus is gone.
    if any(
        token in name
        for name in section_names
        for token in ("entit", "data model", "schema")
    ):
        scores[Archetype.WEB_CRUD] += 3
    if any(
        token in name for name in section_names for token in ("endpoint", "api")
    ):
        scores[Archetype.REST_API] += 2
    # Deliberately no per-framework keyword bonus. A bare mention of "django"
    # used to add +2 to WEB_CRUD - with no equivalent for any other framework -
    # so naming Django anywhere, including inside a prohibition, biased the
    # archetype toward the one path that routes to the Django writer. Archetype is
    # about the SHAPE of the product; the stack is decided separately from the
    # contract's stack_hint and prohibitions.

    winner = max(scores, key=scores.get)
    winning_score = scores[winner]
    runner_up = max((score for key, score in scores.items() if key != winner), default=0)
    if winning_score <= 1:
        return ArchetypeDecision(Archetype.GENERIC_WEB, 0.35, "No strong archetype keywords found.")
    confidence = min(0.95, 0.55 + (winning_score - runner_up) * 0.08 + winning_score * 0.03)
    if confidence < 0.55:
        return ArchetypeDecision(Archetype.GENERIC_WEB, confidence, "Classification was ambiguous.")
    return ArchetypeDecision(winner, confidence, f"Matched {winner.value} PRD signals.")


async def classify_archetype_with_llm(
    parsed: ParsedPRD,
    manager: LLMManager | None = None,
) -> ArchetypeDecision:
    deterministic = classify_archetype(parsed)
    if deterministic.confidence >= 0.65:
        return deterministic
    manager = manager or LLMManager()
    prompt = (
        "Classify this PRD into exactly one SHAMSU archetype enum. "
        "Return JSON only.\n\n"
        f"Title: {parsed.title}\n\n{parsed.raw_text[:6000]}"
    )
    raw = await manager._generate(
        manager.router_model,
        "You classify PRDs into SHAMSU archetypes. Output JSON only.",
        prompt,
        temperature=0.0,
        json_schema=ARCHETYPE_JSON_SCHEMA,
    )
    try:
        data = json.loads(raw)
        return ArchetypeDecision(
            Archetype(data.get("archetype", Archetype.GENERIC_WEB.value)),
            float(data.get("confidence", 0.5)),
            str(data.get("reason", "")),
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return deterministic


def _score(text: str, *keywords: str) -> int:
    return sum(1 for keyword in keywords if keyword in text)


def _sections_text(parsed: ParsedPRD) -> str:
    return "\n".join(
        f"{heading}\n" + "\n".join(lines)
        for heading, lines in parsed.sections.items()
    )

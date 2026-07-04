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
    if "entities" in section_names or "data models" in section_names:
        scores[Archetype.WEB_CRUD] += 3
    if "endpoints" in section_names or "api" in section_names:
        scores[Archetype.REST_API] += 2
    if "django" in text:
        scores[Archetype.WEB_CRUD] += 2

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

"""Deterministic operation parsing for multi-action developer prompts."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

RouteClassifier = Callable[[str, Path], str]
CandidateFinder = Callable[[str, Path], list[str]]

_ACTION = (
    r"(?:read|open|inspect|show|compare|summari[sz]e|explain|search|look\s+up|"
    r"browse|create|write|build|implement|fix|repair|edit|update|modify|change|"
    r"add|remove|delete|rename|move|run|rerun|re-run|test|verify|check|report|"
    r"return|start|launch|serve|commit|stage|push|pull)"
)
_CLAUSE_SPLIT_RE = re.compile(
    rf"\s*(?:;|\b(?:and\s+)?then\b)\s*"
    rf"|,\s*(?={_ACTION}\b)"
    rf"|(?<=[.!?])\s+(?={_ACTION}\b)"
    rf"|\s+and\s+(?={_ACTION}\b)",
    re.IGNORECASE,
)
_REFERENCE_RE = re.compile(
    r"\b(it|that|those|them|the same|what changed|the result|the failure)\b",
    re.IGNORECASE,
)
_ORIGINAL_MARKER = "Original request: "
_PLAN_MARKER = "\n\nOrdered operation plan:"
_ANSWER_MARKER = "\n\n(Answering the earlier question"
_READ_ONLY_CONSTRAINT_RE = re.compile(
    r"\b(?:do\s+not|don't|dont|never)\s+"
    r"(?:change|edit|modify|write|create|delete|remove|touch)\s+"
    r"(?:any\s+)?(?:files?|code|anything|the\s+workspace)\b"
    r"|\bwithout\s+(?:changing|editing|modifying|writing|creating|deleting|touching)\s+"
    r"(?:any\s+)?(?:files?|code|anything|the\s+workspace)\b"
    r"|\b(?:read[ -]?only|no\s+file\s+changes?)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class OperationStep:
    id: int
    kind: str
    route: str
    instruction: str
    depends_on: tuple[int, ...] = ()
    references_previous: bool = False

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "kind": self.kind,
            "route": self.route,
            "instruction": self.instruction,
            "depends_on": list(self.depends_on),
            "references_previous": self.references_previous,
        }


@dataclass(frozen=True)
class OperationPlan:
    prompt: str
    candidates: tuple[str, ...] = ()
    steps: tuple[OperationStep, ...] = ()
    clauses: tuple[str, ...] = ()

    @property
    def is_composite(self) -> bool:
        if len(self.steps) < 2:
            return False
        kinds = {step.kind for step in self.steps}
        if len(kinds) == 1:
            return kinds == {"mutation"}
        return kinds != {"git_inspect"} and not kinds.issubset({"git_inspect", "git_mutate"})

    @property
    def primary_route(self) -> str:
        return self.steps[0].route if self.steps else "qa"

    def to_dict(self) -> dict[str, object]:
        return {
            "prompt": self.prompt,
            "candidates": list(self.candidates),
            "clauses": list(self.clauses),
            "steps": [step.to_dict() for step in self.steps],
            "is_composite": self.is_composite,
        }

    def agent_prompt(self) -> str:
        lines = [
            "Execute this multi-step request completely and in order.",
            "Do not stop after an earlier step while a later step remains.",
            "Use registered tools for every read, mutation, command, web lookup, and Git action.",
            "If a material choice is missing, call ask_user and preserve the remaining steps.",
            "At the end, report the outcome of each numbered step and any step not completed.",
            "",
            f"Original request: {self.prompt}",
            "",
            "Ordered operation plan:",
        ]
        for step in self.steps:
            dependency = f" after step {step.depends_on[-1]}" if step.depends_on else ""
            reference = " Resolve its references from prior step results." if step.references_previous else ""
            lines.append(
                f"{step.id}. [{step.kind} via {step.route}]{dependency}: "
                f"{step.instruction}.{reference}"
            )
        return "\n".join(lines)


def parse_operation_plan(
    prompt: str,
    workspace: Path,
    classify: RouteClassifier,
    find_candidates: CandidateFinder,
) -> OperationPlan:
    clauses = _split_clauses(prompt)
    candidates = _dedupe(find_candidates(prompt, workspace))
    steps: list[OperationStep] = []
    for clause in clauses:
        kind = _operation_kind(clause)
        if not kind:
            continue
        classified = classify(clause, workspace)
        kind = _normalize_kind_for_route(kind, classified)
        route = _route_for_kind(kind, classified)
        references_previous = bool(steps and _REFERENCE_RE.search(clause))
        dependencies = (steps[-1].id,) if steps else ()
        steps.append(
            OperationStep(
                id=len(steps) + 1,
                kind=kind,
                route=route,
                instruction=clause.strip().rstrip(".?!"),
                depends_on=dependencies,
                references_previous=references_previous,
            )
        )
        candidates.extend(find_candidates(clause, workspace))

    if not steps:
        route = classify(prompt, workspace)
        steps = [OperationStep(id=1, kind="answer", route=route, instruction=prompt.strip())]
    return OperationPlan(
        prompt=prompt.strip(),
        candidates=tuple(_dedupe(candidates)),
        steps=tuple(steps),
        clauses=tuple(clauses),
    )


def recover_original_prompt(prompt: str) -> str:
    """Unwrap a paused composite execution contract before rerouting it."""
    if _ORIGINAL_MARKER not in prompt or _PLAN_MARKER not in prompt:
        return prompt
    original_tail = prompt.split(_ORIGINAL_MARKER, 1)[1]
    original = original_tail.split(_PLAN_MARKER, 1)[0].strip()
    answer = ""
    if _ANSWER_MARKER in prompt:
        answer = _ANSWER_MARKER + prompt.split(_ANSWER_MARKER, 1)[1]
    return f"{original}{answer}".strip()


# The verbs that make a clause an independent instruction. A clause without one
# (and not a question) is context/location, not a step of its own.
_STANDALONE_VERB_RE = re.compile(
    r"\b(?:read|open|inspect|show|explain|summari[sz]e|compare|search|browse|"
    r"look\s+up|create|write|build|implement|fix|repair|edit|update|modify|"
    r"change|add|remove|delete|rename|move|run|rerun|re-run|test|verify|compile|"
    r"check|start|launch|serve|commit|stage|push|pull|stash|checkout|report|return)\b",
    re.IGNORECASE,
)
_QUESTION_LEAD_RE = re.compile(
    r"^(?:what|where|why|how|which|who|does|do|is|are|can|could|would|should)\b",
    re.IGNORECASE,
)


def _is_standalone_instruction(clause: str) -> bool:
    """True when a clause can stand as its own step.

    A pure context or location fragment ("In calc.py", "There is a bug in
    calc.py") is neither an action nor a question, so it is not a step - it
    belongs to the instruction it introduces.
    """
    text = clause.strip()
    if not text:
        return False
    if text.endswith("?") or _QUESTION_LEAD_RE.match(text):
        return True
    return bool(_STANDALONE_VERB_RE.search(text))


def _merge_context_fragments(parts: list[str]) -> list[str]:
    """Fold a non-actionable fragment into the instruction it modifies.

    The clause splitter breaks before an action verb, which also severs pure
    context from the instruction it belongs to: "In calc.py, change X" ->
    ["In calc.py", "change X"]. That invents a bogus "answer" step AND strips
    the filename off the real edit, so it can no longer route to file.write and
    lands in the tool-less QA brain - which describes the change instead of
    applying it. Observed live 2026-07-21: a plain "In calc.py, change the
    subtract body ..." fix never touched the file. Merge every non-standalone
    fragment into the following instruction (or, if it trails, the previous one).
    """
    if len(parts) < 2:
        return parts
    out: list[str] = []
    carry: list[str] = []
    for part in parts:
        if _is_standalone_instruction(part):
            out.append(", ".join(carry + [part]) if carry else part)
            carry = []
        else:
            carry.append(part)
    if carry:
        if out:
            out[-1] = ", ".join([out[-1]] + carry)
        else:
            out = [", ".join(carry)]
    return out


def _split_clauses(prompt: str) -> list[str]:
    parts = [part.strip(" ,") for part in _CLAUSE_SPLIT_RE.split(prompt.strip())]
    parts = [part for part in parts if part]
    return _merge_context_fragments(parts)


def _operation_kind(clause: str) -> str:
    text = " ".join(clause.lower().split())
    explicitly_read_only = bool(_READ_ONLY_CONSTRAINT_RE.search(text))
    action_text = _READ_ONLY_CONSTRAINT_RE.sub("", text)
    if not text:
        return ""
    if any(
        phrase in text
        for phrase in (
            "what changed",
            "show changes",
            "show me the changes",
            "show the diff",
            "show diff",
            "git diff",
        )
    ):
        return "git_inspect"
    if re.search(r"\b(commit|stage|push|pull|stash|checkout)\b", text):
        return "git_mutate"
    if re.search(r"\b(summari[sz]e|summary)\b", text):
        return "summarize"
    if re.search(r"\bcompare\b", text):
        return "compare"
    if re.search(r"\b(search|browse|look up|check)\b", text) and re.search(
        r"\b(web|online|latest|current|docs?|documentation|release)\b", text
    ):
        return "web"
    if not explicitly_read_only and re.search(
        r"\b(create|write|build|implement|fix|repair|edit|update|modify|change|add|remove|delete|rename|move)\b",
        action_text,
    ):
        return "mutation"
    if re.search(r"\b(run|rerun|re-run|test|verify|compile|check)\b", text):
        return "verify"
    if re.search(r"\b(start|launch|serve)\b", text) and re.search(
        r"\b(app|project|game|server|site|url)\b", text
    ):
        return "launch"
    if re.search(r"\b(read|open|inspect|show|explain)\b", text):
        return "read"
    question_shaped = "?" in text or bool(
        re.match(r"^(?:what|where|why|how|which|who|does|do|is|are|can|could|would)\b", text)
    )
    if re.search(r"\b(report|return)\b", text) and not question_shaped:
        return "summarize"
    return "answer"


def _route_for_kind(kind: str, classified: str) -> str:
    if kind in {"verify", "launch"} and classified in {
        "run_game",
        "dev_server",
        "dev_server.recovery",
    }:
        return classified
    if kind in {"verify", "compare", "launch"}:
        return "agent-chat"
    if kind in {"git_inspect", "git_mutate"}:
        return "git"
    return classified


def _normalize_kind_for_route(kind: str, classified: str) -> str:
    if classified == "prd_summary":
        return "summarize"
    if classified == "web" and kind in {"answer", "verify", "read"}:
        return "web"
    if classified == "file.read" and kind == "answer":
        return "read"
    return kind


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result

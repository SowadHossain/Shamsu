"""Deterministic operation parsing for multi-action developer prompts."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from shamsu.safety import read_only

RouteClassifier = Callable[[str, Path], str]
CandidateFinder = Callable[[str, Path], list[str]]

_ACTION = (
    r"(?:read|open|inspect|show|compare|summari[sz]e|explain|search|look\s+up|"
    r"browse|create|write|build|implement|scaffold|bootstrap|initiate|initiali[sz]e|"
    r"install|fix|repair|edit|update|modify|change|"
    r"add|remove|removed|delete|deleted|rename|move|run|rerun|re-run|test|verify|check|report|"
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
# The per-step composite contract (`_composite_step_prompt`) wraps the original
# request with this line instead of the whole-plan marker above. Both must
# unwrap: a pending ask_user stored during a composite STEP resumes through
# here, and an unrecognized wrapper re-routes the internal step contract as a
# fresh prompt - whose "Do not modify any files" line then strips the user's
# real mutation intent (observed live 2026-08-01).
_STEP_PLAN_MARKER = "\n\nYou are executing that request one step at a time."
_ANSWER_MARKER = "\n\n(Answering the earlier question"
_FILE_TOKEN_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+[/\\])*[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,12}",
    re.IGNORECASE,
)
_SPECIAL_FILE_TOKEN_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+[/\\])*(?:"
    r"Dockerfile(?:\.[A-Za-z0-9_.-]+)?|"
    r"Makefile(?:\.[A-Za-z0-9_.-]+)?|"
    r"Procfile|"
    r"\.env(?:\.[A-Za-z0-9_.-]+)?|"
    r"\.dockerignore|\.gitignore|\.gitattributes|\.npmrc|\.nvmrc|"
    r"\.prettierrc|\.eslintrc|\.babelrc|\.editorconfig"
    r")",
    re.IGNORECASE,
)
# Real file suffixes. The pattern above accepts ANY short trailing segment, so
# a dotted Python module path counted as a filename: "Import AbstractUser from
# django.contrib.auth.models" contributed `django.contrib.auth.models` AND
# `django.db` as targets, making a ONE-file request look like three and
# shredding it into composite steps (observed live 2026-08-02).
_FILE_SUFFIXES = frozenset(
    """py pyi ipynb js jsx ts tsx mjs cjs json jsonc yaml yml toml ini cfg conf env
    md markdown rst txt csv tsv sql db sqlite sqlite3 html htm css scss sass less
    prisma graphql gql properties proto gradle lock
    png jpg jpeg gif svg ico webp pdf docx doc xlsx lock sh bash ps1 bat cmd
    dockerfile gitignore go rs java kt rb php c h cpp hpp cs xml""".split()
)
_SPECIAL_FILE_PREFIXES = ("dockerfile.", "makefile.", ".env.")
_SPECIAL_FILE_NAMES = {
    "dockerfile",
    "makefile",
    "procfile",
    ".dockerignore",
    ".gitignore",
    ".gitattributes",
    ".npmrc",
    ".nvmrc",
    ".prettierrc",
    ".eslintrc",
    ".babelrc",
    ".editorconfig",
}


def looks_like_real_file(token: str) -> bool:
    """Whether a dotted token is a path rather than a module/attribute name.

    Public because the run contract needs the same judgement: two extractors
    disagreeing about what counts as a file meant a prompt could promise
    `config.settings` and then fail for not writing it.
    """
    normalized = token.replace("\\", "/")
    name = normalized.rsplit("/", 1)[-1].lower()
    if name in _SPECIAL_FILE_NAMES or any(name.startswith(prefix) for prefix in _SPECIAL_FILE_PREFIXES):
        return True
    if "/" in normalized:
        return True
    return normalized.rsplit(".", 1)[-1].lower() in _FILE_SUFFIXES


_IMPORT_CONTEXT_RE = re.compile(r"\b(?:from|import)\s+$", re.IGNORECASE)
# "Write the complete file now with write_file" - an instruction to perform the
# write just specified, not a new target. Must not match a second real edit.
_TRAILING_WRITE_IMPERATIVE_RE = re.compile(
    r"^\s*(?:write|create|save|output|generate)\b[^.]{0,80}?\b(?:file|it|now|write_file)\b",
    re.IGNORECASE,
)


def file_targets(text: str) -> set[str]:
    """Distinct file paths named in *text*, excluding dotted module paths.

    Two filters, because a suffix allowlist alone is not enough: `django.db`
    ends in a genuine file suffix. What actually distinguishes a module is the
    import context it appears in.
    """
    body = text or ""
    targets: set[str] = set()
    for pattern in (_FILE_TOKEN_RE, _SPECIAL_FILE_TOKEN_RE):
        for match in pattern.finditer(body):
            token = match.group(0)
            if not looks_like_real_file(token):
                continue
            if _IMPORT_CONTEXT_RE.search(body[: match.start()]):
                continue
            targets.add(token.replace("\\", "/").casefold())
    return targets


_CREATE_NAMED_FILE_RE = re.compile(
    rf"^\s*(?:create|write|generate|make)\b(?:(?![.!?\n]).)*{_FILE_TOKEN_RE.pattern}",
    re.IGNORECASE,
)
_QUOTED_FILE_RE = re.compile(
    r"[\x60\"'](?P<path>(?:[A-Za-z0-9_.-]+[/\\])*"
    r"[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,12})[\x60\"']"
)
_EDIT_LEAD_RE = re.compile(
    r"^\s*(?:fix|repair|edit|update|modify|change|add|remove)\b",
    re.IGNORECASE,
)
_QUOTED_SPAN_RE = re.compile(r"[`\"'][^`\"']*[`\"']")
_CONTINUATION_LEAD_RE = re.compile(
    r"^\s*(?:continue|continuing|resume|resuming|finish|finishing|complete|completing|"
    r"keep\s+going|pick\s+up)\b",
    re.IGNORECASE,
)
_NEGATED_ACTION_RE = re.compile(
    r"\b(?:do\s+not|don'?t|never|avoid)\s+"
    r"(?:create|write|build|implement|fix|repair|edit|update|modify|change|add|remove|delete|rename|move)\b",
    re.IGNORECASE,
)
_DELETE_ONLY_RE = re.compile(r"\b(?:delete|remove)\s+only\b", re.IGNORECASE)


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
        route = _route_for_kind(kind, classified, clause)
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
    elif _is_single_named_file_creation(prompt, steps):
        # "Create NOTES.md ... Add a Sources heading ..." is one artifact
        # request. Splitting it makes the first turn lose the content contract
        # and the second turn lose the target. Keep true multi-file and edit
        # requests as separately evidenced steps.
        steps = [
            OperationStep(
                id=1,
                kind="mutation",
                route="file.write",
                instruction=prompt.strip().rstrip(".?!"),
            )
        ]
    elif _is_single_file_deletion_workflow(prompt, steps):
        steps = [
            OperationStep(
                id=1,
                kind="mutation",
                route="file.write",
                instruction=prompt.strip().rstrip(".?!"),
            )
        ]
    elif _is_single_quoted_file_edit_workflow(prompt, steps):
        # A cohesive one-file repair often includes dependent read, mutation,
        # and verification sentences. Splitting those sentences strips the
        # target from later fragments and sends them through QA, where a small
        # model describes the edit instead of calling a mutation tool.
        steps = [
            OperationStep(
                id=1,
                kind="mutation",
                route="file.write",
                instruction=prompt.strip().rstrip(".?!"),
            )
        ]
    elif _is_targeted_continuation(prompt, steps):
        # "Continue the milestone: fix BASE_DIR in settings.py, then verify"
        # is one resumed task, not an ordered plan. Splitting it strips the
        # implementation path from later fragments and executes them under a
        # composite contract that never resumes cleanly (observed live
        # 2026-08-01: a targeted continuation was shredded into steps). A
        # continuation carries mutation intent, so the tool-less QA route can
        # never satisfy it - fall back to the tool-equipped agent instead.
        classified = classify(prompt, workspace)
        steps = [
            OperationStep(
                id=1,
                kind="mutation",
                route="agent-chat" if classified == "qa" else classified,
                instruction=prompt.strip().rstrip(".?!"),
            )
        ]
    return OperationPlan(
        prompt=prompt.strip(),
        candidates=tuple(_dedupe(candidates)),
        steps=tuple(steps),
        clauses=tuple(clauses),
    )


def _is_single_named_file_creation(prompt: str, steps: list[OperationStep]) -> bool:
    if len(steps) < 2 or not _CREATE_NAMED_FILE_RE.search(prompt):
        return False
    if any(step.kind != "mutation" for step in steps):
        return False
    return len(file_targets(prompt)) == 1


def _is_single_quoted_file_edit_workflow(prompt: str, steps: list[OperationStep]) -> bool:
    if len(steps) < 2 or not _EDIT_LEAD_RE.search(prompt):
        return False
    targets = {
        match.group("path").replace("\\", "/").casefold()
        for match in _QUOTED_FILE_RE.finditer(prompt)
    }
    if len(targets) != 1:
        return False
    return {step.kind for step in steps}.issubset({"mutation", "read", "verify", "summarize"})


def _is_targeted_continuation(prompt: str, steps: list[OperationStep]) -> bool:
    """A continuation of interrupted work stays one grounded agent turn.

    Requires an explicit continuation lead, at least one mutation step, no
    git/web/launch steps, and at most two named files (the target plus perhaps
    a verifier entry point) so a genuinely multi-artifact request still gets
    per-step evidence."""
    if len(steps) < 2 or not _CONTINUATION_LEAD_RE.match(prompt):
        return False
    kinds = {step.kind for step in steps}
    if "mutation" not in kinds:
        return False
    if not kinds.issubset({"mutation", "read", "verify", "summarize", "answer"}):
        return False
    return len(file_targets(prompt)) <= 2


def _is_single_file_deletion_workflow(prompt: str, steps: list[OperationStep]) -> bool:
    """Keep a scoped delete/preserve/verify request in one grounded agent turn."""
    if len(steps) < 2 or len(_DELETE_ONLY_RE.findall(prompt)) != 1:
        return False
    if not re.search(r"\b(?:keep|preserve|retain|canonical)\b", prompt, re.IGNORECASE):
        return False
    return {step.kind for step in steps}.issubset({"mutation", "read", "verify", "summarize"})


def recover_original_prompt(prompt: str) -> str:
    """Unwrap a paused composite execution contract before rerouting it."""
    if _ORIGINAL_MARKER not in prompt:
        return prompt
    plan_marker = next(
        (marker for marker in (_PLAN_MARKER, _STEP_PLAN_MARKER) if marker in prompt),
        None,
    )
    if plan_marker is None:
        return prompt
    original_tail = prompt.split(_ORIGINAL_MARKER, 1)[1]
    original = original_tail.split(plan_marker, 1)[0].strip()
    answer = ""
    if _ANSWER_MARKER in prompt:
        answer = _ANSWER_MARKER + prompt.split(_ANSWER_MARKER, 1)[1]
    return f"{original}{answer}".strip()


# The verbs that make a clause an independent instruction. A clause without one
# (and not a question) is context/location, not a step of its own.
_STANDALONE_VERB_RE = re.compile(
    r"\b(?:read|open|inspect|show|explain|summari[sz]e|compare|search|browse|"
    r"look\s+up|create|write|build|implement|scaffold|bootstrap|initiate|initiali[sz]e|"
    r"install|fix|repair|edit|update|modify|"
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
    return bool(_STANDALONE_VERB_RE.search(_QUOTED_SPAN_RE.sub(" ", text)))


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


def _merge_specification_clauses(parts: list[str]) -> list[str]:
    """Rejoin an instruction with the clause that specifies it.

    "Build the Django foundation in canvas_lms_lite. Create: manage.py,
    config/settings.py, core/models.py." is ONE task: the second clause is the
    file list for the first, not an independent step. Splitting them left step
    1 with no concrete target at all (it failed) and stranded the entire file
    list in step 2, which then never ran - observed live 2026-08-02 on a
    deliberately detailed, well-specified build request.

    Only a mutation clause carrying NO file target absorbs the following
    clause, and only when that next clause names files; a genuine multi-file
    request whose first clause already names its own target is untouched.
    """
    if len(parts) < 2:
        return parts
    out: list[str] = []
    index = 0
    while index < len(parts):
        current = parts[index]
        following = parts[index + 1] if index + 1 < len(parts) else ""
        if (
            following
            and _operation_kind(current) == "mutation"
            and _operation_kind(following) == "mutation"
            and (
                # instruction -> its file list ("Build the foundation. Create: a.py, b.py")
                (not file_targets(current) and file_targets(following))
                # spec -> a trailing "now write it" imperative. Deliberately
                # narrow: "edit greet in greeting.py, and update the __main__
                # block" is TWO real mutations and must stay split.
                or (
                    file_targets(current)
                    and not file_targets(following)
                    and _TRAILING_WRITE_IMPERATIVE_RE.match(following)
                )
            )
        ):
            out.append(f"{current}. {following}")
            index += 2
            continue
        out.append(current)
        index += 1
    return out


def _split_clauses(prompt: str) -> list[str]:
    parts = [part.strip(" ,") for part in _CLAUSE_SPLIT_RE.split(prompt.strip())]
    parts = [part for part in parts if part]
    return _merge_specification_clauses(_merge_context_fragments(parts))


_MARKUP_RE = re.compile(r"<[^>]+>|\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)


def _without_file_tokens(text: str) -> str:
    """*text* with file paths and dictated markup removed.

    Intent classification must read the instruction, not its payload: a prompt
    that dictates HTML should be judged on "rewrite this template", not on the
    words inside the tags it is quoting.
    """
    stripped = _MARKUP_RE.sub(" ", text)
    return _FILE_TOKEN_RE.sub(" ", stripped)


def _operation_kind(clause: str) -> str:
    text = " ".join(clause.lower().split())
    explicitly_read_only = read_only.applies(text)
    action_text = _NEGATED_ACTION_RE.sub(" ", read_only.strip(text))
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
    # Web intent must come from the instruction, not from a filename or from
    # markup being dictated. "Rewrite templates/browse.html ... Current bid:"
    # supplied both halves of this test - `browse` from the path and `current`
    # from template text - and a prompt whose entire job was to write a file
    # was dispatched to web search, changing nothing.
    intent_text = _without_file_tokens(text)
    if re.search(r"\b(search|browse|look up|check)\b", intent_text) and re.search(
        r"\b(web|online|latest|current|docs?|documentation|release)\b", intent_text
    ):
        return "web"
    if not explicitly_read_only and re.search(
        # `rewrite`/`replace`/`overwrite`/`append` were missing, so "Rewrite
        # core/views.py so it contains ..." was not recognised as a mutation at
        # all and fell through to the answer route - the model described the
        # file instead of writing it.
        r"\b(create|write|rewrite|overwrite|replace|append|build|implement|scaffold"
        r"|bootstrap|initiate|initiali[sz]e|install"
        r"|fix|repair|edit|update|modify|change|add|remove|removed|delete|deleted|rename|move)\b",
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


def _route_for_kind(kind: str, classified: str, clause: str = "") -> str:
    # A turn whose instruction is to write a file is never a web search,
    # whatever the model's classifier decided. Without this the fall-through at
    # the end returned the classifier's answer verbatim, and a prompt dictating
    # HTML ("rewrite templates/browse.html ...") was dispatched to web search:
    # it changed nothing, reported failure, and left the file broken.
    if kind == "mutation" and classified not in {
        "file.write",
        "prd.build",
        "django",
        "mcp",
        "git",
        "package.install",
        "direct_code",
    }:
        return "file.write" if file_targets(clause) else "agent-chat"
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

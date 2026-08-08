"""The context compiler.

The model receives a compiled frame, never a transcript and never a repository.
Each frame is built fresh for one decision from authoritative state plus
retrieved evidence, under an explicit token budget.

Two properties the rest of the runtime depends on:

**Determinism.** The same state produces the same frame. That is what makes a
bad decision reproducible — without it, "why did it do that?" is unanswerable.

**Hot context is never silently dropped.** Sections are filled in priority
order and trimmed from the lowest priority upward. If the hot sections do not
fit, that is an error, not a quiet truncation: a decision made without the
current step is not a worse decision, it is a different one.

Stale artifacts may enter a frame, but only labelled. A stale structural claim
delivered without a warning is the exact failure the whole artifact freshness
system exists to prevent (plan §17.1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from shamsu.interfaces.artifacts import Artifact
from shamsu.interfaces.context import ContextFrame, ContextSection, TokenBudget
from shamsu.interfaces.enums import ArtifactStatus, Phase
from shamsu.interfaces.models import ModelClient
from shamsu.interfaces.tools import ToolContract


class ContextTooLarge(Exception):
    """The hot sections do not fit the budget.

    Raised rather than trimmed. Hot context is the task, the current step, the
    acceptance criteria and the latest observation; a frame missing any of them
    describes a different problem than the one being solved.
    """


@dataclass(frozen=True)
class Section:
    """A candidate section, before budgeting.

    `priority` orders trimming: lower numbers survive longer. `hot` marks
    sections that must never be dropped.
    """

    name: str
    content: str
    priority: int
    hot: bool = False
    stale_warning: str | None = None


@dataclass
class FrameInputs:
    """Everything a frame can be built from.

    A plain container rather than a pile of parameters, so adding a source
    later does not change every call site.
    """

    phase: Phase
    task: str
    output_contract: str

    acceptance_criteria: Sequence[str] = field(default_factory=tuple)
    current_step: str = ""
    plan_summary: str = ""
    project_facts: str = ""
    artifacts: Sequence[Artifact] = field(default_factory=tuple)
    source_excerpts: Sequence[tuple[str, str]] = field(default_factory=tuple)
    latest_observation: str = ""
    previous_step_summary: str = ""
    system_rules: str = ""

    #: Tool calls already made in this step, oldest first, as
    #: `("tool", "one-line outcome")`.
    #:
    #: Without this a decision loop repeats itself. The frame shows only the
    #: *latest* observation, so after one call the next frame is nearly
    #: identical to the last — and at temperature 0 a nearly identical prompt
    #: produces an identical answer. A live run called `git.inspect` four times
    #: in a row and spent its whole action budget doing it. The model is not
    #: being stubborn; it is being asked the same question repeatedly and
    #: answering consistently.
    attempted: Sequence[tuple[str, str]] = field(default_factory=tuple)


#: Priority order. Lower survives longer under budget pressure.
_SYSTEM = 0
_TASK = 1
_STEP = 2
_OBSERVATION = 3
_TOOLS = 4
_FACTS = 5
_ARTIFACTS = 6
_SOURCE = 7
_HISTORY = 8


class ContextCompiler:
    """Builds compact task packets from authoritative state."""

    def __init__(self, model: ModelClient, budget: TokenBudget | None = None) -> None:
        self._model = model
        self._budget = budget or TokenBudget()

    @property
    def budget(self) -> TokenBudget:
        return self._budget

    def compile(
        self,
        inputs: FrameInputs,
        allowed_tools: Sequence[ToolContract],
        budget: TokenBudget | None = None,
    ) -> ContextFrame:
        """Assemble the frame for the next decision.

        Raises:
            ContextTooLarge: the hot sections alone exceed the input budget.
        """
        effective = budget or self._budget
        candidates = self._candidates(inputs, allowed_tools)

        kept: list[ContextSection] = []
        dropped: list[str] = []
        used = 0
        limit = effective.input_total

        # Hot first, so a cold section can never consume budget a hot one needs.
        for section in sorted(candidates, key=lambda s: (not s.hot, s.priority)):
            tokens = self._model.count_tokens(section.content)

            if used + tokens <= limit:
                kept.append(
                    ContextSection(
                        name=section.name,
                        content=section.content,
                        tokens=tokens,
                        stale_warning=section.stale_warning,
                    )
                )
                used += tokens
                continue

            if section.hot:
                raise ContextTooLarge(
                    f"hot section {section.name!r} needs {tokens} tokens but only "
                    f"{limit - used} of {limit} remain; the frame cannot be built "
                    "without it"
                )
            dropped.append(section.name)

        # Restore authored order for readability; budgeting order was internal.
        order = {section.name: section.priority for section in candidates}
        kept.sort(key=lambda s: order.get(s.name, 99))

        return ContextFrame(
            phase=inputs.phase,
            sections=tuple(kept),
            allowed_tools=tuple(allowed_tools),
            output_contract=inputs.output_contract,
            budget=effective,
            tokens_used=used,
            dropped_sections=tuple(dropped),
        )

    # -- section construction ---------------------------------------------

    def _candidates(
        self, inputs: FrameInputs, allowed_tools: Sequence[ToolContract]
    ) -> list[Section]:
        sections: list[Section] = []

        if inputs.system_rules:
            sections.append(Section("system and phase rules", inputs.system_rules, _SYSTEM))

        # Hot: the task itself, and what "done" means for it.
        task_block = inputs.task
        if inputs.acceptance_criteria:
            criteria = "\n".join(f"- {item}" for item in inputs.acceptance_criteria)
            task_block = f"{inputs.task}\n\nAcceptance criteria:\n{criteria}"
        sections.append(Section("current task", task_block, _TASK, hot=True))

        if inputs.current_step or inputs.plan_summary:
            parts = [part for part in (inputs.current_step, inputs.plan_summary) if part]
            sections.append(
                Section("current step", "\n\n".join(parts), _STEP, hot=bool(inputs.current_step))
            )

        if inputs.latest_observation:
            sections.append(
                Section("latest observation", inputs.latest_observation, _OBSERVATION, hot=True)
            )

        if allowed_tools:
            sections.append(
                Section("allowed tools", self._render_tools(allowed_tools), _TOOLS, hot=True)
            )

        if inputs.project_facts:
            sections.append(Section("project facts", inputs.project_facts, _FACTS))

        artifact_section = self._render_artifacts(inputs.artifacts)
        if artifact_section is not None:
            sections.append(artifact_section)

        if inputs.source_excerpts:
            sections.append(
                Section(
                    "relevant source code",
                    self._render_source(inputs.source_excerpts),
                    _SOURCE,
                )
            )

        if inputs.attempted:
            # Hot: this is the section that stops the loop, so it must survive
            # budget pressure. Dropping it to fit more source back would
            # restore exactly the behaviour it exists to prevent.
            sections.append(
                Section(
                    "already tried in this step",
                    self._render_attempts(inputs.attempted),
                    _OBSERVATION,
                    hot=True,
                )
            )

        if inputs.previous_step_summary:
            sections.append(
                Section("previous step summary", inputs.previous_step_summary, _HISTORY)
            )

        return sections

    @staticmethod
    def _render_attempts(attempted: Sequence[tuple[str, str]]) -> str:
        """What has been called, and how it went.

        Numbered, because "you have already done these three things" is the
        fact that has to land. The outcome is one line: enough to know whether
        repeating it could produce anything new, without re-showing output the
        latest-observation section already carries.
        """
        lines = [
            f"{index}. {tool} — {outcome}" for index, (tool, outcome) in enumerate(attempted, 1)
        ]
        lines.append("Do not repeat a call above unless its arguments differ meaningfully.")
        return "\n".join(lines)

    @staticmethod
    def _render_tools(contracts: Sequence[ToolContract]) -> str:
        return "\n".join(f"- {c.name}: {c.purpose}" for c in contracts)

    @staticmethod
    def _render_source(excerpts: Sequence[tuple[str, str]]) -> str:
        return "\n\n".join(f"--- {path} ---\n{body}" for path, body in excerpts)

    @staticmethod
    def _render_artifacts(artifacts: Sequence[Artifact]) -> Section | None:
        """Render artifacts, refusing unusable ones and labelling stale ones.

        The refusal is not defensive duplication of the registry's `usable()`
        check — it is the last gate before the model sees the content, and the
        one place where "unusable" and "in the prompt" are mutually exclusive
        by construction.
        """
        usable = [artifact for artifact in artifacts if artifact.meta.usable]
        if not usable:
            return None

        blocks: list[str] = []
        stale: list[str] = []
        for artifact in usable:
            meta = artifact.meta
            label = f"{meta.kind.value}: {meta.key}"
            if meta.status is ArtifactStatus.STALE:
                stale.append(meta.key)
                label += "  [STALE]"
            blocks.append(f"--- {label} ---\n{artifact.content}")

        warning = None
        if stale:
            warning = (
                f"{len(stale)} artifact(s) are out of date because their source files "
                f"changed since they were built: {', '.join(sorted(stale))}. "
                "Verify with a fresh tool call before relying on them."
            )

        return Section("relevant artifacts", "\n\n".join(blocks), _ARTIFACTS, stale_warning=warning)


__all__ = ["ContextCompiler", "ContextTooLarge", "FrameInputs", "Section"]

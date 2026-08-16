"""The tool gateway: the only path from a model decision to a side effect.

One registry, not two. v1 had `ToolRegistry` (3 tools) and `AgentToolRegistry`
(42 tools in a 3,731-line file) with different validation and gating, so "is
this allowed right now?" had two answers depending on which surface you asked.

Enforcement order, and why it is this order:

1. **Resolve the tool.** An unknown name is refused before anything else runs.
2. **Check the phase.** Cheapest real policy check, and it is the one the model
   is least able to reason about.
3. **Check approval.** Before argument validation, so a high-risk call is not
   quietly rejected on a typo when the real answer is "a human must decide".
4. **Validate arguments.** Against the same schema the model was shown.
5. **Check the write scope.** A repair may only touch failure-related files
   (plan §20.5), and a refused write must not spend the mutation budget.
6. **Check the mutation budget.** Last gate before execution.
7. **Execute** under timeout, racing cancellation.
8. **Cap output** before it can reach any context.

Every refusal happens *before* the side effect. `ToolPolicyViolation` from this
class always means nothing was executed.

What this class does not do: record tool events or register evidence. Evidence
is non-forgeable because `EvidenceRecord.source_event_id` is a foreign key to a
persisted `tool_events` row, so registration needs the state store. The gateway
*reports* the evidence a result is entitled to; the runtime records the event
and registers it. Splitting it that way keeps policy free of persistence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from shamsu.interfaces.cancellation import CancellationToken, Cancelled
from shamsu.interfaces.enums import Phase
from shamsu.interfaces.tools import (
    ToolContract,
    ToolPolicyViolation,
    ToolRequest,
    ToolResult,
    WriteScope,
)
from shamsu.security.paths import looks_like_a_test, workspace_key
from shamsu.tools.base import Tool

#: Asked whether a risky call may proceed. Returning False is a refusal, not an
#: error -- the runtime turns it into a pending approval.
ApprovalCallback = Callable[[ToolContract, ToolRequest], bool]


def _normalise(path: str) -> str:
    """One spelling per file, so `./src/a.py` and `src\\a.py` are the same key."""
    return workspace_key(path)


def deny_all(contract: ToolContract, request: ToolRequest) -> bool:
    """Default approval policy: refuse.

    Fail closed. An unconfigured gateway must not be a gateway that approves
    everything -- that is the one default that turns a policy layer into
    decoration.
    """
    return False


class ToolGateway:
    """Registers tools and enforces policy on every call."""

    def __init__(
        self,
        tools: Sequence[Tool[Any]] | None = None,
        *,
        approval: ApprovalCallback | None = None,
        mutating_calls_per_decision: int = 1,
        require_read_before_edit: bool = True,
        workspace: Path | None = None,
        allow_test_edits: bool = False,
    ) -> None:
        self._tools: dict[str, Tool[Any]] = {}
        self._approval = approval or deny_all
        self._mutation_budget = mutating_calls_per_decision
        self._mutations_used = 0
        self._scope: WriteScope | None = None
        #: Files read (or authored) through this gateway. Held here rather than
        #: in a tool because it is a fact about the *run*, and the tool that
        #: reads is not the tool that edits.
        self._read: set[str] = set()

        #: Where the workspace is, needed only to answer "does this test file
        #: already exist?". `None` means the check cannot run and no edit is
        #: refused on its account -- a policy that cannot be evaluated must not
        #: be enforced on a guess.
        self._workspace = Path(workspace).resolve() if workspace is not None else None

        #: Whether existing test files may be modified. The caller's decision,
        #: never the model's, exactly as `RepairScope` has always had it.
        self._allow_test_edits = allow_test_edits

        #: On by default. Turned off by tests that exercise a *tool* rather
        #: than the policy — the same opt-out `WriteScope` and `approval` have,
        #: for the same reason: a mechanism has to be testable without every
        #: policy that guards it in production.
        self._require_read_before_edit = require_read_before_edit

        for tool in tools or ():
            self.register(tool)

    # -- registration ------------------------------------------------------

    def register(self, tool: Tool[Any]) -> None:
        """Add a tool.

        Duplicate names are rejected rather than overwritten: silently
        replacing a tool would let a later registration change what an earlier
        contract promised.
        """
        name = tool.contract.name
        if name in self._tools:
            raise ValueError(f"tool {name!r} is already registered")
        self._tools[name] = tool

    def get(self, name: str) -> Tool[Any] | None:
        return self._tools.get(name)

    @property
    def files_read(self) -> tuple[str, ...]:
        """Files opened through this gateway, in no particular order.

        Exposed because a file the agent *looked at* is the best available
        statement of what a step was about when nothing else says. A live
        repair refused with "the failure implicates no editable file" on a run
        whose only action had been reading the very file it was asked to fix —
        the plan named no files, nothing had been written yet, and the one real
        signal was sitting here unused.
        """
        return tuple(sorted(self._read))

    @property
    def names(self) -> Sequence[str]:
        return tuple(sorted(self._tools))

    # -- what the model may see -------------------------------------------

    def available(self, phase: Phase) -> Sequence[ToolContract]:
        """Contracts reachable in `phase`.

        This is what the model is shown. It is never shown a tool it cannot
        call, which makes a wrong-phase call a runtime bug rather than an
        expected model mistake -- and means the failure gets fixed in the
        runtime instead of patched with another prompt instruction.

        The same reasoning is why `arguments` is filled in here from each
        tool's own input model. A tool the model can call but whose parameters
        it has to guess produces exactly the wrong-shaped call this method
        exists to prevent, and the names are already known.
        """
        return tuple(
            tool.contract.model_copy(update={"arguments": _argument_names(tool)})
            for _, tool in sorted(self._tools.items())
            if phase in tool.contract.allowed_phases
        )

    def schemas(self, phase: Phase) -> Sequence[dict[str, object]]:
        """Tool definitions for the prompt: name, purpose, and argument schema."""
        return tuple(
            {
                "name": tool.contract.name,
                "description": tool.contract.purpose,
                "parameters": tool.input_schema(),
            }
            for _, tool in sorted(self._tools.items())
            if phase in tool.contract.allowed_phases
        )

    # -- decisions ---------------------------------------------------------

    @contextmanager
    def decision(self) -> Iterator[None]:
        """Scope one model decision.

        At most `mutating_calls_per_decision` side effects may occur inside.
        The bound exists so a single misunderstood instruction cannot cascade
        into a sequence of writes before anything verifies the first one.
        """
        previous = self._mutations_used
        self._mutations_used = 0
        try:
            yield
        finally:
            self._mutations_used = previous

    @property
    def mutations_remaining(self) -> int:
        return max(0, self._mutation_budget - self._mutations_used)

    @contextmanager
    def restricted_to(self, scope: WriteScope) -> Iterator[None]:
        """Restrict which files may be written, for the duration of the block.

        Used by repair (plan §20.5: modify failure-related files, not the
        repository). Restoring the previous scope rather than clearing it means
        nesting narrows and never widens — an inner block cannot hand back more
        permission than it was given.
        """
        previous = self._scope
        self._scope = scope
        try:
            yield
        finally:
            self._scope = previous

    @property
    def scope(self) -> WriteScope | None:
        """The active write restriction, if any."""
        return self._scope

    # -- execution ---------------------------------------------------------

    async def invoke(
        self,
        request: ToolRequest,
        phase: Phase,
        cancel: CancellationToken,
    ) -> ToolResult:
        """Execute one tool call under full policy enforcement.

        Raises:
            ToolPolicyViolation: refused. Nothing was executed.
            Cancelled: the token fired before or during execution.
        """
        tool = self._resolve(request, phase)
        contract = tool.contract

        # Checked here as well as inside execution: refusing before starting is
        # cheaper and gives a cleaner reason than an abandoned call.
        cancel.raise_if_cancelled()

        arguments = tool.parse(request.arguments)

        if contract.mutating:
            # Read before scope: an edit to a file the model has not looked at
            # is refused whatever the scope says, and the reason is more useful.
            await self._check_read_first(tool, arguments, cancel)

            # Before scope, because this refusal is about *what* the file is
            # rather than which files this repair may touch, and saying so is
            # more useful than "outside the permitted write scope".
            self._check_tests_protected(tool, arguments)

            # Scope before budget: a call the scope forbids must not consume the
            # one mutation this decision is allowed.
            self._check_scope(tool, arguments)

            if self._mutations_used >= self._mutation_budget:
                raise ToolPolicyViolation(
                    f"{contract.name}: mutation budget exhausted "
                    f"({self._mutation_budget} per decision)"
                )
            self._mutations_used += 1

        result = await self._execute(tool, arguments, cancel)

        # Recorded after success only. A read that failed revealed nothing, and
        # crediting it would license an edit against contents never seen.
        if result.ok:
            self._read.update(_normalise(path) for path in tool.read_targets(arguments))
            # Authoring deliberately does *not* count. It used to, on the
            # reasoning that the model supplied the contents and so knows them
            # — true within a single decision, false by the next one. A live
            # build created `tasks.py` with stubbed methods in step 2, and in
            # step 3 anchored a `replace_text` on "# Define the TaskList class",
            # a line it had never written. Its context had rolled over; the
            # credit outlived the memory it stood for, and the edit was let
            # through to fail on an invented anchor.
            #
            # A file this run created is still reachable without a read, via
            # `mode="create"` — `FilePatchTool` allows overwriting its own
            # drafts. So nothing is unreachable here, and an edit that needs an
            # anchor now has to look at the anchor first.

        return result

    async def _check_read_first(
        self, tool: Tool[Any], arguments: Any, cancel: CancellationToken
    ) -> None:
        """Refuse an edit to a file that has not been read in this run.

        The invariant every editor a user is accustomed to enforces, and one
        SHAMSU lacked. A §31.1 run asked to add a section to `README.md` opened
        with `file.patch` on an anchor it had guessed, failed the exact match,
        and never recovered — two of seven tasks died that way.

        Refused *before* execution, so a guess costs a turn and not a file.

        **The refusal performs the read.** Telling a model to go and read
        something is asking it to spend a turn on an errand the runtime can run
        itself, and the §31.1 suite showed the errand does not reliably come
        back: a run refused here read `payments.py`, read it a second time, and
        concluded — having arrived with a correct anchor and a correct patch on
        its first call. So the gateway reads the file, credits it, and puts the
        contents *in the refusal*. The call still fails, because an anchor
        composed without seeing the file is still a guess; but the retry is
        immediate and informed rather than another round trip.
        """
        if not self._require_read_before_edit:
            return

        unseen = [
            path
            for path in tool.requires_prior_read(arguments)
            if _normalise(path) not in self._read
        ]
        if not unseen:
            return

        listed = ", ".join(unseen)
        shown = await self._read_now(unseen, cancel)
        raise ToolPolicyViolation(
            f"{tool.contract.name}: {listed} has not been read in this run. "
            f"An anchor you have not seen either fails to match or matches the "
            f"wrong place — so it has been read for you. Send the call again "
            f"with 'find' copied exactly from below.\n{shown}"
            if shown
            else f"{tool.contract.name}: {listed} has not been read in this run. "
            f"Call file.read on it first — an anchor you have not seen either "
            f"fails to match or matches the wrong place."
        )

    async def _read_now(self, paths: Sequence[str], cancel: CancellationToken) -> str:
        """Read the files an edit needed, crediting each one that succeeds.

        Uses the registered `file.read`, so the output is the line-numbered
        form the model already knows, and a workspace the gateway never has to
        be told about. A path that cannot be read is simply omitted — it is
        about to be reported as unread anyway.
        """
        reader = self._tools.get("file.read")
        if reader is None:  # pragma: no cover - authoring_tools always has it
            return ""

        parts: list[str] = []
        for path in paths:
            try:
                result = await reader.run(reader.parse({"path": path}), cancel)
            except (ToolPolicyViolation, OSError):
                continue
            if result.ok:
                self._read.add(_normalise(path))
                parts.append(result.output)
        return "\n".join(parts)

    def _check_tests_protected(self, tool: Tool[Any], arguments: Any) -> None:
        """Refuse an edit to a test file that already existed.

        The hole that produced this runtime's first false success. `RepairScope`
        protects test files during *repair*, on the reasoning that editing the
        failing test is indistinguishable from deleting the evidence — and
        nothing applied that reasoning to ordinary authoring, where the model
        spends most of its actions.

        Measured: the §31.1 task `must_be_wired_in` asks for a handler to be
        registered in a dispatch table. The model wrote the handler, was told by
        the reachability check that nothing reached it, and resolved that by
        editing `test_dispatch.py`. The runtime reported the task **complete**.
        A modified test that passes is indistinguishable from working code, so
        this is the one failure the evidence gate cannot see by construction —
        it has to be prevented instead.

        **Creating a new test file is always allowed.** "Write a unit test for
        `slugify` in a new file" is a legitimate task and a common one; what is
        refused is rewriting a test that was already there to agree with the
        code. Existence is the whole distinction, and it is exactly the line
        between writing tests and editing away the evidence.

        `allow_test_edits` is the caller's decision, never the model's — same
        rule the repair scope has always had.
        """
        if self._allow_test_edits:
            return

        for target in tool.write_targets(arguments):
            if not looks_like_a_test(target):
                continue
            try:
                exists = (self._workspace / target).exists() if self._workspace else False
            except OSError:  # pragma: no cover - defensive
                exists = False
            if not exists:
                continue
            raise ToolPolicyViolation(
                f"{tool.contract.name}: {target} is an existing test file, and this "
                "run may not modify tests. A test that was changed to agree with "
                "the code proves nothing about the code. Change the code it tests "
                "instead, or write a new test file."
            )

    def _check_scope(self, tool: Tool[Any], arguments: Any) -> None:
        """Refuse a write the active scope does not cover.

        A mutating tool that declares no write targets is not constrained. That
        is correct for `git.checkpoint`, which records what is already on disk,
        and it is the reason `write_targets` is a tool's own declaration rather
        than something the gateway infers.
        """
        if self._scope is None:
            return
        for target in tool.write_targets(arguments):
            if not self._scope.permits(target):
                raise ToolPolicyViolation(
                    f"{tool.contract.name}: {target} is outside the permitted "
                    f"write scope. {self._scope.describe()}"
                )

    def _resolve(self, request: ToolRequest, phase: Phase) -> Tool[Any]:
        tool = self._tools.get(request.tool)
        if tool is None:
            available = ", ".join(c.name for c in self.available(phase)) or "(none)"
            raise ToolPolicyViolation(
                f"unknown tool {request.tool!r}; available in {phase.value}: {available}"
            )

        contract = tool.contract
        if phase not in contract.allowed_phases:
            allowed = ", ".join(sorted(p.value for p in contract.allowed_phases))
            raise ToolPolicyViolation(
                f"{contract.name} is not allowed in phase {phase.value} (allowed: {allowed})"
            )

        if contract.requires_approval and not self._approval(contract, request):
            raise ToolPolicyViolation(f"{contract.name} requires approval and it was not granted")

        return tool

    async def _execute(
        self,
        tool: Tool[Any],
        arguments: object,
        cancel: CancellationToken,
    ) -> ToolResult:
        """Run under the contract's timeout, racing cancellation."""
        contract = tool.contract
        runner = asyncio.ensure_future(tool.run(arguments, cancel))
        watcher = asyncio.ensure_future(cancel.wait_cancelled())

        try:
            done, _ = await asyncio.wait(
                {runner, watcher},
                timeout=contract.timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            if not watcher.done():
                watcher.cancel()

        if runner in done:
            watcher.cancel()
            await asyncio.gather(watcher, return_exceptions=True)
            try:
                result = runner.result()
            except Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001 - an honest failure beats a crash
                return ToolResult(
                    tool=contract.name,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            return self._cap(result, contract)

        # Neither timeout nor cancellation leaves a task running: an abandoned
        # subprocess or file handle is exactly how a "cancelled" run keeps
        # touching the workspace.
        runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)

        # Ask the token, rather than inferring from the watcher having
        # completed. A watcher can finish for reasons other than cancellation
        # -- raising, or being resolved by an implementation that does not wait
        # -- and reporting a timeout as a cancellation would send the runtime
        # down the wrong recovery path entirely.
        if cancel.cancelled:
            raise Cancelled(cancel.reason or "run cancelled")

        return ToolResult(
            tool=contract.name,
            ok=False,
            error=f"timed out after {contract.timeout_seconds:g}s",
            duration_seconds=contract.timeout_seconds,
        )

    @staticmethod
    def _cap(result: ToolResult, contract: ToolContract) -> ToolResult:
        """Truncate output before it can reach any context.

        v1's comment on this was precise and correct, and the mechanism is kept
        because the failure mode is real: a budget-aware trimmer always keeps
        the most recent message, so one oversized read survives trimming and
        crowds out everything else. Capping *here* -- before the result is
        stored, summarised, or compiled into a frame -- is the only place that
        holds.

        The model is told what happened and how to see more, so truncation is a
        recoverable condition rather than a silent hole in what it was shown.
        """
        encoded = result.output.encode("utf-8")
        if len(encoded) <= contract.max_output_bytes:
            return result

        kept = encoded[: contract.max_output_bytes].decode("utf-8", errors="ignore")
        notice = (
            f"\n\n[truncated: {len(encoded)} bytes exceeded this tool's "
            f"{contract.max_output_bytes}-byte cap. Narrow the range or query to see more.]"
        )
        return result.model_copy(
            update={
                "output": kept + notice,
                "truncated": True,
                "original_bytes": len(encoded),
            }
        )


def _argument_names(tool: Tool[Any]) -> tuple[str, ...]:
    """A tool's parameters, required first and marked with `*`.

    Required first because that ordering is itself information: a model reading
    `path*, mode, find, replace` learns which one it cannot omit without having
    to parse a schema. Derived from `input_schema()`, which is the same document
    `parse` validates against, so the two cannot disagree.
    """
    try:
        schema = tool.input_schema()
    except Exception:  # noqa: BLE001 - a tool must not break the listing
        return ()

    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return ()

    required = schema.get("required")
    mandatory = set(required) if isinstance(required, list) else set()

    names = list(properties)
    ordered = [name for name in names if name in mandatory]
    ordered += [name for name in names if name not in mandatory]
    return tuple(f"{name}*" if name in mandatory else name for name in ordered)


__all__ = ["ApprovalCallback", "ToolGateway", "deny_all"]

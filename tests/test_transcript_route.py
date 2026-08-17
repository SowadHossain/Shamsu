"""Tests for the append-only transcript build route.

The property under test throughout is the one the route exists for: the prompt
prefix stays byte-stable as the conversation grows, so the model sees its own
prior output and the server can reuse its cache. The old state-frame compiler
fails every one of these by construction.
"""
from __future__ import annotations

import asyncio
from pathlib import Path


from shamsu.transcript.build import (
    TranscriptBuilder,
    build_system_prompt,
)
from shamsu.transcript.session import Message, Transcript


# --- fakes ------------------------------------------------------------------


class FakeLLM:
    """Records every payload it is sent and replays scripted answers."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = list(answers)
        self.payloads: list[list[dict]] = []

    async def chat_with_tools(self, *, model, messages, **kwargs):
        self.payloads.append([dict(m) for m in messages])
        answer = self.answers.pop(0) if self.answers else "nothing left to implement"
        return {"message": {"content": answer}}


class FakeResult:
    def __init__(self, ok: bool, message: str = "") -> None:
        self.ok = ok
        self.message = message
        self.data: dict = {}


class FakeRegistry:
    """Writes into a real directory so verification has something to look at."""

    def __init__(self, root: Path, fail: set[str] | None = None) -> None:
        self.root = root
        self.fail = fail or set()
        self.calls: list[tuple[str, dict]] = []

    def execute(self, name: str, arguments: dict) -> FakeResult:
        self.calls.append((name, dict(arguments)))
        path = str(arguments.get("filepath") or "")
        if path in self.fail:
            return FakeResult(False, f"refused to write {path}")
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(str(arguments.get("content") or ""), encoding="utf-8")
        return FakeResult(True, f"wrote {path}")


def file_block(path: str, body: str) -> str:
    return f"```\n# write_file: {path}\n{body}\n```"


def make_builder(tmp_path: Path, answers: list[str], **kwargs) -> TranscriptBuilder:
    builder = TranscriptBuilder(
        tmp_path,
        FakeRegistry(tmp_path, fail=kwargs.pop("fail", None)),
        FakeLLM(answers),
        "test-model",
        **kwargs,
    )
    # Verification is exercised separately; default it out of the way so loop
    # tests are not at the mercy of what tooling exists on the machine.
    builder._verify = _stub_verify("skipped", "")  # type: ignore[method-assign]
    return builder


def _stub_verify(status: str, error: str):
    async def _verify(files):
        return status, error

    return _verify


# --- Transcript: the cache-preservation property ----------------------------


def test_system_prompt_is_frozen_across_appends():
    transcript = Transcript("SYSTEM", max_tokens=4096)
    first = transcript.messages()[0]["content"]
    for i in range(10):
        transcript.append_user(f"turn {i}")
        transcript.append_assistant(f"answer {i}")
    assert transcript.messages()[0]["content"] == first == "SYSTEM"


def test_appending_never_rewrites_earlier_messages():
    transcript = Transcript("SYSTEM", max_tokens=8192)
    transcript.append_user("plan this")
    transcript.append_assistant("the plan", pinned=True)
    before = transcript.messages()
    transcript.append_user("next")
    after = transcript.messages()
    # Every previously sent message survives byte-identical, in order.
    assert after[: len(before)] == before


def test_shared_prefix_grows_with_the_conversation():
    """The measurement that distinguishes this route from the state frame."""
    transcript = Transcript("SYSTEM PROMPT TEXT", max_tokens=8192)
    transcript.append_user("plan this project please")
    previous = transcript.messages()
    transcript.append_assistant("here is a long plan " * 20)
    transcript.append_user("next")
    shared = transcript.shared_prefix_tokens(previous)
    # The whole previous payload is still a prefix of the current one.
    assert shared > 0
    assert shared == transcript.shared_prefix_tokens(previous)
    full_previous = sum(len(m["content"]) for m in previous)
    assert full_previous > 0


def test_shared_prefix_is_zero_when_the_head_is_rewritten():
    """A rebuilt frame scores zero — this is the behaviour being replaced."""
    transcript = Transcript("SYSTEM A", max_tokens=8192)
    transcript.append_user("hello")
    previous = transcript.messages()
    rebuilt = Transcript("SYSTEM B", max_tokens=8192)
    rebuilt.append_user("hello")
    assert rebuilt.shared_prefix_tokens(previous) == 0


def test_compaction_drops_whole_messages_and_keeps_pinned_and_tail():
    transcript = Transcript("SYS", max_tokens=600)
    transcript.append_user("plan")
    transcript.append_assistant("THE PLAN " * 20, pinned=True)
    for i in range(12):
        transcript.append_user(f"user filler {i} " * 20)
        transcript.append_assistant(f"assistant filler {i} " * 20)
    before = len(transcript)
    dropped = transcript.compact(keep_tail=4)
    assert dropped > 0
    assert len(transcript) == before - dropped
    contents = [m["content"] for m in transcript.messages()]
    assert contents[0] == "SYS"
    assert any("THE PLAN" in c for c in contents), "pinned plan must survive"
    assert any("assistant filler 11" in c for c in contents), "tail must survive"
    assert transcript.dropped_messages == dropped
    assert transcript.compactions == 1


def test_compaction_is_a_noop_below_the_tail_size():
    transcript = Transcript("SYS", max_tokens=64)
    transcript.append_user("a")
    transcript.append_assistant("b")
    assert transcript.compact(keep_tail=8) == 0


def test_pin_last_marks_the_newest_turn():
    transcript = Transcript("SYS", max_tokens=1024)
    transcript.append_assistant("the plan")
    transcript.pin_last()
    assert transcript.messages()[-1]["content"] == "the plan"
    transcript_messages = [Message("assistant", "the plan", pinned=True)]
    assert transcript_messages[0].pinned is True


# --- the build loop ---------------------------------------------------------


def test_plan_is_kept_verbatim_and_pinned(tmp_path):
    builder = make_builder(tmp_path, ["# Build plan\n\n1. Foundation\n2. API"])
    plan = asyncio.run(builder.plan("build a marketplace"))
    assert plan == "# Build plan\n\n1. Foundation\n2. API"
    payload = builder.transcript.messages()
    assert payload[-1]["role"] == "assistant"
    assert payload[-1]["content"] == plan, "the plan must survive byte-for-byte"


def test_model_sees_its_own_previous_output(tmp_path):
    """The single behaviour the state-frame compiler removes."""
    builder = make_builder(
        tmp_path,
        [
            "# Plan\n1. models\n2. routes",
            file_block("models.py", "class User: pass"),
            file_block("routes.py", "ROUTES = []"),
        ],
    )
    asyncio.run(builder.run("build it", max_slices=2))
    # The payload for the LAST call must contain the plan AND the first
    # milestone's code, both as assistant turns.
    final_payload = builder.llm.payloads[-1]
    roles = [m["role"] for m in final_payload]
    assert roles.count("assistant") >= 2
    blob = "\n".join(m["content"] for m in final_payload)
    assert "1. models" in blob, "the plan must still be in context"
    assert "class User: pass" in blob, "its own code must still be in context"


def test_every_call_extends_the_previous_payload(tmp_path):
    builder = make_builder(
        tmp_path,
        [
            "# Plan",
            file_block("a.py", "A = 1"),
            file_block("b.py", "B = 2"),
            file_block("c.py", "C = 3"),
        ],
    )
    asyncio.run(builder.run("build it", max_slices=3))
    payloads = builder.llm.payloads
    assert len(payloads) >= 3
    for older, newer in zip(payloads, payloads[1:]):
        assert newer[: len(older)] == older, "a call rewrote history and killed the cache"


def test_writes_reach_the_registry_and_land_on_disk(tmp_path):
    builder = make_builder(
        tmp_path,
        ["# Plan", file_block("pkg/app.py", "print('hello')")],
    )
    report = asyncio.run(builder.run("build it", max_slices=1))
    assert report.files == ["pkg/app.py"]
    assert (tmp_path / "pkg" / "app.py").read_text(encoding="utf-8").strip() == "print('hello')"
    assert builder.registry.calls[0][0] == "write_file"


def test_many_files_in_one_turn(tmp_path):
    """One milestone per call, not one action per call."""
    answer = "\n\n".join(
        [
            "Milestone 1: foundation.",
            file_block("one.py", "ONE = 1"),
            file_block("two.py", "TWO = 2"),
            file_block("three.py", "THREE = 3"),
        ]
    )
    builder = make_builder(tmp_path, ["# Plan", answer])
    report = asyncio.run(builder.run("build it", max_slices=1))
    assert report.slices[0].files_written == ["one.py", "two.py", "three.py"]
    assert report.slices[0].model_calls == 1, "three files must not cost three calls"


def test_verification_failure_is_fed_back_as_a_user_turn(tmp_path):
    builder = make_builder(
        tmp_path,
        [
            "# Plan",
            file_block("app.py", "syntax ((("),
            file_block("app.py", "app = 1"),
        ],
    )
    states = iter([("failed", "SyntaxError: unexpected ((("), ("passed", "")])
    builder._verify = lambda files: _resolved(next(states))  # type: ignore[method-assign]
    report = asyncio.run(builder.run("build it", max_slices=1))
    outcome = report.slices[0]
    assert outcome.repairs == 1
    assert outcome.verify_status == "passed"
    feedback = [m for m in builder.transcript.messages() if m["role"] == "user"]
    assert any("SyntaxError: unexpected (((" in m["content"] for m in feedback), (
        "the verbatim error must reach the model"
    )


def test_repair_budget_is_bounded(tmp_path):
    builder = make_builder(
        tmp_path,
        ["# Plan"] + [file_block("app.py", f"broken {i}") for i in range(6)],
    )
    builder._verify = lambda files: _resolved(("failed", "still broken"))  # type: ignore[method-assign]
    report = asyncio.run(builder.run("build it", max_slices=1))
    outcome = report.slices[0]
    assert outcome.repairs == 2, "must stop at MAX_REPAIRS_PER_SLICE"
    assert report.stopped_reason.startswith("milestone 1 still failing")


def test_tool_failure_is_reported_back_not_swallowed(tmp_path):
    builder = make_builder(
        tmp_path,
        ["# Plan", file_block("blocked.py", "x = 1"), file_block("blocked.py", "x = 2")],
        fail={"blocked.py"},
    )
    report = asyncio.run(builder.run("build it", max_slices=1))
    user_turns = [m["content"] for m in builder.transcript.messages() if m["role"] == "user"]
    assert any("refused to write blocked.py" in c for c in user_turns)
    assert report.slices[0].repairs >= 1


def test_done_signal_ends_the_build(tmp_path):
    builder = make_builder(
        tmp_path,
        ["# Plan", file_block("a.py", "A = 1"), "All milestones are implemented."],
    )
    report = asyncio.run(builder.run("build it", max_slices=10))
    assert report.completed is True
    assert "complete" in report.stopped_reason
    assert len(report.slices) == 2


def test_a_chatty_non_building_model_does_not_loop_forever(tmp_path):
    builder = make_builder(
        tmp_path,
        ["# Plan", "Sure, I can help with that.", "Let me know how to proceed."],
    )
    report = asyncio.run(builder.run("build it", max_slices=10))
    assert report.completed is False
    assert "two milestones in a row" in report.stopped_reason
    assert report.files == []
    assert len(report.slices) == 2, "must stop on the second empty turn, not the tenth"


def test_slice_cap_is_honoured(tmp_path):
    answers = ["# Plan"] + [file_block(f"f{i}.py", f"X = {i}") for i in range(10)]
    builder = make_builder(tmp_path, answers)
    report = asyncio.run(builder.run("build it", max_slices=3))
    assert len(report.slices) == 3
    assert "3-milestone cap" in report.stopped_reason


def test_every_instruction_names_its_milestone_number(tmp_path):
    """A bare "next" returned prose and no files on the 2026-08-17 live run.

    The loop cannot see what just happened and re-steer the way a person in a
    chat can, so each turn states which milestone it wants.
    """
    answers = ["# Plan"] + [file_block(f"f{i}.py", f"X = {i}") for i in range(3)]
    builder = make_builder(tmp_path, answers)
    asyncio.run(builder.run("build it", max_slices=3))
    user_turns = [m["content"] for m in builder.transcript.messages() if m["role"] == "user"]
    for number in (1, 2, 3):
        assert any(c.startswith(f"Implement milestone {number} ") for c in user_turns)
    assert not any(c.strip() == "next" for c in user_turns)


def test_instruction_never_offers_an_early_exit(tmp_path):
    """Asked to implement and to self-certify in one message, the model on
    2026-08-17 answered ALL MILESTONES COMPLETE before writing a single line."""
    builder = make_builder(tmp_path, ["# Plan", file_block("a.py", "A = 1")])
    asyncio.run(builder.run("build it", max_slices=1))
    user_turns = [m["content"] for m in builder.transcript.messages() if m["role"] == "user"]
    implement = [c for c in user_turns if c.startswith("Implement milestone")]
    assert implement
    assert not any("ALL MILESTONES COMPLETE" in c for c in implement)


def test_milestone_count_is_read_from_the_plan(tmp_path):
    plan = (
        "## Milestone 1: Models\n## Milestone 2: Routes\n## Milestone 3: Templates\n"
    )
    answers = [plan] + [file_block(f"f{i}.py", f"X = {i}") for i in range(6)]
    builder = make_builder(tmp_path, answers)
    report = asyncio.run(builder.run("build it", max_slices=20))
    assert report.declared_milestones == 3
    assert len(report.slices) == 3, "the plan's own count drives the loop"
    assert report.completed is True
    assert "all 3 planned milestone" in report.stopped_reason


def test_max_slices_still_caps_an_over_long_plan(tmp_path):
    plan = "\n".join(f"## Milestone {i}: thing" for i in range(1, 9))
    answers = [plan] + [file_block(f"f{i}.py", f"X = {i}") for i in range(9)]
    builder = make_builder(tmp_path, answers)
    report = asyncio.run(builder.run("build it", max_slices=2))
    assert report.declared_milestones == 8
    assert len(report.slices) == 2
    assert "2-milestone cap" in report.stopped_reason


def test_one_empty_milestone_does_not_end_the_build(tmp_path):
    """A milestone an earlier turn already covered writes nothing, legitimately."""
    plan = "## Milestone 1: a\n## Milestone 2: b\n## Milestone 3: c\n"
    builder = make_builder(
        tmp_path,
        [
            plan,
            file_block("a.py", "A = 1"),
            "Milestone 2 is already covered by the file above.",
            file_block("c.py", "C = 3"),
        ],
    )
    report = asyncio.run(builder.run("build it", max_slices=10))
    assert len(report.slices) == 3, "an empty middle milestone must not stop the run"
    assert report.files == ["a.py", "c.py"]


def test_two_empty_milestones_in_a_row_stop_the_build(tmp_path):
    plan = "## Milestone 1: a\n## Milestone 2: b\n## Milestone 3: c\n"
    builder = make_builder(
        tmp_path,
        [plan, file_block("a.py", "A = 1"), "nothing to do", "still nothing"],
    )
    report = asyncio.run(builder.run("build it", max_slices=10))
    assert len(report.slices) == 3
    assert "two milestones in a row" in report.stopped_reason


def test_count_milestones_handles_common_headings():
    from shamsu.transcript.build import count_milestones

    assert count_milestones("## Milestone 1: x\n## Milestone 2: y") == 2
    assert count_milestones("### Milestone #3 - z") == 1
    assert count_milestones("**Milestone 1:** x\n**Milestone 2:** y") == 2
    assert count_milestones("no milestones here") == 0
    assert count_milestones("") == 0
    # Repeated references to the same milestone must not inflate the count.
    assert count_milestones("## Milestone 1: x\nsee Milestone 1 above") == 1


def test_approved_plan_is_seeded_without_a_model_call(tmp_path):
    builder = make_builder(tmp_path, [file_block("a.py", "A = 1")])
    report = asyncio.run(
        builder.run("build it", max_slices=1, plan_text="# Approved plan\n1. thing")
    )
    assert report.plan == "# Approved plan\n1. thing"
    assert len(builder.llm.payloads) == 1, "seeding a plan must not re-plan"
    assert builder.transcript.messages()[2]["content"] == "# Approved plan\n1. thing"


def test_cache_reuse_ratio_is_reported(tmp_path):
    builder = make_builder(
        tmp_path,
        ["# Plan " + "x " * 200, file_block("a.py", "A = 1"), file_block("b.py", "B = 2")],
    )
    report = asyncio.run(builder.run("build it", max_slices=2))
    assert report.cache_reuse_ratio > 0.0
    assert report.model_calls == 3


def test_thinking_spans_are_stripped_from_the_transcript(tmp_path):
    answer = "<think>long private reasoning</think>\n" + file_block("a.py", "A = 1")
    builder = make_builder(tmp_path, ["# Plan", answer])
    asyncio.run(builder.run("build it", max_slices=1))
    blob = "\n".join(m["content"] for m in builder.transcript.messages())
    assert "long private reasoning" not in blob
    assert "A = 1" in blob, "the code must stay"


def test_system_prompt_names_the_workspace(tmp_path):
    prompt = build_system_prompt("my-project")
    assert "my-project" in prompt
    assert "write_file:" in prompt


def _resolved(value):
    future: asyncio.Future = asyncio.Future()
    future.set_result(value)
    return future

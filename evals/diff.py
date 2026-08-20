"""Compare two eval runs and say whether a change helped, hurt, or did nothing.

Adapted from smallcode `bench/diff.js` (itself from itsy's
benchmark-driven-development skill), with three changes this project's numbers
force.

**Why this exists at all.** §31.1 scored anywhere from 1/7 to 5/7 across nine
runs of identical code. At that variance a human comparing two BENCHMARK.md
tables cannot tell an 8% gain from a coin flip, and every behavioural change
after this one is a change to a stochastic system. Without a mechanical verdict
we ship guard after guard and genuinely do not know which helped.

**What is different from smallcode.** Their harness runs one attempt per task,
and their own comment concedes it: *"soft == hard for now."* Ours runs N samples
and already records `passes`/`runs`/`flaky` per case. So:

1. A case's reward is its PASS FRACTION, not a boolean. 2/3 -> 3/3 is a real
   movement smallcode's model cannot express, and it is exactly the size of
   movement a guard change produces.
2. A case flaky in EITHER run is reported but does NOT drive the verdict.
   `render_report`'s own footer has said so in prose for months - *"a delta
   that lives entirely inside the flaky set is no delta"* - and nothing
   enforced it.
3. Unequal sample counts REFUSE to compare rather than printing a caveat. The
   footer asks for equal `--samples`; a tool that shrugs and compares anyway is
   how the caveat gets ignored.

Usage::

    python -m evals.diff <baseline> <feature> [--threshold 0.02] [--json]

Each argument is a harness JSON file (``--json-out``) or a directory holding
one; the newest is used. Exit codes are the point - they make it CI-usable:

    0  improved   1  regressed   2  noise   3  usage or IO error
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# A movement smaller than this is not a result. smallcode's default, and about
# a third of one case in a 12-case suite - below which the sample count is
# doing the talking rather than the change.
DEFAULT_THRESHOLD = 0.02

IMPROVED = "IMPROVED"
REGRESSED = "REGRESSED"
NOISE = "NOISE"

EXIT_IMPROVED = 0
EXIT_REGRESSED = 1
EXIT_NOISE = 2
EXIT_USAGE = 3


class DiffError(Exception):
    """Anything that makes a comparison impossible rather than merely negative."""


@dataclass(frozen=True)
class CaseRun:
    """One case's outcome in one run."""

    name: str
    passes: int
    runs: int
    duration_s: float = 0.0

    @property
    def rate(self) -> float:
        return (self.passes / self.runs) if self.runs else 0.0

    @property
    def flaky(self) -> bool:
        """Passed sometimes and failed sometimes - the result to distrust."""
        return 0 < self.passes < self.runs


@dataclass(frozen=True)
class RunSummary:
    """One whole run, reduced to what a comparison needs."""

    path: str
    tier: str
    cases: dict[str, CaseRun] = field(default_factory=dict)

    @property
    def reward(self) -> float:
        """Mean per-case pass fraction.

        Per CASE, not per sample: every case is one behaviour and deserves one
        vote. Pooling samples would let a case that happened to run more
        attempts outweigh one that ran fewer.
        """
        if not self.cases:
            return 0.0
        return sum(c.rate for c in self.cases.values()) / len(self.cases)

    @property
    def samples(self) -> set[int]:
        return {c.runs for c in self.cases.values()}

    @property
    def duration_s(self) -> float:
        return sum(c.duration_s for c in self.cases.values())


@dataclass(frozen=True)
class Moves:
    """How individual cases moved, sorted by whether the verdict may use them."""

    regressed: list[tuple[str, float, float]] = field(default_factory=list)
    recovered: list[tuple[str, float, float]] = field(default_factory=list)
    # Moved, but flaky in one run or both - reported, never counted.
    unreliable: list[tuple[str, float, float]] = field(default_factory=list)
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)

    @property
    def hard_regressions(self) -> list[str]:
        """Cases that were solid and are now broken.

        The one movement that overrides an otherwise positive delta: a change
        that lifts the average while destroying a case that used to work every
        time is not an improvement.
        """
        return [name for name, before, after in self.regressed if before == 1.0 and after == 0.0]


def load_run(argument: str) -> RunSummary:
    """Read one harness JSON report, or the newest one in a directory."""
    target = Path(argument)
    if not target.exists():
        raise DiffError(f"not found: {argument}")
    if target.is_dir():
        candidates = sorted(
            target.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
        )
        if not candidates:
            raise DiffError(f"no .json files in {argument}")
        target = candidates[0]
    try:
        raw: Any = json.loads(target.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise DiffError(f"malformed JSON in {target}: {exc}") from exc
    except OSError as exc:
        raise DiffError(f"could not read {target}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("results"), list):
        raise DiffError(f'{target}: no "results" array - is this a --json-out report?')
    cases: dict[str, CaseRun] = {}
    for entry in raw["results"]:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        runs = int(entry.get("runs") or 1)
        cases[name] = CaseRun(
            name=name,
            # A report written before `passes` existed still compares: fall
            # back to the boolean, which is what `passes` collapses to at
            # runs=1 anyway.
            passes=int(entry.get("passes", 1 if entry.get("passed") else 0)),
            runs=max(runs, 1),
            duration_s=float(entry.get("duration_s") or 0.0),
        )
    if not cases:
        raise DiffError(f"{target}: no named cases in the report")
    return RunSummary(path=str(target), tier=str(raw.get("tier") or ""), cases=cases)


def classify(base: RunSummary, feature: RunSummary) -> Moves:
    """Sort every case into moved-and-countable, moved-but-flaky, or absent."""
    moves = Moves()
    for name in sorted(set(base.cases) | set(feature.cases)):
        before = base.cases.get(name)
        after = feature.cases.get(name)
        if before is None:
            moves.added.append(name)
            continue
        if after is None:
            moves.removed.append(name)
            continue
        if before.rate == after.rate:
            continue
        movement = (name, before.rate, after.rate)
        if before.flaky or after.flaky:
            # The rule BENCHMARK.md has stated in prose and never enforced. A
            # case that passed 1/3 and now passes 2/3 has told us nothing: both
            # numbers are inside its own noise.
            moves.unreliable.append(movement)
        elif after.rate < before.rate:
            moves.regressed.append(movement)
        else:
            moves.recovered.append(movement)
    return moves


def reliable_delta(base: RunSummary, feature: RunSummary, moves: Moves) -> float:
    """The reward delta with flaky cases held out of BOTH sides.

    Computed over the cases the verdict is allowed to use, so a suite where
    every real movement is flaky reports 0.0 rather than a number built from
    coin flips.
    """
    unreliable = {name for name, _b, _a in moves.unreliable}
    shared = [
        name
        for name in set(base.cases) & set(feature.cases)
        if name not in unreliable
    ]
    if not shared:
        return 0.0
    before = sum(base.cases[n].rate for n in shared) / len(shared)
    after = sum(feature.cases[n].rate for n in shared) / len(shared)
    return after - before


def verdict(delta: float, moves: Moves, threshold: float) -> str:
    """Improved, regressed, or noise - in that order of precedence."""
    if moves.hard_regressions:
        return REGRESSED
    if delta >= threshold:
        return IMPROVED
    if delta <= -threshold:
        return REGRESSED
    return NOISE


def exit_code_for(result: str) -> int:
    if result == IMPROVED:
        return EXIT_IMPROVED
    if result == REGRESSED:
        return EXIT_REGRESSED
    return EXIT_NOISE


def check_comparable(base: RunSummary, feature: RunSummary) -> None:
    """Refuse a comparison the numbers cannot support.

    Not a warning. `render_report`'s footer already asks for equal `--samples`
    and a footer is a thing people scroll past; comparing 1 sample against 3 is
    comparing a coin flip to a measurement, and the tool exists to stop exactly
    that.
    """
    base_samples = base.samples
    feature_samples = feature.samples
    if base_samples != feature_samples:
        raise DiffError(
            "the two runs used different sample counts "
            f"(baseline {sorted(base_samples)}, feature {sorted(feature_samples)}). "
            "Re-run both at the same --samples: a delta between an unequal "
            "number of attempts is a delta between different questions."
        )
    if base_samples == {1}:
        # Not fatal - a 1-sample pair is still a legitimate smoke comparison -
        # but it must not be mistaken for a measurement.
        print(
            "  note: 1 sample per case. Local models are stochastic; treat "
            "anything short of a hard regression as unproven.\n",
            file=sys.stderr,
        )


def _pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def _signed(value: float, digits: int = 3) -> str:
    return f"+{value:.{digits}f}" if value >= 0 else f"{value:.{digits}f}"


def _moved(before: float, after: float) -> str:
    return f"{_pct(before)} -> {_pct(after)}"


def render(
    base: RunSummary,
    feature: RunSummary,
    moves: Moves,
    delta: float,
    result: str,
    threshold: float,
) -> str:
    """The report. Deliberately plain text - it goes in a log and a PR body."""
    lines: list[str] = []
    lines.append("")
    lines.append("  SHAMSU eval diff")
    lines.append("  " + "-" * 56)
    lines.append(f"  baseline : {base.path}" + (f"  [{base.tier}]" if base.tier else ""))
    lines.append(f"  feature  : {feature.path}" + (f"  [{feature.tier}]" if feature.tier else ""))
    samples = sorted(base.samples)
    lines.append(f"  samples  : {samples[0] if len(samples) == 1 else samples} per case")
    lines.append("")
    lines.append(f"  reward   : {_pct(base.reward)}  ->  {_pct(feature.reward)}")
    lines.append(f"  reliable : {_signed(delta)}   (threshold +/-{threshold:.3f})")
    lines.append(
        f"  walltime : {base.duration_s:.0f}s  ->  {feature.duration_s:.0f}s   "
        f"({_signed(feature.duration_s - base.duration_s, 0)}s)"
    )
    lines.append("")
    if moves.regressed:
        lines.append(f"  REGRESSED ({len(moves.regressed)}):")
        for name, before, after in moves.regressed:
            hard = "  <- was solid" if before == 1.0 and after == 0.0 else ""
            lines.append(f"      {name}: {_moved(before, after)}{hard}")
    if moves.recovered:
        lines.append(f"  RECOVERED ({len(moves.recovered)}):")
        for name, before, after in moves.recovered:
            lines.append(f"      {name}: {_moved(before, after)}")
    if moves.unreliable:
        lines.append(f"  MOVED BUT FLAKY - not counted ({len(moves.unreliable)}):")
        for name, before, after in moves.unreliable:
            lines.append(f"      {name}: {_moved(before, after)}")
    if moves.added:
        lines.append(f"  NEW CASES - not counted ({len(moves.added)}): {', '.join(moves.added)}")
    if moves.removed:
        lines.append(f"  GONE - not counted ({len(moves.removed)}): {', '.join(moves.removed)}")
    if not (moves.regressed or moves.recovered or moves.unreliable):
        lines.append("  No case changed its pass rate.")
    lines.append("")
    lines.append(f"  VERDICT  : {result}")
    if result == REGRESSED and moves.hard_regressions:
        lines.append(
            "             a case that passed every attempt now fails every "
            "attempt, which no average can offset"
        )
    lines.append("")
    return "\n".join(lines)


def as_dict(
    base: RunSummary,
    feature: RunSummary,
    moves: Moves,
    delta: float,
    result: str,
) -> dict[str, Any]:
    return {
        "verdict": result,
        "exit_code": exit_code_for(result),
        "baseline": {"path": base.path, "tier": base.tier, "reward": base.reward},
        "feature": {"path": feature.path, "tier": feature.tier, "reward": feature.reward},
        "reliable_delta": delta,
        "regressed": [{"name": n, "before": b, "after": a} for n, b, a in moves.regressed],
        "recovered": [{"name": n, "before": b, "after": a} for n, b, a in moves.recovered],
        "unreliable": [{"name": n, "before": b, "after": a} for n, b, a in moves.unreliable],
        "hard_regressions": moves.hard_regressions,
        "added": moves.added,
        "removed": moves.removed,
    }


def compare(
    baseline: str, feature: str, threshold: float = DEFAULT_THRESHOLD
) -> tuple[RunSummary, RunSummary, Moves, float, str]:
    """Load, check, classify, judge. The whole comparison, without any printing."""
    base_run = load_run(baseline)
    feature_run = load_run(feature)
    check_comparable(base_run, feature_run)
    moves = classify(base_run, feature_run)
    delta = reliable_delta(base_run, feature_run, moves)
    return base_run, feature_run, moves, delta, verdict(delta, moves, threshold)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m evals.diff",
        description="Compare two eval runs. Exit 0 improved, 1 regressed, 2 noise, 3 error.",
    )
    parser.add_argument("baseline", help="Harness JSON report, or a directory holding one.")
    parser.add_argument("feature", help="The run to judge against the baseline.")
    parser.add_argument(
        "--threshold",
        type=float,
        default=DEFAULT_THRESHOLD,
        help=f"Reward delta below which a change is noise (default {DEFAULT_THRESHOLD}).",
    )
    parser.add_argument("--json", action="store_true", help="Emit the comparison as JSON.")
    args = parser.parse_args(argv)

    try:
        base, feature, moves, delta, result = compare(
            args.baseline, args.feature, args.threshold
        )
    except DiffError as exc:
        print(f"eval diff: {exc}", file=sys.stderr)
        return EXIT_USAGE

    if args.json:
        print(json.dumps(as_dict(base, feature, moves, delta, result), indent=2))
    else:
        print(render(base, feature, moves, delta, result, args.threshold))
    return exit_code_for(result)


if __name__ == "__main__":
    raise SystemExit(main())

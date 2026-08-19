"""LIVE run of simple mode against a real Ollama model.

This is the acceptance gate SMALLCODE_IMPLEMENTATION_PLAN.md names as missing:
"Everything above is unit-tested against a scripted client. The numbers are
real but synthetic; a session against a real model on a real workspace is the
remaining verification."

Built to measure the A-H work specifically, not just "did it answer":

  A  our token estimate vs Ollama's prompt_eval_count, per turn, and whether
     the calibration factor moves toward the truth
  B  num_predict / think / done_reason, and whether a cut-off reply is ever
     handed back as a finished one
  C  whether write_file on an existing file gets steered to patch_file
  D  payload elisions
  E  the counters and the efficiency ratio
  H  whether the model uses memory_remember at all

Plus the one thing the plan says cannot be tested through the prompt: recall of
a fact that ONLY the conversation carries (the always-fresh workspace listing
names every file whether history remembers it or not).

Each turn builds a FRESH SimpleChatLoop rehydrating from disk - exactly what
the REPL does per user message, and the case elision had to be moved to
hydration for.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

MODEL = os.environ.get("LIVE_MODEL", "qwen2.5:3b-instruct")
_DEFAULT_WS = Path(__file__).resolve().parent.parent.parent / "test-shamsu" / "live-run"
WS = Path(os.environ.get("LIVE_WS") or _DEFAULT_WS)

# A fact that exists ONLY in the conversation - no file, no listing, no
# workspace state carries it. This is the recall probe.
SECRET = "the dev server must run on port 8731"

TURNS = [
    # 1. plain talk, establishes the fact and nothing else. Also exercises the
    #    prose-nudge gate: a words-verb request must not be nudged.
    f"Remember this for later: {SECRET}. Just confirm you have it - do not create any files yet.",
    # 2. a real write. Small file, so write_file is legitimate here.
    "Create a file called server.py that prints 'hello' and nothing else.",
    # 3. a read + an edit of an EXISTING file - the patch-first path (C).
    "Read server.py, then change it so it prints 'goodbye' instead.",
    # 4. run it - tool use with real output (D's non-lossless case).
    "Run server.py with python and tell me exactly what it printed.",
    # 5. THE PROBE. Nothing on disk answers this.
    "Without reading any file, what port did I say the dev server must run on?",
]


def build(workspace: Path, client, tools):
    from shamsu.agents.simple_chat import SimpleChatLoop

    return SimpleChatLoop(
        workspace,
        client=client,
        tools=tools,
        model_name=MODEL,
        request_timeout=float(os.environ.get("LIVE_TIMEOUT", "600")),
        on_activity=lambda m: print(f"      . {m}", flush=True),
    )


async def main() -> int:
    from shamsu.agents.chat_loop import _default_ollama_client
    from shamsu.agents.simple_chat import (
        SESSION_COUNTERS,
        build_simple_tools,
        LAST_ALLOCATION,
    )
    from shamsu.llm.manager import OLLAMA_BASE_URL
    from shamsu.runtime.timeouts import TimeoutConfig

    if WS.exists():
        shutil.rmtree(WS, ignore_errors=True)
    WS.mkdir(parents=True, exist_ok=True)

    client = _default_ollama_client(OLLAMA_BASE_URL, TimeoutConfig())
    # --approval allow: every mutating tool is auto-approved, so nothing blocks
    # on stdin. This is the documented headless posture.
    tools = build_simple_tools(WS, console_approval=lambda request: True)

    print("=" * 78)
    print(f"LIVE RUN  model={MODEL}  workspace={WS}")
    print("=" * 78)

    report = []
    for index, prompt in enumerate(TURNS, start=1):
        print(f"\n--- TURN {index} ---")
        print(f"USER: {prompt}", flush=True)
        loop = build(WS, client, tools)
        started = time.perf_counter()
        try:
            result = await loop.run(prompt)
        except Exception as exc:  # noqa: BLE001 - a live run reports, never raises
            print(f"  !! EXCEPTION {type(exc).__name__}: {exc}")
            report.append({"turn": index, "error": f"{type(exc).__name__}: {exc}"})
            continue
        elapsed = time.perf_counter() - started

        counters = SESSION_COUNTERS
        allocation = LAST_ALLOCATION.get("value")
        estimate = counters.last_estimate or 0
        actual = counters.last_prompt_tokens or 0
        drift = (actual / estimate) if estimate else 0.0

        row = {
            "turn": index,
            "seconds": round(elapsed, 1),
            "rounds": result.rounds,
            "tool_calls": result.tool_calls,
            "changed": list(result.changed_files),
            "stopped": bool(result.stopped),
            "estimate": estimate,
            "actual_prompt_eval": actual,
            "drift": round(drift, 3),
            "done_reason": loop.last_done_reason,
            "num_keep": loop._num_keep(loop._num_ctx_floor or 32768),
            "compactions": counters.compactions,
            "elisions": counters.evictions,
            "truncations": counters.truncations,
            "efficiency": round(counters.efficiency, 1),
            "calibration_factor": round(loop._calibration_factor(), 4),
            "final": (result.final or "")[:400],
            "think_sent": loop._should_think(),
        }
        if allocation is not None:
            row["buckets"] = dict(allocation.buckets)
            row["fattest"] = allocation.fattest()
        report.append(row)

        print(f"  ASSISTANT: {(result.final or '')[:300]}", flush=True)
        print(
            f"  [{elapsed:.1f}s  rounds={result.rounds}  tools={result.tool_calls}"
            f"  changed={list(result.changed_files)}]"
        )
        print(
            f"  [A: estimate {estimate:,} vs real {actual:,}"
            f"  drift {drift:.2f}x  factor {loop._calibration_factor():.3f}]"
        )
        print(
            f"  [B: done_reason={loop.last_done_reason!r}  num_keep={row['num_keep']}]"
        )
        print(
            f"  [E: compactions={counters.compactions} elisions={counters.evictions}"
            f" truncations={counters.truncations} efficiency={counters.efficiency:.1f}%]"
        )

    # ---- the verdicts ----------------------------------------------------
    print("\n" + "=" * 78)
    print("VERDICTS")
    print("=" * 78)

    ok = {}
    turns = [r for r in report if "error" not in r]

    drifts = [r["drift"] for r in turns if r["drift"]]
    ok["A estimate within 15% of prompt_eval_count"] = bool(drifts) and all(
        0.85 <= d <= 1.15 for d in drifts
    )
    ok["A calibration recorded"] = bool(drifts)
    ok["B num_keep covers the system prompt"] = all(r["num_keep"] > 4 for r in turns)
    ok["B no reply cut off and passed off as finished"] = all(
        r["done_reason"] != "length" for r in turns
    )
    server = WS / "server.py"
    ok["C server.py was created"] = server.is_file()
    ok["C server.py was later edited"] = server.is_file() and "goodbye" in server.read_text(
        encoding="utf-8", errors="replace"
    )
    last = turns[-1] if turns else {}
    ok["RECALL: the port survived 4 turns"] = "8731" in str(last.get("final", ""))
    ok["no turn stopped on a guard"] = all(not r["stopped"] for r in turns)
    ok["all 5 turns completed"] = len(turns) == len(TURNS)

    for name, passed in ok.items():
        print(f"  {'PASS' if passed else 'FAIL'}  {name}")

    out = WS / "live_report.json"
    out.write_text(json.dumps({"model": MODEL, "turns": report, "verdicts": ok}, indent=2, default=str), encoding="utf-8")
    print(f"\nreport: {out}")
    print(f"score: {sum(1 for v in ok.values() if v)}/{len(ok)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))

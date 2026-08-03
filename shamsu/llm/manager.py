"""
shamsu/llm/manager.py â€” Dev B owns this file.

Implements ILLMManager. Encodes the harness defaults directly:
  - keep_alive=-1 (router never unloads; see ENGINEERING_HARNESS.md Â§8)
  - temperature=0 for routing/JSON, 0.1 for code, per SPECIALIST_TEMPS
  - Ollama's native `format` schema param for structured output â€”
    this is the PRIMARY mechanism, not a library. json_repair is the
    fallback when schema enforcement still produces something invalid
    (rare, but happens under quantization-induced glitches).
  - num_ctx=8192 default â€” drop to 4096 if you observe swapping
    (see harness Â§8, "num_ctx tradeoffs").

This file does NOT manage model loading/unloading lifecycle yet â€”
that's the Day 8 ModelManager work (see WEEK3_PLAN.md Day 8, Dev B).
For Day 1-3 scaffolding, run_specialist() always targets the model
named in OLLAMA_MODELS[specialist] and trusts Ollama's own keep_alive
to decide what's resident.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from urllib.parse import urlparse

import httpx
from json_repair import repair_json

from shamsu.action_ledger.ledger import ActionLedger
from shamsu.action_ledger.redaction import redact_text
from shamsu.context.manager import ContextBudgetManager
from shamsu.interfaces import ILLMManager
from shamsu.memory.service import MemoryService
from shamsu.runtime.models import (
    SPECIALIST_MODELS,
    model_for_role,
    model_is_reasoning,
    role_should_think,
)
from shamsu.session.manager import SessionLogger
from shamsu.types import ContextPack, LLMResponse, RoutingDecision
from pathlib import Path

OLLAMA_BASE_URL = "http://localhost:11434"
LOCAL_LLM_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _timeout_env(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, "").strip())
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


# Timeouts are split so slow hardware isn't cut off mid-answer while a genuine
# deadlock still fails instead of hanging forever:
#  - connect: short, so a dead Ollama fails fast instead of burning the budget.
#  - idle (streaming read): max SILENCE between streamed tokens. As long as the
#    model keeps emitting tokens the call continues no matter how long the whole
#    generation takes (slow CPU box is fine); if it goes quiet for this long we
#    treat it as stalled/deadlocked and abort. Tunable via SHAMSU_LLM_IDLE_TIMEOUT.
#  - total (non-streaming): a bounded overall cap for the one non-streamed path
#    (tool calls), generous for slow HW but never "wait all day". SHAMSU_LLM_TIMEOUT.
LLM_CONNECT_TIMEOUT_SECONDS = _timeout_env("SHAMSU_LLM_CONNECT_TIMEOUT", 15.0)
LLM_IDLE_TIMEOUT_SECONDS = _timeout_env("SHAMSU_LLM_IDLE_TIMEOUT", 180.0)
LLM_TOTAL_TIMEOUT_SECONDS = _timeout_env("SHAMSU_LLM_TIMEOUT", 600.0)


def _streaming_timeout() -> httpx.Timeout:
    """No total cap - progress (any token within the idle window) keeps it alive;
    silence past the idle window trips ReadTimeout = stalled/deadlocked."""
    return httpx.Timeout(
        connect=LLM_CONNECT_TIMEOUT_SECONDS,
        read=LLM_IDLE_TIMEOUT_SECONDS,
        write=LLM_CONNECT_TIMEOUT_SECONDS,
        pool=LLM_CONNECT_TIMEOUT_SECONDS,
    )


def _blocking_timeout() -> httpx.Timeout:
    """Bounded overall cap for the non-streamed path, short connect to fail fast."""
    return httpx.Timeout(LLM_TOTAL_TIMEOUT_SECONDS, connect=LLM_CONNECT_TIMEOUT_SECONDS)


# Models that rejected `think: true` (older Ollama build, or a model without a
# thinking mode). Remembered per-process so the retry-without-think costs at
# most one 400 per model rather than one per call.
_THINK_UNSUPPORTED: set[str] = set()


class LLMStalledError(RuntimeError):
    """Raised when a local model produces no output for the idle window - a
    likely deadlock/stall, surfaced clearly instead of a raw httpx timeout."""

# Model assignment â€” see SHAMSU_model_architecture.md for full rationale.
OLLAMA_MODELS = SPECIALIST_MODELS

# Temperature per specialist â€” see ENGINEERING_HARNESS.md Â§1.
SPECIALIST_TEMPS = {
    "router": 0.0, "planner": 0.1, "coder": 0.1,
    "bugfix": 0.1, "bugfixer": 0.1, "reviewer": 0.2,
    "test_gen": 0.1, "test_agent": 0.1,
    "doc_agent": 0.4, "summarizer": 0.3, "qa": 0.2,
}

# Guards concurrent lazy pulls of the same model across LLMManager instances
# (e.g. two workflows both needing "coder" right after a fresh install).
_MODEL_PULL_LOCKS: dict[str, asyncio.Lock] = {}

# Models confirmed installed in THIS process. Presence cannot become false while
# we run, so re-shelling `ollama list` before every call bought nothing; see
# _ensure_model.
_MODELS_CONFIRMED_PRESENT: set[str] = set()


def _pull_lock_for(model_name: str) -> asyncio.Lock:
    lock = _MODEL_PULL_LOCKS.get(model_name)
    if lock is None:
        lock = asyncio.Lock()
        _MODEL_PULL_LOCKS[model_name] = lock
    return lock


@dataclass
class ModelPullProgress:
    """Optional hooks a caller can use to render UI around a lazy model pull."""

    on_start: Callable[[str], None] | None = None
    on_chunk: Callable[[str, str], None] | None = None
    on_finish: Callable[[str, bool], None] | None = None


ROUTER_SYSTEM_PROMPT = """You are SHAMSU's master task harness. Your ONLY job is to analyze
the user's request, choose the right mode, and output a routing decision as JSON.
You do NOT generate code. You do NOT explain anything.
You output ONLY valid JSON. Nothing else.

Available specialists:
- "planner"    -> breaks PRDs into project plans
- "coder"      -> generates and edits code, produces diffs
- "bugfixer"   -> analyzes errors and proposes fixes
- "reviewer"   -> reviews code for bugs, security, style
- "test_agent" -> generates unit and integration tests
- "doc_agent"  -> writes README, docstrings, API docs
- "summarizer" -> summarizes progress, writes reports
- "qa"         -> answers questions about the codebase

Mode guidance:
- bug reports, tracebacks, failed builds/tests, compiler logs -> "bug_fix"
- direct project edits/refactors/features -> "code_edit"
- requests for tests -> "test_gen"
- requests for README/docs -> "doc_gen"
- audits/reviews/security checks -> "audit"
- questions/explanations -> "qa" or "explain"

For steps, create a short execution handoff:
1. inspect/search relevant context,
2. execute with the appropriate specialist,
3. verify with tests/commands/review when safe.
Put likely files in target_files when the prompt names them.
Put deterministic tools in needs_tools, such as search_index, read_file, write_file, run_command.

Output schema:
{"intent": string, "complexity": "single"|"multi_step",
 "steps": [{"id": N, "specialist": string, "task": string}],
 "needs_tools": [string], "target_files": [string], "confidence": float}
"""

ROUTING_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": [
            "code_edit", "bug_fix", "audit", "generate",
            "explain", "test_gen", "doc_gen", "qa",
        ]},
        "complexity": {"type": "string", "enum": ["single", "multi_step"]},
        "steps": {"type": "array"},
        "needs_tools": {"type": "array", "items": {"type": "string"}},
        "target_files": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
    "required": ["intent", "complexity"],
}


class LLMManager(ILLMManager):
    def __init__(
        self,
        base_url: str = OLLAMA_BASE_URL,
        session_logger: SessionLogger | None = None,
        model_pull_progress: ModelPullProgress | None = None,
        action_ledger: ActionLedger | None = None,
        budget_manager: ContextBudgetManager | None = None,
        on_thinking: Callable[[str, str], None] | None = None,
    ):
        _validate_local_llm_url(base_url)
        self.base_url = base_url
        self.router_model = model_for_role("router")
        self.session_logger = session_logger
        self.action_ledger = action_ledger
        self.model_pull_progress = model_pull_progress
        self.budget_manager = budget_manager
        # Called with (model, thinking) whenever a reasoning model produces a
        # chain-of-thought, so the REPL can surface it instead of it living only
        # in the session log. Receives the FULL trace; the display side decides
        # how much to show.
        self.on_thinking = on_thinking

    async def _ensure_model(self, model_name: str) -> None:
        """Lazily pull `model_name` the first time it's actually needed."""
        # Local import: shamsu.runtime.ollama imports OLLAMA_BASE_URL from
        # this module at top level, so a module-level import here would cycle.
        from shamsu.runtime.ollama import (
            ensure_model_available,
            find_ollama_executable,
            list_installed_models,
        )

        ollama_path = find_ollama_executable()
        if ollama_path is None:
            return  # let _generate's HTTP call surface the real error

        # Confirmed-present models are remembered for the process: this ran an
        # `ollama list` subprocess before EVERY route/run_specialist call, which on
        # a single-model setup is pure per-call overhead for an answer that cannot
        # change once true. A model can only appear, not vanish, mid-process; a
        # deleted model surfaces as an HTTP error from the generate call itself.
        if model_name in _MODELS_CONFIRMED_PRESENT:
            return

        lock = _pull_lock_for(model_name)
        async with lock:
            if model_name in _MODELS_CONFIRMED_PRESENT:
                return
            installed = await asyncio.to_thread(list_installed_models, ollama_path)
            if model_name in installed:
                _MODELS_CONFIRMED_PRESENT.add(model_name)
                return
            if self.session_logger:
                self.session_logger.log(
                    "model.pull.started",
                    {"model": model_name},
                    f"Pulling missing model {model_name}",
                    workflow_id="model_pull",
                )
            if self.model_pull_progress and self.model_pull_progress.on_start:
                self.model_pull_progress.on_start(model_name)

            def _on_chunk(chunk: str) -> None:
                if self.model_pull_progress and self.model_pull_progress.on_chunk:
                    self.model_pull_progress.on_chunk(model_name, chunk)

            available = await asyncio.to_thread(
                ensure_model_available, ollama_path, model_name, _on_chunk
            )
            if available:
                _MODELS_CONFIRMED_PRESENT.add(model_name)
            if self.model_pull_progress and self.model_pull_progress.on_finish:
                self.model_pull_progress.on_finish(model_name, available)
            if self.session_logger:
                self.session_logger.log(
                    "model.pull.finished",
                    {"model": model_name, "success": available},
                    f"Pull finished for {model_name}" if available else f"Pull failed for {model_name}",
                    workflow_id="model_pull",
                )

    async def _stream_once(
        self, model: str, payload: dict, on_token: Callable[[str], None] | None = None
    ) -> tuple[str, str, int]:
        """One POST to /api/generate; returns (text, thinking, prompt_eval_count)."""
        chunks: list[str] = []
        thinking_chunks: list[str] = []
        prompt_eval_count = 0
        try:
            async with httpx.AsyncClient(timeout=_streaming_timeout()) as client:
                async with client.stream("POST", f"{self.base_url}/api/generate", json=payload) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if not line.strip():
                            continue
                        try:
                            data = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        token = data.get("response", "")
                        if token:
                            chunks.append(token)
                            if on_token is not None:
                                on_token(token)
                        # Reasoning models stream their chain-of-thought in a
                        # separate "thinking" field, not "response".
                        thinking_token = data.get("thinking", "")
                        if thinking_token:
                            thinking_chunks.append(thinking_token)
                        if data.get("done"):
                            prompt_eval_count = data.get("prompt_eval_count", 0)
                            break
        except httpx.ReadTimeout as exc:
            raise LLMStalledError(
                f"{model} produced no output for {LLM_IDLE_TIMEOUT_SECONDS:.0f}s "
                "(stalled or deadlocked). Set SHAMSU_LLM_IDLE_TIMEOUT to allow longer "
                "silences on slow hardware."
            ) from exc
        return "".join(chunks), "".join(thinking_chunks), prompt_eval_count

    async def _stream_completion(
        self,
        model: str,
        payload: dict,
        on_token: Callable[[str], None] | None = None,
        role: str = "",
    ) -> tuple[str, str, int]:
        """Stream a completion, asking reasoning models to separate their CoT.

        Streaming is used even when the caller wants one whole string: it lets
        the idle-timeout tell "slow" apart from "deadlocked" instead of blocking
        on one big non-streaming read.

        A reasoning model only fills the separate `thinking` field when asked
        (`think: true`). Without it, deepseek-r1 leaks `<think>` inline into the
        answer and `thinking` stays empty forever - which is why SHAMSU appeared
        to have no chain of thought at all on this path. Ollama builds or models
        that reject the flag get one retry without it, remembered per-model so it
        costs at most one 400 per model per process.
        """
        # Gated per CALL, not per model. With one model serving every role, the old
        # defence (send the router to a non-reasoning model) is gone, so a
        # mechanical role - routing, classification, extraction - must not pay for a
        # chain-of-thought pass just because the shared model is capable of one.
        want_think = (
            role_should_think(role, model) if role else model_is_reasoning(model)
        ) and model not in _THINK_UNSUPPORTED
        if not want_think:
            return await self._stream_once(model, payload, on_token)
        try:
            return await self._stream_once(model, {**payload, "think": True}, on_token)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code not in (400, 422):
                raise
            _THINK_UNSUPPORTED.add(model)
            return await self._stream_once(model, payload, on_token)

    async def _generate(
        self, model: str, system: str, prompt: str,
        temperature: float = 0.1, json_schema: dict | None = None,
        keep_alive: str = "10m", num_ctx: int = 8192,
        num_predict: int | None = None,
        _estimated_tokens: int = 0,
        _ledger_call_id: str = "",
        _role: str = "",
    ) -> str:
        payload = {
            "model": model,
            "prompt": prompt,
            "system": system,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx,
                "top_p": 0.9,
                "repeat_penalty": 1.0,
            },
            "keep_alive": keep_alive,
        }
        if num_predict is not None:
            payload["options"]["num_predict"] = int(num_predict)
        if json_schema is not None:
            payload["format"] = json_schema   # Ollama-native structured output
        text, thinking, prompt_eval_count = await self._stream_completion(
            model, payload, role=_role
        )
        # Capture the reasoning trace (surfaced/logged, kept out of the text).
        self._log_thinking(model, thinking, _ledger_call_id, _role)
        # Calibrate future token estimates with Ollama's ground-truth count.
        if self.budget_manager and _estimated_tokens > 0 and prompt_eval_count:
            self.budget_manager.calibrate_from_response(model, prompt_eval_count, _estimated_tokens)
        return text

    def _log_thinking(
        self, model: str, thinking: str, call_id: str = "", role: str = ""
    ) -> None:
        """Record a reasoning model's chain-of-thought trace if it produced one.

        Kept out of the returned text on purpose (answers/diffs must stay clean).
        The FULL trace goes to the run's `cot/` artifact via the ActionLedger;
        the session event deliberately carries only metadata plus the path, so
        raw reasoning never lands in the session timeline that other views read
        back. Also handed to `on_thinking` so the REPL can show a glimpse -
        logging it to a file alone made reasoning invisible to the person
        actually watching. Best-effort: never breaks a generation on a
        logging/display failure."""
        thinking = (thinking or "").strip()
        if not thinking:
            return
        cot_path = ""
        if self.action_ledger:
            try:
                cot_path = self.action_ledger.log_model_thinking(
                    call_id, role or "specialist", model, thinking
                )
            except Exception:
                pass
        if self.session_logger:
            try:
                self.session_logger.log(
                    "llm.thinking",
                    {
                        "model": model,
                        "thinking_chars": len(thinking),
                        "reasoning_available": True,
                        "cot_path": cot_path,
                    },
                    "Model reasoning metadata",
                    workflow_id="reasoning",
                )
            except Exception:
                pass
        if self.on_thinking:
            try:
                self.on_thinking(model, thinking)
            except Exception:
                pass

    async def chat_with_tools(
        self,
        model: str,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.1,
        keep_alive: str = "10m",
        num_ctx: int = 8192,
        role: str = "agent-chat-tools",
        workflow_id: str = "agent-chat-tools",
    ) -> dict:
        """Call Ollama's native chat endpoint with optional tool schemas."""
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx,
                "top_p": 0.9,
                "repeat_penalty": 1.0,
            },
            "keep_alive": keep_alive,
        }
        if tools:
            payload["tools"] = tools
        if self.session_logger:
            self.session_logger.log(
                "llm.request",
                {
                    "specialist": role,
                    "model": model,
                    "endpoint": f"{self.base_url}/api/chat",
                    "tool_count": len(tools or []),
                },
                "Chat tool request sent to local model",
                workflow_id=workflow_id,
            )
        ledger_call_id = ""
        if self.action_ledger:
            # Hand over the real request - system prompt, every message, and the
            # tool schemas - instead of a reconstruction from the last 8
            # messages, which silently dropped exactly the context needed to
            # explain why a model answered the way it did.
            ledger_call_id = self.action_ledger.log_model_call_started(
                role, model, messages=messages, tools=tools
            )
        started = time.perf_counter()
        try:
            async with httpx.AsyncClient(timeout=_blocking_timeout()) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
        except Exception as exc:
            if self.action_ledger:
                self.action_ledger.log_model_call_finished(
                    role,
                    model,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    call_id=ledger_call_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            raise
        content = ""
        message = data.get("message")
        if isinstance(message, dict):
            content = str(message.get("content") or "")
        if self.session_logger:
            self.session_logger.log(
                "llm.response",
                {
                    "specialist": role,
                    "model": model,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "response_chars": len(content),
                    "has_tool_calls": bool((message or {}).get("tool_calls") if isinstance(message, dict) else False),
                },
                "Chat tool response returned",
                workflow_id=workflow_id,
            )
        if self.action_ledger:
            self.action_ledger.log_model_call_finished(
                role,
                model,
                content,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                meta={"tool_count": len(tools or [])},
                call_id=ledger_call_id,
            )
        return data
    async def generate_structured(
        self,
        role: str,
        system: str,
        prompt: str,
        schema: dict,
        *,
        temperature: float = 0.1,
        num_predict: int | None = None,
    ) -> str:
        """Schema-constrained generation for a given role's model. Public seam
        used by the scaffold/freeform hole-fill and the strict repair proposer.
        Does not manage model pulls (the session ensures models at startup), so
        it is safe to call from a fresh event loop in a worker thread."""
        model = model_for_role(role)
        workflow_id = f"{role}-structured"
        if self.session_logger:
            self.session_logger.log(
                "llm.request",
                {
                    "specialist": role,
                    "model": model,
                    "endpoint": self.base_url,
                    "structured": True,
                    "schema_keys": sorted((schema or {}).keys()),
                    "num_predict": num_predict,
                },
                f"Structured request sent to {role}",
                workflow_id=workflow_id,
            )
        ledger_call_id = ""
        prompt_text = f"{system}\n\n{prompt}".strip()
        if self.action_ledger:
            ledger_call_id = self.action_ledger.log_model_call_started(
                role, model, prompt, system=system
            )
            self.action_ledger.log_context_preview(
                {
                    "task_id": workflow_id,
                    "specialist": role,
                    "token_estimate": 0,
                    "snippets": [
                        {
                            "path": "structured-prompt",
                            "content_preview": _short_preview(redact_text(prompt_text), 4000),
                        }
                    ],
                },
                model_call_id=ledger_call_id,
            )
        started = time.perf_counter()
        try:
            raw = await self._generate(
                model,
                system,
                prompt,
                temperature=temperature,
                json_schema=schema,
                num_predict=num_predict,
                _ledger_call_id=ledger_call_id,
                _role=role,
            )
        except BaseException as exc:
            if self.action_ledger:
                self.action_ledger.log_model_call_finished(
                    role,
                    model,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    call_id=ledger_call_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            if self.session_logger:
                self.session_logger.log(
                    "llm.error",
                    {"specialist": role, "model": model, "error": str(exc), "structured": True},
                    f"Structured request for {role} failed",
                    workflow_id=workflow_id,
                )
            raise
        if self.session_logger:
            self.session_logger.log(
                "llm.response",
                {
                    "specialist": role,
                    "model": model,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "response_chars": len(raw),
                    "structured": True,
                },
                f"Structured request for {role} returned a response",
                workflow_id=workflow_id,
            )
        if self.action_ledger:
            self.action_ledger.log_model_call_finished(
                role,
                model,
                raw,
                duration_ms=round((time.perf_counter() - started) * 1000, 2),
                meta={"structured": True, "schema_keys": sorted((schema or {}).keys())},
                call_id=ledger_call_id,
            )
        return raw

    async def route(self, prompt: str, project_summary: str) -> RoutingDecision:
        """
        Router stays loaded the whole session (keep_alive='-1' would be set
        at the Ollama Modelfile / env level â€” see ENGINEERING_HARNESS.md
        Â§8). temperature=0, schema-constrained â€” this is the harness's
        primary defense against malformed routing JSON, not a try/except.
        """
        started = time.perf_counter()
        await self._ensure_model(self.router_model)
        user_msg = f"USER PROMPT: {prompt}\n\nPROJECT: {project_summary}"
        if self.session_logger:
            self.session_logger.log(
                "llm.request",
                {"specialist": "router", "model": self.router_model, "endpoint": self.base_url},
                "Routing request sent to local model",
                workflow_id="router",
            )
        router_call_id = ""
        if self.action_ledger:
            router_call_id = self.action_ledger.log_model_call_started(
                "router", self.router_model, user_msg, system=ROUTER_SYSTEM_PROMPT
            )
        raw = await self._generate(
            self.router_model, ROUTER_SYSTEM_PROMPT, user_msg,
            temperature=0.0, json_schema=ROUTING_JSON_SCHEMA, keep_alive="-1",
            _ledger_call_id=router_call_id, _role="router",
        )
        if self.action_ledger:
            self.action_ledger.log_model_call_finished(
                "router", self.router_model, raw, duration_ms=round((time.perf_counter() - started) * 1000, 2),
                call_id=router_call_id,
            )
        decision = self._parse_routing(raw)
        if decision is not None:
            self._log_route_decision(decision, started, retry_count=0)
            return decision

        # One retry with explicit correction, per harness Â§4 retry phrasing.
        retry_prompt = (
            f"Your previous output failed JSON validation. "
            f"Output ONLY corrected JSON matching the schema. "
            f"Do not include any prose, markdown, or code fences.\n\n{user_msg}"
        )
        retry_started = time.perf_counter()
        retry_call_id = ""
        if self.action_ledger:
            retry_call_id = self.action_ledger.log_model_call_started("router", self.router_model, retry_prompt)
        raw2 = await self._generate(
            self.router_model, ROUTER_SYSTEM_PROMPT, retry_prompt,
            temperature=0.0, json_schema=ROUTING_JSON_SCHEMA, keep_alive="-1",
        )
        if self.action_ledger:
            self.action_ledger.log_model_call_finished(
                "router", self.router_model, raw2, duration_ms=round((time.perf_counter() - retry_started) * 1000, 2),
                call_id=retry_call_id,
            )
        decision = self._parse_routing(raw2)
        if decision is not None:
            self._log_route_decision(decision, started, retry_count=1)
            return decision

        # Safe fallback â€” never crash the conversation on a routing failure.
        if self.session_logger:
            self.session_logger.log(
                "llm.error",
                {"specialist": "router", "model": self.router_model, "retry_count": 1},
                "Router output could not be parsed; falling back to QA",
                workflow_id="router",
            )
        if self.action_ledger:
            self.action_ledger.log_decision(
                "router_fallback_to_qa",
                reason_summary="Router output failed JSON validation twice; falling back to a safe QA routing.",
                evidence=[f"router_model: {self.router_model}"],
                chosen_action="route_as_qa",
                confidence=0.3,
                outcome="fallback",
            )
        return RoutingDecision(
            intent="qa", complexity="single",
            steps=[{"id": 1, "specialist": "qa", "task": prompt}],
            needs_tools=["search"], target_files=[], confidence=0.3,
        )

    @staticmethod
    def _parse_routing(raw: str) -> RoutingDecision | None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                data = repair_json(raw, return_objects=True)
            except Exception:
                return None
        if not isinstance(data, dict) or "intent" not in data:
            return None
        try:
            return RoutingDecision(
                intent=data["intent"],
                complexity=data.get("complexity", "single"),
                steps=data.get("steps", []),
                needs_tools=data.get("needs_tools", []),
                target_files=data.get("target_files", []),
                confidence=float(data.get("confidence", 0.5)),
            )
        except (KeyError, ValueError, TypeError):
            return None

    async def run_specialist(self, specialist: str, pack: ContextPack) -> LLMResponse:
        model_name = model_for_role(specialist)
        await self._ensure_model(model_name)
        temp = SPECIALIST_TEMPS.get(specialist, 0.2)
        pack = self._with_long_term_memory(pack, specialist)
        prompt = self._format_pack(pack)

        # Budget check: estimate tokens, compact if near the limit, show indicator.
        estimated_tokens = 0
        if self.budget_manager:
            budget = self.budget_manager.compute(model_name, specialist, prompt)
            if self.budget_manager.should_compact(budget):
                pack = self.budget_manager.compact_pack(pack, budget)
                prompt = self._format_pack(pack)
                budget = self.budget_manager.compute(model_name, specialist, prompt)
                budget = replace(budget, compacted=True)
            self.budget_manager.show_indicator(budget)
            estimated_tokens = budget.estimated_tokens

        started = time.perf_counter()
        if self.session_logger:
            self.session_logger.log_context_pack(pack, workflow_id=pack.task_id)
            self.session_logger.log(
                "llm.request",
                {
                    "specialist": specialist,
                    "model": model_name,
                    "endpoint": self.base_url,
                    "prompt_token_estimate": pack.token_estimate,
                },
                f"Specialist request sent to {specialist}",
                workflow_id=pack.task_id,
            )
        ledger_call_id = ""
        if self.action_ledger:
            ledger_call_id = self.action_ledger.log_model_call_started(specialist, model_name, prompt)
            self.action_ledger.log_context_preview(
                _context_preview(pack, self.action_ledger.workspace),
                model_call_id=ledger_call_id,
            )
        try:
            raw = await self._generate(
                model_name, "", prompt, temperature=temp,
                _estimated_tokens=estimated_tokens,
                _ledger_call_id=ledger_call_id, _role=specialist,
            )
        except Exception as exc:
            if self.action_ledger:
                self.action_ledger.log_model_call_finished(
                    specialist,
                    model_name,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    call_id=ledger_call_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            if self.session_logger:
                self.session_logger.log(
                    "llm.error",
                    {"specialist": specialist, "model": model_name, "error": str(exc)},
                    f"Specialist {specialist} failed",
                    workflow_id=pack.task_id,
                )
            raise
        if self.session_logger:
            self.session_logger.log(
                "llm.response",
                {
                    "specialist": specialist,
                    "model": model_name,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "response_chars": len(raw),
                    "retry_count": 0,
                },
                f"Specialist {specialist} returned a response",
                workflow_id=pack.task_id,
            )
        if self.action_ledger:
            self.action_ledger.log_model_call_finished(
                specialist, model_name, raw, duration_ms=round((time.perf_counter() - started) * 1000, 2),
                call_id=ledger_call_id,
            )
        return LLMResponse(raw=raw, format="text", model_used=model_name)

    async def _generate_stream(
        self, model: str, system: str, prompt: str,
        on_token: Callable[[str], None],
        temperature: float = 0.1, keep_alive: str = "10m", num_ctx: int = 8192,
        _estimated_tokens: int = 0,
        _ledger_call_id: str = "",
        _role: str = "",
    ) -> str:
        payload = {
            "model": model,
            "prompt": prompt,
            "system": system,
            "stream": True,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx,
                "top_p": 0.9,
                "repeat_penalty": 1.0,
            },
            "keep_alive": keep_alive,
        }
        # Reasoning stays out of the visible answer tokens: `on_token` only ever
        # sees "response" chunks, never "thinking" ones.
        text, thinking, prompt_eval_count = await self._stream_completion(
            model, payload, on_token, role=_role
        )
        self._log_thinking(model, thinking, _ledger_call_id, _role)
        # Same calibration as the non-streaming path (see _generate) - the
        # final streamed chunk carries the same ground-truth prompt_eval_count.
        if self.budget_manager and _estimated_tokens > 0 and prompt_eval_count:
            self.budget_manager.calibrate_from_response(model, prompt_eval_count, _estimated_tokens)
        return text

    async def run_specialist_stream(
        self, specialist: str, pack: ContextPack, on_token: Callable[[str], None]
    ) -> LLMResponse:
        """Like run_specialist, but streams tokens to on_token as they arrive.

        Falls back cleanly: the caller still gets the full text in the returned
        LLMResponse, so a non-streaming renderer can print it after the fact.
        """
        model_name = model_for_role(specialist)
        await self._ensure_model(model_name)
        temp = SPECIALIST_TEMPS.get(specialist, 0.2)
        pack = self._with_long_term_memory(pack, specialist)
        prompt = self._format_pack(pack)

        # Budget check: estimate tokens, compact if near the limit, show
        # indicator. Mirrors run_specialist - streaming must not skip this,
        # otherwise the context/token indicator never renders for the
        # streaming path (QA, PRD summaries, ...) and an over-budget pack is
        # never compacted before being sent.
        estimated_tokens = 0
        if self.budget_manager:
            budget = self.budget_manager.compute(model_name, specialist, prompt)
            if self.budget_manager.should_compact(budget):
                pack = self.budget_manager.compact_pack(pack, budget)
                prompt = self._format_pack(pack)
                budget = self.budget_manager.compute(model_name, specialist, prompt)
                budget = replace(budget, compacted=True)
            self.budget_manager.show_indicator(budget)
            estimated_tokens = budget.estimated_tokens

        started = time.perf_counter()
        if self.session_logger:
            self.session_logger.log_context_pack(pack, workflow_id=pack.task_id)
            self.session_logger.log(
                "llm.request",
                {
                    "specialist": specialist,
                    "model": model_name,
                    "endpoint": self.base_url,
                    "prompt_token_estimate": pack.token_estimate,
                    "streaming": True,
                },
                f"Specialist request sent to {specialist} (streaming)",
                workflow_id=pack.task_id,
            )
        ledger_call_id = ""
        if self.action_ledger:
            ledger_call_id = self.action_ledger.log_model_call_started(specialist, model_name, prompt)
            self.action_ledger.log_context_preview(
                _context_preview(pack, self.action_ledger.workspace),
                model_call_id=ledger_call_id,
            )
        try:
            raw = await self._generate_stream(
                model_name, "", prompt, on_token, temperature=temp,
                _estimated_tokens=estimated_tokens,
                _ledger_call_id=ledger_call_id, _role=specialist,
            )
        except Exception as exc:
            if self.action_ledger:
                self.action_ledger.log_model_call_finished(
                    specialist,
                    model_name,
                    duration_ms=round((time.perf_counter() - started) * 1000, 2),
                    call_id=ledger_call_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
            if self.session_logger:
                self.session_logger.log(
                    "llm.error",
                    {"specialist": specialist, "model": model_name, "error": str(exc)},
                    f"Specialist {specialist} failed",
                    workflow_id=pack.task_id,
                )
            raise
        if self.session_logger:
            self.session_logger.log(
                "llm.response",
                {
                    "specialist": specialist,
                    "model": model_name,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "response_chars": len(raw),
                    "retry_count": 0,
                },
                f"Specialist {specialist} returned a streamed response",
                workflow_id=pack.task_id,
            )
        if self.action_ledger:
            self.action_ledger.log_model_call_finished(
                specialist, model_name, raw, duration_ms=round((time.perf_counter() - started) * 1000, 2),
                call_id=ledger_call_id,
            )
        return LLMResponse(raw=raw, format="text", model_used=model_name)

    def _with_long_term_memory(self, pack: ContextPack, specialist: str) -> ContextPack:
        if not self.session_logger:
            return pack
        try:
            workspace = self.session_logger.metadata.workspace
            memory_context = MemoryService(Path(workspace)).render_relevant(
                pack.user_request,
                task_type=specialist,
                limit=8,
            )
        except Exception:
            return pack
        if not memory_context:
            return pack
        prd_context = f"{memory_context}\n\n{pack.prd_context}" if pack.prd_context else memory_context
        self.session_logger.log(
            "memory.retrieved",
            {"specialist": specialist, "has_memory": True},
            "Retrieved relevant Graphiti memories",
            workflow_id=pack.task_id,
        )
        return replace(pack, prd_context=prd_context)
    def _log_route_decision(
        self,
        decision: RoutingDecision,
        started: float,
        retry_count: int,
    ) -> None:
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        if self.session_logger:
            self.session_logger.log(
                "router.decision",
                {
                    "intent": decision.intent,
                    "complexity": decision.complexity,
                    "confidence": decision.confidence,
                    "retry_count": retry_count,
                    "duration_ms": duration_ms,
                },
                f"Routed prompt as {decision.intent}",
                workflow_id="router",
            )
        if self.action_ledger:
            self.action_ledger.log_decision(
                f"route_prompt_as_{decision.intent}",
                reason_summary=f"Router model selected intent '{decision.intent}' with complexity '{decision.complexity}'.",
                evidence=[f"router_model: {self.router_model}", f"retry_count: {retry_count}"],
                chosen_action=f"route as {decision.intent}",
                confidence=decision.confidence,
                outcome="routed",
            )

    @staticmethod
    def _format_pack(pack: ContextPack) -> str:
        # Only emit a section when it actually has content. A small local model
        # fed empty "## Relevant code" / "## Errors" scaffolding on every plain
        # question wastes context and can hallucinate references to code that
        # isn't there; omitting empty sections keeps the prompt honest.
        sections: list[str] = []
        if pack.snippets:
            snippets_text = "\n\n".join(
                f"# File: {s.file_path} (lines {s.line_start}-{s.line_end})\n{s.content}"
                for s in pack.snippets
            )
            sections.append(f"## Relevant code\n{snippets_text}")
        if pack.prd_context and pack.prd_context.strip():
            sections.append(f"## Context\n{pack.prd_context.strip()}")
        if pack.error_context and pack.error_context.strip():
            sections.append(f"## Errors / test output\n{pack.error_context.strip()}")
        # Task restated at the very end - exploits the recency side of the
        # "Lost in the Middle" U-curve (Liu et al., TACL 2024). See
        # ENGINEERING_HARNESS.md section 1.
        sections.append(f"## Task (read this carefully)\n{pack.user_request}")
        return "\n\n".join(sections) + "\n"


def _context_preview(pack: ContextPack, workspace: Path | None = None) -> dict:
    """Safe, human-inspectable summary of a ContextPack for ActionLedger -
    never fed back into a model, see shamsu/action_ledger/ledger.py."""
    snippets = []
    for item in pack.snippets:
        mtime = None
        if workspace is not None:
            try:
                candidate = (workspace / item.file_path).resolve()
                if candidate.is_relative_to(workspace.resolve()) and candidate.is_file():
                    mtime = candidate.stat().st_mtime
            except OSError:
                mtime = None
        snippets.append(
            {
                "file_path": item.file_path,
                "line_start": item.line_start,
                "line_end": item.line_end,
                "symbol_name": item.symbol_name,
                "retrieval_score": item.score,
                "inclusion_reason": "retrieval_result",
                "content_sha256": hashlib.sha256(item.content.encode("utf-8")).hexdigest(),
                "file_mtime": mtime,
                "content_preview": redact_text(item.content)[:4000],
            }
        )
    index_metadata: dict = {}
    if workspace is not None:
        try:
            from shamsu.abstract.service import AbstractService

            index_metadata = AbstractService(workspace).index_metadata()
        except Exception:
            index_metadata = {}
    return {
        "task_id": pack.task_id,
        "step_id": pack.step_id,
        "specialist": pack.specialist,
        "token_estimate": pack.token_estimate,
        "prompt_preview": pack.user_request[:500],
        "prompt_sha256": hashlib.sha256(pack.user_request.encode("utf-8")).hexdigest(),
        "system_prompt_version": "specialist-pack-v1",
        "snippets": snippets,
        "context": {
            "user_request": redact_text(pack.user_request)[:8000],
            "prd_context": redact_text(pack.prd_context)[:16000],
            "error_context": redact_text(pack.error_context)[:8000],
        },
        "code_memory_index": index_metadata,
        "omitted_context": {
            "user_request_chars": max(0, len(pack.user_request) - 8000),
            "prd_context_chars": max(0, len(pack.prd_context) - 16000),
            "error_context_chars": max(0, len(pack.error_context) - 8000),
        },
    }


def _short_preview(text: str, limit: int) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars]"


def _validate_local_llm_url(base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid Ollama URL: {base_url}")
    if parsed.hostname not in LOCAL_LLM_HOSTS:
        raise ValueError(
            "SHAMSU only supports local Ollama endpoints. "
            f"Refusing non-local LLM URL: {base_url}"
        )




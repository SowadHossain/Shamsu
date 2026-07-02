"""
shamsu/llm/manager.py — Dev B owns this file.

Implements ILLMManager. Encodes the harness defaults directly:
  - keep_alive=-1 (router never unloads; see ENGINEERING_HARNESS.md §8)
  - temperature=0 for routing/JSON, 0.1 for code, per SPECIALIST_TEMPS
  - Ollama's native `format` schema param for structured output —
    this is the PRIMARY mechanism, not a library. json_repair is the
    fallback when schema enforcement still produces something invalid
    (rare, but happens under quantization-induced glitches).
  - num_ctx=8192 default — drop to 4096 if you observe swapping
    (see harness §8, "num_ctx tradeoffs").

This file does NOT manage model loading/unloading lifecycle yet —
that's the Day 8 ModelManager work (see WEEK3_PLAN.md Day 8, Dev B).
For Day 1-3 scaffolding, run_specialist() always targets the model
named in OLLAMA_MODELS[specialist] and trusts Ollama's own keep_alive
to decide what's resident.
"""
from __future__ import annotations

import json
import time
from urllib.parse import urlparse

import httpx
from json_repair import repair_json

from shamsu.interfaces import ILLMManager
from shamsu.runtime.models import SPECIALIST_MODELS
from shamsu.session.manager import SessionLogger
from shamsu.types import ContextPack, LLMResponse, RoutingDecision

OLLAMA_BASE_URL = "http://localhost:11434"
LOCAL_LLM_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Model assignment — see SHAMSU_model_architecture.md for full rationale.
OLLAMA_MODELS = SPECIALIST_MODELS

# Temperature per specialist — see ENGINEERING_HARNESS.md §1.
SPECIALIST_TEMPS = {
    "router": 0.0, "planner": 0.1, "coder": 0.1,
    "bugfix": 0.1, "bugfixer": 0.1, "reviewer": 0.2,
    "test_gen": 0.1, "test_agent": 0.1,
    "doc_agent": 0.4, "summarizer": 0.3, "qa": 0.2,
}

ROUTER_SYSTEM_PROMPT = """You are SHAMSU's routing brain. Your ONLY job is to classify
the user's request and output a routing decision as JSON.
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
    def __init__(self, base_url: str = OLLAMA_BASE_URL, session_logger: SessionLogger | None = None):
        _validate_local_llm_url(base_url)
        self.base_url = base_url
        self.router_model = OLLAMA_MODELS["router"]
        self.session_logger = session_logger

    async def _generate(
        self, model: str, system: str, prompt: str,
        temperature: float = 0.1, json_schema: dict | None = None,
        keep_alive: str = "10m", num_ctx: int = 8192,
    ) -> str:
        payload = {
            "model": model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_ctx": num_ctx,
                "top_p": 0.9,
                "repeat_penalty": 1.0,
            },
            "keep_alive": keep_alive,
        }
        if json_schema is not None:
            payload["format"] = json_schema   # Ollama-native structured output
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{self.base_url}/api/generate", json=payload)
            r.raise_for_status()
            return r.json().get("response", "")

    async def route(self, prompt: str, project_summary: str) -> RoutingDecision:
        """
        Router stays loaded the whole session (keep_alive='-1' would be set
        at the Ollama Modelfile / env level — see ENGINEERING_HARNESS.md
        §8). temperature=0, schema-constrained — this is the harness's
        primary defense against malformed routing JSON, not a try/except.
        """
        started = time.perf_counter()
        user_msg = f"USER PROMPT: {prompt}\n\nPROJECT: {project_summary}"
        if self.session_logger:
            self.session_logger.log(
                "llm.request",
                {"specialist": "router", "model": self.router_model, "endpoint": self.base_url},
                "Routing request sent to local model",
                workflow_id="router",
            )
        raw = await self._generate(
            self.router_model, ROUTER_SYSTEM_PROMPT, user_msg,
            temperature=0.0, json_schema=ROUTING_JSON_SCHEMA, keep_alive="-1",
        )
        decision = self._parse_routing(raw)
        if decision is not None:
            self._log_route_decision(decision, started, retry_count=0)
            return decision

        # One retry with explicit correction, per harness §4 retry phrasing.
        retry_prompt = (
            f"Your previous output failed JSON validation. "
            f"Output ONLY corrected JSON matching the schema. "
            f"Do not include any prose, markdown, or code fences.\n\n{user_msg}"
        )
        raw2 = await self._generate(
            self.router_model, ROUTER_SYSTEM_PROMPT, retry_prompt,
            temperature=0.0, json_schema=ROUTING_JSON_SCHEMA, keep_alive="-1",
        )
        decision = self._parse_routing(raw2)
        if decision is not None:
            self._log_route_decision(decision, started, retry_count=1)
            return decision

        # Safe fallback — never crash the conversation on a routing failure.
        if self.session_logger:
            self.session_logger.log(
                "llm.error",
                {"specialist": "router", "model": self.router_model, "retry_count": 1},
                "Router output could not be parsed; falling back to QA",
                workflow_id="router",
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
        model_name = OLLAMA_MODELS.get(specialist) or self.router_model
        temp = SPECIALIST_TEMPS.get(specialist, 0.2)
        prompt = self._format_pack(pack)
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
        try:
            raw = await self._generate(model_name, "", prompt, temperature=temp)
        except Exception as exc:
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
        return LLMResponse(raw=raw, format="text", model_used=model_name)

    def _log_route_decision(
        self,
        decision: RoutingDecision,
        started: float,
        retry_count: int,
    ) -> None:
        if not self.session_logger:
            return
        self.session_logger.log(
            "router.decision",
            {
                "intent": decision.intent,
                "complexity": decision.complexity,
                "confidence": decision.confidence,
                "retry_count": retry_count,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
            f"Routed prompt as {decision.intent}",
            workflow_id="router",
        )

    @staticmethod
    def _format_pack(pack: ContextPack) -> str:
        snippets_text = "\n\n".join(
            f"# File: {s.file_path} (lines {s.line_start}-{s.line_end})\n{s.content}"
            for s in pack.snippets
        )
        # Task restated at the very end — exploits the recency side of the
        # "Lost in the Middle" U-curve (Liu et al., TACL 2024). See
        # ENGINEERING_HARNESS.md §1.
        return f"""## Relevant code
{snippets_text}

## Context
{pack.prd_context}

## Errors / test output
{pack.error_context}

## Task (read this carefully)
{pack.user_request}
"""


def _validate_local_llm_url(base_url: str) -> None:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"Invalid Ollama URL: {base_url}")
    if parsed.hostname not in LOCAL_LLM_HOSTS:
        raise ValueError(
            "SHAMSU only supports local Ollama endpoints. "
            f"Refusing non-local LLM URL: {base_url}"
        )

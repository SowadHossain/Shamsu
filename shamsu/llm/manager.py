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
import httpx
from json_repair import repair_json

from shamsu.interfaces import ILLMManager
from shamsu.types import ContextPack, LLMResponse, RoutingDecision

OLLAMA_BASE_URL = "http://localhost:11434"

# Model assignment — see SHAMSU_model_architecture.md for full rationale.
OLLAMA_MODELS = {
    "router": "phi3:mini-4k-instruct",
    "coder": "qwen2.5-coder:7b-instruct-q4_K_M",
    "bugfixer": "deepseek-coder:6.7b-instruct-q4_K_M",
    "reviewer": "mistral:7b-instruct-q4_K_M",
    "test_agent": "qwen2.5-coder:7b-instruct-q4_K_M",
    "planner": None,      # null = reuse router model, no swap
    "doc_agent": None,
    "summarizer": None,
}

# Temperature per specialist — see ENGINEERING_HARNESS.md §1.
SPECIALIST_TEMPS = {
    "router": 0.0, "planner": 0.1, "coder": 0.1,
    "bugfixer": 0.1, "reviewer": 0.2, "test_agent": 0.1,
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
    def __init__(self, base_url: str = OLLAMA_BASE_URL):
        self.base_url = base_url
        self.router_model = OLLAMA_MODELS["router"]

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
        user_msg = f"USER PROMPT: {prompt}\n\nPROJECT: {project_summary}"
        raw = await self._generate(
            self.router_model, ROUTER_SYSTEM_PROMPT, user_msg,
            temperature=0.0, json_schema=ROUTING_JSON_SCHEMA, keep_alive="-1",
        )
        decision = self._parse_routing(raw)
        if decision is not None:
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
            return decision

        # Safe fallback — never crash the conversation on a routing failure.
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
        raw = await self._generate(model_name, "", prompt, temperature=temp)
        return LLMResponse(raw=raw, format="text", model_used=model_name)

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

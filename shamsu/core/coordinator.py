"""
Thin coordinator for Day 1 wiring.

It routes a user request through the LLM manager, then prepares the first
workflow preview. Live specialist execution comes later; this gives the CLI and
tests a real shared surface immediately.
"""
from __future__ import annotations

from dataclasses import dataclass

from shamsu.agents.qa_workflow import QAWorkflow
from shamsu.interfaces import ILLMManager
from shamsu.llm.manager import LLMManager
from shamsu.types import RoutingDecision


@dataclass
class CoordinatorResult:
    decision: RoutingDecision
    preview: str
    answer: str = ""
    model_used: str = ""
    fallback_reason: str = ""


class Coordinator:
    def __init__(
        self,
        llm: ILLMManager | None = None,
        qa_workflow: QAWorkflow | None = None,
        project_summary: str = "No indexed project summary yet.",
    ):
        self.llm = llm or LLMManager()
        self.qa_workflow = qa_workflow or QAWorkflow()
        self.project_summary = project_summary

    async def handle(self, user_input: str) -> CoordinatorResult:
        try:
            decision = await self.llm.route(user_input, self.project_summary)
        except Exception:
            decision = RoutingDecision(
                intent="qa",
                complexity="single",
                steps=[{"id": 1, "specialist": "qa", "task": user_input}],
                needs_tools=["search"],
                confidence=0.2,
            )
        preview = ""
        answer = ""
        model_used = ""
        fallback_reason = ""
        if decision.intent in {"qa", "explain"}:
            try:
                qa_answer = await self.qa_workflow.answer(user_input, self.llm)
                preview = qa_answer.prompt
                answer = qa_answer.answer
                model_used = qa_answer.model_used
            except Exception as exc:
                preview = self.qa_workflow.build_prompt(user_input).prompt
                fallback_reason = f"Live QA unavailable: {exc}"
        return CoordinatorResult(
            decision=decision,
            preview=preview,
            answer=answer,
            model_used=model_used,
            fallback_reason=fallback_reason,
        )

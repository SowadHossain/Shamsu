"""
Question-answering workflow preview.

Day 1 scope wires search -> context packing -> prompt assembly without making a
live specialist call. That keeps the workflow testable on machines without
Ollama while preserving the final prompt shape.
"""
from __future__ import annotations

from dataclasses import dataclass

from shamsu.context.builder import ContextBuilder
from shamsu.interfaces import IContextBuilder, ILLMManager, ISearchAgent
from shamsu.llm.manager import LLMManager
from shamsu.retriever.search import SearchAgentStub
from shamsu.types import ContextPack


@dataclass
class QAPreview:
    pack: ContextPack
    prompt: str


@dataclass
class QAAnswer:
    pack: ContextPack
    prompt: str
    answer: str
    model_used: str


class QAWorkflow:
    def __init__(
        self,
        search: ISearchAgent | None = None,
        context_builder: IContextBuilder | None = None,
    ):
        self.search = search or SearchAgentStub()
        self.context_builder = context_builder or ContextBuilder()

    def build_prompt(self, request: str, task_id: str = "qa-preview") -> QAPreview:
        results = self.search.search(request, top_k=5)
        pack = self.context_builder.pack(
            results=results,
            request=request,
            task_id=task_id,
            step_id=1,
            specialist="qa",
        )
        return QAPreview(pack=pack, prompt=LLMManager._format_pack(pack))

    async def answer(
        self,
        request: str,
        llm: ILLMManager,
        task_id: str = "qa-live",
    ) -> QAAnswer:
        preview = self.build_prompt(request, task_id=task_id)
        response = await llm.run_specialist("qa", preview.pack)
        return QAAnswer(
            pack=preview.pack,
            prompt=preview.prompt,
            answer=response.raw.strip(),
            model_used=response.model_used,
        )

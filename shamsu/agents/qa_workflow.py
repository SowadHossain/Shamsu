"""
Question-answering workflow preview.

Day 1 scope wires search -> context packing -> prompt assembly without making a
live specialist call. That keeps the workflow testable on machines without
Ollama while preserving the final prompt shape.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from shamsu.context.builder import ContextBuilder
from shamsu.interfaces import IContextBuilder, ILLMManager, ISearchAgent
from shamsu.llm.manager import LLMManager
from shamsu.retriever.search import NullSearchAgent
from shamsu.types import ContextPack, SearchResult

NO_LIVE_TOOLS_NOTICE = (
    "Answer using ONLY the workspace context already provided above (file listings, "
    "code snippets, and conversation). Those workspace files ARE available to you as "
    "context — do NOT claim you 'cannot access files'; read and use what is shown. "
    "If a Mentioned file context block is present, treat its # @path header and "
    "content as authoritative; cite that path exactly and do not invent absolute "
    "paths or files that are not shown. "
    "In this reply you cannot fetch new data or run tools, so if the question needs "
    "real-time external info (weather, news, prices) or an action beyond answering "
    "from the given context, say so briefly. Never claim you searched the web, ran "
    "code, or edited files unless a tool result is actually shown."
)

MENTIONED_FILE_BLOCK_RE = re.compile(
    r"(?ms)^# @(?P<path>[^\r\n]+?) \(file\)\r?\n(?P<content>.*?)(?=^\s*# @|\Z)"
)


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
        self.search = search or NullSearchAgent()
        self.context_builder = context_builder or ContextBuilder()

    def build_prompt(self, request: str, task_id: str = "qa-preview") -> QAPreview:
        mentioned_results = _mentioned_file_results_from_request(request)
        results = mentioned_results + self.search.search(request, top_k=5)
        pack = self.context_builder.pack(
            results=results,
            request=request,
            task_id=task_id,
            step_id=1,
            specialist="qa",
        )
        pack.prd_context = NO_LIVE_TOOLS_NOTICE
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


def _mentioned_file_results_from_request(request: str) -> list[SearchResult]:
    results: list[SearchResult] = []
    seen: set[str] = set()
    for match in MENTIONED_FILE_BLOCK_RE.finditer(request or ""):
        path = match.group("path").strip()
        content = match.group("content").strip("\n")
        if not path or not content or path in seen:
            continue
        seen.add(path)
        results.append(
            SearchResult(
                file_path=path,
                language=path.rsplit(".", 1)[-1] if "." in path else "text",
                line_start=1,
                line_end=max(1, len(content.splitlines())),
                content=content,
                score=50.0,
                symbol_name=path,
            )
        )
    return results

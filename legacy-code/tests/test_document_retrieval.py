from __future__ import annotations

import json
from pathlib import Path

from shamsu.action_ledger.context import clear_current_run, set_current_run
from shamsu.action_ledger.ledger import start_run
from shamsu.agents.task_harness import append_task_handoff, build_task_plan
from shamsu.retriever.documents import DocumentStore
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.types import RoutingDecision


def _register_large_manual(tmp_path: Path, *, approval=True) -> tuple[AgentToolRegistry, dict]:
    paragraphs = [
        "# Acme Platform",
        "The platform introduction explains accounts and workspaces.",
        "## Authentication",
        "Create a session by calling `AcmeClient.login(api_key)`. "
        "The returned access token expires after sixty minutes.",
        "## Widgets",
        "Create widgets with `client.widgets.create(name=...)` and list them "
        "with `client.widgets.list()`.",
        "## Webhooks",
        "Webhook deliveries use an HMAC-SHA256 signature in the X-Acme-Signature header.",
    ]
    source = tmp_path / "acme-manual.md"
    source.write_text("\n\n".join(paragraphs) + "\n" + ("Appendix details. " * 900), encoding="utf-8")
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: approval)
    result = registry.ingest_docs("acme-manual.md", "Acme Platform")
    return registry, result.data


def test_large_markdown_is_registered_as_chunked_document(tmp_path: Path):
    approvals = []
    source = tmp_path / "manual.md"
    source.write_text("# Manual\n\n" + ("Configuration details. " * 900), encoding="utf-8")
    registry = AgentToolRegistry(
        tmp_path,
        approval_func=lambda request: approvals.append(request) or True,
    )

    result = registry.ingest_docs("manual.md", "Operations Manual")

    assert result.ok is True
    assert result.data["mode"] == "document"
    assert result.data["chunks"] > 1
    assert result.data["transaction_id"]
    assert len(approvals) == 1
    path = tmp_path / result.data["document_path"]
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["name"] == "Operations Manual"
    assert len(payload["chunks"]) == result.data["chunks"]
    assert not (tmp_path / ".shamsu" / "skills").exists()


def test_keyword_search_returns_relevant_section_with_line_citation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHAMSU_SEMANTIC_SEARCH", "0")
    registry, _ = _register_large_manual(tmp_path)

    result = registry.search_docs(
        "How do webhook signatures work?",
        "Acme Platform",
        top_k=2,
    )

    assert result.ok is True
    assert result.data["semantic_used"] is False
    assert result.data["results"]
    hit = result.data["results"][0]
    assert "HMAC-SHA256" in hit["text"]
    assert hit["section"] == "Webhooks"
    assert "line " in hit["citation"]
    assert "section 'Webhooks'" in hit["citation"]


def test_semantic_search_reranks_when_keyword_terms_do_not_match(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("SHAMSU_SEMANTIC_SEARCH", raising=False)

    def embed(texts: list[str]) -> list[list[float]]:
        vectors = []
        for text in texts:
            lowered = text.lower()
            vectors.append(
                [
                    float("login" in lowered or "identity" in lowered),
                    float("widget" in lowered),
                ]
            )
        return vectors

    prepared = DocumentStore(tmp_path).prepare_text(
        "# Sessions\n\nCall login to receive a token.\n\n# Widgets\n\nCreate a widget.",
        source="manual.md",
        source_kind="local",
        name="Acme",
    )
    target = tmp_path / prepared.relative_path
    target.parent.mkdir(parents=True)
    target.write_text(prepared.json_content, encoding="utf-8")
    store = DocumentStore(tmp_path, embed=embed)

    result = store.search("identity management", document="Acme", top_k=1)

    assert result.semantic_used is True
    assert result.hits
    assert "login" in result.hits[0].chunk.text.lower()
    assert "semantic" in result.hits[0].match_kind


def test_document_chunk_embeddings_are_persisted_and_reused(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv("SHAMSU_SEMANTIC_SEARCH", raising=False)
    prepared = DocumentStore(tmp_path).prepare_text(
        "# Sessions\n\nCall login to receive a token.",
        source="manual.md",
        source_kind="local",
        name="Acme",
    )
    target = tmp_path / prepared.relative_path
    target.parent.mkdir(parents=True)
    target.write_text(prepared.json_content, encoding="utf-8")

    def initial_embed(texts: list[str]) -> list[list[float]]:
        return [
            [float("login" in text.lower() or "identity" in text.lower()), 0.0]
            for text in texts
        ]

    first = DocumentStore(tmp_path, embed=initial_embed)
    assert first.search("identity", document="Acme").hits
    vector_path = (
        tmp_path
        / ".shamsu"
        / "documents"
        / "vectors"
        / f"{prepared.record.document_id}.json"
    )
    assert vector_path.is_file()

    calls: list[list[str]] = []

    def query_only(texts: list[str]) -> list[list[float]]:
        calls.append(texts)
        assert texts == ["identity"]
        return [[1.0, 0.0]]

    second = DocumentStore(tmp_path, embed=query_only)
    result = second.search("identity", document="Acme")

    assert result.hits
    assert calls == [["identity"]]


def test_named_document_context_uses_semantic_retrieval_automatically(
    tmp_path: Path,
    monkeypatch,
):
    monkeypatch.delenv("SHAMSU_SEMANTIC_SEARCH", raising=False)
    prepared = DocumentStore(tmp_path).prepare_text(
        "# Sessions\n\nCall login to receive a token.",
        source="manual.md",
        source_kind="local",
        name="Acme",
    )
    target = tmp_path / prepared.relative_path
    target.parent.mkdir(parents=True)
    target.write_text(prepared.json_content, encoding="utf-8")

    def embed(texts: list[str]) -> list[list[float]]:
        return [
            [float("login" in text.lower() or "identity" in text.lower()), 0.0]
            for text in texts
        ]

    context, hits = DocumentStore(tmp_path, embed=embed).relevant_context(
        "Implement identity management from the Acme guide"
    )

    assert "Call login" in context
    assert hits
    assert "semantic" in hits[0].match_kind


def test_embedding_failure_falls_back_to_keyword_results(tmp_path: Path):
    prepared = DocumentStore(tmp_path).prepare_text(
        "# Retry\n\nRetry failed requests with exponential backoff.",
        source="retry.md",
        source_kind="local",
        name="Retry Manual",
    )
    target = tmp_path / prepared.relative_path
    target.parent.mkdir(parents=True)
    target.write_text(prepared.json_content, encoding="utf-8")

    def broken(_texts):
        raise ConnectionError("Ollama embedding model is unavailable")

    result = DocumentStore(tmp_path, embed=broken).search("exponential backoff")

    assert result.hits
    assert result.hits[0].match_kind == "keyword"
    assert "unavailable" in result.semantic_error


def test_ask_docs_returns_answer_contract_and_citations(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHAMSU_SEMANTIC_SEARCH", "0")
    registry, _ = _register_large_manual(tmp_path)

    result = registry.ask_docs("How long does a token last?", "Acme Platform")

    assert result.ok is True
    assert "only from these excerpts" in result.data["answer_instruction"]
    assert any("sixty minutes" in item["text"] for item in result.data["results"])
    assert all(item["citation"] for item in result.data["results"])


def test_summary_is_bounded_and_samples_late_document_chunks(tmp_path: Path):
    text = "\n".join(
        f"# Section {index}\n\nSection {index} has unique fact value-{index}."
        for index in range(1, 16)
    )
    prepared = DocumentStore(tmp_path).prepare_text(
        text,
        source="long-guide.md",
        source_kind="local",
        name="Long Guide",
    )
    target = tmp_path / prepared.relative_path
    target.parent.mkdir(parents=True)
    target.write_text(prepared.json_content, encoding="utf-8")

    summary = DocumentStore(tmp_path).summarize("Long Guide", max_tokens=250)

    assert summary.total_chunks >= 10
    assert summary.covered_chunks > 1
    assert "value-15" in summary.text
    assert all("Long Guide" in citation for citation in summary.citations)


def test_named_document_context_is_injected_and_logged(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHAMSU_SEMANTIC_SEARCH", "0")
    registry, _ = _register_large_manual(tmp_path)
    ledger = start_run(tmp_path, "Implement Acme Platform webhook verification")
    set_current_run(ledger)
    try:
        decision = RoutingDecision(intent="code_edit", complexity="single", confidence=1.0)
        plan = build_task_plan(
            decision,
            "Implement Acme Platform webhook verification",
            workspace=tmp_path,
        )
        rendered = append_task_handoff(
            "Implement Acme Platform webhook verification",
            plan,
        )
    finally:
        clear_current_run()

    assert registry.document_store.load_all()
    assert "## Registered Document Evidence" in rendered
    assert "HMAC-SHA256" in rendered
    assert "has already been retrieved from the source" in rendered
    assert "do not read the original document again" in rendered
    events = [
        json.loads(line)
        for line in ledger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    injected = next(event for event in events if event["type"] == "document_context_injected")
    assert injected["chunks"][0]["citation"]


def test_denied_large_document_registration_writes_nothing(tmp_path: Path):
    source = tmp_path / "manual.md"
    original = "# Manual\n\n" + ("Large manual content. " * 900)
    source.write_text(original, encoding="utf-8")
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: False)

    result = registry.ingest_docs("manual.md", "Manual")

    assert result.ok is False
    assert result.data["approval"] == "denied"
    assert source.read_text(encoding="utf-8") == original
    assert not (tmp_path / ".shamsu" / "documents").exists()


def test_registered_document_reingestion_is_idempotent(tmp_path: Path):
    approvals = []
    source = tmp_path / "manual.md"
    source.write_text("# Manual\n\n" + ("Large manual content. " * 900), encoding="utf-8")
    registry = AgentToolRegistry(
        tmp_path,
        approval_func=lambda request: approvals.append(request) or True,
    )

    first = registry.ingest_docs("manual.md", "Manual")
    second = registry.ingest_docs("manual.md", "Manual")

    assert first.ok and second.ok
    assert second.data["unchanged"] is True
    assert len(approvals) == 1


def test_pdf_chunks_preserve_normalized_page_citations(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("SHAMSU_SEMANTIC_SEARCH", "0")

    class _Page:
        def __init__(self, text: str) -> None:
            self.text = text

        def extract_text(self) -> str:
            return self.text

    class _Pdf:
        pages = [
            _Page(
                f"Acme Manual\n{index}\n{index} Chapter\n"
                + (
                    "Webhook verification uses HMAC-SHA256 and X-Acme-Signature. "
                    if index == 40
                    else f"Chapter {index} reference material. "
                )
                + ("Background details for this chapter. " * 8)
            )
            for index in range(1, 46)
        ]

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    monkeypatch.setattr("shamsu.retriever.documents.pdfplumber.open", lambda _path: _Pdf())
    source = tmp_path / "manual.pdf"
    source.write_bytes(b"%PDF-fake")
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    ingested = registry.ingest_docs("manual.pdf", "Acme Manual")
    result = registry.search_docs("How are HMAC signatures verified?", "Acme Manual")

    assert ingested.ok is True
    assert ingested.data["source_kind"] == "pdf"
    hit = result.data["results"][0]
    assert hit["page"] == 40
    assert "page 40" in hit["citation"]

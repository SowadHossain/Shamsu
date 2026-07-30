from __future__ import annotations

import json
from pathlib import Path

from shamsu.action_ledger.context import clear_current_run, set_current_run
from shamsu.action_ledger.ledger import start_run
from shamsu.agents.task_harness import append_task_handoff, build_task_plan
from shamsu.skills.ingest import MAX_REFERENCE_SOURCE_CHARS
from shamsu.skills.loader import discover_skills
from shamsu.skills.selector import render_skill_context, select_skills_for_task
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.tools.web import WebFetchResult
from shamsu.types import RoutingDecision


class _FakeWeb:
    def __init__(self, result: WebFetchResult) -> None:
        self.result = result
        self.calls: list[tuple[str, str]] = []

    def fetch(self, url: str, reason: str = "") -> WebFetchResult:
        self.calls.append((url, reason))
        return self.result


def test_local_markdown_is_ingested_as_discoverable_workspace_skill(tmp_path: Path):
    docs = tmp_path / "acme-sdk-docs.md"
    docs.write_text(
        "# Acme SDK\n\nUse `Client(token=...)` and call `client.widgets.list()`.\n",
        encoding="utf-8",
    )
    approvals = []
    registry = AgentToolRegistry(
        tmp_path,
        approval_func=lambda request: approvals.append(request) or True,
    )

    result = registry.ingest_docs("acme-sdk-docs.md", "Acme SDK")

    assert result.ok is True
    assert result.data["skill_name"] == "ref-acme-sdk"
    assert result.data["source_kind"] == "local"
    assert len(approvals) == 1
    assert approvals[0].action_type == "file_write"
    skill_path = tmp_path / result.data["skill_path"]
    assert skill_path.is_file()
    written = skill_path.read_text(encoding="utf-8")
    assert "Reference Boundary" in written
    assert "client.widgets.list()" in written
    catalog = discover_skills(tmp_path)
    skill = catalog.skills["ref-acme-sdk"]
    assert skill.source == "workspace"
    assert skill.metadata["kind"] == "reference"
    assert skill.metadata["reference_source"] == "acme-sdk-docs.md"
    assert result.data["transaction_id"]


def test_ingested_reference_is_selected_and_injected_for_named_library(tmp_path: Path):
    (tmp_path / "acme.md").write_text(
        "Create clients with `AcmeClient.from_env()` before issuing requests.\n",
        encoding="utf-8",
    )
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    assert registry.ingest_docs("acme.md", "Acme SDK").ok

    selection = select_skills_for_task(
        tmp_path,
        "Build an integration using the Acme SDK client",
        intent="code_edit",
    )
    names = [item.skill.name for item in selection.selected]
    rendered = render_skill_context(selection)

    assert "ref-acme-sdk" in names
    assert names.index("ref-acme-sdk") <= 1
    assert "named ingested reference matched" in rendered
    assert "AcmeClient.from_env()" in rendered


def test_ingestion_and_actual_context_injection_are_logged(tmp_path: Path):
    (tmp_path / "acme.txt").write_text("Use `acme.connect()`.\n", encoding="utf-8")
    ledger = start_run(tmp_path, "Use Acme SDK")
    set_current_run(ledger)
    try:
        registry = AgentToolRegistry(
            tmp_path,
            approval_func=lambda _request: True,
            action_ledger=ledger,
        )
        assert registry.ingest_docs("acme.txt", "Acme SDK").ok
        decision = RoutingDecision(intent="code_edit", complexity="single", confidence=1.0)
        plan = build_task_plan(decision, "Use Acme SDK to add a client", workspace=tmp_path)
        append_task_handoff("Use Acme SDK to add a client", plan)
    finally:
        clear_current_run()

    events = [
        json.loads(line)
        for line in ledger.events_path.read_text(encoding="utf-8").splitlines()
    ]
    ingested = next(event for event in events if event["type"] == "docs_ingested")
    injected = next(event for event in events if event["type"] == "skill_context_injected")
    assert ingested["skill_name"] == "ref-acme-sdk"
    assert injected["references"][0]["name"] == "ref-acme-sdk"
    assert injected["references"][0]["source"] == "acme.txt"


def test_url_ingestion_uses_fetched_readable_text(tmp_path: Path):
    url = "https://docs.example.com/acme/start"
    web = _FakeWeb(
        WebFetchResult(
            approved=True,
            url=url,
            final_url="https://docs.example.com/acme/start/",
            title="Acme SDK",
            text="Call `acme.start(config)` exactly once.",
        )
    )
    registry = AgentToolRegistry(
        tmp_path,
        approval_func=lambda _request: True,
        web_tool=web,
    )

    result = registry.execute("ingest_docs", {"source": url, "name": "Acme SDK"})

    assert result.ok is True
    assert result.data["source_kind"] == "url"
    assert result.data["source"] == "https://docs.example.com/acme/start/"
    assert web.calls and "reusable reference" in web.calls[0][1]
    skill = discover_skills(tmp_path).skills["ref-acme-sdk"]
    assert "acme.start(config)" in skill.instructions


def test_url_ingestion_infers_meaningful_name_when_generic_host_is_used(tmp_path: Path):
    url = "https://docs.example.com/acme/start"
    web = _FakeWeb(
        WebFetchResult(
            approved=True,
            url=url,
            final_url=url,
            text="Call `acme.start()`.",
        )
    )
    registry = AgentToolRegistry(
        tmp_path,
        approval_func=lambda _request: True,
        web_tool=web,
    )

    result = registry.ingest_docs(url)

    assert result.ok is True
    assert result.data["skill_name"] == "ref-acme"


def test_denied_url_fetch_does_not_create_reference(tmp_path: Path):
    url = "https://docs.example.com/acme"
    web = _FakeWeb(WebFetchResult(approved=False, url=url, error="Web fetch denied by user."))
    registry = AgentToolRegistry(
        tmp_path,
        approval_func=lambda _request: True,
        web_tool=web,
    )

    result = registry.ingest_docs(url, "Acme SDK")

    assert result.ok is False
    assert result.data["approval"] == "denied"
    assert not (tmp_path / ".shamsu" / "skills").exists()


def test_denied_reference_write_leaves_source_and_workspace_unchanged(tmp_path: Path):
    source = tmp_path / "acme.md"
    source.write_text("Acme facts.\n", encoding="utf-8")
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: False)

    result = registry.ingest_docs("acme.md", "Acme SDK")

    assert result.ok is False
    assert result.data["approval"] == "denied"
    assert source.read_text(encoding="utf-8") == "Acme facts.\n"
    assert not (tmp_path / ".shamsu" / "skills" / "ref-acme-sdk" / "SKILL.md").exists()


def test_read_only_mode_blocks_ingestion_before_fetch_or_write(tmp_path: Path):
    url = "https://docs.example.com/acme"
    web = _FakeWeb(WebFetchResult(approved=True, url=url, text="Acme facts."))
    registry = AgentToolRegistry(
        tmp_path,
        approval_func=lambda _request: True,
        web_tool=web,
    )
    registry.set_read_only(True)

    result = registry.ingest_docs(url, "Acme SDK")

    assert result.ok is False
    assert result.data["read_only"] is True
    assert web.calls == []


def test_large_reference_uses_document_pipeline_and_malformed_pdf_fails(tmp_path: Path):
    (tmp_path / "manual.pdf").write_bytes(b"%PDF-1.4")
    (tmp_path / "large.md").write_text(
        "x" * (MAX_REFERENCE_SOURCE_CHARS + 1),
        encoding="utf-8",
    )
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    pdf = registry.ingest_docs("manual.pdf", "Manual")
    large = registry.ingest_docs("large.md", "Large Manual")

    assert pdf.ok is False
    assert "PDF document" in pdf.message
    assert large.ok is True
    assert large.data["mode"] == "document"
    assert large.data["chunks"] > 1
    assert not (tmp_path / ".shamsu" / "skills").exists()
    assert (tmp_path / large.data["document_path"]).is_file()


def test_identical_reference_reingestion_is_idempotent(tmp_path: Path):
    (tmp_path / "acme.md").write_text("Acme facts.\n", encoding="utf-8")
    approvals = []
    registry = AgentToolRegistry(
        tmp_path,
        approval_func=lambda request: approvals.append(request) or True,
    )

    first = registry.ingest_docs("acme.md", "Acme SDK")
    second = registry.ingest_docs("acme.md", "Acme SDK")

    assert first.ok and second.ok
    assert second.data["unchanged"] is True
    assert len(approvals) == 1


def test_agent_tool_schema_exposes_document_ingestion(tmp_path: Path):
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)

    schemas = {
        schema["function"]["name"]: schema["function"]
        for schema in registry.tool_schemas()
    }

    assert "ingest_docs" in schemas
    assert schemas["ingest_docs"]["parameters"]["required"] == ["source"]
    assert {"search_docs", "ask_docs", "summarize_docs"} <= set(schemas)


def test_explicit_ingestion_requirement_blocks_generic_file_tool_substitution(tmp_path: Path):
    (tmp_path / "acme.md").write_text("Acme facts.\n", encoding="utf-8")
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    registry.require_tool_prefix("ingest_docs")

    substituted = registry.execute("read_file", {"filepath": "acme.md"})
    ingested = registry.execute(
        "ingest_docs",
        {"source": "acme.md", "name": "Acme SDK"},
    )

    assert substituted.ok is False
    assert substituted.data["required_tool_prefix"] == "ingest_docs"
    assert ingested.ok is True

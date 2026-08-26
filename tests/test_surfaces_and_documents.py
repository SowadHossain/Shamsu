from __future__ import annotations

import inspect

from shamsu.retriever.documents import extract_document_text, normalize_pdf_pages


def test_generic_document_text_extraction_reads_text_files(tmp_path) -> None:
    path = tmp_path / "brief.txt"
    path.write_text("Build the small harness.\nKeep web and Telegram.", encoding="utf-8")

    assert "small harness" in extract_document_text(path)


def test_pdf_normalization_preserves_page_numbers() -> None:
    normalized = normalize_pdf_pages(["First page\n", "", "Third page"])

    assert normalized.text == "First page\n\nThird page"
    assert [(line.text, line.page) for line in normalized.lines] == [
        ("First page", 1),
        ("Third page", 3),
    ]
    assert "page 2" in normalized.warnings[0]


def test_web_surface_imports() -> None:
    import shamsu.webui.cli
    import shamsu.webui.server

    assert shamsu.webui.cli.DEFAULT_PORT == shamsu.webui.server.DEFAULT_PORT


def test_telegram_surface_uses_small_loop_only() -> None:
    import shamsu.integrations.telegram.sessions as sessions

    source = inspect.getsource(sessions.LocalShamsuSessionGateway._run_simple)

    assert "SimpleChatLoop" in source
    assert "shamsu.agents.chat_loop" not in inspect.getsource(sessions)
    assert "AgentChatLoop" not in inspect.getsource(sessions)

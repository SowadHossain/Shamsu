from __future__ import annotations

from types import SimpleNamespace

from shamsu.agents.orchestrator import AgentOrchestrator
from shamsu.session.manager import SessionManager
from shamsu.session.memory import ConversationMemory
from shamsu.tools.workspace import MentionResolver, WorkspaceTool


def test_orchestrator_lists_workspace_files_before_model(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "app").mkdir()

    result = AgentOrchestrator(tmp_path).run("what files do i have here?")

    assert result.handled
    assert result.action == "workspace.files"
    assert "README.md" in result.message
    assert "app" in result.message


def test_orchestrator_reports_workspace_location_before_model(tmp_path):
    result = AgentOrchestrator(tmp_path).run("what folder are you in rn?")

    assert result.handled
    assert result.action == "workspace.location"
    assert str(tmp_path) in result.message


def test_capabilities_question_answered_from_real_registry(tmp_path):
    # "what tools can you use?" must be handled deterministically (not routed to
    # the tool-less QA brain) and list the tools SHAMSU can actually call.
    result = AgentOrchestrator(tmp_path).run("what tools can you use?")

    assert result.handled
    assert result.action == "capabilities"
    for tool_name in ("read_file", "write_file", "run_command", "find_file", "ask_user"):
        assert tool_name in result.message


def test_conversation_memory_resolves_web_followup_from_prior_turn(tmp_path):
    logger = SessionManager(tmp_path).create_session("Memory")
    logger.log("user.prompt", {"prompt": "what is the weather in Dhaka today?"}, "User")
    logger.log("assistant.message", {"message": "I need web access for current weather."}, "Assistant")
    logger.log("user.prompt", {"prompt": "check on the web"}, "User")

    memory = ConversationMemory.from_session(logger)

    assert memory.resolve_followup("check on the web") == (
        "what is the weather in Dhaka today? Please check on the web for this."
    )


def test_orchestrator_uses_session_memory_for_followup(tmp_path):
    logger = SessionManager(tmp_path).create_session("Memory")
    logger.log("user.prompt", {"prompt": "open http://127.0.0.1:8000"}, "User")
    logger.log("user.prompt", {"prompt": "open it"}, "User")

    result = AgentOrchestrator(tmp_path, session_logger=logger).run("open it")

    assert "http://127.0.0.1:8000" in result.effective_input
    assert "browser" in result.effective_input.lower()


def test_mention_resolver_reads_file_inside_workspace(tmp_path):
    (tmp_path / "README.md").write_text("# Demo\nThis is the project.", encoding="utf-8")

    context = MentionResolver(tmp_path).resolve("README.md")

    assert context.resolved
    assert context.kind == "file"
    assert "This is the project" in context.content


def test_orchestrator_adds_plain_filename_context_for_questions(tmp_path):
    (tmp_path / "qa_probe.py").write_text(
        "def add(a, b):\n    return a + b\n",
        encoding="utf-8",
    )

    result = AgentOrchestrator(tmp_path).run(
        "What does qa_probe.py do? Do not change files."
    )

    assert result.handled is False
    assert "# @qa_probe.py (file)" in result.context
    assert "def add(a, b)" in result.context


def test_orchestrator_deduplicates_at_and_plain_filename_context(tmp_path):
    (tmp_path / "qa_probe.py").write_text("print(5)\n", encoding="utf-8")

    result = AgentOrchestrator(tmp_path).run(
        "Using @qa_probe.py, tell me what qa_probe.py does."
    )

    assert result.context.count("# @qa_probe.py (file)") == 1


def test_mention_resolver_reads_quoted_path_with_spaces(tmp_path):
    docs = tmp_path / "agent context"
    docs.mkdir()
    (docs / "PROGRESS.md").write_text("# Progress\n", encoding="utf-8")

    contexts = MentionResolver(tmp_path).resolve_all('summarize @"agent context/PROGRESS.md"')

    assert contexts[0].resolved
    assert contexts[0].path.as_posix() == "agent context/PROGRESS.md"
    assert "# Progress" in contexts[0].content


def test_mention_resolver_rejects_path_escape(tmp_path):
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret", encoding="utf-8")

    context = MentionResolver(tmp_path).resolve(str(outside))

    assert not context.resolved
    assert "outside workspace" in context.error


def test_workspace_tool_suggests_at_mentions(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")

    suggestions = WorkspaceTool(tmp_path).mention_suggestions("src/a")

    assert suggestions == ["@src/app.py"]


def test_workspace_tool_quotes_at_mentions_with_spaces(tmp_path):
    (tmp_path / "Product Requirements Document.pdf").write_bytes(b"%PDF-1.4 stub")

    suggestions = WorkspaceTool(tmp_path).mention_suggestions("Product")

    assert suggestions == ['@"Product Requirements Document.pdf"']


def test_mention_resolver_reads_pdf_via_extractor(monkeypatch, tmp_path):
    (tmp_path / "Product Requirements Document.pdf").write_bytes(b"%PDF-1.4 stub")

    from shamsu.types import ParsedPRD

    def fake_parse(path):
        return ParsedPRD(title="Cube Runner", sections={}, raw_text="Milestone 1: Setup the game")

    # _read_pdf imports parse_prd_file lazily from shamsu.prd.input.
    monkeypatch.setattr("shamsu.prd.input.parse_prd_file", fake_parse)

    contexts = MentionResolver(tmp_path).resolve_all('@"Product Requirements Document.pdf" summarize')

    assert contexts[0].resolved
    assert contexts[0].kind == "file"
    assert "Milestone 1: Setup the game" in contexts[0].content


def test_weather_without_location_asks_before_web(tmp_path):
    result = AgentOrchestrator(tmp_path).run("what is the weather today?")

    assert result.handled
    assert result.action == "web.needs_location"
    assert "Which location" in result.message


def test_read_only_turn_does_not_auto_build_code_memory_index(tmp_path):
    class RecordingAbstract:
        def __init__(self):
            self.auto_build: list[bool] = []

        def ensure_ready(self, auto_build=True):
            self.auto_build.append(auto_build)
            return SimpleNamespace(allowed=True, reason="", status=None)

    abstract = RecordingAbstract()

    result = AgentOrchestrator(tmp_path, abstract_service=abstract).run(
        "Run calc.py and report output. Do not change files."
    )

    assert result.handled is False
    assert abstract.auto_build == [False]

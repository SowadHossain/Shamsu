from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from shamsu.agents.chat_loop import AgentChatLoop
from shamsu.artifacts.code import (
    CONTRADICTIONS_LOG,
    REGENERATION_QUEUE,
    FreshnessStatus,
    artifact_brief,
    build_repository_artifacts,
    hash_source_text,
    invalidate_artifacts_if_hash_mismatch,
    load_freshness_index,
    mark_artifacts_stale_for_paths,
    refresh_artifacts_for_paths,
    retrieve_structural_context,
    search_artifacts,
)
from shamsu.context.compiler import ContextCompiler
from shamsu.runtime.task_state import RuntimeStateStore
from shamsu.tools.agent_tools import AgentToolRegistry
from shamsu.types import RunStatus


def _write_sample_repo(root: Path) -> None:
    (root / "app").mkdir()
    (root / "tests").mkdir()
    (root / "app" / "__init__.py").write_text("", encoding="utf-8")
    (root / "app" / "service.py").write_text(
        '"""Business service."""\n'
        "from app.storage import save_user\n\n"
        "class UserService:\n"
        "    def create(self, name):\n"
        "        return save_user(name)\n\n"
        "def public_api(name):\n"
        "    return UserService().create(name)\n",
        encoding="utf-8",
    )
    (root / "app" / "storage.py").write_text(
        "def save_user(name):\n"
        "    return {'name': name}\n",
        encoding="utf-8",
    )
    (root / "app" / "routes.py").write_text(
        "from app.service import public_api\n\n"
        "def register_routes(app):\n"
        "    app.get('/users')\n"
        "    return public_api('Ada')\n",
        encoding="utf-8",
    )
    (root / "tests" / "test_service.py").write_text(
        "from app.service import public_api\n\n"
        "def test_public_api():\n"
        "    assert public_api('Ada')['name'] == 'Ada'\n",
        encoding="utf-8",
    )
    (root / "pyproject.toml").write_text("[project]\nname='demo'\n", encoding="utf-8")


def test_build_repository_artifacts_writes_manifest_maps_and_cards(tmp_path: Path):
    _write_sample_repo(tmp_path)

    result = build_repository_artifacts(tmp_path)

    root = tmp_path / ".shamsu" / "artifacts"
    manifest = json.loads((root / "repository_manifest.json").read_text(encoding="utf-8"))
    code_index = json.loads((root / "code_index.json").read_text(encoding="utf-8"))
    dependency_graph = json.loads((root / "dependency_graph.json").read_text(encoding="utf-8"))
    api_map = json.loads((root / "api_map.json").read_text(encoding="utf-8"))
    test_map = json.loads((root / "test_map.json").read_text(encoding="utf-8"))
    config_map = json.loads((root / "configuration_map.json").read_text(encoding="utf-8"))

    assert result.modules >= 4
    assert result.symbols >= 4
    assert manifest["schema_version"] == 1
    assert manifest["artifact_paths"]["code_index"] == "code_index.json"
    assert manifest["workspace"]["manifest_hash"]
    assert "app.service.public_api" in code_index["symbols"]
    assert "app.routes.register_routes" in code_index["symbols"]["app.service.public_api"]["callers"]
    assert (root / "repository_map.md").exists()
    module_card = root / "modules" / "app__service.py.json"
    assert module_card.exists()
    service = json.loads(module_card.read_text(encoding="utf-8"))
    assert service["path"] == "app/service.py"
    assert "app.service.UserService" in service["main_symbols"]
    assert "app.storage.save_user" in service["callees"] or "save_user" in service["callees"]
    assert dependency_graph["imports"]
    assert any(symbol["fully_qualified_symbol"] == "app.service.public_api" for symbol in api_map["symbols"])
    assert "tests/test_service.py" in [test["path"] for test in test_map["tests"]]
    assert config_map["configuration_files"][0]["path"] == "pyproject.toml"


def test_artifacts_record_freshness_metadata(tmp_path: Path):
    _write_sample_repo(tmp_path)
    build_repository_artifacts(tmp_path)

    root = tmp_path / ".shamsu" / "artifacts"
    manifest = json.loads((root / "repository_manifest.json").read_text(encoding="utf-8"))
    module = json.loads((root / "modules" / "app__service.py.json").read_text(encoding="utf-8"))
    symbol_path = next((root / "symbols").glob("*.json"))
    symbol = json.loads(symbol_path.read_text(encoding="utf-8"))
    repo_map = (root / "repository_map.md").read_text(encoding="utf-8")
    index = load_freshness_index(tmp_path)

    required = {
        "artifact_id",
        "artifact_type",
        "source_paths",
        "source_hashes",
        "artifact_version",
        "generator_version",
        "created_at",
        "refreshed_at",
        "confidence",
        "freshness_status",
    }
    assert required.issubset(manifest)
    assert required.issubset(module)
    assert required.issubset(symbol)
    assert module["freshness_status"] == FreshnessStatus.FRESH.value
    assert module["source_hashes"]["app/service.py"] == module["source_hash"]
    assert repo_map.startswith("<!-- shamsu-artifact ")
    assert "modules/app__service.py.json" in index["artifacts"]


def test_source_change_marks_related_artifacts_stale_and_schedules_regeneration(tmp_path: Path):
    _write_sample_repo(tmp_path)
    build_repository_artifacts(tmp_path)

    result = mark_artifacts_stale_for_paths(tmp_path, ["app/service.py"], reason="unit test change")

    root = tmp_path / ".shamsu" / "artifacts"
    module = json.loads((root / "modules" / "app__service.py.json").read_text(encoding="utf-8"))
    dependency_graph = json.loads((root / "dependency_graph.json").read_text(encoding="utf-8"))
    test_map = json.loads((root / "test_map.json").read_text(encoding="utf-8"))
    queue = json.loads((root / REGENERATION_QUEUE).read_text(encoding="utf-8"))

    assert "modules/app__service.py.json" in result["affected"]
    assert module["freshness_status"] == FreshnessStatus.STALE.value
    assert "code_index.json" in result["affected"]
    assert dependency_graph["freshness_status"] == FreshnessStatus.STALE.value
    assert test_map["freshness_status"] == FreshnessStatus.STALE.value
    assert queue["items"][0]["source_paths"] == ["app/service.py"]


def test_artifact_consumers_skip_known_stale_cards(tmp_path: Path):
    _write_sample_repo(tmp_path)
    build_repository_artifacts(tmp_path)

    root = tmp_path / ".shamsu" / "artifacts"
    index = load_freshness_index(tmp_path)
    for directory in ("modules", "symbols"):
        for path in (root / directory).glob("*.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("path") == "app/service.py":
                data["freshness_status"] = FreshnessStatus.STALE.value
                path.write_text(json.dumps(data), encoding="utf-8")
                rel = path.relative_to(root).as_posix()
                index["artifacts"][rel]["freshness_status"] = FreshnessStatus.STALE.value
    (root / "freshness_index.json").write_text(json.dumps(index), encoding="utf-8")

    hits = search_artifacts(tmp_path, "UserService", limit=5)

    assert all(not hit.startswith("app.service") for hit in hits)


def test_fresh_file_read_contradiction_invalidates_and_records_artifact(tmp_path: Path):
    _write_sample_repo(tmp_path)
    build_repository_artifacts(tmp_path)
    changed = (
        '"""Changed service."""\n'
        "def public_api(name):\n"
        "    return {'changed': name}\n"
    )
    (tmp_path / "app" / "service.py").write_text(changed, encoding="utf-8")

    result = invalidate_artifacts_if_hash_mismatch(
        tmp_path,
        "app/service.py",
        hash_source_text(changed),
        source="file.read",
    )

    root = tmp_path / ".shamsu" / "artifacts"
    module = json.loads((root / "modules" / "app__service.py.json").read_text(encoding="utf-8"))
    contradictions = (root / CONTRADICTIONS_LOG).read_text(encoding="utf-8")
    queue = json.loads((root / REGENERATION_QUEUE).read_text(encoding="utf-8"))

    assert "modules/app__service.py.json" in result["affected"]
    assert module["freshness_status"] == FreshnessStatus.INVALIDATED.value
    assert "app/service.py" in contradictions
    assert queue["items"][0]["status"] == "PENDING"


def test_refresh_regenerates_invalidated_artifacts_with_current_hash(tmp_path: Path):
    _write_sample_repo(tmp_path)
    build_repository_artifacts(tmp_path)
    changed = (
        '"""Changed service."""\n'
        "def public_api(name):\n"
        "    return {'changed': name}\n"
    )
    (tmp_path / "app" / "service.py").write_text(changed, encoding="utf-8")
    invalidate_artifacts_if_hash_mismatch(tmp_path, "app/service.py", hash_source_text(changed))

    refresh_artifacts_for_paths(tmp_path, ["app/service.py"])

    root = tmp_path / ".shamsu" / "artifacts"
    module = json.loads((root / "modules" / "app__service.py.json").read_text(encoding="utf-8"))
    queue = json.loads((root / REGENERATION_QUEUE).read_text(encoding="utf-8"))
    assert module["freshness_status"] == FreshnessStatus.FRESH.value
    assert module["source_hashes"]["app/service.py"] == hash_source_text(changed)
    assert queue["items"] == []


def test_chat_loop_refreshes_artifacts_after_successful_mutation(tmp_path: Path):
    _write_sample_repo(tmp_path)
    build_repository_artifacts(tmp_path)
    changed = (
        '"""Changed service."""\n'
        "def public_api(name):\n"
        "    return {'changed': name}\n"
    )
    (tmp_path / "app" / "service.py").write_text(changed, encoding="utf-8")
    loop = AgentChatLoop(
        tmp_path,
        client=SimpleNamespace(),
        llm=SimpleNamespace(),
        use_planner=False,
        use_long_term_memory=False,
        hydrate_history=False,
    )

    loop._sync_artifacts_after_tool_result(
        name="write_file",
        arguments={"filepath": "app/service.py"},
        result=SimpleNamespace(
            ok=True,
            data={"resolved_filepath": "app/service.py", "touched_files": ["app/service.py"]},
        ),
    )

    root = tmp_path / ".shamsu" / "artifacts"
    module = json.loads((root / "modules" / "app__service.py.json").read_text(encoding="utf-8"))
    assert module["freshness_status"] == FreshnessStatus.FRESH.value
    assert module["source_hashes"]["app/service.py"] == hash_source_text(changed)


def test_artifact_brief_and_search_use_cards_without_raw_code_dump(tmp_path: Path):
    _write_sample_repo(tmp_path)
    build_repository_artifacts(tmp_path)

    hits = search_artifacts(tmp_path, "UserService", limit=3)
    brief = artifact_brief(tmp_path, query="public_api", files=["app/service.py"])

    assert any("UserService" in hit for hit in hits)
    assert "Repository Map" in brief
    assert "Module card" in brief
    assert "app.service.public_api" in brief


def test_structural_retrieval_finds_symbol_relationships_tests_config_and_source(tmp_path: Path):
    _write_sample_repo(tmp_path)
    build_repository_artifacts(tmp_path)

    result = retrieve_structural_context(tmp_path, "public_api", limit=5)

    symbols = {item["fully_qualified_symbol"]: item for item in result["symbols"]}
    assert result["ok"] is True
    assert result["retrieval_order"][:5] == [
        "exact_path",
        "text_search",
        "symbol_lookup",
        "references",
        "call_graph",
    ]
    assert "app.service.public_api" in symbols
    assert symbols["app.service.public_api"]["definition"]["path"] == "app/service.py"
    assert "UserService.create" in result["call_graph"]["app.service.public_api"]["calls"]
    assert "app.routes.register_routes" in result["references"]["symbol_callers"]["app.service.public_api"]
    assert "tests/test_service.py" in result["related_tests"]
    assert any(item["path"] == "app/service.py" and "def public_api" in item["content"] for item in result["source"])
    assert any(item["path"] == "pyproject.toml" for item in result["configuration"])
    assert "app.routes.register_routes" in result["impact"]["likely_breaks"]


def test_structural_retrieval_answers_exact_path_and_module_importers(tmp_path: Path):
    _write_sample_repo(tmp_path)
    build_repository_artifacts(tmp_path)

    result = retrieve_structural_context(tmp_path, "app/service.py", limit=5)

    assert result["exact_paths"] == ["app/service.py"]
    assert "app/service.py" in result["dependency_graph"]
    assert "app.routes" in result["dependency_graph"]["app/service.py"]["importers"] or "app/routes.py" in result["dependency_graph"]["app/service.py"]["importers"]
    assert any(route["path"] == "app/routes.py" for route in result["routes"])


@pytest.mark.asyncio
async def test_context_compiler_includes_code_artifact_brief(tmp_path: Path):
    _write_sample_repo(tmp_path)
    build_repository_artifacts(tmp_path)
    store = RuntimeStateStore(tmp_path, db_path=tmp_path / "state.db")
    task = store.create_task(
        run_id="run",
        task_id="task",
        user_request="where is UserService implemented?",
        project_id="demo",
    )
    task.status = RunStatus.RUNNING
    store.save_task(task, checkpoint_kind="started")
    compiler = ContextCompiler(
        store=store,
        task_id_getter=lambda: "task",
        workspace_root=tmp_path,
        system_prompt_getter=lambda: "SYSTEM",
        allowed_tools_getter=lambda: [],
    )

    messages = await compiler.compile(8000, [])

    assert "code_artifacts" in messages[1]["content"]
    assert "UserService" in messages[1]["content"]


def test_project_inspect_exposes_artifact_summary(tmp_path: Path):
    _write_sample_repo(tmp_path)
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    registry.use_logical_tools(True)

    result = registry.execute("project.inspect", {"path": ".", "include_git": False})

    assert result.ok is True
    assert result.data["code_artifacts"]["modules"] >= 4
    assert "Repository Map" in result.data["code_artifacts"]["brief"]


def test_code_search_exposes_structural_context_before_semantic_fallback(tmp_path: Path):
    _write_sample_repo(tmp_path)
    registry = AgentToolRegistry(tmp_path, approval_func=lambda _request: True)
    registry.use_logical_tools(True)

    result = registry.execute("code.search", {"query": "public_api", "limit": 5})

    assert result.ok is True
    structural = result.data["structural"]
    assert structural["retrieval_order"][0] == "exact_path"
    assert structural["symbols"][0]["fully_qualified_symbol"] == "app.service.public_api"
    assert "app.routes.register_routes" in structural["references"]["symbol_callers"]["app.service.public_api"]

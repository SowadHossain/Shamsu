"""Node/Express as a first-class backend stack.

A Node PRD used to declare no foundation files at all: the architecture file
map knew Django and React and nothing else. Two failures followed from that one
gap - the file-at-a-time pass had no target so the milestone collapsed into one
oversized turn, and verification ran `npm install` against a directory with no
manifest, producing `npm error code ENOENT` and ending the run.
"""
from __future__ import annotations

import json
from pathlib import Path

from shamsu.prd.requirements import (
    _architecture_components,
    _architecture_expected_files_for_milestone,
)
from shamsu.prd.contract import PRDContract
from shamsu.verify import semantic
from shamsu.verify.gate import build_verification_plan, default_verify_command


def _contract(*stack: str) -> PRDContract:
    return PRDContract(title="Marketplace", stack_hint=stack[0], required_stack=list(stack[1:]))


# ── architecture file map ─────────────────────────────────────────────────


def test_a_node_backend_declares_foundation_files():
    paths = _architecture_expected_files_for_milestone("M-001", _contract("node", "express"))

    assert "backend/package.json" in paths
    assert "backend/server.js" in paths


def test_a_bare_node_stack_counts_as_a_backend():
    assert _architecture_expected_files_for_milestone("M-001", _contract("node"))


def test_a_react_frontend_is_not_given_a_node_backend():
    paths = _architecture_expected_files_for_milestone("M-001", _contract("node", "react", "vite"))

    assert "backend/server.js" not in paths


def test_the_chosen_backend_wins_over_a_mention_in_the_document():
    """The OpenBazaar PRD's architecture diagram says "Node.js / Go
    microservices". Asking to build it with Django must not also scaffold an
    Express server - `stack_hint` already folds in the user's instruction."""
    paths = _architecture_expected_files_for_milestone(
        "M-001", _contract("django", "django", "node")
    )

    assert "backend/manage.py" in paths
    assert "backend/server.js" not in paths


def test_an_explicit_express_dependency_does_not_override_a_python_primary():
    paths = _architecture_expected_files_for_milestone(
        "M-001", _contract("django", "express", "sqlite")
    )

    assert "backend/server.js" not in paths


def test_node_product_milestones_do_not_repeat_the_foundation():
    assert not _architecture_expected_files_for_milestone("M-002", _contract("node", "express"))


def test_an_express_stack_owns_a_backend_component():
    components = _architecture_components(_contract("node", "express"))

    assert any(item["id"] == "backend" for item in components)


# ── npm ENOENT ────────────────────────────────────────────────────────────


def test_npm_install_is_not_run_before_a_manifest_exists():
    """`npm install` with no package.json exits ENOENT, which reads as a broken
    build rather than as "the manifest has not been written yet"."""
    command = default_verify_command(["src/server.js"], stack_hint="node", lightweight=False)

    assert command == ""


def test_npm_install_runs_once_the_manifest_is_there():
    command = default_verify_command(
        ["package.json", "src/server.js"], stack_hint="node", lightweight=False
    )

    assert command == "npm install && npm run build"


# ── semantic probe ────────────────────────────────────────────────────────


def _express_project(root: Path, *, dependency: str = "express") -> Path:
    (root / "package.json").write_text(
        json.dumps({"name": "app", "type": "module", "dependencies": {dependency: "^4.0.0"}}),
        encoding="utf-8",
    )
    return root


def test_a_server_project_is_probed(tmp_path: Path):
    _express_project(tmp_path)

    assert semantic.should_probe_node(["src/app.js"], tmp_path) is True


def test_a_frontend_only_project_is_not_probed(tmp_path: Path):
    """Vite/React is "node" too, but it has no app to mount routes on."""
    _express_project(tmp_path, dependency="react")

    assert semantic.should_probe_node(["src/App.tsx"], tmp_path) is False


def test_a_project_without_a_manifest_is_not_probed(tmp_path: Path):
    assert semantic.should_probe_node(["src/app.js"], tmp_path) is False


def test_an_unparseable_manifest_does_not_raise(tmp_path: Path):
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")

    assert semantic.should_probe_node(["src/app.js"], tmp_path) is False


def test_changes_that_cannot_affect_routing_are_not_probed(tmp_path: Path):
    _express_project(tmp_path)

    assert semantic.should_probe_node(["README.md"], tmp_path) is False


def test_the_probe_is_materialized_and_planned(tmp_path: Path):
    _express_project(tmp_path)
    (tmp_path / "node_modules").mkdir()

    plan = build_verification_plan(tmp_path, ["src/app.js"], stack_hint="node")

    assert (tmp_path / ".shamsu_probe.mjs").is_file()
    assert any(step.stage == "semantic" for step in plan.steps)
    assert semantic.node_probe_command() == "node .shamsu_probe.mjs"


def test_the_probe_reads_both_express_4_and_5_router_locations():
    """Express 4 stores the stack on `_router` and makes `router` a getter that
    THROWS; reading it unguarded killed the probe before it checked anything."""
    assert '"_router", "router"' in semantic.NODE_PROBE
    assert "catch" in semantic.NODE_PROBE

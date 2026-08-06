"""Behavioural milestones are split into one turn per file, not one per milestone."""
from __future__ import annotations

from pathlib import Path

from shamsu.cli.repl import (
    _prd_app_package,
    _prd_behavioural_file_groups,
)


def _django_project(tmp_path: Path, root: str = "site", app: str = "library") -> None:
    base = tmp_path / root if root else tmp_path
    (base / app).mkdir(parents=True)
    (base / app / "models.py").write_text("from django.db import models\n", encoding="utf-8")


def _req(rid: str, kind: str, text: str, scope: str = "in") -> dict:
    return {"id": rid, "kind": kind, "text": text, "scope": scope, "priority": "must"}


class TestAppPackageDiscovery:
    def test_finds_the_app_beside_models_py(self, tmp_path: Path):
        _django_project(tmp_path)
        assert _prd_app_package("site", tmp_path) == "library"

    def test_finds_the_app_by_apps_py(self, tmp_path: Path):
        (tmp_path / "site" / "catalog").mkdir(parents=True)
        (tmp_path / "site" / "catalog" / "apps.py").write_text("x = 1\n", encoding="utf-8")
        assert _prd_app_package("site", tmp_path) == "catalog"

    def test_no_app_layout_returns_empty(self, tmp_path: Path):
        (tmp_path / "site").mkdir()
        assert _prd_app_package("site", tmp_path) == ""

    def test_dunder_and_hidden_directories_are_skipped(self, tmp_path: Path):
        base = tmp_path / "site"
        (base / "__pycache__").mkdir(parents=True)
        (base / "__pycache__" / "models.py").write_text("", encoding="utf-8")
        (base / "library").mkdir()
        (base / "library" / "models.py").write_text("", encoding="utf-8")
        assert _prd_app_package("site", tmp_path) == "library"


class TestGrouping:
    def test_requirements_are_grouped_by_the_file_that_carries_them(self, tmp_path: Path):
        _django_project(tmp_path)
        preflight = {
            "requirements": [
                _req("R1", "entity", "Store Member records"),
                _req("R2", "feature", "Members can borrow a copy"),
                _req("R3", "auth", "Members sign in with a password"),
            ]
        }
        groups = _prd_behavioural_file_groups(preflight, "site", tmp_path)
        assert [path for path, _ in groups] == [
            "site/library/models.py",
            "site/library/views.py",
        ]
        assert [item["id"] for item in groups[0][1]] == ["R1"]
        assert [item["id"] for item in groups[1][1]] == ["R2", "R3"]

    def test_models_come_before_views_and_urls(self, tmp_path: Path):
        _django_project(tmp_path)
        preflight = {
            "requirements": [
                _req("R1", "api", "Expose /api/loans"),
                _req("R2", "feature", "List loans"),
                _req("R3", "entity", "Loan record"),
            ]
        }
        groups = _prd_behavioural_file_groups(preflight, "site", tmp_path)
        assert [path.rsplit("/", 1)[-1] for path, _ in groups] == [
            "models.py",
            "views.py",
            "urls.py",
        ]

    def test_acceptance_and_out_of_scope_produce_no_turn(self, tmp_path: Path):
        _django_project(tmp_path)
        preflight = {
            "requirements": [
                _req("R1", "acceptance", "All tests pass"),
                _req("R2", "out_of_scope", "Mobile app"),
            ]
        }
        assert _prd_behavioural_file_groups(preflight, "site", tmp_path) == []

    def test_out_of_scope_requirements_are_excluded(self, tmp_path: Path):
        _django_project(tmp_path)
        preflight = {
            "requirements": [
                _req("R1", "feature", "Borrowing", scope="out"),
                _req("R2", "feature", "Returning"),
            ]
        }
        groups = _prd_behavioural_file_groups(preflight, "site", tmp_path)
        assert [item["id"] for item in groups[0][1]] == ["R2"]

    def test_project_without_an_app_falls_back_to_no_decomposition(self, tmp_path: Path):
        (tmp_path / "site").mkdir()
        preflight = {"requirements": [_req("R1", "feature", "Borrowing")]}
        assert _prd_behavioural_file_groups(preflight, "site", tmp_path) == []

    def test_group_count_is_capped(self, tmp_path: Path):
        _django_project(tmp_path)
        preflight = {
            "requirements": [
                _req("R1", "entity", "a"),
                _req("R2", "feature", "b"),
                _req("R3", "api", "c"),
            ]
        }
        groups = _prd_behavioural_file_groups(preflight, "site", tmp_path)
        assert len(groups) <= 4

    def test_empty_project_root_uses_the_workspace(self, tmp_path: Path):
        _django_project(tmp_path, root="")
        preflight = {"requirements": [_req("R1", "entity", "Member")]}
        groups = _prd_behavioural_file_groups(preflight, "", tmp_path)
        assert groups[0][0] == "library/models.py"

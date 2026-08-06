"""Semantic verification: a change must work, not merely parse.

`py_compile` and `manage.py check` both pass on a route appended outside
`urlpatterns`, so SHAMSU reported "the members route has been added ...
Verification passed" for a page that did not exist (observed live 2026-08-02).
"""
from __future__ import annotations

import re
from pathlib import Path

from shamsu.verify import semantic


class TestProbeRelevance:
    def test_url_view_and_template_changes_are_probed(self, tmp_path: Path):
        (tmp_path / "manage.py").write_text("x = 1\n", encoding="utf-8")
        for changed in (["core/urls.py"], ["core/views.py"], ["core/t/page.html"]):
            assert semantic.should_probe(changed, tmp_path), changed

    def test_unrelated_changes_are_not_probed(self, tmp_path: Path):
        (tmp_path / "manage.py").write_text("x = 1\n", encoding="utf-8")
        assert not semantic.should_probe(["README.md"], tmp_path)
        assert not semantic.should_probe(["core/models.py"], tmp_path)

    def test_non_django_project_is_not_probed(self, tmp_path: Path):
        assert not semantic.should_probe(["core/urls.py"], tmp_path)

    def test_no_changes_is_not_probed(self, tmp_path: Path):
        (tmp_path / "manage.py").write_text("x = 1\n", encoding="utf-8")
        assert not semantic.should_probe([], tmp_path)


class TestProbeContent:
    def test_probe_is_written_next_to_manage_py(self, tmp_path: Path):
        written = semantic.write_probe(tmp_path)
        assert written.is_file()
        assert written.parent == tmp_path

    def test_route_name_scan_ignores_app_name(self):
        """`app_name = 'library'` is a namespace, not a route.

        Without the word boundary this reported the namespace itself as an
        unresolvable route and failed a correct project.
        """
        pattern = re.compile(r"\bname\s*=\s*[\"']([A-Za-z0-9_]+)[\"']")
        source = (
            "app_name = 'library'\n"
            "urlpatterns = [\n"
            "    path('', views.book_list, name='book_list'),\n"
            "]\n"
        )
        assert pattern.findall(source) == ["book_list"]

    def test_probe_reads_settings_module_from_manage_py(self):
        assert "DJANGO_SETTINGS_MODULE" in semantic.DJANGO_PROBE
        assert "urlpatterns" in semantic.DJANGO_PROBE or "reverse" in semantic.DJANGO_PROBE

    def test_probe_fails_loudly(self):
        assert "SEMANTIC FAILURES" in semantic.DJANGO_PROBE
        assert "sys.exit(1)" in semantic.DJANGO_PROBE

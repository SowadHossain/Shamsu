from __future__ import annotations

from shamsu.tools.path_resolve import remap_diff_paths, resolve_reported_path


def _make(tmp_path, rel: str, content: str = "x\n") -> None:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def test_resolve_returns_path_when_already_real(tmp_path):
    _make(tmp_path, "app.py")
    assert resolve_reported_path(tmp_path, "app.py") == "app.py"
    assert resolve_reported_path(tmp_path, "./app.py") == "app.py"


def test_resolve_maps_reported_suffix_to_real_frontend_path(tmp_path):
    # Build reports src/App.tsx; the file really lives at client/src/App.tsx.
    _make(tmp_path, "client/src/App.tsx")
    assert resolve_reported_path(tmp_path, "src/App.tsx") == "client/src/App.tsx"


def test_resolve_handles_quotes_and_backslashes(tmp_path):
    _make(tmp_path, "client/src/App.tsx")
    assert resolve_reported_path(tmp_path, "`src\\App.tsx`") == "client/src/App.tsx"


def test_resolve_returns_none_when_ambiguous(tmp_path):
    # Two files both end with src/App.tsx -> refuse to guess.
    _make(tmp_path, "client/src/App.tsx")
    _make(tmp_path, "admin/src/App.tsx")
    assert resolve_reported_path(tmp_path, "src/App.tsx") is None


def test_resolve_returns_none_when_missing(tmp_path):
    assert resolve_reported_path(tmp_path, "does/not/exist.ts") is None


def test_remap_diff_rewrites_headers_to_real_path(tmp_path):
    _make(tmp_path, "client/src/App.tsx")
    diff = (
        "--- a/src/App.tsx\n"
        "+++ b/src/App.tsx\n"
        "@@ -1 +1 @@\n"
        "-x\n"
        "+y\n"
    )
    new_diff, remaps = remap_diff_paths(tmp_path, diff)
    assert "--- a/client/src/App.tsx" in new_diff
    assert "+++ b/client/src/App.tsx" in new_diff
    assert remaps == [("src/App.tsx", "client/src/App.tsx")]
    # Hunk body is untouched.
    assert "@@ -1 +1 @@" in new_diff and "+y" in new_diff


def test_remap_diff_leaves_real_and_devnull_paths_alone(tmp_path):
    _make(tmp_path, "app.py")
    diff = (
        "--- /dev/null\n"
        "+++ b/app.py\n"
        "@@ -0,0 +1 @@\n"
        "+x\n"
    )
    new_diff, remaps = remap_diff_paths(tmp_path, diff)
    assert new_diff == diff
    assert remaps == []


def test_remap_diff_handles_git_and_rename_headers(tmp_path):
    _make(tmp_path, "client/src/App.tsx")
    diff = (
        "diff --git a/src/App.tsx b/src/App.tsx\n"
        "--- a/src/App.tsx\n"
        "+++ b/src/App.tsx\n"
    )
    new_diff, remaps = remap_diff_paths(tmp_path, diff)
    assert "diff --git a/client/src/App.tsx b/client/src/App.tsx" in new_diff
    assert remaps == [("src/App.tsx", "client/src/App.tsx")]

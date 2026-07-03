"""Read-only workspace tools and @mention resolution."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from shamsu.safety.sandbox import Sandbox, SecurityError

IGNORED_DIRS = {
    ".git",
    ".hg",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".shamsu",
    ".venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
}
TEXT_EXTENSIONS = {
    ".cfg",
    ".css",
    ".env",
    ".html",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".py",
    ".rst",
    ".sh",
    ".toml",
    ".txt",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
MENTION_RE = re.compile(
    r"(?<!\S)@(?:(?P<double>\"[^\"]+\")|(?P<single>'[^']+')|(?P<path>[\w./\\-]+))"
)
MAX_FILE_CHARS = 6000


@dataclass(frozen=True)
class WorkspaceListing:
    workspace: Path
    entries: list[Path]
    total: int
    hidden_count: int = 0

    def render(self, limit: int = 30) -> str:
        if not self.entries:
            return f"Workspace: {self.workspace}\n\nThis workspace is empty."
        shown = self.entries[:limit]
        body = "\n".join(
            f"[dir]  {path.name}" if path.is_dir() else f"[file] {path.name}"
            for path in shown
        )
        remaining = self.total - len(shown)
        if remaining > 0:
            body = f"{body}\n... {remaining} more"
        if self.hidden_count:
            body = f"{body}\n\nIgnored/internal entries hidden: {self.hidden_count}"
        return f"Workspace: {self.workspace}\nFiles shown: {len(shown)} of {self.total}\n\n{body}"


@dataclass(frozen=True)
class MentionContext:
    mention: str
    workspace_root: Path | None = None
    path: Path | None = None
    kind: str = "missing"
    content: str = ""
    matches: list[Path] = field(default_factory=list)
    error: str = ""

    @property
    def resolved(self) -> bool:
        return self.kind in {"file", "folder"}


class WorkspaceTool:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.sandbox = Sandbox(self.workspace_root)

    def list_files(self, limit: int = 30) -> WorkspaceListing:
        entries = []
        hidden_count = 0
        for path in self.workspace_root.iterdir():
            if path.name in IGNORED_DIRS:
                hidden_count += 1
                continue
            entries.append(path)
        entries = sorted(entries, key=lambda item: (not item.is_dir(), item.name.lower()))
        return WorkspaceListing(self.workspace_root, entries[:limit], len(entries), hidden_count)

    def read_file(self, path_text: str, max_chars: int = MAX_FILE_CHARS) -> str:
        target = self.sandbox.validate(path_text)
        if not target.is_file():
            raise ValueError(f"Not a file: {path_text}")
        if target.suffix.lower() not in TEXT_EXTENSIONS and target.name not in {"Dockerfile", "Makefile"}:
            raise ValueError(f"Not a supported text file: {path_text}")
        text = target.read_text(encoding="utf-8", errors="replace")
        if len(text) > max_chars:
            return f"{text[:max_chars]}\n... [truncated {len(text) - max_chars} chars]"
        return text

    def find_files(self, query: str, limit: int = 20) -> list[Path]:
        query = query.strip().lower().lstrip("@")
        if not query:
            return []
        matches: list[Path] = []
        for path in self._walk_files_and_dirs():
            rel = path.relative_to(self.workspace_root).as_posix()
            if query in rel.lower() or query == path.name.lower():
                matches.append(path)
                if len(matches) >= limit:
                    break
        return matches

    def find_prds(self, limit: int = 20) -> list[Path]:
        candidates = []
        for path in self._walk_files_and_dirs():
            if path.is_file() and path.suffix.lower() in {".md", ".markdown", ".txt", ".pdf"}:
                if "prd" in path.name.lower():
                    candidates.append(path.relative_to(self.workspace_root))
                    if len(candidates) >= limit:
                        break
        return sorted(candidates)

    def mention_suggestions(self, prefix: str, limit: int = 30) -> list[str]:
        return [
            f"@{path.relative_to(self.workspace_root).as_posix()}"
            for path in self.find_files(prefix, limit=limit)
        ]

    def _walk_files_and_dirs(self) -> list[Path]:
        results: list[Path] = []
        for path in self.workspace_root.rglob("*"):
            if any(part in IGNORED_DIRS for part in path.relative_to(self.workspace_root).parts):
                continue
            results.append(path)
        return sorted(results, key=lambda item: item.relative_to(self.workspace_root).as_posix().lower())


class MentionResolver:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.tool = WorkspaceTool(self.workspace_root)
        self.sandbox = Sandbox(self.workspace_root)

    def resolve_all(self, text: str) -> list[MentionContext]:
        contexts = []
        for match in MENTION_RE.finditer(text):
            raw = match.group("double") or match.group("single") or match.group("path") or ""
            contexts.append(self.resolve(raw.strip("\"'")))
        return contexts

    def resolve(self, mention: str) -> MentionContext:
        raw = mention.lstrip("@").strip()
        try:
            target = self.sandbox.validate(raw)
        except SecurityError as exc:
            return MentionContext(mention=mention, workspace_root=self.workspace_root, error=str(exc))
        if target.exists():
            return self._context_for_path(mention, target)
        matches = self.tool.find_files(raw)
        if len(matches) == 1:
            return self._context_for_path(mention, matches[0])
        if len(matches) > 1:
            return MentionContext(mention=mention, workspace_root=self.workspace_root, kind="ambiguous", matches=matches[:10])
        return MentionContext(mention=mention, workspace_root=self.workspace_root, error=f"No workspace file matched @{raw}.")

    def _context_for_path(self, mention: str, path: Path) -> MentionContext:
        rel = path.relative_to(self.workspace_root)
        if path.is_dir():
            listing = WorkspaceTool(path).list_files(limit=20).render()
            return MentionContext(mention=mention, workspace_root=self.workspace_root, path=rel, kind="folder", content=listing)
        try:
            content = self.tool.read_file(str(rel))
        except Exception as exc:
            return MentionContext(mention=mention, workspace_root=self.workspace_root, path=rel, error=str(exc))
        return MentionContext(mention=mention, workspace_root=self.workspace_root, path=rel, kind="file", content=content)


def render_mention_context(contexts: list[MentionContext]) -> str:
    parts: list[str] = []
    for context in contexts:
        if context.kind == "ambiguous":
            matches = "\n".join(
                f"- {_display_match(path, context.workspace_root)}"
                for path in context.matches
            )
            parts.append(f"Ambiguous mention @{context.mention}. Matches:\n{matches}")
            continue
        if context.error:
            parts.append(f"@{context.mention}: {context.error}")
            continue
        if context.path:
            parts.append(f"# @{context.path.as_posix()} ({context.kind})\n{context.content}")
    return "\n\n".join(parts)


def _display_match(path: Path, workspace_root: Path | None) -> str:
    if workspace_root is None:
        return path.as_posix()
    try:
        return path.relative_to(workspace_root).as_posix()
    except ValueError:
        return path.as_posix()

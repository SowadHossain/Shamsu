"""Read-only workspace tools and @mention resolution."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from shamsu.indexer.policy import (
    DEFAULT_EXCLUDED_DIRS,
    is_workspace_path,
    walk_workspace_paths,
)
from shamsu.safety.sandbox import Sandbox, SecurityError

IGNORED_DIRS = DEFAULT_EXCLUDED_DIRS
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
# Documents SHAMSU can extract text from rather than read as plain text.
DOCUMENT_EXTENSIONS = {".pdf"}


def extract_document_text(path: Path) -> str:
    """Plain text of a document SHAMSU knows how to parse (currently PDF)."""
    # Lazy: pdf extraction (via pdfplumber) is heavy and rarely needed.
    from shamsu.prd.input import parse_prd_file

    return parse_prd_file(path).raw_text


def _truncate(text: str, max_chars: int) -> str:
    if len(text) > max_chars:
        return f"{text[:max_chars]}\n... [truncated {len(text) - max_chars} chars]"
    return text


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
            if not is_workspace_path(path, self.workspace_root):
                hidden_count += 1
                continue
            entries.append(path)
        entries = sorted(entries, key=lambda item: (not item.is_dir(), item.name.lower()))
        return WorkspaceListing(self.workspace_root, entries[:limit], len(entries), hidden_count)

    def read_file(self, path_text: str, max_chars: int = MAX_FILE_CHARS) -> str:
        target = self.sandbox.validate(path_text)
        if not target.is_file():
            raise ValueError(f"Not a file: {path_text}")
        if target.suffix.lower() in DOCUMENT_EXTENSIONS:
            # SHAMSU already extracts these for @-mentions and PRD builds, so
            # refusing them here stranded any turn told to "read the PRD": live
            # 2026-08-02 the plan route asked the user what `canvas lite.pdf`
            # was rather than reading the document sitting in the workspace.
            return _truncate(extract_document_text(target), max_chars)
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
        from shamsu.prd.input import is_prd_filename

        candidates = []
        for path in self._walk_files_and_dirs():
            if path.is_file() and is_prd_filename(path.name):
                candidates.append(path.relative_to(self.workspace_root))
                if len(candidates) >= limit:
                    break
        return sorted(candidates)

    def mention_suggestions(self, prefix: str, limit: int = 30) -> list[str]:
        suggestions: list[str] = []
        for path in self.find_files(prefix, limit=limit):
            rel = path.relative_to(self.workspace_root).as_posix()
            # Quote paths with spaces so the mention regex (which stops at the
            # first space for unquoted paths) still captures the whole name.
            suggestions.append(f'@"{rel}"' if " " in rel else f"@{rel}")
        return suggestions

    def names_in_text(self, text: str, limit: int = 10) -> list[Path]:
        """Workspace files whose filename appears verbatim in *text*.

        Matching on the whole name (not a token regex) is what makes this work
        for names containing spaces, e.g. `canvas lite.pdf`.
        """
        lowered = " ".join(str(text or "").lower().split())
        if not lowered:
            return []
        hits: list[Path] = []
        for path in self._walk_files_and_dirs():
            if path.is_file() and path.name.lower() in lowered:
                hits.append(path)
                if len(hits) >= limit:
                    break
        return hits

    def _walk_files_and_dirs(self) -> list[Path]:
        results = walk_workspace_paths(self.workspace_root)
        return sorted(results, key=lambda item: item.relative_to(self.workspace_root).as_posix().lower())


class MentionResolver:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.tool = WorkspaceTool(self.workspace_root)
        self.sandbox = Sandbox(self.workspace_root)

    def resolve_all(self, text: str) -> list[MentionContext]:
        contexts = []
        for match in MENTION_RE.finditer(text):
            # A Python decorator (`@app.route(...)`, `@property`) sits at the
            # start of a line just like a real mention does, and the greedy
            # path group happily matches its dotted name. Live repro: replayed
            # model-generated code containing `@app.route(...)` got parsed as
            # `@app.route`, "resolved" against workspace files, and derailed
            # the whole turn into a file dump instead of the actual task.
            # Only the unquoted bare-path form can collide with code syntax,
            # and only with no space before the paren - a real mention
            # followed by a parenthetical note ("@app.py (see below)") always
            # has one; a decorator or call never does.
            if match.group("path") and text[match.end() : match.end() + 1] == "(":
                continue
            raw = match.group("double") or match.group("single") or match.group("path") or ""
            mention = raw.strip("\"'")
            if match.group("path"):
                # An unquoted mention of a filename containing spaces
                # (`@canvas lite.pdf`) stops at the first space and resolves
                # to nothing - or to the wrong file. If absorbing the next
                # word(s) yields a real workspace path, that longer name is
                # what the user meant. Observed live 2026-08-01: the PRD
                # `canvas lite.pdf` was read as `canvas`, derailing the build.
                extended = self._extend_unquoted_mention(mention, text[match.end() :])
                if extended is not None:
                    mention = extended
            contexts.append(self.resolve(mention))
        return contexts

    def _extend_unquoted_mention(self, base: str, following: str) -> str | None:
        """Return `base` plus following words when only the longer form names a
        real file. None leaves the original mention untouched."""
        if self._resolves(base):
            return None
        candidate = base
        rest = following
        for _ in range(3):
            word = re.match(r"[ \t]([\w][\w.\\/-]*)", rest)
            if word is None:
                return None
            candidate = f"{candidate} {word.group(1)}"
            rest = rest[word.end() :]
            if self._resolves(candidate):
                return candidate
        return None

    def _resolves(self, raw: str) -> bool:
        try:
            target = self.sandbox.validate(raw)
        except SecurityError:
            return False
        if target.exists():
            return True
        return len(self.tool.find_files(raw)) == 1

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
            if path.suffix.lower() == ".pdf":
                content = self._read_pdf(path)
            else:
                content = self.tool.read_file(str(rel))
        except Exception as exc:
            return MentionContext(mention=mention, workspace_root=self.workspace_root, path=rel, error=str(exc))
        return MentionContext(mention=mention, workspace_root=self.workspace_root, path=rel, kind="file", content=content)

    def _read_pdf(self, path: Path, max_chars: int = MAX_FILE_CHARS) -> str:
        return _truncate(extract_document_text(path), max_chars)


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

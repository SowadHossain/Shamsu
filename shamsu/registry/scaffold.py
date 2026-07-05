"""Copy category templates into a workspace target directory."""
from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from shamsu.registry.schema import RegistryEntry
from shamsu.safety.approval import ask_approval
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.safety.sandbox import Sandbox
from shamsu.session.manager import SessionLogger
from shamsu.types import ApprovalRequest


@dataclass(frozen=True)
class ScaffoldResult:
    target_dir: Path
    copied_files: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)


def scaffold_template(
    entry: RegistryEntry,
    workspace_root: Path,
    target_dir: Path | str,
    approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
    session_logger: SessionLogger | None = None,
    approval_manager: ApprovalManager | None = None,
) -> ScaffoldResult:
    sandbox = Sandbox(workspace_root)
    target = sandbox.validate(target_dir)
    source = entry.root / "template"
    if not source.exists():
        raise FileNotFoundError(f"Template directory missing: {source}")
    if target.exists() and not target.is_dir():
        raise ValueError(f"Template target is not a directory: {target}")
    target.mkdir(parents=True, exist_ok=True)
    manager = approval_manager or ApprovalManager(approval_func, session_logger)
    copied: list[str] = []
    skipped: list[str] = []
    for source_file in sorted(path for path in source.rglob("*") if path.is_file()):
        relative = source_file.relative_to(source).as_posix()
        destination = sandbox.validate(target / relative)
        if destination.exists():
            request = ApprovalRequest(
                action_type="file_edit",
                description=f"Overwrite template file: {relative}",
                risk_level="medium",
                preview=source_file.read_text(encoding="utf-8", errors="replace")[:4000],
                working_dir=str(target),
                reason=f"Scaffold {entry.category.value} template into the selected workspace.",
            )
            if not manager.ask(request):
                skipped.append(relative)
                continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_file, destination)
        copied.append(relative)
    if session_logger:
        session_logger.log(
            "project.scaffolded",
            {"category": entry.category.value, "target": str(target), "files": copied},
            f"Scaffolded {entry.category.value} template",
            workflow_id="registry",
        )
    return ScaffoldResult(target_dir=target, copied_files=copied, skipped_files=skipped)

"""Interactive approval-manager wiring and process-scoped permission memory."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from rich.console import Console

from shamsu.safety.approval import ask_approval, ask_approval_menu
from shamsu.safety.approval_context import get_approval_override
from shamsu.safety.approval_manager import ApprovalManager
from shamsu.safety.autonomy import is_long_running_enabled
from shamsu.safety.permission_store import PermissionMemory
from shamsu.session.manager import SessionLogger
from shamsu.types import ApprovalRequest

DEFAULT_ASK_APPROVAL = ask_approval
_PERMISSION_MEMORY_CACHE: dict[Path, PermissionMemory] = {}


def get_permission_memory(workspace: Path) -> PermissionMemory:
    resolved = workspace.resolve()
    memory = _PERMISSION_MEMORY_CACHE.get(resolved)
    if memory is None:
        memory = PermissionMemory(resolved)
        _PERMISSION_MEMORY_CACHE[resolved] = memory
    return memory


def make_approval_manager(
    workspace: Path,
    session_logger: SessionLogger | None,
    console: Console,
    approval_func: Callable[[ApprovalRequest], bool] = ask_approval,
    menu_prompt_func: Callable[..., tuple[bool, str]] = ask_approval_menu,
) -> ApprovalManager:
    injected_approval = get_approval_override()
    if approval_func is DEFAULT_ASK_APPROVAL and injected_approval is not None:
        approval_func = injected_approval
    if approval_func is DEFAULT_ASK_APPROVAL and is_long_running_enabled(workspace):
        return ApprovalManager(
            approval_func=lambda _request: True,
            session_logger=session_logger,
            memory=get_permission_memory(workspace),
        )
    menu_prompt = (
        (lambda request, offer: menu_prompt_func(request, offer_remember=offer, console=console))
        if approval_func is DEFAULT_ASK_APPROVAL
        else None
    )
    return ApprovalManager(
        approval_func=approval_func,
        session_logger=session_logger,
        memory=get_permission_memory(workspace),
        menu_prompt=menu_prompt,
    )

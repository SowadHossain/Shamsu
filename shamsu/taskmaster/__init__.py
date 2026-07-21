"""Real upstream Taskmaster (task-master-ai) integration for SHAMSU.

Taskmaster owns PRD parsing, task breakdown, dependencies, task status, and
execution order. SHAMSU owns execution, safety, context, patching,
verification, memory boundaries, and logging - see
`agent context/prompts/Taskmaster.md`.
"""
from shamsu.taskmaster.adapter import TaskmasterAdapter
from shamsu.taskmaster.service import TaskmasterService

__all__ = ["TaskmasterAdapter", "TaskmasterService"]

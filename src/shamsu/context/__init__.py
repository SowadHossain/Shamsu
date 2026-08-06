"""The context compiler: repository state in, compact task packet out.

The model never receives the conversation, the repository, or the memory
system. It receives a compiled frame assembled per call under an explicit token
budget.

Two properties the runtime relies on: compilation is **deterministic** (the
same state produces the same frame, which is what makes a bad decision
reproducible), and **hot context is never silently dropped** — if the task, the
current step, or the latest observation will not fit, that is an error rather
than a quiet truncation.

Milestone 4. See plan section 19.
"""

from shamsu.context.compiler import ContextCompiler, ContextTooLarge, FrameInputs, Section

__all__ = ["ContextCompiler", "ContextTooLarge", "FrameInputs", "Section"]

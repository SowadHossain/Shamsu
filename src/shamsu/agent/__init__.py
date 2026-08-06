"""Task classification, planning, step execution, repair, and completion.

Each of these is a bounded controller invoked by the runtime, not a loop in its
own right. The bounded inner execution loop lives in `runtime`.

Milestones 4-7. See plan sections 20, 21, 25, 27.
"""

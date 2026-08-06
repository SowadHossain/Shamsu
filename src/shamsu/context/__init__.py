"""The context compiler: repository state in, compact task packet out.

The model never receives the conversation, the repository, or the memory
system. It receives a compiled frame assembled per call under an explicit token
budget.

Milestone 4. See plan section 19.
"""

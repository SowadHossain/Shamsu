"""Typed state records and the authoritative SQLite store.

Every fact the runtime relies on to make a transition lives here. If it is not
in SQLite, it is not authoritative -- artifacts, memory, and model output are
all derived or advisory.

Milestone 2. See plan section 12.
"""

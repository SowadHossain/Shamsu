"""Scored evaluations, not pass/fail tests.

`test_retrieval_accuracy.py` scores retrieval and needs no model. `tasks.py`
and `harness.py` score *the agent*, which means they need a real one — so they
never run as part of `pytest tests/`. See `test_task_suite.py` for how that is
enforced.
"""

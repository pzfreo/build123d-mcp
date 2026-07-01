"""Shared op-budget derivation for worker-run tools.

A tool runs *inside* the worker and sub-divides the parent-side op budget to
bound its own out-of-process work below the parent's SIGKILL deadline. That
budget is ``max(OP_BUDGET_FLOOR_S, exec_timeout)`` — identical to the parent's
``_export_budget`` in ``worker.py`` (which sets ``_EXPORT_TIMEOUT`` from the same
floor). Keeping the floor in one place means the tool budget provably tracks the
parent op budget: raise the floor and both sides move together, so a tool can
never think it has more time than the parent will grant before killing the worker.
"""

# Floor for the export/geometry op budget, shared with worker._EXPORT_TIMEOUT.
OP_BUDGET_FLOOR_S = 60


def op_budget(session) -> int:
    """Seconds a worker-run tool may spend, matching the parent op budget."""
    return max(OP_BUDGET_FLOOR_S, int(getattr(session, "exec_timeout", 120)))

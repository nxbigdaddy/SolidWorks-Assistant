"""Separate model response inactivity from a bounded multi-call agent turn."""

from __future__ import annotations


def turn_budget_seconds(response_timeout: float) -> float:
    # A turn may contain up to eight investigations and multiple model replies.
    # Retain a hard ceiling so continuous tokens cannot keep it alive forever.
    return min(1800.0, max(300.0, float(response_timeout) * 10.0))

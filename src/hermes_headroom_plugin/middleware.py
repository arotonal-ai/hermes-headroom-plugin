"""Behavior-changing middleware hooks."""
from __future__ import annotations


def on_llm_request(**kwargs):
    # P0 intentionally does not mutate provider requests. Advanced request routing
    # belongs behind explicit P3 gates.
    del kwargs
    return None

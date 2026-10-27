"""Shared by the fixture ops through a relative import (proves the namespace package imports by path)."""

PROMPT_PARTS = ("shapes", "changes", "findings", "feedback", "task", "declared_inputs")


def numbered(errors):
    return "\n".join(f"{i}. {e}" for i, e in enumerate(errors, 1))

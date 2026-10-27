"""Prompt templates: strict slot filling.

The consumer owns the template file; llm-kit binds and renders it (decision
Q32). A template is an optional header for people, one line that is exactly
``---PROMPT---``, then the prompt text; a file without the marker line is all
prompt (:func:`llm_kit.graph.loader.prompt_body`). A slot is ``{{name}}``
(lower-case letters, digits and ``_``, starting with a letter).

Filling is strict in both directions: a slot with no value and a value with no
slot are both errors, and a value must be a string (format other JSON values in
an operation). Values are inserted once, in a single pass, so a value that
contains ``{{x}}`` is sent as written. The result is the prompt text after the
marker with every slot replaced, then the optional ``suffix`` appended as
written: the text that produces the suffix owns its leading newlines.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from .loader import SLOT_RE, prompt_body, prompt_slots
from .model import GraphError

__all__ = ["PromptError", "render_prompt", "render_prompt_file"]


class PromptError(GraphError):
    """A prompt template cannot be filled with the slots it was given."""


def render_prompt(text: str, slots: Mapping[str, Any], *, suffix: Optional[str] = None, where: str = "") -> str:
    """Fill the template ``text`` with ``slots``; append ``suffix`` when given.

    Raises :class:`PromptError` for a slot without a value, a value without a
    slot (both are listed), a value or suffix that is not a string, or a
    template with more than one marker line.
    """
    body = prompt_body(text, where=where)
    names = prompt_slots(text, where=where)
    missing = [name for name in names if name not in slots]
    unused = sorted(str(name) for name in slots if name not in names)
    if missing or unused:
        parts = []
        if missing:
            parts.append(f"slots with no value: {missing}")
        if unused:
            parts.append(f"values with no slot: {unused}")
        raise PromptError("; ".join(parts), where)
    for name in names:
        if not isinstance(slots[name], str):
            raise PromptError(f"slot {name!r} must be a string, not {type(slots[name]).__name__}; "
                              "format other values in an operation", where)
    if suffix is not None and not isinstance(suffix, str):
        raise PromptError(f"suffix must be a string, not {type(suffix).__name__}", where)
    filled = SLOT_RE.sub(lambda match: slots[match.group(1)], body)
    return filled if suffix is None else filled + suffix


def render_prompt_file(path: Path | str, slots: Mapping[str, Any], *, suffix: Optional[str] = None) -> str:
    """:func:`render_prompt` for the UTF-8 template file at ``path``."""
    file = Path(path)
    try:
        text = file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PromptError(f"cannot read the template: {exc}", str(file)) from None
    return render_prompt(text, slots, suffix=suffix, where=str(file))

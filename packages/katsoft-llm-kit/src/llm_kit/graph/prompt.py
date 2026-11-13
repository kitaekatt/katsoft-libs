"""Prompt templates: strict slot filling.

The consumer owns the template file; llm-kit binds and renders it (decision
Q32). A template is an optional header for people, one line that is exactly
``---PROMPT---``, then the prompt text; a file without the marker line is all
prompt (:func:`llm_kit.graph.loader.prompt_body`). A slot is ``{{name}}``
(lower-case letters, digits and ``_``, starting with a letter).

The file format is ``llm-kit.prompt-file/v1``: UTF-8 bytes, one leading byte
order mark removed, CRLF and lone CR read as LF, at most one marker line. The
body hash is the SHA-256 hex digest of the body's UTF-8 bytes.
:func:`parse_prompt_file` and :func:`read_prompt_file` return the parsed form
(:class:`PromptFile`) that the JSON Schema ``llm_kit.schema.formats`` ships for the
format describes. The full contract is in ``docs/reference/llm-kit-formats.md``.

Filling is strict in both directions: a slot with no value and a value with no
slot are both errors, and a value must be a string (format other JSON values in
an operation). Values are inserted once, in a single pass, so a value that
contains ``{{x}}`` is sent as written. The result is the prompt text after the
marker with every slot replaced, then the optional ``suffix`` appended as
written: the text that produces the suffix owns its leading newlines. Values and
the suffix are not newline-normalized.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .loader import SLOT_RE, prompt_body, prompt_parts, prompt_slots
from .model import GraphError

__all__ = [
    "PromptError",
    "PromptFile",
    "parse_prompt_file",
    "read_prompt_file",
    "render_prompt",
    "render_prompt_file",
]


class PromptError(GraphError):
    """A prompt template cannot be read, or cannot be filled with the slots it was given."""


@dataclass(frozen=True)
class PromptFile:
    """One prompt file, parsed under ``llm-kit.prompt-file/v1``.

    ``header`` is the text before the marker line, or ``None`` when the file
    has no marker line. ``body`` is the prompt text (after the marker line, or
    the whole file). ``slots`` are the body's slot names in order of first use.
    ``sha256`` is the lowercase SHA-256 hex digest of ``body`` encoded as UTF-8.
    ``source`` names where the text came from (a path), or is ``None``.
    """

    header: Optional[str]
    body: str
    slots: tuple[str, ...]
    sha256: str
    source: Optional[str] = None

    def to_json(self) -> dict[str, Any]:
        """The parsed form as JSON data; it conforms to the format's JSON Schema."""
        out: dict[str, Any] = {
            "header": self.header,
            "body": self.body,
            "slots": list(self.slots),
            "sha256": self.sha256,
        }
        if self.source is not None:
            out["source"] = self.source
        return out


def _decode(data: bytes, where: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PromptError(f"the template is not UTF-8: {exc}", where) from None


def parse_prompt_file(data: bytes | str, *, where: str = "") -> PromptFile:
    """Parse a prompt file's bytes (or its decoded text) under ``llm-kit.prompt-file/v1``.

    ``data`` as bytes is decoded strictly as UTF-8. Then one leading U+FEFF is
    removed, every CRLF and lone CR becomes LF, and the text is split at the
    one marker line. ``where`` names the source in errors and becomes
    :attr:`PromptFile.source` when it is not empty. Returns a
    :class:`PromptFile`. Raises :class:`PromptError` for bytes that are not
    UTF-8 and :class:`GraphError` for more than one marker line.
    """
    text = _decode(data, where) if isinstance(data, (bytes, bytearray)) else data
    header, body = prompt_parts(text, where=where)
    return PromptFile(
        header=header,
        body=body,
        slots=prompt_slots(text, where=where),
        sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        source=where or None,
    )


def read_prompt_file(path: Path | str) -> PromptFile:
    """:func:`parse_prompt_file` for the file at ``path``; the path is the source.

    Raises :class:`PromptError` when the file cannot be read or is not UTF-8,
    and :class:`GraphError` for more than one marker line.
    """
    file = Path(path)
    try:
        data = file.read_bytes()
    except OSError as exc:
        raise PromptError(f"cannot read the template: {exc}", str(file)) from None
    return parse_prompt_file(data, where=str(file))


def render_prompt(text: str, slots: Mapping[str, Any], *, suffix: Optional[str] = None, where: str = "") -> str:
    """Fill the template ``text`` with ``slots``; append ``suffix`` when given.

    ``text`` follows the newline and byte order mark rules of
    :func:`parse_prompt_file`. Returns the filled body plus the suffix.
    Raises :class:`PromptError` for a slot without a value, a value without a
    slot (both are listed), a value or suffix that is not a string, and
    :class:`GraphError` for a template with more than one marker line.
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
    """:func:`render_prompt` for the UTF-8 template file at ``path``.

    Raises :class:`PromptError` when the file cannot be read or is not UTF-8,
    and as :func:`render_prompt` does.
    """
    file = Path(path)
    try:
        text = file.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise PromptError(f"cannot read the template: {exc}", str(file)) from None
    return render_prompt(text, slots, suffix=suffix, where=str(file))

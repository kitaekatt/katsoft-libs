"""Reply cache formats (``llm-kit.reply-key/v1``, ``llm-kit.reply-entry/v1``): the request key and the stored entry.

The TypeScript package ``@katsoft/llm-kit-send`` keeps a reply cache on its
non-stream calls: a stored refusal, or a stored success of a temperature-0 chat
call, answers a request with the same wire bytes without a send. This module
reads the same formats so a Python tool can compute the same keys and check the
same entries; it keeps no cache and sends nothing. The shared cases in
``tests/formats/reply-key/cases.json`` and ``tests/formats/reply-entry/cases.json``
fix both readers. Contract: ``docs/reference/llm-kit-formats.md``,
"llm-kit.reply-key/v1" and "llm-kit.reply-entry/v1".

It checks the entry format itself, as the TypeScript reader does; the tests
validate the shared cases against the schema
``llm_kit.schema.formats.REPLY_ENTRY_V1`` too. Stdlib only.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Union

__all__ = [
    "REPLY_ENTRY_FORMAT",
    "REPLY_KEY_FORMAT",
    "FormPart",
    "ReplyEntryError",
    "read_entry",
    "request_key",
    "step_key",
]

#: The format id of a request key: the first part of every step key.
REPLY_KEY_FORMAT = "llm-kit.reply-key/v1"
#: The format id of a stored entry: the value of its ``format`` field.
REPLY_ENTRY_FORMAT = "llm-kit.reply-entry/v1"

_KEPT_REQUEST_HEADERS = frozenset({"accept", "content-type"})
_STEP_KEYS = ("format", "layer", "outcome", "status", "headers", "body", "at")
_OPERATION_KEYS = ("format", "layer", "outcome", "status", "note", "at")


@dataclass(frozen=True)
class FormPart:
    """One entry of a multipart form body, in form order.

    A text field has ``filename`` and ``type`` empty and its text as UTF-8
    ``data``; a file has its file name, media type and bytes.
    """

    name: str
    data: bytes
    filename: str = ""
    type: str = ""


Body = Union[str, bytes, Sequence[FormPart], None]


class ReplyEntryError(ValueError):
    """A stored value that breaks ``llm-kit.reply-entry/v1``.

    ``path`` names the place (``note.code``, ``headers.content-type``, or ``$``
    for the whole value); the message starts with it.
    """

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


def _bytes(part: str | bytes) -> bytes:
    return part.encode("utf-8") if isinstance(part, str) else bytes(part)


def request_key(parts: Sequence[str | bytes]) -> str:
    """The key of a list of parts: lowercase hex SHA-256 of each part as ``<byte length>:<bytes>\\n``, in order.

    A ``str`` part is its UTF-8 bytes. The framing keeps parts apart, so
    ``["ab", "c"]`` and ``["a", "bc"]`` give different keys. Raises nothing.
    """
    digest = hashlib.sha256()
    for part in parts:
        data = _bytes(part)
        digest.update(f"{len(data)}:".encode("ascii"))
        digest.update(data)
        digest.update(b"\n")
    return digest.hexdigest()


def _header_lines(headers: Mapping[str, str]) -> str:
    kept = sorted((name.lower(), value) for name, value in headers.items() if name.lower() in _KEPT_REQUEST_HEADERS)
    return "\n".join(f"{name}:{value}" for name, value in kept)


def _body_parts(body: Body) -> list[str | bytes]:
    if body is None:
        return [""]
    if isinstance(body, (str, bytes, bytearray)):
        return [bytes(body) if isinstance(body, bytearray) else body]
    parts: list[str | bytes] = []
    for entry in body:
        if not isinstance(entry, FormPart):
            raise TypeError("a form body is a sequence of FormPart")
        parts.extend([entry.name, entry.filename, entry.type, entry.data])
    return parts


def step_key(account: str, method: str, url: str, headers: Mapping[str, str], body: Body,
             scope: Sequence[str] = ()) -> str:
    """The step key of one request, as ``stepKey`` of ``@katsoft/llm-kit-send`` computes it.

    The parts, in order: the format id, ``account``, one part
    ``scope:<request_key(scope)>`` when ``scope`` is not empty, ``method`` in
    upper case, ``url`` as sent, the kept request headers (``accept`` and
    ``content-type``, names lowercased, sorted, one ``name:value`` line each, as
    one part), and the body: a ``str`` or ``bytes`` as one part, ``None`` as one
    empty part, a form as four parts per entry (``name``, ``filename``, ``type``,
    the bytes). Secrets, attribution headers and the multipart boundary are not
    inputs. Raises :class:`TypeError` for a form entry that is not a
    :class:`FormPart`.
    """
    return request_key([
        REPLY_KEY_FORMAT, account, *([f"scope:{request_key(list(scope))}"] if scope else []),
        method.upper(), url, _header_lines(headers), *_body_parts(body),
    ])


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _base64(value: str) -> bool:
    if len(value) % 4:
        return False
    try:
        base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return False
    return True


def read_entry(value: Any) -> dict[str, Any]:
    """Checks a parsed stored value against ``llm-kit.reply-entry/v1`` and returns it.

    Besides the schema it applies the reader rules: ``body`` is canonical
    base64 with padding, response header names are lowercase and not empty,
    and ``note.code`` is not empty. Raises :class:`ReplyEntryError` with the
    path of the first rule the value breaks, in the order the TypeScript
    ``readEntry`` checks them.
    """
    if not isinstance(value, Mapping):
        raise ReplyEntryError("$", "an entry is an object")
    if value.get("format") != REPLY_ENTRY_FORMAT:
        raise ReplyEntryError("format", f'must be "{REPLY_ENTRY_FORMAT}"')
    layer = value.get("layer")
    if layer not in ("step", "operation"):
        raise ReplyEntryError("layer", 'must be "step" or "operation"')
    allowed = _STEP_KEYS if layer == "step" else _OPERATION_KEYS
    for key in allowed:
        if key not in value:
            raise ReplyEntryError(key, "is required")
    for key in value:
        if key not in allowed:
            raise ReplyEntryError(str(key), "is not a field of this layer")
    outcomes = ("refusal", "success") if layer == "step" else ("refusal",)
    if value["outcome"] not in outcomes:
        raise ReplyEntryError("outcome", f"must be one of {', '.join(outcomes)}")
    status = value["status"]
    if not _integer(status) or not 100 <= status <= 599:
        raise ReplyEntryError("status", "must be an integer from 100 to 599")
    if not _integer(value["at"]) or value["at"] < 0:
        raise ReplyEntryError("at", "must be an integer of 0 or more")
    if layer == "step":
        headers = value["headers"]
        if not isinstance(headers, Mapping):
            raise ReplyEntryError("headers", "must be an object")
        for name, header in headers.items():
            if not isinstance(header, str):
                raise ReplyEntryError(f"headers.{name}", "must be a string")
            if not name or name != name.lower():
                raise ReplyEntryError(f"headers.{name}", "a header name is lowercase and not empty")
        if not isinstance(value["body"], str) or not _base64(value["body"]):
            raise ReplyEntryError("body", "must be base64")
    else:
        note = value["note"]
        if not isinstance(note, Mapping):
            raise ReplyEntryError("note", "must be an object")
        for key in ("code", "message"):
            if key not in note:
                raise ReplyEntryError(f"note.{key}", "is required")
        for key in note:
            if key not in ("code", "message"):
                raise ReplyEntryError(f"note.{key}", "is not a field of a note")
        if not isinstance(note["code"], str) or not note["code"]:
            raise ReplyEntryError("note.code", "must be a string that is not empty")
        if not isinstance(note["message"], str):
            raise ReplyEntryError("note.message", "must be a string")
    return dict(value)

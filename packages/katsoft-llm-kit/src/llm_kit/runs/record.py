"""The run record format ``llm-kit.run-record/v1``: validation, builders and the reuse key.

A run record is one attempt of one case on one model selection. A comparison is
the records that share one ``group.id``. The JSON Schema is
``llm_kit/schema/formats/run-record.v1.schema.json``; the contract and the
shared cases are in ``docs/reference/llm-kit-formats.md``.

The fields a record holds depend on its ``status``:

``started``  the attempt began; it has no result fields (no ``ended_at``,
             ``output``, ``error``, ``scores``, ``usage``, ``cost``,
             ``timings``).
``ok``       an accepted answer: ``output`` is required and ``error`` is null.
``invalid``  an answer arrived but failed the caller's parse or contract:
             ``output`` and ``error`` are required.
``failed``   no usable answer: ``error`` is required, ``output`` may be null.
``refused``  the model or provider declined: ``error`` is required, ``output``
             may be null.

Text bodies (the case input, the answer, a score note) are not in the record:
the record names them with ``blob:<sha256>`` references into the store's blob
store (:mod:`llm_kit.graph.media` syntax).

This module is standard library only and never touches a store.
"""

from __future__ import annotations

import copy
import datetime as _dt
import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional, Union

from llm_kit.graph.media import BLOB_PREFIX, StoreError, check_json
from llm_kit.graph.model import GraphError
from llm_kit.graph.records import check_record_text, new_run_id, utc_now
from llm_kit.pipeline.artifacts.freshness.hashing import FULL_DIGEST_LENGTH, digest, stable_json
from llm_kit.schema.formats import RUN_RECORD_V1, load_schema
from llm_kit.schema.json_schema import validate

__all__ = [
    "CONTENT_CLASSES",
    "RECORD_STATUSES",
    "RESULT_STATUSES",
    "REUSE_KEY_VERSION",
    "RUN_RECORD_V1",
    "RunRecordError",
    "blob_ref",
    "blob_refs",
    "finish_record",
    "reuse_key",
    "start_record",
    "validate_record",
]

#: Content classes, from least to most sensitive. A record's class covers the record and every blob it names.
CONTENT_CLASSES = ("synthetic", "internal", "sensitive")
#: Every record status. ``started`` is the started event; the others are results.
RECORD_STATUSES = ("started", "ok", "invalid", "failed", "refused")
#: The statuses of a result event.
RESULT_STATUSES = RECORD_STATUSES[1:]
#: The version tag hashed into every reuse key; a change to the key contract is a new tag.
REUSE_KEY_VERSION = "llm-kit.run-key/v1"

_ID_RE = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{4,32}$")
_TIME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z$")
_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_SCHEMA = load_schema(RUN_RECORD_V1)


class RunRecordError(ValueError):
    """A run record does not conform to ``llm-kit.run-record/v1``.

    ``code`` is always ``invalid``. ``where`` is the JSON Pointer of the first
    problem (``""`` for the record as a whole, ``/case/input`` for a field).
    """

    def __init__(self, where: str, message: str) -> None:
        super().__init__(f"run record {where or '/'}: {message}")
        self.code = "invalid"
        self.where = where


def _pointer(*tokens: Union[str, int]) -> str:
    return "".join("/" + str(t).replace("~", "~0").replace("/", "~1") for t in tokens)


def _time_key(text: str, where: str) -> tuple[_dt.datetime, str]:
    match = _TIME_RE.match(text)
    if match is None:
        raise RunRecordError(where, f"{text!r} is not a UTC time YYYY-MM-DDTHH:MM:SS[.fraction]Z")
    try:
        moment = _dt.datetime.strptime(match.group(1), "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        raise RunRecordError(where, f"{text!r} is not a real date and time") from None
    return moment, (match.group(2) or "").ljust(9, "0")


def _check_hex(value: Any, where: str) -> None:
    if value is not None and not _HEX_RE.match(value):
        raise RunRecordError(where, f"{value!r} is not a lowercase SHA-256 hex digest")


def _check_blob(value: Any, where: str) -> None:
    if value is not None and not (value.startswith(BLOB_PREFIX) and _HEX_RE.match(value[len(BLOB_PREFIX):])):
        raise RunRecordError(where, f"{value!r} is not a blob reference blob:<64 lowercase hex>")


def _check_path(value: Any, where: str) -> None:
    if value is None:
        return
    try:
        check_record_text(value)
    except GraphError:
        raise RunRecordError(where, f"{value!r} is not a project-relative POSIX path") from None


def _schema_errors(record: Mapping) -> tuple:
    status = record.get("status")
    if status not in RECORD_STATUSES:
        return (("/status", "enum"),)
    branch = {"$defs": _SCHEMA["$defs"], "$ref": f"#/$defs/{status}"}
    return validate(branch, record)


def _reader_rules(record: Mapping) -> None:
    if not _ID_RE.match(record["id"]):
        raise RunRecordError("/id", f"{record['id']!r} is not YYYYMMDDTHHMMSSffffffZ-<4 to 32 lowercase hex>")
    started = _time_key(record["started_at"], "/started_at")
    if "ended_at" in record and _time_key(record["ended_at"], "/ended_at") < started:
        raise RunRecordError("/ended_at", "ended_at is before started_at")
    case = record["case"]
    _check_hex(case["input_sha256"], "/case/input_sha256")
    _check_blob(case["input"], "/case/input")
    if case["input"] is not None and case["input"] != BLOB_PREFIX + case["input_sha256"]:
        raise RunRecordError("/case/input", "case.input is not blob:<case.input_sha256>")
    prompt = record["prompt"]
    if prompt is not None:
        _check_path(prompt["file"], "/prompt/file")
        _check_hex(prompt["sha256"], "/prompt/sha256")
        _check_hex(prompt["slots_sha256"], "/prompt/slots_sha256")
    if record["resolved"] is not None:
        _check_hex(record["resolved"]["registry_sha256"], "/resolved/registry_sha256")
    _check_hex(record["request"]["sent_sha256"], "/request/sent_sha256")
    for name in sorted(record["inputs"]):
        _check_hex(record["inputs"][name], _pointer("inputs", name))
    provenance = record["provenance"]
    for path in sorted(provenance["files"]):
        _check_path(path, _pointer("provenance", "files", path))
        _check_hex(provenance["files"][path], _pointer("provenance", "files", path))
    if provenance["scorer"] is not None:
        _check_hex(provenance["scorer"]["sha256"], "/provenance/scorer/sha256")
    output = record.get("output")
    if output is not None:
        _check_blob(output["text"], "/output/text")
    for index, score in enumerate(record.get("scores", ())):
        _check_blob(score["note"], _pointer("scores", index, "note"))


def validate_record(record: Any) -> None:
    """Check that ``record`` is a ``llm-kit.run-record/v1`` record.

    ``record`` is a JSON object (a ``dict``). The check is offline: it reads no
    store and no blob. It applies the record's status branch of the JSON
    Schema, then the reader rules the schema cannot state (id, time, hex and
    blob grammars; ``ended_at`` not before ``started_at``; ``case.input`` equal
    to ``blob:<case.input_sha256>``; project-relative POSIX paths). Returns
    None. Raises :class:`RunRecordError` naming the first problem.
    """
    if not isinstance(record, Mapping):
        raise RunRecordError("", f"a run record is a JSON object, not {type(record).__name__}")
    try:
        check_json(dict(record), "record")
    except StoreError as exc:
        raise RunRecordError("", str(exc)) from None
    errors = _schema_errors(record)
    if errors:
        pointer, keyword = errors[0]
        raise RunRecordError(pointer, f"fails the schema keyword {keyword!r}")
    _reader_rules(record)


def blob_ref(data: Union[bytes, str]) -> str:
    """The ``blob:<sha256>`` reference of ``data`` (a ``str`` is hashed as UTF-8).

    Use it to name a text body in a record; pass the same bytes to the store in
    ``blobs``. Raises :class:`TypeError` for anything but ``bytes`` or ``str``.
    """
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, bytes):
        raise TypeError(f"a blob is bytes or str, not {type(data).__name__}")
    return BLOB_PREFIX + hashlib.sha256(data).hexdigest()


def blob_refs(record: Mapping) -> list[str]:
    """Every blob reference ``record`` names, each once, in field order.

    The reference fields are ``case.input``, ``output.text`` and each
    ``scores[].note``. ``record`` must be valid (:func:`validate_record`);
    raises :class:`RunRecordError` otherwise.
    """
    validate_record(record)
    refs = [record["case"]["input"]]
    if record.get("output") is not None:
        refs.append(record["output"]["text"])
    refs.extend(score["note"] for score in record.get("scores", ()))
    return list(dict.fromkeys(ref for ref in refs if ref is not None))


def reuse_key(record: Mapping) -> str:
    """The reuse key of ``record``: a 64-hex SHA-256 over every input that can change the answer.

    Key contract ``llm-kit.run-key/v1`` (docs/reference/llm-kit-formats.md):
    the key hashes ``flow``, ``case.input_sha256``, ``prompt.sha256`` and
    ``prompt.slots_sha256`` (null when there is no prompt), the model that
    answered (``resolved`` endpoint, model and effort, or the ``selection``
    model and effort when nothing was resolved, with a flag that says which),
    the whole ``request`` (``max_output_tokens``, ``extras``, ``sent_sha256``)
    and the whole ``inputs`` mapping. It does not hash the id, times, status,
    group, labels, content class, selection name, provenance or any result
    field. Two records with the same key asked the same question of the same
    model; a writer that adds anything else to the request must name it in
    ``inputs``. Raises :class:`RunRecordError` for an invalid record.
    """
    validate_record(record)
    resolved = record["resolved"]
    if resolved is not None:
        model = {"resolved": True, "endpoint": resolved["endpoint"], "model": resolved["model"],
                 "effort": resolved["effort"]}
    else:
        selection = record["selection"]
        model = {"resolved": False, "endpoint": None, "model": selection["model"], "effort": selection["effort"]}
    prompt = record["prompt"]
    payload = {
        "flow": record["flow"],
        "case": record["case"]["input_sha256"],
        "prompt": None if prompt is None else {"sha256": prompt["sha256"], "slots_sha256": prompt["slots_sha256"]},
        "model": model,
        "request": record["request"],
        "inputs": record["inputs"],
    }
    return digest(REUSE_KEY_VERSION.encode("ascii"), stable_json(payload), length=FULL_DIGEST_LENGTH)


_DEFAULT_PROVENANCE = {"git_sha": None, "git_dirty": None, "tool": None, "files": {}, "scorer": None}


def start_record(
    *,
    flow: str,
    group: Mapping[str, Any],
    case: Mapping[str, Any],
    selection: Mapping[str, Any],
    content: str,
    tool: Mapping[str, Any],
    prompt: Optional[Mapping[str, Any]] = None,
    resolved: Optional[Mapping[str, Any]] = None,
    request: Optional[Mapping[str, Any]] = None,
    inputs: Optional[Mapping[str, str]] = None,
    provenance: Optional[Mapping[str, Any]] = None,
    labels: Optional[Mapping[str, str]] = None,
    record_id: Optional[str] = None,
    started_at: Optional[str] = None,
) -> dict:
    """A new ``started`` record (the started event of one attempt).

    The mappings take the format's field names. Omitted fields get their empty
    form: ``group.label`` null, ``case.input`` null, ``selection.name`` and
    ``selection.effort`` null, ``request`` with ``max_output_tokens`` null,
    ``extras`` ``{}`` and ``sent_sha256`` null, ``inputs`` and ``labels``
    ``{}``, and ``provenance`` with ``tool`` (``{name, version}``, required)
    and nulls elsewhere. ``record_id`` defaults to a new id and
    ``started_at`` to now. Returns a new dict. Raises :class:`RunRecordError`
    when the result is not a valid record.
    """
    record = {
        "format": RUN_RECORD_V1,
        "id": record_id or new_run_id(),
        "status": "started",
        "started_at": started_at or utc_now(),
        "flow": flow,
        "group": {"label": None, **group},
        "case": {"input": None, **case},
        "prompt": None if prompt is None else {"id": None, "file": None, "slots_sha256": None, **prompt},
        "selection": {"name": None, "effort": None, **selection},
        "resolved": None if resolved is None else {"row": None, "endpoint": None, "effort": None,
                                                    "registry_sha256": None, **resolved},
        "request": {"max_output_tokens": None, "extras": {}, "sent_sha256": None, **(request or {})},
        "inputs": dict(inputs or {}),
        "provenance": {**_DEFAULT_PROVENANCE, **(provenance or {}), "tool": {"version": None, **tool}},
        "content": content,
        "labels": dict(labels or {}),
    }
    record = copy.deepcopy(record)
    validate_record(record)
    return record


def finish_record(
    started: Mapping[str, Any],
    *,
    status: str,
    output: Optional[Mapping[str, Any]] = None,
    error: Optional[Mapping[str, Any]] = None,
    scores: Sequence[Mapping[str, Any]] = (),
    usage: Optional[Mapping[str, Any]] = None,
    cost: Optional[Mapping[str, Any]] = None,
    timings: Optional[Mapping[str, Any]] = None,
    ended_at: Optional[str] = None,
) -> dict:
    """The result event of the attempt ``started`` (a ``started`` record).

    The result repeats every field of ``started`` unchanged and adds
    ``ended_at`` (default now), ``output`` (``{text, parsed}``; ``parsed``
    defaults to null), ``error`` (``{code, message}``; ``message`` defaults to
    null), ``scores``, ``usage``, ``cost`` and ``timings``. ``status`` is one of
    :data:`RESULT_STATUSES`. Returns a new dict. Raises :class:`RunRecordError`
    when ``started`` is not a started record or the result is not valid (an
    ``ok`` result without ``output``, a ``failed`` one without ``error``).
    """
    validate_record(started)
    if started["status"] != "started":
        raise RunRecordError("/status", f"finish_record needs a started record, not {started['status']!r}")
    if status not in RESULT_STATUSES:
        raise RunRecordError("/status", f"{status!r} is not a result status {RESULT_STATUSES}")
    record = copy.deepcopy(dict(started))
    record.update({
        "status": status,
        "ended_at": ended_at or utc_now(),
        "output": None if output is None else {"parsed": None, **output},
        "error": None if error is None else {"message": None, **error},
        "scores": [dict(score) for score in scores],
        "usage": None if usage is None else dict(usage),
        "cost": None if cost is None else dict(cost),
        "timings": None if timings is None else dict(timings),
    })
    record = copy.deepcopy(record)
    validate_record(record)
    return record

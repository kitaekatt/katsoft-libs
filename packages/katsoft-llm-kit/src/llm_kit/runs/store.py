"""The run store: write-once record events, a content-addressed blob store and a policy.

A run store is a directory::

    store.json                          llm-kit.run-store/v1: the content classes it accepts
    .gitignore                          ignores the derived index (written by init)
    records/<YYYY-MM>/<id>.started.json the started event of an attempt (optional)
    records/<YYYY-MM>/<id>.result.json  the result event of an attempt
    blobs/<h[:2]>/<h>                   blob bytes, named by their SHA-256
    .index.sqlite                       the derived index (llm_kit.runs.index); never committed

``YYYY-MM`` comes from the record id. Every file is written once and never
replaced, so two clones of a store never conflict. An attempt has at most one
started event and at most one result event; the result repeats every field of
the started event unchanged. The record of an id is its result event, or its
started event while no result exists (an unfinished attempt stays visible with
status ``started``).

Write contract (:meth:`RunRecordStore.put_many`). Before it writes any file, the
store checks the whole batch: every record is valid; every record's declared
content class is in the store's ``accepts``; every blob a record names is in
the batch or already in the store; every blob in the batch hashes to its
reference and is named by a record of the batch; no event conflicts with a file
already written. One failed check refuses the whole batch and writes nothing.

What the store can and cannot detect: it cannot read text and decide what it is
about, so it does not detect personal data, secrets or client content inside a
record or a blob. It enforces the declared class (one class covers a record and
every blob it names, so a writer with parts of different classes declares the
most sensitive one), the policy, blob completeness, blob hashes and the
record format's path rules (no absolute or machine paths in path fields). The
declared class is the writer's statement; the store refuses what that statement
and the policy do not allow.

The library never runs git. This module is standard library only.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from llm_kit.graph.media import BLOB_PREFIX, ContentStore, StoreError, read_file_bytes, replace_file
from llm_kit.schema.formats import RUN_IMPORT_V1, RUN_STORE_V1, load_schema
from llm_kit.schema.json_schema import validate

from .record import CONTENT_CLASSES, RunRecordError, blob_ref, blob_refs, validate_record

__all__ = [
    "INDEX_FILE",
    "RUN_IMPORT_V1",
    "RUN_STORE_V1",
    "STORE_ENV",
    "PutResult",
    "RunRecordStore",
    "RunStoreError",
    "import_file",
    "read_import_file",
    "resolve_store_path",
]

#: The environment variable that names the run store when no path is given.
STORE_ENV = "LLM_KIT_RUN_STORE"
#: The derived index file at the store root; the store's ``.gitignore`` ignores it.
INDEX_FILE = ".index.sqlite"
_IGNORE_LINE = INDEX_FILE + "*"
_STORE_SCHEMA = load_schema(RUN_STORE_V1)
_IMPORT_SCHEMA = load_schema(RUN_IMPORT_V1)
_KINDS = ("started", "result")

Blobs = Mapping[str, Union[bytes, str]]


class RunStoreError(ValueError):
    """The store refused an operation; nothing was written.

    ``code`` names the reason: ``no-store`` (no store path given or
    configured), ``invalid`` (a record, an import line or ``store.json``),
    ``policy`` (a content class the store does not accept), ``incomplete`` (a
    named blob is neither supplied nor stored), ``unreferenced`` (a supplied
    blob no record names), ``blob-hash`` (supplied bytes do not match their
    reference), ``conflict`` (an event differs from one already written, or a
    result changes a field of its started event), ``not-found`` (no record or
    blob), ``index-not-ignored`` (the store's ``.gitignore`` does not ignore
    the index).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def resolve_store_path(explicit: Union[str, Path, None] = None, *, env: Optional[Mapping[str, str]] = None) -> Path:
    """The run store directory: ``explicit``, else the ``LLM_KIT_RUN_STORE`` variable of ``env``.

    ``env`` defaults to ``os.environ``. Returns the path (it is not opened).
    Raises :class:`RunStoreError` ``no-store`` when neither names a store, so a
    run never falls back to a directory nobody chose.
    """
    if explicit not in (None, ""):
        return Path(explicit)
    value = (os.environ if env is None else env).get(STORE_ENV, "")
    if not value:
        raise RunStoreError("no-store", f"no run store: pass --store or set {STORE_ENV} to the store directory")
    return Path(value)


@dataclass(frozen=True)
class PutResult:
    """What a put or an import wrote, or would write when only checked.

    ``events_written`` and ``blobs_written`` count new files;
    ``events_present`` and ``blobs_present`` count files that already held the
    same bytes. ``paths`` lists the event files of the batch in batch order.
    """

    events_written: int = 0
    events_present: int = 0
    blobs_written: int = 0
    blobs_present: int = 0
    paths: tuple[Path, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict:
        """The counts as a JSON object (paths as strings)."""
        return {"events_written": self.events_written, "events_present": self.events_present,
                "blobs_written": self.blobs_written, "blobs_present": self.blobs_present,
                "paths": [str(p) for p in self.paths]}


def _event_bytes(record: Mapping) -> bytes:
    return (json.dumps(record, indent=1, sort_keys=True, ensure_ascii=True) + "\n").encode("ascii")


def _event_kind(record: Mapping) -> str:
    return "started" if record["status"] == "started" else "result"


def _create_once(path: Path, data: bytes) -> bool:
    """Create ``path`` holding ``data`` unless it exists; True when this call created it.

    The file appears whole (a hard link of a finished temporary file), so a
    reader never sees a partial event and two writers cannot both create it.
    An existing file with other bytes raises :class:`RunStoreError` ``conflict``.
    """
    if path.exists():
        if read_file_bytes(path) != data:
            raise RunStoreError("conflict", f"{path} exists with different content")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            if read_file_bytes(path) != data:
                raise RunStoreError("conflict", f"{path} exists with different content") from None
            return False
        return True
    finally:
        os.unlink(temporary)


def _same_attempt(started: Mapping, result: Mapping) -> Optional[str]:
    """The first field of ``started`` (other than status) that ``result`` changes, or None."""
    for key in sorted(started):
        if key != "status" and result.get(key) != started[key]:
            return key
    return None


def _blob_bytes(ref: Any, data: Any, where: str) -> bytes:
    if not isinstance(ref, str) or not ref.startswith(BLOB_PREFIX):
        raise RunStoreError("invalid", f"{where}: {ref!r} is not a blob reference")
    if isinstance(data, str):
        data = data.encode("utf-8")
    if not isinstance(data, bytes):
        raise RunStoreError("invalid", f"{where}: blob {ref} is bytes or str, not {type(data).__name__}")
    if blob_ref(data) != ref:
        raise RunStoreError("blob-hash", f"{where}: the bytes given for {ref} hash to {blob_ref(data)}")
    return data


class RunRecordStore:
    """One run store directory (see the module docstring for the layout and the write contract).

    Open an existing store with :meth:`open`; create one with :meth:`init`.
    ``root`` is the directory and ``accepts`` the frozenset of content classes
    the policy takes.
    """

    def __init__(self, root: Path, accepts: Iterable[str]) -> None:
        self.root = Path(root)
        self.accepts = frozenset(accepts)
        self.records_dir = self.root / "records"
        self._blobs = ContentStore(self.root / "values", self.root / "blobs", self.root / "media")

    # -- create and open --------------------------------------------------------

    @staticmethod
    def _check_descriptor(data: Any, where: str) -> frozenset:
        if validate(_STORE_SCHEMA, data):
            raise RunStoreError("invalid", f"{where} is not a {RUN_STORE_V1} document: "
                                f"{list(validate(_STORE_SCHEMA, data))}")
        if len(set(data["accepts"])) != len(data["accepts"]):
            raise RunStoreError("invalid", f"{where}: accepts names a class twice")
        return frozenset(data["accepts"])

    @classmethod
    def init(cls, root: Union[str, Path], *, accepts: Sequence[str]) -> "RunRecordStore":
        """Create a store at ``root`` that accepts the content classes ``accepts``.

        Writes ``store.json`` and a ``.gitignore`` that ignores the index (or
        adds that line to an existing ``.gitignore``). Running it again with the
        same classes changes nothing else and returns the store; a store with a
        different policy raises :class:`RunStoreError` ``conflict`` (a policy
        is changed by editing ``store.json`` on purpose, not by init).
        ``accepts`` holds classes of :data:`~llm_kit.runs.record.CONTENT_CLASSES`.
        """
        ordered = [c for c in CONTENT_CLASSES if c in set(accepts)]
        unknown = sorted(set(accepts) - set(CONTENT_CLASSES))
        if unknown or not ordered:
            raise RunStoreError("invalid", f"accepts must name classes of {CONTENT_CLASSES}, got {list(accepts)}")
        root = Path(root)
        descriptor = {"format": RUN_STORE_V1, "accepts": ordered}
        path = root / "store.json"
        if path.exists():
            existing = cls.open(root)
            if existing.accepts != frozenset(ordered):
                raise RunStoreError("conflict", f"{path} accepts {sorted(existing.accepts)}, not {ordered}")
        else:
            root.mkdir(parents=True, exist_ok=True)
            _create_once(path, (json.dumps(descriptor, indent=1) + "\n").encode("ascii"))
        ignore = root / ".gitignore"
        lines = ignore.read_text(encoding="utf-8").splitlines() if ignore.exists() else []
        if _IGNORE_LINE not in lines:
            text = "\n".join([*lines, _IGNORE_LINE]) + "\n"
            descriptor_fd, temporary = tempfile.mkstemp(prefix=".gitignore.", dir=str(root))
            with os.fdopen(descriptor_fd, "w", encoding="utf-8", newline="\n") as stream:
                stream.write(text)
            replace_file(temporary, ignore)
        return cls.open(root)

    @classmethod
    def open(cls, root: Union[str, Path]) -> "RunRecordStore":
        """Open the store at ``root``; raise :class:`RunStoreError` when ``store.json`` is missing or invalid."""
        root = Path(root)
        path = root / "store.json"
        try:
            data = json.loads(read_file_bytes(path).decode("utf-8"))
        except FileNotFoundError:
            raise RunStoreError("not-found", f"{root} is not a run store (no store.json); "
                                "create one with `llm-kit runs init`") from None
        except (UnicodeError, ValueError) as exc:
            raise RunStoreError("invalid", f"{path} is not JSON: {exc}") from None
        return cls(root, cls._check_descriptor(data, str(path)))

    # -- paths ----------------------------------------------------------------

    def event_path(self, record_id: str, kind: str) -> Path:
        """The file of the ``kind`` (``started`` or ``result``) event of ``record_id``."""
        if kind not in _KINDS:
            raise ValueError(f"event kind {kind!r} is not one of {_KINDS}")
        return self.records_dir / f"{record_id[:4]}-{record_id[4:6]}" / f"{record_id}.{kind}.json"

    def event_files(self) -> Iterator[Path]:
        """Every event file of the store, sorted by path."""
        if not self.records_dir.is_dir():
            return iter(())
        return iter(sorted(p for p in self.records_dir.glob("*/*.json") if not p.name.startswith(".")))

    # -- write ----------------------------------------------------------------

    def put(self, record: Mapping, blobs: Optional[Blobs] = None) -> Path:
        """Write one record event (a started event or a result) with the blobs it brings.

        ``blobs`` maps each ``blob:`` reference to its bytes (or text, stored
        as UTF-8). Returns the event file. The checks and refusals are those of
        :meth:`put_many`.
        """
        return self.put_many([(record, blobs or {})]).paths[0]

    def check_many(self, items: Sequence[tuple[Mapping, Blobs]]) -> PutResult:
        """Run every check of :meth:`put_many` and report what it would write; write nothing."""
        return self._put(items, write=False)

    def put_many(self, items: Sequence[tuple[Mapping, Blobs]]) -> PutResult:
        """Write a batch of ``(record, blobs)`` events after checking the whole batch.

        The checks, in order, each over the whole batch: record validity
        (``invalid``), blob bytes against their references (``blob-hash``),
        the policy (``policy``), blob completeness (``incomplete``), supplied
        blobs that no record names (``unreferenced``), and events against the
        store and the batch (``conflict``: a different event with the same id
        and kind, or a result that changes a field of its started event). The
        first failure raises :class:`RunStoreError` and nothing is written.
        Then blobs are written, then events. Writing an event that exists with
        the same bytes is a no-op, so an import can be repeated. Returns a
        :class:`PutResult`.
        """
        return self._put(items, write=True)

    def _put(self, items: Sequence[tuple[Mapping, Blobs]], *, write: bool) -> PutResult:
        records, supplied = self._check_items(items)
        self._check_policy(records)
        named = self._check_blobs(records, supplied)
        events = self._check_events(records)
        blobs_written = blobs_present = 0
        for ref in named:
            if ref not in supplied:
                continue
            if self.has_blob(ref):
                blobs_present += 1
                continue
            blobs_written += 1
            if write:
                self._blobs.put_blob(supplied[ref])
        written = present = 0
        paths: list[Path] = []
        for path, data, new in events:
            paths.append(path)
            if not new:
                present += 1
            elif not write or _create_once(path, data):
                written += 1
            else:
                present += 1
        return PutResult(written, present, blobs_written, blobs_present, tuple(paths))

    def _check_items(self, items: Sequence[tuple[Mapping, Blobs]]) -> tuple[list[Mapping], dict[str, bytes]]:
        records: list[Mapping] = []
        supplied: dict[str, bytes] = {}
        for index, (record, blobs) in enumerate(items):
            try:
                validate_record(record)
            except RunRecordError as exc:
                raise RunStoreError("invalid", f"item {index}: {exc}") from None
            records.append(record)
            for ref, data in (blobs or {}).items():
                supplied[ref] = _blob_bytes(ref, data, f"item {index}")
        return records, supplied

    def _check_policy(self, records: Sequence[Mapping]) -> None:
        for index, record in enumerate(records):
            if record["content"] not in self.accepts:
                raise RunStoreError("policy", f"item {index} (record {record['id']}) is {record['content']!r}; "
                                    f"the store {self.root} accepts only {sorted(self.accepts)}")

    def _check_blobs(self, records: Sequence[Mapping], supplied: Mapping[str, bytes]) -> list[str]:
        named: dict[str, None] = {}
        for index, record in enumerate(records):
            for ref in blob_refs(record):
                if ref not in supplied and not self.has_blob(ref):
                    raise RunStoreError("incomplete", f"item {index} (record {record['id']}) names {ref}, "
                                        "which is neither supplied nor in the store")
                named[ref] = None
        extra = sorted(set(supplied) - set(named))
        if extra:
            raise RunStoreError("unreferenced", f"blobs no record names (their class is unknown): {extra}")
        return list(named)

    def _check_events(self, records: Sequence[Mapping]) -> list[tuple[Path, bytes, bool]]:
        batch: dict[tuple[str, str], Mapping] = {}
        out: list[tuple[Path, bytes, bool]] = []
        for index, record in enumerate(records):
            kind = _event_kind(record)
            path = self.event_path(record["id"], kind)
            data = _event_bytes(record)
            earlier = batch.get((record["id"], kind))
            if earlier is not None and _event_bytes(earlier) != data:
                raise RunStoreError("conflict", f"item {index}: two different {kind} events for {record['id']}")
            new = True
            if path.exists():
                if read_file_bytes(path) != data:
                    raise RunStoreError("conflict", f"item {index}: {path} exists with different content")
                new = False
            batch[(record["id"], kind)] = record
            out.append((path, data, new and earlier is None))
        for (record_id, kind), record in batch.items():
            if kind != "result":
                continue
            started = batch.get((record_id, "started")) or self._read_event(record_id, "started")
            if started is not None:
                changed = _same_attempt(started, record)
                if changed is not None:
                    raise RunStoreError("conflict", f"the result of {record_id} changes {changed!r} "
                                        "of its started event")
        for (record_id, kind), record in batch.items():
            if kind == "started" and (record_id, "result") not in batch:
                result = self._read_event(record_id, "result")
                if result is not None and _same_attempt(record, result) is not None:
                    raise RunStoreError("conflict", f"the started event of {record_id} differs from its result")
        return out

    # -- read -----------------------------------------------------------------

    def _read_event(self, record_id: str, kind: str) -> Optional[dict]:
        path = self.event_path(record_id, kind)
        try:
            data = read_file_bytes(path)
        except FileNotFoundError:
            return None
        try:
            record = json.loads(data.decode("ascii"))
            validate_record(record)
        except (UnicodeError, ValueError) as exc:
            raise RunStoreError("invalid", f"{path} is not a valid run record: {exc}") from None
        if record["id"] != record_id or _event_kind(record) != kind:
            raise RunStoreError("invalid", f"{path} holds the {_event_kind(record)} event of {record['id']}")
        return record

    def events(self, record_id: str) -> dict[str, Optional[dict]]:
        """``{"started": record or None, "result": record or None}`` for ``record_id``."""
        return {kind: self._read_event(record_id, kind) for kind in _KINDS}

    def get(self, record_id: str) -> dict:
        """The record of ``record_id``: its result, else its started event.

        Raises :class:`RunStoreError` ``not-found`` when the store has neither,
        ``invalid`` when an event file is not a valid record.
        """
        events = self.events(record_id)
        record = events["result"] or events["started"]
        if record is None:
            raise RunStoreError("not-found", f"no record {record_id!r} in {self.root}")
        return record

    def ids(self) -> list[str]:
        """Every record id with at least one event, sorted (ids sort by start time)."""
        return sorted({p.name.split(".", 1)[0] for p in self.event_files()})

    def records(self) -> Iterator[dict]:
        """Every record (see :meth:`get`), in id order."""
        for record_id in self.ids():
            yield self.get(record_id)

    def has_blob(self, ref: str) -> bool:
        """Whether the blob store holds ``ref`` (a ``blob:`` reference)."""
        digest = ref[len(BLOB_PREFIX):]
        return (self.root / "blobs" / digest[:2] / digest).is_file()

    def get_blob(self, ref: str) -> bytes:
        """The bytes of ``ref``; raises :class:`RunStoreError` ``not-found`` when missing or corrupt."""
        try:
            return self._blobs.get_blob(ref)
        except StoreError as exc:
            raise RunStoreError("not-found", str(exc)) from None

    def export_line(self, record_id: str) -> dict:
        """The record of ``record_id`` and its blobs as one ``llm-kit.run-import/v1`` line (a dict)."""
        record = self.get(record_id)
        blobs: dict[str, dict] = {}
        for ref in blob_refs(record):
            data = self.get_blob(ref)
            try:
                blobs[ref] = {"text": data.decode("utf-8")}
            except UnicodeDecodeError:
                blobs[ref] = {"base64": base64.b64encode(data).decode("ascii")}
        return {"format": RUN_IMPORT_V1, "record": record, "blobs": blobs}

    def index(self):
        """The derived index of this store (:class:`llm_kit.runs.index.RunIndex`)."""
        from .index import RunIndex  # noqa: PLC0415 -- the index imports this module

        return RunIndex(self)

    def check_ignored(self) -> None:
        """Raise :class:`RunStoreError` ``index-not-ignored`` unless ``.gitignore`` ignores the index."""
        ignore = self.root / ".gitignore"
        lines = ignore.read_text(encoding="utf-8").splitlines() if ignore.exists() else []
        if _IGNORE_LINE not in lines:
            raise RunStoreError("index-not-ignored", f"{ignore} does not hold the line {_IGNORE_LINE!r}; run "
                                f"`llm-kit runs init --store {self.root}` with the store's classes to add it")


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def read_import_file(source: Union[str, Path, bytes]) -> list[tuple[dict, dict[str, bytes]]]:
    """Parse and check a run import file (JSON Lines, ``llm-kit.run-import/v1``) offline.

    ``source`` is a path or the file's bytes. Each non-empty line is one JSON
    object ``{format, record, blobs}``; a final newline is allowed, an empty
    line elsewhere is not. Each line is checked against the import schema, its
    blobs are decoded (``text`` as UTF-8, ``base64`` strictly) and checked
    against their references, and its record is validated. Returns
    ``[(record, {ref: bytes})]`` in file order. Raises :class:`RunStoreError`
    (``invalid`` or ``blob-hash``) naming the line. Policy and completeness
    against a store are checked by :meth:`RunRecordStore.put_many`.
    """
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RunStoreError("invalid", f"the import file is not UTF-8: {exc}") from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    items: list[tuple[dict, dict[str, bytes]]] = []
    for number, line in enumerate(lines, start=1):
        where = f"line {number}"
        line = line[:-1] if line.endswith("\r") else line
        if not line.strip():
            raise RunStoreError("invalid", f"{where} is empty")
        try:
            value = json.loads(line, parse_constant=_reject_constant)
        except ValueError as exc:
            raise RunStoreError("invalid", f"{where} is not JSON: {exc}") from None
        problems = validate(_IMPORT_SCHEMA, value)
        if problems:
            raise RunStoreError("invalid", f"{where} is not a {RUN_IMPORT_V1} line: {list(problems)}")
        blobs: dict[str, bytes] = {}
        for ref, body in value["blobs"].items():
            if "text" in body:
                raw = body["text"].encode("utf-8")
            else:
                try:
                    raw = base64.b64decode(body["base64"], validate=True)
                except binascii.Error as exc:
                    raise RunStoreError("invalid", f"{where}: blob {ref} is not base64: {exc}") from None
            blobs[ref] = _blob_bytes(ref, raw, where)
        try:
            validate_record(value["record"])
        except RunRecordError as exc:
            raise RunStoreError("invalid", f"{where}: {exc}") from None
        items.append((value["record"], blobs))
    return items


def import_file(store: RunRecordStore, source: Union[str, Path, bytes], *, check: bool = False) -> PutResult:
    """Import a run import file into ``store`` as one batch (all or nothing).

    Reads the file with :func:`read_import_file`, then calls
    :meth:`RunRecordStore.put_many` (or :meth:`RunRecordStore.check_many` when
    ``check`` is true, which writes nothing). Returns the :class:`PutResult`.
    Raises :class:`RunStoreError` on the first refusal; nothing is written.
    """
    items = read_import_file(source)
    return store.check_many(items) if check else store.put_many(items)

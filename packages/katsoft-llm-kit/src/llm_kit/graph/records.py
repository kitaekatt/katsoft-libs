"""Run records and the per-item run store.

One run directory per work item (``runs.dir`` of the project config, with
``{item}`` filled) holds ``runs/<run_id>.json`` (one record per run or seed)
and the content-addressed stores of :mod:`llm_kit.graph.media`. The record
format (``llm-kit.graph.run/v1``) is specified in
``docs/reference/llm-kit-graph-runtime.md``; this module reads and writes it
and indexes the records of an item. It never runs anything.

Records are committed in consumer repositories and read on other machines, so
every file path in a record is relative to the project root, in POSIX form
(:func:`record_path`); :func:`resolve_record_path` turns one back into a path
under the project root.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import re
import secrets
import subprocess
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any, Optional

from .config import GraphConfig, expand_run_path
from .media import ContentStore, StoreError
from .model import GraphError

__all__ = [
    "ITEM_RE",
    "NODE_STATUSES",
    "REUSABLE_STATUSES",
    "RUN_FORMAT",
    "RUN_STATUSES",
    "RunStore",
    "list_items",
    "new_run_id",
    "record_path",
    "resolve_record_path",
    "utc_now",
]

RUN_FORMAT = "llm-kit.graph.run/v1"
RUN_KINDS = ("run", "seed")
RUN_STATUSES = ("running", "done", "failed", "stopped")
NODE_STATUSES = ("done", "reused", "seeded", "skipped", "failed", "gated", "stopped", "refused", "pending")
#: Statuses whose record can be reused (a ``seeded`` record only when valid).
REUSABLE_STATUSES = frozenset({"done", "reused", "seeded"})

ITEM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_RUN_ID_RE = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{4}$")


def record_path(root: Path | str, path: Path | str) -> str:
    """``path`` as a run record writes it: relative to the project ``root``, POSIX form.

    A path outside the project root is refused (GraphError): a record must not
    name a machine path.
    """
    base = Path(root).resolve()
    target = Path(path)
    target = (target if target.is_absolute() else base / target).resolve()
    try:
        relative = target.relative_to(base)
    except ValueError:
        raise GraphError(f"{target} is outside the project root {base}; a run record holds only "
                         "project-relative paths") from None
    return PurePosixPath(*relative.parts).as_posix()


def resolve_record_path(root: Path | str, text: str) -> Path:
    """A record's project-relative path as a path under ``root``; refuse an absolute or escaping one."""
    if not isinstance(text, str) or not text:
        raise GraphError(f"record path {text!r} is not a non-empty string")
    pure = PurePosixPath(text)
    if pure.is_absolute() or "\\" in text or ".." in pure.parts or (pure.parts and ":" in pure.parts[0]):
        raise GraphError(f"record path {text!r} is not a project-relative POSIX path")
    return Path(root).resolve().joinpath(*pure.parts)


def utc_now() -> str:
    """The current UTC time as ``YYYY-MM-DDTHH:MM:SS.ffffffZ``."""
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def new_run_id() -> str:
    """``YYYYMMDDTHHMMSSffffffZ-<4 hex>``: sorting ids sorts runs by start."""
    stamp = _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{stamp}-{secrets.token_hex(2)}"


def check_item(item: Any) -> str:
    if not isinstance(item, str) or not ITEM_RE.match(item):
        raise GraphError(f"item {item!r} must match {ITEM_RE.pattern}")
    return item


def _git_common_dir(root: Path) -> str:
    try:
        result = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=root, capture_output=True,
                                text=True, check=False)
    except FileNotFoundError:
        raise GraphError("runs paths use {git_common_dir}, but git is not installed") from None
    if result.returncode != 0:
        raise GraphError(f"runs paths use {{git_common_dir}}, but {root} is not in a git repository: "
                         f"{result.stderr.strip()}")
    path = Path(result.stdout.strip())
    return str(path if path.is_absolute() else (root / path).resolve())


def _placeholder_values(config: GraphConfig, template: str, item: Optional[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    if "{item}" in template:
        if item is None:
            raise GraphError(f"runs path {template!r} needs an item")
        values["item"] = item
    if "{git_common_dir}" in template:
        values["git_common_dir"] = _git_common_dir(config.root)
    return values


def _write_json(path: Path, payload: Mapping) -> None:
    text = json.dumps(payload, indent=1, sort_keys=True, ensure_ascii=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class RunStore(ContentStore):
    """The run directory of one work item: run records plus the content stores."""

    def __init__(self, directory: Path | str, *, blobs: Optional[Path] = None,
                 media: Optional[Path] = None) -> None:
        self.dir = Path(directory)
        self.runs_dir = self.dir / "runs"
        super().__init__(self.dir / "values", Path(blobs) if blobs else self.dir / "blobs",
                         Path(media) if media else self.dir / "media")

    @classmethod
    def for_item(cls, config: GraphConfig, item: str) -> "RunStore":
        """The run store of ``item`` from the project config (``runs.dir`` is required)."""
        check_item(item)
        if "dir" not in config.runs:
            raise GraphError(f"{config.path}: runs.dir is not configured; a run needs a run directory")

        def path(key: str) -> Optional[Path]:
            if key not in config.runs:
                return None
            template = config.runs[key]
            return expand_run_path(config.root, template, _placeholder_values(config, template, item),
                                   where=f"{config.path}: runs.{key}")

        return cls(path("dir"), blobs=path("blobs"), media=path("media"))

    # -- run records ----------------------------------------------------------

    def run_path(self, run_id: str) -> Path:
        if not _RUN_ID_RE.match(run_id):
            raise GraphError(f"run id {run_id!r} is malformed")
        return self.runs_dir / f"{run_id}.json"

    def run_ids(self) -> list[str]:
        """Every run and seed id of the item, oldest first."""
        if not self.runs_dir.is_dir():
            return []
        ids = []
        for path in self.runs_dir.iterdir():
            if path.suffix == ".json" and not path.name.startswith("."):
                if not _RUN_ID_RE.match(path.stem):
                    raise StoreError(f"{path} is not a run record name")
                ids.append(path.stem)
        return sorted(ids)

    def load_run(self, run_id: str) -> dict:
        path = self.run_path(run_id)
        try:
            record = json.loads(path.read_text(encoding="ascii"))
        except FileNotFoundError:
            raise StoreError(f"run {run_id} does not exist: {path}") from None
        except (UnicodeError, ValueError) as exc:
            raise StoreError(f"run record {path} is not ASCII JSON: {exc}") from None
        if not isinstance(record, dict) or record.get("format") != RUN_FORMAT:
            raise StoreError(f"run record {path} is not in format {RUN_FORMAT}")
        if record.get("run_id") != run_id:
            raise StoreError(f"run record {path} names run {record.get('run_id')!r}")
        return record

    def write_run(self, record: Mapping) -> Path:
        if record.get("format") != RUN_FORMAT:
            raise StoreError(f"a run record needs format {RUN_FORMAT}")
        path = self.run_path(record["run_id"])
        _write_json(path, record)
        return path

    def runs(self, *, newest_first: bool = True) -> Iterator[dict]:
        ids = self.run_ids()
        for run_id in (reversed(ids) if newest_first else ids):
            yield self.load_run(run_id)

    # -- indexes ----------------------------------------------------------------

    def records_by_path(self) -> dict[str, list[tuple[str, dict]]]:
        """node path -> ``[(run_id, node record), ...]``, newest first."""
        out: dict[str, list[tuple[str, dict]]] = {}
        for record in self.runs(newest_first=True):
            for path, node in record["nodes"].items():
                out.setdefault(path, []).append((record["run_id"], node))
        return out

    def latest_records(self) -> dict[str, tuple[str, dict]]:
        """node path -> ``(run_id, node record)`` of the newest record at that path."""
        return {path: entries[0] for path, entries in self.records_by_path().items()}

    def latest_inputs(self, graph_id: str) -> tuple[dict, dict[str, str]]:
        """The newest recorded graph inputs for ``graph_id``: ``(inputs, name -> run id)``.

        Each input name takes the value of the newest run or seed of this
        graph that recorded it.
        """
        values: dict[str, Any] = {}
        sources: dict[str, str] = {}
        for record in self.runs(newest_first=True):
            if record["graph"]["id"] != graph_id or record.get("inputs") is None:
                continue
            for name, value in self.get_value(record["inputs"]).items():
                if name not in values:
                    values[name] = value
                    sources[name] = record["run_id"]
        return values, sources


def list_items(config: GraphConfig) -> list[str]:
    """Item ids that have a run directory under ``runs.dir``."""
    if "dir" not in config.runs:
        raise GraphError(f"{config.path}: runs.dir is not configured")
    template = config.runs["dir"]
    if "{item}" not in template:
        raise GraphError(f"{config.path}: runs.dir {template!r} has no {{item}} placeholder")
    values = _placeholder_values(config, template.replace("{item}", ""), None)
    head, tail = template.split("{item}", 1)
    if "{item}" in tail:
        raise GraphError(f"{config.path}: runs.dir {template!r} names {{item}} twice")
    if (head and not head.endswith("/")) or (tail and not tail.startswith("/")):
        raise GraphError(f"{config.path}: runs.dir {template!r} must use {{item}} as a whole path segment")
    base = expand_run_path(config.root, head, values) if head else config.root
    rest = tail.format(**values) if tail else ""
    items = []
    if Path(base).is_dir():
        for entry in sorted(Path(base).iterdir()):
            if ITEM_RE.match(entry.name) and (Path(str(entry) + rest) / "runs").is_dir():
                items.append(entry.name)
    return items

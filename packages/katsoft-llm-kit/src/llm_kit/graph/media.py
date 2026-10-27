"""Content-addressed stores for graph runs: JSON values, blobs and media.

Three write-once stores, each addressed by the SHA-256 of the stored bytes:

``values``  ``<values>/<h[:2]>/<h>.json``: one JSON value as
            ``stable_json`` bytes (sorted keys, ASCII, no spaces). Node inputs,
            outputs and reads live here; the run records refer to them.
``blobs``   ``<blobs>/<h[:2]>/<h>``: raw bytes. Large text (exact prompts,
            raw answers) and graph file snapshots. A consumer may keep this
            store outside version control.
``media``   ``<media>/<h[:16]><ext>``: media files (images, audio), named by
            the first 16 hex of their SHA-256 plus the extension.

A reference string names a stored object: ``value:<64 hex>``,
``blob:<64 hex>`` or ``media:<16 hex><ext>``. Writing never replaces a file:
an existing file with the same name must hold the same bytes, else the write
raises (a 16-hex media name collision, or a corrupt store).

The record format that uses these references is in
``docs/reference/llm-kit-graph-runtime.md``.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Optional, Union

from llm_kit.pipeline.artifacts.freshness.hashing import stable_json

from .model import GraphError

__all__ = [
    "BLOB_PREFIX",
    "MEDIA_PREFIX",
    "VALUE_PREFIX",
    "ContentStore",
    "StoreError",
    "check_json",
    "ref_kind",
]

VALUE_PREFIX = "value:"
BLOB_PREFIX = "blob:"
MEDIA_PREFIX = "media:"

_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MEDIA_RE = re.compile(r"^[0-9a-f]{16}\.[a-z0-9]{1,8}$")
_EXT_RE = re.compile(r"^\.[a-z0-9]{1,8}$")


class StoreError(GraphError):
    """A store object is missing, corrupt, or a reference is malformed."""


def check_json(value: Any, where: str = "value") -> None:
    """Raise :class:`StoreError` unless ``value`` is plain JSON.

    Plain JSON: ``dict`` with string keys, ``list``, ``str``, ``int``, finite
    ``float``, ``bool`` and ``None``. Tuples, sets and other objects are refused.
    """
    stack = [(value, where)]
    while stack:
        item, at = stack.pop()
        if item is None or isinstance(item, (bool, str, int)):
            continue
        if isinstance(item, float):
            if not math.isfinite(item):
                raise StoreError(f"{at}: non-finite number is not JSON")
            continue
        if isinstance(item, list):
            stack.extend((v, f"{at}[{i}]") for i, v in enumerate(item))
            continue
        if isinstance(item, dict):
            for key, v in item.items():
                if not isinstance(key, str):
                    raise StoreError(f"{at}: key {key!r} is not a string")
                stack.append((v, f"{at}.{key}"))
            continue
        raise StoreError(f"{at}: {type(item).__name__} is not a JSON value")


def ref_kind(ref: Any) -> str:
    """``value``, ``blob`` or ``media`` for a well-formed reference; else raise."""
    if isinstance(ref, str):
        if ref.startswith(VALUE_PREFIX) and _HASH_RE.match(ref[len(VALUE_PREFIX):]):
            return "value"
        if ref.startswith(BLOB_PREFIX) and _HASH_RE.match(ref[len(BLOB_PREFIX):]):
            return "blob"
        if ref.startswith(MEDIA_PREFIX) and _MEDIA_RE.match(ref[len(MEDIA_PREFIX):]):
            return "media"
    raise StoreError(f"{ref!r} is not a store reference (value:<64 hex>, blob:<64 hex> or media:<16 hex><ext>)")


def _write_once(path: Path, data: bytes) -> None:
    if path.exists():
        if path.read_bytes() != data:
            raise StoreError(f"{path} exists with different content")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


class ContentStore:
    """The value, blob and media stores of one run directory."""

    def __init__(self, values: Path, blobs: Path, media: Path) -> None:
        self.values_dir = Path(values)
        self.blobs_dir = Path(blobs)
        self.media_dir = Path(media)

    # -- values -------------------------------------------------------------

    def put_value(self, value: Any) -> str:
        """Store a JSON value; return its ``value:`` reference."""
        check_json(value)
        data = stable_json(value)
        digest = hashlib.sha256(data).hexdigest()
        _write_once(self.values_dir / digest[:2] / f"{digest}.json", data)
        return VALUE_PREFIX + digest

    def get_value(self, ref: str) -> Any:
        if ref_kind(ref) != "value":
            raise StoreError(f"{ref!r} is not a value reference")
        digest = ref[len(VALUE_PREFIX):]
        path = self.values_dir / digest[:2] / f"{digest}.json"
        data = self._read(path, digest)
        return json.loads(data.decode("ascii"))

    # -- blobs --------------------------------------------------------------

    def put_blob(self, data: Union[bytes, str]) -> str:
        """Store bytes (a ``str`` is stored as UTF-8); return its ``blob:`` reference."""
        if isinstance(data, str):
            data = data.encode("utf-8")
        if not isinstance(data, bytes):
            raise StoreError(f"a blob is bytes or str, not {type(data).__name__}")
        digest = hashlib.sha256(data).hexdigest()
        _write_once(self.blobs_dir / digest[:2] / digest, data)
        return BLOB_PREFIX + digest

    def get_blob(self, ref: str) -> bytes:
        if ref_kind(ref) != "blob":
            raise StoreError(f"{ref!r} is not a blob reference")
        digest = ref[len(BLOB_PREFIX):]
        return self._read(self.blobs_dir / digest[:2] / digest, digest)

    def get_text(self, ref: str) -> str:
        """A blob decoded as UTF-8."""
        return self.get_blob(ref).decode("utf-8")

    # -- media --------------------------------------------------------------

    def put_media(self, source: Union[bytes, Path, str], ext: Optional[str] = None) -> str:
        """Store a media file (bytes, or a path to copy); return its ``media:`` reference.

        ``ext`` (``.png``) is required for bytes; for a path it defaults to the
        path's suffix in lower case.
        """
        if isinstance(source, (str, Path)):
            path = Path(source)
            data = path.read_bytes()
            if ext is None:
                ext = path.suffix.lower()
        elif isinstance(source, bytes):
            data = source
        else:
            raise StoreError(f"media is bytes or a path, not {type(source).__name__}")
        if not isinstance(ext, str) or not _EXT_RE.match(ext):
            raise StoreError(f"media extension {ext!r} must match {_EXT_RE.pattern}")
        name = hashlib.sha256(data).hexdigest()[:16] + ext
        _write_once(self.media_dir / name, data)
        return MEDIA_PREFIX + name

    def media_path(self, ref: str) -> Path:
        """The file of a ``media:`` reference (it must exist)."""
        if ref_kind(ref) != "media":
            raise StoreError(f"{ref!r} is not a media reference")
        path = self.media_dir / ref[len(MEDIA_PREFIX):]
        if not path.is_file():
            raise StoreError(f"media {ref} is missing: {path}")
        return path

    # -- any reference --------------------------------------------------------

    def resolve(self, ref: str) -> Any:
        """A value as JSON, a blob as UTF-8 text, a media reference as its ``Path``."""
        kind = ref_kind(ref)
        if kind == "value":
            return self.get_value(ref)
        if kind == "blob":
            return self.get_text(ref)
        return self.media_path(ref)

    def put_attribute(self, value: Any) -> str:
        """An op attribute other than ``out``: a string goes to the blob store, else a value."""
        if isinstance(value, str):
            return self.put_blob(value)
        return self.put_value(value)

    def get_attribute(self, ref: str) -> Any:
        return self.get_text(ref) if ref_kind(ref) == "blob" else self.get_value(ref)

    @staticmethod
    def _read(path: Path, digest: str) -> bytes:
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise StoreError(f"store object {digest} is missing: {path}") from None
        if hashlib.sha256(data).hexdigest() != digest:
            raise StoreError(f"store object {path} does not match its hash")
        return data

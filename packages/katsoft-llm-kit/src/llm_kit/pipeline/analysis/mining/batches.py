"""Stable batches: chunk a corpus so an unchanged chunk keeps its key across runs.

Items are grouped (a conversation, a document), kept in their given order inside a group, and cut into chunks of at
most ``size``. A chunk's key is a hash of its item ids, so a cache keyed by batch key reuses every chunk whose items did
not change. Appending items to a group changes only that group's last chunk and adds new ones after it.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Callable, Generic, Sequence, TypeVar

T = TypeVar("T")

#: Hex characters of a batch key: 64 bits, ample for a corpus-sized set of chunks.
KEY_CHARS = 16


@dataclass(frozen=True)
class Batch(Generic[T]):
    """One chunk: its stable ``key``, the ``group`` it came from, its position in that group and its items."""

    key: str
    group: str
    index: int
    items: tuple[T, ...]


def batch_key(ids: Sequence[str]) -> str:
    """The key of a chunk with these item ids, in order: ``KEY_CHARS`` hex characters. An empty list raises ``ValueError``."""
    if not ids:
        raise ValueError("a batch needs at least one item")
    return hashlib.sha256("\n".join(ids).encode("utf-8")).hexdigest()[:KEY_CHARS]


def stable_batches(items: Sequence[T], *, group: Callable[[T], str], ident: Callable[[T], str],
                   size: int) -> list[Batch[T]]:
    """Group ``items`` (groups in order of first appearance), then cut each group into chunks of at most ``size``.

    Returns the batches in group order, then chunk order. ``ident`` must be unique across ``items``; a repeated id
    raises ``ValueError``, because two chunks could then share a key. A ``size`` below 1 raises ``ValueError``.
    """
    if size < 1:
        raise ValueError(f"size must be at least 1, got {size}")
    seen: set[str] = set()
    groups: dict[str, list[T]] = {}
    for item in items:
        item_id = ident(item)
        if item_id in seen:
            raise ValueError(f"item id {item_id!r} occurs twice")
        seen.add(item_id)
        groups.setdefault(group(item), []).append(item)
    out: list[Batch[T]] = []
    for name, members in groups.items():
        for index, start in enumerate(range(0, len(members), size)):
            chunk = tuple(members[start:start + size])
            out.append(Batch(key=batch_key([ident(i) for i in chunk]), group=name, index=index, items=chunk))
    return out


__all__ = ["KEY_CHARS", "Batch", "batch_key", "stable_batches"]

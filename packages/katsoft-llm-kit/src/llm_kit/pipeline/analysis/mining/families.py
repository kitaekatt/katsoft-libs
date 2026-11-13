"""Reduce tagged records per family: how often, how widely, and with which details.

A record is one tagged occurrence (one label on one corpus item) as a plain mapping. The caller names which fields
count distinct values (``dims``: the item, the person, the conversation), which boolean fields become shares
(``flags``), and which mapping field holds free-form details whose values are tallied (``details``).
"""
from __future__ import annotations

from collections import Counter
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence


def _detail_values(record: Mapping[str, Any], field: Optional[str]) -> Mapping[str, Any]:
    if field is None:
        return {}
    value = record.get(field) or {}
    if not isinstance(value, Mapping):
        raise TypeError(f"record field {field!r} must be a mapping, got {type(value).__name__}")
    return value


def family_stats(records: Iterable[Mapping[str, Any]], *, family_of: Callable[[Mapping[str, Any]], str],
                 dims: Sequence[str] = (), flags: Sequence[str] = (), details: Optional[str] = None,
                 top: int = 8, totals: Optional[Mapping[str, int]] = None) -> list[dict]:
    """Per family: ``records``, distinct counts per dim, the true share per flag, and per detail key its count and
    ``top`` most common values (lower case, trimmed). ``totals`` (dim -> corpus size) adds ``share`` per dim.
    Families are ordered by records, most first, then by id. A record missing a dim field raises ``KeyError``.
    """
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for record in records:
        groups.setdefault(family_of(record), []).append(record)
    out = []
    for family, rows in groups.items():
        distinct = {dim: len({row[dim] for row in rows}) for dim in dims}
        entry: dict[str, Any] = {
            "family": family,
            "records": len(rows),
            "distinct": distinct,
            "flags": {flag: sum(1 for row in rows if row.get(flag) is True) / len(rows) for flag in flags},
            "details": _tally_details(rows, details, top),
        }
        if totals:
            entry["share"] = {dim: distinct[dim] / totals[dim] for dim in dims if totals.get(dim)}
        out.append(entry)
    return sorted(out, key=lambda e: (-e["records"], e["family"]))


def _tally_details(rows: Sequence[Mapping[str, Any]], field: Optional[str], top: int) -> dict:
    keys: Counter = Counter()
    values: dict[str, Counter] = {}
    for row in rows:
        for key, value in _detail_values(row, field).items():
            text = str(value).strip().lower()
            if not text:
                continue
            keys[key] += 1
            values.setdefault(key, Counter())[text] += 1
    return {key: {"count": n, "top": [[v, c] for v, c in sorted(values[key].items(), key=lambda kv: (-kv[1], kv[0]))[:top]]}
            for key, n in sorted(keys.items(), key=lambda kv: (-kv[1], kv[0]))}


__all__ = ["family_stats"]

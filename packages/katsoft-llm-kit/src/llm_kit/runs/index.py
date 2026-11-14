"""The derived index of a run store: metadata only, rebuilt from the record events.

The index is one SQLite file at the store root (``.index.sqlite``). It is a
cache: the record events are the truth, :meth:`RunIndex.sync` brings the index
up to date with them (new, changed and removed event files), and
:meth:`RunIndex.rebuild` makes it again from nothing. A query syncs first, so
records that arrive by ``git pull`` are found without a manual step.

The index holds metadata and never text: ids, statuses, times, flow, group id
and kind, case id, selection and model names, content class, cost, token
counts, total time, error code and the reuse key. It holds no group label, no
label, no error message, no blob and no output. The store's ``.gitignore``
must ignore it (:meth:`llm_kit.runs.store.RunRecordStore.check_ignored`); the
index refuses to open otherwise, so it is never committed by accident.

This module is standard library only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from typing import Any, Optional

from .record import reuse_key
from .store import INDEX_FILE, RunRecordStore

__all__ = ["INDEX_COLUMNS", "INDEX_VERSION", "RunIndex"]

#: The index layout version; an index file of another version is rebuilt.
INDEX_VERSION = "1"

#: The columns of a query row, in order.
INDEX_COLUMNS = (
    "id", "status", "started_at", "ended_at", "flow", "group_id", "group_kind", "case_id",
    "selection_name", "selection_model", "model", "endpoint", "effort", "content", "cost_usd",
    "input_tokens", "output_tokens", "total_ms", "error_code", "reuse_key",
)

_ROW_SQL = ", ".join(f"{name}" for name in INDEX_COLUMNS)


def _fraction_key(text: Optional[str]) -> Optional[str]:
    """A time as ``YYYY-MM-DDTHH:MM:SS.fffffffffZ`` so that text order is time order."""
    if text is None:
        return None
    head, _, rest = text[:-1].partition(".")
    return f"{head}.{rest.ljust(9, '0')}Z"


def _row(record: Mapping) -> dict[str, Any]:
    resolved = record["resolved"]
    selection = record["selection"]
    usage = record.get("usage") or {}
    cost = record.get("cost") or {}
    timings = record.get("timings") or {}
    error = record.get("error") or {}
    return {
        "id": record["id"],
        "status": record["status"],
        "started_at": record["started_at"],
        "ended_at": record.get("ended_at"),
        "flow": record["flow"],
        "group_id": record["group"]["id"],
        "group_kind": record["group"]["kind"],
        "case_id": record["case"]["id"],
        "selection_name": selection["name"],
        "selection_model": selection["model"],
        "model": resolved["model"] if resolved else selection["model"],
        "endpoint": resolved["endpoint"] if resolved else None,
        "effort": resolved["effort"] if resolved else selection["effort"],
        "content": record["content"],
        "cost_usd": cost.get("usd"),
        "input_tokens": usage.get("input_tokens"),
        "output_tokens": usage.get("output_tokens"),
        "total_ms": timings.get("total_ms"),
        "error_code": error.get("code"),
        "reuse_key": reuse_key(record),
    }


class RunIndex:
    """The index of one :class:`~llm_kit.runs.store.RunRecordStore`.

    Construct it with ``store.index()``. Raises
    :class:`~llm_kit.runs.store.RunStoreError` ``index-not-ignored`` when the
    store's ``.gitignore`` does not ignore the index file.
    """

    def __init__(self, store: RunRecordStore) -> None:
        store.check_ignored()
        self.store = store
        self.path = store.root / INDEX_FILE

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure(self, connection: sqlite3.Connection) -> None:
        connection.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        version = connection.execute("SELECT value FROM meta WHERE key = 'version'").fetchone()
        if version is not None and version[0] == INDEX_VERSION:
            return
        connection.execute("DROP TABLE IF EXISTS files")
        connection.execute("DROP TABLE IF EXISTS records")
        connection.execute("CREATE TABLE files (file TEXT PRIMARY KEY, id TEXT NOT NULL, size INTEGER NOT NULL,"
                           " mtime_ns INTEGER NOT NULL)")
        columns = ", ".join(f"{name} {'TEXT PRIMARY KEY' if name == 'id' else ''}".strip() for name in INDEX_COLUMNS)
        connection.execute(f"CREATE TABLE records ({columns}, started_key TEXT NOT NULL)")
        connection.execute("INSERT OR REPLACE INTO meta (key, value) VALUES ('version', ?)", (INDEX_VERSION,))

    def sync(self) -> dict[str, int]:
        """Bring the index up to date with the event files; return ``{"records", "updated", "removed"}``.

        ``records`` is the number of records in the index after the sync,
        ``updated`` the records re-read, ``removed`` the records whose events
        are all gone. Raises :class:`~llm_kit.runs.store.RunStoreError` when an
        event file is not a valid record.
        """
        with closing(self._connect()) as connection, connection:
            self._ensure(connection)
            known = {row["file"]: (row["id"], row["size"], row["mtime_ns"])
                     for row in connection.execute("SELECT file, id, size, mtime_ns FROM files")}
            current: dict[str, tuple[str, int, int]] = {}
            for path in self.store.event_files():
                stat = path.stat()
                name = path.relative_to(self.store.root).as_posix()
                current[name] = (path.name.split(".", 1)[0], stat.st_size, stat.st_mtime_ns)
            changed = {entry[0] for name, entry in current.items() if known.get(name) != entry}
            changed |= {entry[0] for name, entry in known.items() if name not in current}
            removed = 0
            for record_id in sorted(changed):
                events = self.store.events(record_id)
                record = events["result"] or events["started"]
                connection.execute("DELETE FROM records WHERE id = ?", (record_id,))
                if record is None:
                    removed += 1
                    continue
                row = _row(record)
                connection.execute(
                    f"INSERT INTO records ({_ROW_SQL}, started_key) VALUES "
                    f"({', '.join('?' for _ in INDEX_COLUMNS)}, ?)",
                    (*[row[name] for name in INDEX_COLUMNS], _fraction_key(row["started_at"])),
                )
            connection.execute("DELETE FROM files")
            connection.executemany("INSERT INTO files (file, id, size, mtime_ns) VALUES (?, ?, ?, ?)",
                                   [(name, *entry) for name, entry in sorted(current.items())])
            total = connection.execute("SELECT COUNT(*) FROM records").fetchone()[0]
        return {"records": total, "updated": len(changed) - removed, "removed": removed}

    def rebuild(self) -> dict[str, int]:
        """Delete the index file and make it again from the events; return the :meth:`sync` counts."""
        for suffix in ("", "-journal", "-wal", "-shm"):
            target = self.path.with_name(self.path.name + suffix)
            if target.exists():
                target.unlink()
        return self.sync()

    def query(
        self,
        *,
        flow: Optional[str] = None,
        group: Optional[str] = None,
        model: Optional[str] = None,
        status: Optional[str] = None,
        case: Optional[str] = None,
        content: Optional[str] = None,
        key: Optional[str] = None,
        since: Optional[str] = None,
        until: Optional[str] = None,
        limit: Optional[int] = None,
    ) -> list[dict[str, Any]]:
        """Index rows that match every given filter, oldest start first.

        ``model`` matches the answering model or the selection model; ``key``
        is a reuse key (:func:`~llm_kit.runs.record.reuse_key`); ``since`` and
        ``until`` are a date (``YYYY-MM-DD``) or a time prefix, and select
        records that started at or after ``since`` and before ``until``.
        ``limit`` keeps the newest rows of the match. Each row is a dict of
        :data:`INDEX_COLUMNS`. Syncs the index first.
        """
        self.sync()
        where: list[str] = []
        args: list[Any] = []
        for column, value in (("flow", flow), ("group_id", group), ("status", status), ("case_id", case),
                              ("content", content), ("reuse_key", key)):
            if value is not None:
                where.append(f"{column} = ?")
                args.append(value)
        if model is not None:
            where.append("(model = ? OR selection_model = ?)")
            args.extend([model, model])
        if since is not None:
            where.append("started_key >= ?")
            args.append(since)
        if until is not None:
            where.append("started_key < ?")
            args.append(until)
        sql = f"SELECT {_ROW_SQL} FROM records"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY started_key DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        with closing(self._connect()) as connection:
            rows = [dict(row) for row in connection.execute(sql, args)]
        return list(reversed(rows))

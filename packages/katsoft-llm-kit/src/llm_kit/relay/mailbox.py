"""Relay mailbox: the SQLite ledger behind messaging, grants, audit and named conversations.

One database file per machine (``mailbox.sqlite3`` under the relay state
directory, see :func:`mailbox_db_path`), opened through :mod:`llm_kit.storage`.
Every write runs in ``BEGIN IMMEDIATE``, so concurrent relay threads or
processes serialize on the write lock and a read-then-write sequence cannot
interleave. ``schema_version`` is 3: version 2 (katsoft-llm-kit 1.21.0,
amendment 2) adds the nullable ``messages.forwarded_by`` and
``audit.on_behalf_of`` columns; version 3 (amendment 3, remote resume) adds
the nullable ``messages.claimed_by`` column and the ``opt_ins`` and
``resume_leases`` tables. An older file is migrated in place (old rows keep
null).

What lives here (protocol sections 10-15 of ``llm-kit-relay-protocol.md``):

- **Messages.** ``send_message`` creates a ``queued`` row (or a ``refused`` one
  for a retry-proof refusal); ``poll``, ``ack`` and ``reply`` move it through
  the F0 state machine. Every move goes through
  :func:`llm_kit.relay.protocol.require_transition`, so an illegal transition
  raises ``invalid_transition``. A message id is an idempotency key: a resend of
  the same id and content returns the original row (``replayed``) and delivers
  nothing again. Bodies are capped at 8192 UTF-8 bytes, ``hop`` above 1 is
  refused, and per-sender, per-target-queue and global rate limits apply
  (:class:`MailboxLimits`).
- **Delivery.** At least once: a message not acked within ``ack_timeout_s``
  (60 s) is returned by a later ``poll`` again with ``attempt`` increased. A
  message past ``expires_at`` that is still ``queued`` or ``delivered`` becomes
  ``lapsed`` (never redelivered stale).
- **Grants.** ``mint_grant``, ``check_grant`` (no side effect) and
  ``consume_grant`` (one use, atomic: of N concurrent consumers of a one-use
  grant exactly one wins) and ``refund_grant`` (give one reserved use back for
  an operation that did not take effect). ``send_message`` can consume its
  grant in the same transaction as the insert, so a refused or replayed send
  consumes nothing.
- **Audit.** An append-only table: this class offers ``append_audit`` and
  ``audit_rows`` only, and the table carries triggers that abort any UPDATE or
  DELETE, so even raw SQL cannot rewrite a row. Every mailbox state change
  appends a ``transition`` row; the relay server appends ``call`` rows with
  :meth:`Mailbox.append_audit`. Rows carry the origin identity.
- **Resume claims (amendment 3).** ``continue_session`` stores its message
  and, in the same IMMEDIATE transaction, claims it: the row moves
  ``queued -> delivered`` with ``claimed_by`` set to a fresh lease id, and a
  ``resume_leases`` row (one per address) records the run's phase and child.
  ``poll``, ``ack``, ``reply``, expiry and ``end_session`` never touch a
  claimed row. Every lease write is fenced by its lease id (:class:`LeaseLost`
  when another writer took the lease), and :meth:`Mailbox.finish_claim` is the
  one way a claim ends: it writes the reply and the terminal state, clears
  ``claimed_by`` and deletes the lease in one transaction. ``opt_ins`` records
  every address that registered with a non-empty ``accepts``; only those may
  be resumed.
- **Conversations.** ``append_turn(s)`` / ``load_conversation`` keep the named
  conversations ``ask_llm`` continues, keyed by (endpoint, conversation).

Not here: the pause flags (``lifecycle.PauseStore``), registrations and
selector resolution (the relay resolves a target to an address before calling
in), token checks and origin-tier checks (the server applies protocol section
6 before calling). Security posture: grants are a guardrail under alternative
A, not a boundary against a same-user model.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence, Union

from llm_kit.relay import protocol as P
from llm_kit.relay.config import StatePaths
from llm_kit.storage.connection import ConnectionPolicy, connect
from llm_kit.storage.schema_version import read_schema_version
from llm_kit.storage.transaction import read_transaction, write_transaction

SCHEMA_VERSION = 3
_OPT_INS_DDL = (
    "CREATE TABLE opt_ins (address TEXT PRIMARY KEY, first_at REAL NOT NULL, last_at REAL NOT NULL)"
)
_LEASES_DDL = (
    "CREATE TABLE resume_leases (address TEXT PRIMARY KEY, message_id TEXT NOT NULL UNIQUE REFERENCES messages (id),"
    " lease_id TEXT NOT NULL UNIQUE, proc_tag TEXT NOT NULL UNIQUE, relay_instance TEXT NOT NULL, phase TEXT NOT NULL,"
    " child_pid INTEGER,"
    " child_start REAL, pgid INTEGER, created_at REAL NOT NULL, expires_at REAL NOT NULL)"
)
#: Version -> the statements that move a mailbox from the previous version to it (additive only).
_MIGRATIONS: Mapping[int, tuple[str, ...]] = {
    2: (
        "ALTER TABLE messages ADD COLUMN forwarded_by TEXT",
        "ALTER TABLE audit ADD COLUMN on_behalf_of TEXT",
    ),
    3: (
        "ALTER TABLE messages ADD COLUMN claimed_by TEXT",
        _OPT_INS_DDL,
        _LEASES_DDL,
    ),
}
#: Phases of a resume lease: claimed (nothing spawned), spawning (a child may exist, pid not yet recorded),
#: running (child pid, create time and process group recorded).
LEASE_PHASES = ("claimed", "spawning", "running")
MAILBOX_FILENAME = "mailbox.sqlite3"
_DB_MODE = 0o600

#: Refusal codes a caller may pass for a refusal only it can detect (it owns
#: registrations); the mailbox detects ``hop_limit`` and ``too_large`` itself.
CALLER_REFUSALS = frozenset({"not_opted_in", "session_ended"})
#: Codes stored as a ``refused`` row (protocol section 12).
STORED_REFUSALS = frozenset({"not_opted_in", "session_ended", "hop_limit", "too_large"})

MAX_AUDIT_DETAIL_CHARS = 2048
MAX_CONVERSATION_NAME_CHARS = 64
MAX_TURN_CHARS = 32768
_GRANT_PRUNE_AFTER_S = 86400.0
_CONVERSATION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def mailbox_db_path(paths: StatePaths) -> Path:
    """The mailbox database inside the relay state directory."""
    return paths.root / MAILBOX_FILENAME


@dataclass(frozen=True)
class MailboxLimits:
    """Rate limits and caps. Every value must be a positive integer.

    ``sender_per_window`` counts messages one sender identity created in the
    last ``window_s`` seconds; ``global_per_window`` counts every sender;
    ``max_pending_per_target`` bounds ``queued`` plus ``delivered`` messages
    for one address. Exceeding any is ``rate_limited`` (retryable) and creates
    no row. A replay creates nothing and is never counted.
    """

    window_s: int = 60
    sender_per_window: int = 30
    global_per_window: int = 240
    max_pending_per_target: int = 64
    max_conversation_turns: int = 200

    def __post_init__(self) -> None:
        for name in (
            "window_s", "sender_per_window", "global_per_window",
            "max_pending_per_target", "max_conversation_turns",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError(f"MailboxLimits.{name} must be a positive integer, got {value!r}")


@dataclass(frozen=True)
class GrantUse:
    """A call's grant reference: the calling session and the optional ``X-Relay-Grant`` id."""

    session: str
    grant_id: Optional[str] = None


_SCHEMA = """
CREATE TABLE schema_version (version INTEGER NOT NULL);
CREATE TABLE messages (
    id TEXT PRIMARY KEY,
    to_addr TEXT NOT NULL,
    sender TEXT NOT NULL,
    kind TEXT NOT NULL,
    body TEXT NOT NULL,
    hop INTEGER NOT NULL,
    task TEXT,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    state TEXT NOT NULL,
    state_reason TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    delivered_at REAL,
    content_hash TEXT NOT NULL,
    forwarded_by TEXT,
    claimed_by TEXT
);
CREATE INDEX messages_to_state ON messages (to_addr, state);
CREATE INDEX messages_sender_created ON messages (sender, created_at);
CREATE INDEX messages_created ON messages (created_at);
CREATE TABLE replies (
    id TEXT PRIMARY KEY,
    in_reply_to TEXT NOT NULL UNIQUE REFERENCES messages (id),
    sender TEXT NOT NULL,
    state TEXT NOT NULL,
    body TEXT NOT NULL,
    at REAL NOT NULL
);
CREATE TABLE audit (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    at REAL NOT NULL,
    machine TEXT NOT NULL,
    ingress TEXT NOT NULL,
    event TEXT NOT NULL,
    origin TEXT NOT NULL,
    verb TEXT NOT NULL,
    outcome TEXT NOT NULL,
    request_id TEXT,
    target TEXT,
    grant_id TEXT,
    message_id TEXT,
    from_state TEXT,
    to_state TEXT,
    code TEXT,
    detail TEXT,
    on_behalf_of TEXT
);
CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit
BEGIN SELECT RAISE(ABORT, 'audit rows are append-only'); END;
CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit
BEGIN SELECT RAISE(ABORT, 'audit rows are append-only'); END;
CREATE TABLE grants (
    grant_id TEXT PRIMARY KEY,
    session TEXT NOT NULL,
    verb TEXT NOT NULL,
    targets TEXT NOT NULL,
    issued_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    uses INTEGER NOT NULL,
    uses_remaining INTEGER NOT NULL,
    origin_proof TEXT NOT NULL,
    origin TEXT NOT NULL,
    command TEXT
);
CREATE INDEX grants_session_verb ON grants (session, verb);
{OPT};
{LEASE};
CREATE TABLE conversation_turns (
    endpoint TEXT NOT NULL,
    conversation TEXT NOT NULL,
    seq INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    at REAL NOT NULL,
    PRIMARY KEY (endpoint, conversation, seq)
);
"""


_SCHEMA = _SCHEMA.replace("{OPT}", _OPT_INS_DDL).replace("{LEASE}", _LEASES_DDL)


class LeaseLost(Exception):
    """A resume lease write found that ``lease_id`` no longer owns the lease (another writer recovered it).

    Not a wire error: the runner that gets it stops its own child and writes nothing."""


@dataclass(frozen=True)
class LeaseClaim:
    """What ``send_message(lease=...)`` needs to claim the stored message for a ``continue_session`` run.

    ``lease_id`` is the fencing token (replaced when a relay takes the lease over for recovery); ``proc_tag``
    is the immutable tag the run's processes carry in their environment, so a later relay still finds them
    after any number of takeovers. It defaults to the lease id."""

    lease_id: str
    relay_instance: str
    lease_s: float
    proc_tag: Optional[str] = None

    @property
    def tag(self) -> str:
        return self.proc_tag or self.lease_id


def _fingerprint(*parts: Any) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


def _identity(text: Union[str, P.Identity]) -> P.Identity:
    return text if isinstance(text, P.Identity) else P.parse_identity(text)


def _bad(message: str) -> P.RelayError:
    return P.RelayError("bad_request", message)


class Mailbox:
    """The relay's mailbox ledger. Cheap to construct; one connection per operation.

    ``machine`` is this machine's fleet label (it names the relay identity in
    audit rows). ``clock`` returns epoch seconds and is the only time source.
    """

    def __init__(
        self,
        db_path: Union[str, Path],
        *,
        machine: str,
        clock: Callable[[], float] = time.time,
        limits: Optional[MailboxLimits] = None,
        ack_timeout_s: int = P.ACK_TIMEOUT_S,
        busy_timeout_ms: int = 5000,
    ) -> None:
        if not P.is_machine_label(machine):
            raise ValueError(f"{machine!r} is not a machine label")
        if not isinstance(ack_timeout_s, int) or ack_timeout_s < 1:
            raise ValueError("ack_timeout_s must be a positive integer")
        self.db_path = Path(db_path)
        self.machine = machine
        self.limits = limits or MailboxLimits()
        self.ack_timeout_s = ack_timeout_s
        self._clock = clock
        self._policy = ConnectionPolicy(
            busy_timeout_ms=busy_timeout_ms,
            post_wal_pragmas=(("synchronous", "NORMAL"),),
            explicit_transactions=True,
        )
        self._relay_identity = f"relay:{machine}"
        self._init_schema()

    # -- connection plumbing ------------------------------------------------

    def _connect(self):
        return connect(self.db_path, self._policy)

    def _write(self):
        return write_transaction(self._connect)

    def _read(self):
        return read_transaction(self._connect)

    def _init_schema(self) -> None:
        if not self.db_path.exists():
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.db_path), os.O_CREAT | os.O_RDWR, _DB_MODE)
            os.close(fd)
        with self._connect() as conn:
            if read_schema_version(conn, as_int=True) == SCHEMA_VERSION:
                return
        with self._write() as conn:
            version = read_schema_version(conn, as_int=True)  # re-read under the lock
            if version == SCHEMA_VERSION:
                return
            if version == 0:
                for statement in _split_script(_SCHEMA):
                    conn.execute(statement)
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
                return
            if not 1 <= version < SCHEMA_VERSION:
                raise P.RelayError(
                    "internal", f"mailbox {self.db_path} has schema_version {version}, expected {SCHEMA_VERSION}"
                )
            for step in range(version + 1, SCHEMA_VERSION + 1):
                for statement in _MIGRATIONS[step]:
                    conn.execute(statement)
            conn.execute("UPDATE schema_version SET version = ?", (SCHEMA_VERSION,))

    def schema_version(self) -> int:
        with self._connect() as conn:
            return read_schema_version(conn, as_int=True)

    # -- audit ----------------------------------------------------------------

    def _audit(
        self,
        conn: sqlite3.Connection,
        *,
        event: str,
        origin: Union[str, P.Identity],
        verb: str,
        outcome: str,
        ingress: str = "relay",
        request_id: Optional[str] = None,
        target: Optional[str] = None,
        grant_id: Optional[str] = None,
        message_id: Optional[str] = None,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        code: Optional[str] = None,
        detail: Optional[str] = None,
        at: Optional[float] = None,
        on_behalf_of: Optional[Union[str, P.Identity]] = None,
    ) -> int:
        if event not in P.AUDIT_EVENTS:
            raise _bad(f"unknown audit event {event!r}")
        if outcome not in P.AUDIT_OUTCOMES:
            raise _bad(f"unknown audit outcome {outcome!r}")
        if ingress not in P.AUDIT_INGRESSES:
            raise _bad(f"unknown audit ingress {ingress!r}")
        if verb not in P.VERBS and verb not in P.AUDIT_INTERNAL_VERBS:
            raise _bad(f"unknown audit verb {verb!r}")
        if code is not None and code not in P.ERROR_CODES:
            raise _bad(f"unknown audit code {code!r}")
        origin_text = str(_identity(origin))
        delegate = None if on_behalf_of is None else str(_identity(on_behalf_of))
        if detail is not None:
            detail = detail[:MAX_AUDIT_DETAIL_CHARS]
        cursor = conn.execute(
            "INSERT INTO audit (at, machine, ingress, event, origin, verb, outcome, request_id, target,"
            " grant_id, message_id, from_state, to_state, code, detail, on_behalf_of)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self._clock() if at is None else at, self.machine, ingress, event, origin_text, verb, outcome,
                request_id, target, grant_id, message_id, from_state, to_state, code, detail, delegate,
            ),
        )
        return int(cursor.lastrowid)

    def append_audit(
        self,
        *,
        event: str,
        origin: Union[str, P.Identity],
        verb: str,
        outcome: str,
        ingress: str = "relay",
        request_id: Optional[str] = None,
        target: Optional[str] = None,
        grant_id: Optional[str] = None,
        message_id: Optional[str] = None,
        from_state: Optional[str] = None,
        to_state: Optional[str] = None,
        code: Optional[str] = None,
        detail: Optional[str] = None,
        on_behalf_of: Optional[Union[str, P.Identity]] = None,
    ) -> P.AuditRow:
        """Append one audit row (the server records a ``call`` row per request)."""
        with self._write() as conn:
            seq = self._audit(
                conn, event=event, origin=origin, verb=verb, outcome=outcome, ingress=ingress,
                request_id=request_id, target=target, grant_id=grant_id, message_id=message_id,
                from_state=from_state, to_state=to_state, code=code, detail=detail, on_behalf_of=on_behalf_of,
            )
            return _audit_row(conn.execute("SELECT * FROM audit WHERE seq = ?", (seq,)).fetchone())

    def audit_rows(
        self, *, since_seq: int = 0, limit: int = 100, message_id: Optional[str] = None,
        code: Optional[str] = None,
    ) -> list[P.AuditRow]:
        """Audit rows with ``seq`` above ``since_seq``, oldest first (read-only); ``code`` keeps one error code."""
        if limit < 1:
            raise _bad("limit must be positive")
        sql = "SELECT * FROM audit WHERE seq > ?"
        args: list[Any] = [since_seq]
        if message_id is not None:
            sql += " AND message_id = ?"
            args.append(message_id)
        if code is not None:
            sql += " AND code = ?"
            args.append(code)
        sql += " ORDER BY seq LIMIT ?"
        args.append(limit)
        with self._read() as conn:
            return [_audit_row(r) for r in conn.execute(sql, args).fetchall()]

    # -- messages: state machine ----------------------------------------------

    def _transition(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        to_state: str,
        *,
        verb: str,
        origin: Union[str, P.Identity],
        now: float,
        reason: Optional[str] = None,
        delivered_at: Optional[float] = None,
        attempts: Optional[int] = None,
    ) -> None:
        """The one place a message changes state: check F0, guard on the old state, audit."""
        P.require_transition(row["state"], to_state, row["kind"])
        cursor = conn.execute(
            "UPDATE messages SET state = ?, state_reason = COALESCE(?, state_reason), updated_at = ?,"
            " delivered_at = COALESCE(?, delivered_at), attempts = COALESCE(?, attempts)"
            " WHERE id = ? AND state = ?",
            (to_state, reason, now, delivered_at, attempts, row["id"], row["state"]),
        )
        if cursor.rowcount != 1:
            raise P.RelayError("invalid_transition", f"message {row['id']} changed state concurrently")
        self._audit(
            conn, event="transition", origin=origin, verb=verb, outcome="ok", message_id=row["id"],
            target=row["to_addr"], from_state=row["state"], to_state=to_state, at=now,
            detail=reason,
        )

    def _expire(self, conn: sqlite3.Connection, now: float, address: Optional[str] = None) -> int:
        sql = "SELECT * FROM messages WHERE state IN ('queued', 'delivered') AND claimed_by IS NULL AND expires_at <= ?"
        args: list[Any] = [now]
        if address is not None:
            sql += " AND to_addr = ?"
            args.append(address)
        rows = conn.execute(sql + " ORDER BY created_at, rowid", args).fetchall()
        for row in rows:
            self._transition(
                conn, row, "lapsed", verb="expire", origin=self._relay_identity, now=now, reason="expired"
            )
        return len(rows)

    def expire(self) -> int:
        """Lapse every ``queued`` or ``delivered`` message past ``expires_at``; returns the count."""
        with self._write() as conn:
            return self._expire(conn, self._clock())

    def end_session(
        self,
        address: str,
        *,
        reason: str = "session_ended",
        verb: str = "deregister",
        origin: Optional[Union[str, P.Identity]] = None,
    ) -> int:
        """Move a gone or renumbered session's unfinished messages to ``session-ended``.

        ``queued``, ``delivered`` and (for kinds that expect a reply) ``acked``
        messages to ``address`` move; a ``note`` is complete once acked and
        terminal messages stay put. Returns how many moved.
        """
        P.parse_address(address)
        now = self._clock()
        with self._write() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE to_addr = ? AND state IN ('queued', 'delivered', 'acked')"
                " AND claimed_by IS NULL ORDER BY created_at, rowid",
                (address,),
            ).fetchall()
            moved = 0
            for row in rows:
                if not P.transition_allowed(row["state"], "session-ended", row["kind"]):
                    continue
                self._transition(
                    conn, row, "session-ended", verb=verb, origin=origin or self._relay_identity,
                    now=now, reason=reason,
                )
                moved += 1
            return moved

    # -- messages: send --------------------------------------------------------

    def send_message(
        self,
        *,
        origin: Union[str, P.Identity],
        to: str,
        kind: str,
        body: str,
        id: Optional[str] = None,
        expires_in_s: Optional[int] = None,
        task: Optional[str] = None,
        hop: int = 0,
        grant: Optional[GrantUse] = None,
        refuse: Optional[str] = None,
        ingress: str = "relay",
        forwarded_by: Optional[Union[str, P.Identity]] = None,
        lease: Optional[LeaseClaim] = None,
        recheck: Optional[Callable[[], None]] = None,
    ) -> P.SendMessageResult:
        """Queue one message for ``to`` (a RESOLVED address) and return ``{message, replayed}``.

        ``grant``, when given, is checked first (every ``grant_*`` refusal
        leaves no trace, a replay included) and consumed in the same
        transaction as the insert, only when the send succeeds. ``refuse`` is a
        refusal only the caller can detect (``not_opted_in``, ``session_ended``):
        the row is stored ``refused`` and the error raised. Order: shape
        (``bad_request``), grant, replay / ``idempotency_conflict``,
        ``rate_limited``, ``hop_limit``, ``too_large``, ``refuse``, insert.
        A refusal stored as a row raises AFTER that row is committed; a replay
        of a refused id raises the same refusal again, writing nothing.

        ``forwarded_by`` (amendment 2) is ``relay:<M>`` for a send the relay
        accepted from the authenticated peer ``<M>`` on behalf of ``origin``,
        which must then be ``direct:<M>/claude/<S>``. It is stored on the row
        and returned on the record, and the transition audit row names the
        peer as its origin with ``origin`` as ``on_behalf_of``. Only the relay's
        authenticated relay-origin path passes it.

        ``lease`` (amendment 3, ``continue_session``) claims the new row for a
        resume run in the same transaction: an address that already holds a
        lease is ``rate_limited`` (``what: resume_running``) with no row, the
        row moves ``queued -> delivered`` with ``claimed_by`` set, and a lease
        row with phase ``claimed`` is inserted. ``recheck`` runs inside the
        transaction just before the insert (the relay re-checks the target's
        liveness there); a RelayError from it leaves nothing written. A
        ``lease`` send is fingerprinted apart from a plain send, so the same
        id cannot be replayed across the two verbs.
        """
        sender = _identity(origin)
        if sender.kind not in ("direct", "hub"):
            raise P.RelayError("origin_not_allowed", f"a {sender.kind} origin may not send messages")
        forwarder: Optional[P.Identity] = None
        if forwarded_by is not None:
            forwarder = _identity(forwarded_by)
            if forwarder.kind != "relay" or sender.kind != "direct" or sender.subject.split("/", 1)[0] != forwarder.subject \
                    or forwarder.subject == self.machine:
                raise P.RelayError("internal", f"relay defect: {forwarder} cannot have forwarded a send from {sender}")
        address = P.parse_address(to) if not isinstance(to, P.Address) else to
        to_text = str(address)
        if kind not in P.MESSAGE_KINDS:
            raise _bad(f"unknown message kind {kind!r}")
        if not isinstance(body, str) or not body:
            raise _bad("body must be a non-empty string")
        if id is None:
            id = "msg_" + secrets.token_hex(12)
        elif not P.is_valid_id("message", id):
            raise _bad(f"{id!r} is not a message id")
        if expires_in_s is None:
            expires_in_s = P.DEFAULT_EXPIRES_IN_S
        if isinstance(expires_in_s, bool) or not isinstance(expires_in_s, int) or not 1 <= expires_in_s <= P.MAX_EXPIRES_IN_S:
            raise _bad(f"expires_in_s must be 1..{P.MAX_EXPIRES_IN_S}")
        if task is not None and (not isinstance(task, str) or not 1 <= len(task) <= 300):
            raise _bad("task must be 1..300 characters")
        if isinstance(hop, bool) or not isinstance(hop, int) or hop < 0:
            raise _bad("hop must be a non-negative integer")
        if refuse is not None and refuse not in CALLER_REFUSALS:
            raise _bad(f"refuse must be one of {sorted(CALLER_REFUSALS)}")
        if lease is not None and (forwarded_by is not None or refuse is not None):
            raise P.RelayError("internal", "relay defect: a claimed send is never forwarded or refused by the caller")
        sender_text = str(sender)
        forwarded_text = None if forwarder is None else str(forwarder)
        # the forwarder joins the fingerprint only when set, so every stored hash of version 1 stays valid
        content = _fingerprint(to_text, kind, body, task or "", hop, sender_text,
                               *(() if forwarded_text is None else (forwarded_text,)),
                               *(() if lease is None else ("continue_session",)))
        body_bytes = len(body.encode("utf-8"))
        refusal: Optional[P.RelayError] = None
        result: Optional[P.SendMessageResult] = None

        with self._write() as conn:
            now = self._clock()  # read once the write lock is held: waiting for it must not stale the expiry check
            if grant is not None:
                self._check_grant_in(conn, grant.session, "continue_session" if lease is not None else "send_message",
                                     to_text, grant.grant_id, now)
            existing = conn.execute("SELECT * FROM messages WHERE id = ?", (id,)).fetchone()
            if existing is not None:
                if existing["content_hash"] != content:
                    raise P.RelayError(
                        "idempotency_conflict", f"message id {id} was already used with other content",
                        details={"message_id": id},
                    )
                if existing["state"] == "refused":
                    raise P.RelayError(
                        existing["state_reason"] or "internal",
                        f"message {id} was refused", details={"message_id": id},
                    )
                return {"message": self._record(conn, existing), "replayed": True}

            if lease is not None:
                held = conn.execute("SELECT message_id FROM resume_leases WHERE address = ?", (to_text,)).fetchone()
                if held is not None:
                    raise P.RelayError(
                        "rate_limited", f"a continue_session run for {to_text} is already running",
                        details={"what": "resume_running", "message_id": held["message_id"]},
                    )
            self._check_rate(conn, sender_text, to_text, now)
            if recheck is not None:
                recheck()
            code = None
            if hop > P.MAX_HOP:
                code = "hop_limit"
            elif body_bytes > P.MAX_BODY_BYTES:
                code = "too_large"
            elif refuse is not None:
                code = refuse
            stored_body = body
            if code == "too_large":
                stored_body = f"(body of {body_bytes} bytes not stored)"
            state = "refused" if code else "queued"
            conn.execute(
                "INSERT INTO messages (id, to_addr, sender, kind, body, hop, task, created_at, expires_at,"
                " updated_at, state, state_reason, attempts, content_hash, forwarded_by)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                (id, to_text, sender_text, kind, stored_body, hop, task, now, now + expires_in_s, now,
                 state, code, content, forwarded_text),
            )
            self._audit(
                conn, event="transition", origin=forwarder if forwarder is not None else sender,
                verb="continue_session" if lease is not None else "send_message",
                outcome="refused" if code else "ok", ingress=ingress, message_id=id, target=to_text,
                from_state=None, to_state=state, code=code, at=now,
                on_behalf_of=sender if forwarder is not None else None,
            )
            if code:
                refusal = P.RelayError(
                    code, f"message {id} refused: {code}", details={"message_id": id}
                )
            else:
                if grant is not None:
                    self._consume_in(conn, grant.session, "continue_session" if lease is not None else "send_message",
                                     to_text, grant.grant_id, now)
                row = conn.execute("SELECT * FROM messages WHERE id = ?", (id,)).fetchone()
                if lease is not None:
                    self._claim_in(conn, row, lease, now)
                    row = conn.execute("SELECT * FROM messages WHERE id = ?", (id,)).fetchone()
                result = {"message": self._record(conn, row), "replayed": False}
        if refusal is not None:
            raise refusal
        assert result is not None
        return result

    def _check_rate(self, conn: sqlite3.Connection, sender: str, to_addr: str, now: float) -> None:
        lim = self.limits
        since = now - lim.window_s
        per_sender = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE sender = ? AND created_at > ?", (sender, since)
        ).fetchone()[0]
        if per_sender >= lim.sender_per_window:
            raise P.RelayError(
                "rate_limited", f"{sender} sent {per_sender} messages in {lim.window_s}s",
                details={"scope": "sender", "limit": lim.sender_per_window, "window_s": lim.window_s},
            )
        overall = conn.execute("SELECT COUNT(*) FROM messages WHERE created_at > ?", (since,)).fetchone()[0]
        if overall >= lim.global_per_window:
            raise P.RelayError(
                "rate_limited", f"the relay accepted {overall} messages in {lim.window_s}s",
                details={"scope": "global", "limit": lim.global_per_window, "window_s": lim.window_s},
            )
        pending = conn.execute(
            "SELECT COUNT(*) FROM messages WHERE to_addr = ? AND state IN ('queued', 'delivered')", (to_addr,)
        ).fetchone()[0]
        if pending >= lim.max_pending_per_target:
            raise P.RelayError(
                "rate_limited", f"{to_addr} already has {pending} unfinished messages",
                details={"scope": "target", "limit": lim.max_pending_per_target},
            )

    # -- messages: delivery ----------------------------------------------------

    def poll(self, address: str, *, max_messages: int = P.MAX_POLL_BATCH) -> list[P.DeliveredMessage]:
        """Non-blocking: lapse expired messages, then deliver up to ``max_messages``.

        A ``queued`` message becomes ``delivered`` (attempt 1). A ``delivered``
        message whose last delivery is older than ``ack_timeout_s`` is
        returned again (``delivered -> delivered``, attempt + 1). Oldest
        first. The relay's long-poll loops over this call.
        """
        P.parse_address(address)
        if isinstance(max_messages, bool) or not isinstance(max_messages, int) or not 1 <= max_messages <= P.MAX_POLL_BATCH:
            raise _bad(f"max_messages must be 1..{P.MAX_POLL_BATCH}")
        origin = f"channel:{address}"
        now = self._clock()
        out: list[P.DeliveredMessage] = []
        with self._read() as conn:  # an idle poll must not take the write lock
            pending = conn.execute(
                "SELECT 1 FROM messages WHERE to_addr = ? AND claimed_by IS NULL"
                " AND ((state IN ('queued', 'delivered') AND expires_at <= ?)"
                " OR state = 'queued' OR (state = 'delivered' AND delivered_at <= ?)) LIMIT 1",
                (address, now, now - self.ack_timeout_s),
            ).fetchone()
        if pending is None:
            return out
        with self._write() as conn:
            self._expire(conn, now, address)
            rows = conn.execute(
                "SELECT * FROM messages WHERE to_addr = ? AND claimed_by IS NULL AND (state = 'queued'"
                " OR (state = 'delivered' AND delivered_at <= ?)) ORDER BY created_at, rowid LIMIT ?",
                (address, now - self.ack_timeout_s, max_messages),
            ).fetchall()
            for row in rows:
                attempt = row["attempts"] + 1
                self._transition(
                    conn, row, "delivered", verb="poll", origin=origin, now=now,
                    delivered_at=now, attempts=attempt,
                    reason=None if row["state"] == "queued" else "redelivered",
                )
                delivered: P.DeliveredMessage = {
                    "id": row["id"], "sender": row["sender"], "kind": row["kind"], "body": row["body"],
                    "hop": row["hop"], "created_at": row["created_at"], "expires_at": row["expires_at"],
                    "attempt": attempt,
                }
                if row["task"] is not None:
                    delivered["task"] = row["task"]
                if row["forwarded_by"] is not None:
                    delivered["forwarded_by"] = row["forwarded_by"]
                out.append(delivered)
        return out

    def ack(self, address: str, message_id: str) -> P.AckResult:
        """Acknowledge ONE delivered message. Already acked/answered/declined returns its state."""
        row_state: str
        with self._write() as conn:
            row = self._owned(conn, address, message_id)
            if row["state"] in ("acked", "answered", "declined"):
                return {"message_id": message_id, "state": row["state"]}
            self._transition(
                conn, row, "acked", verb="ack", origin=f"channel:{address}", now=self._clock()
            )
            row_state = "acked"
        return {"message_id": message_id, "state": row_state}

    def reply(
        self,
        address: str,
        in_reply_to: str,
        state: str,
        body: str,
        *,
        id: Optional[str] = None,
    ) -> P.ReplyResult:
        """Answer or decline one delivered message from its recipient ``address``.

        Allowed from ``delivered`` or ``acked``. A resend of the same reply id
        and content returns the first reply with ``replayed``; the same id with
        other content is ``idempotency_conflict``; any other second reply is
        ``invalid_transition``.
        """
        if state not in ("answered", "declined"):
            raise _bad("state must be answered or declined")
        if not isinstance(body, str) or not body:
            raise _bad("body must be a non-empty string")
        if id is None:
            id = "rpl_" + secrets.token_hex(12)
        elif not P.is_valid_id("reply", id):
            raise _bad(f"{id!r} is not a reply id")
        if len(body.encode("utf-8")) > P.MAX_BODY_BYTES:
            raise P.RelayError("too_large", f"reply body exceeds {P.MAX_BODY_BYTES} bytes")
        now = self._clock()
        sender = f"channel:{address}"
        with self._write() as conn:
            prior = conn.execute("SELECT * FROM replies WHERE id = ?", (id,)).fetchone()
            if prior is not None:
                same = (
                    prior["in_reply_to"] == in_reply_to and prior["state"] == state
                    and prior["body"] == body and prior["sender"] == sender
                )
                if not same:
                    raise P.RelayError(
                        "idempotency_conflict", f"reply id {id} was already used with other content"
                    )
                message = conn.execute("SELECT state FROM messages WHERE id = ?", (in_reply_to,)).fetchone()
                return {"reply": _reply(prior), "message_state": message["state"], "replayed": True}
            row = self._owned(conn, address, in_reply_to)
            self._transition(conn, row, state, verb="reply", origin=sender, now=now)
            conn.execute(
                "INSERT INTO replies (id, in_reply_to, sender, state, body, at) VALUES (?, ?, ?, ?, ?, ?)",
                (id, in_reply_to, sender, state, body, now),
            )
            created = conn.execute("SELECT * FROM replies WHERE id = ?", (id,)).fetchone()
            return {"reply": _reply(created), "message_state": state, "replayed": False}

    def _owned(self, conn: sqlite3.Connection, address: str, message_id: str) -> sqlite3.Row:
        P.parse_address(address)
        row = conn.execute("SELECT * FROM messages WHERE id = ? AND to_addr = ?", (message_id, address)).fetchone()
        if row is None:
            raise P.RelayError("message_not_found", f"no message {message_id} for {address}")
        if row["claimed_by"] is not None:
            raise P.RelayError("invalid_transition", f"message {message_id} is claimed by a continue_session run",
                               details={"from_state": row["state"], "why": "resume_running"})
        return row

    # -- messages: reading -----------------------------------------------------

    def get_message(self, message_id: str) -> P.MessageRecord:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            if row is None:
                raise P.RelayError("message_not_found", f"no message {message_id}")
            return self._record(conn, row)

    def find_message(self, message_id: str) -> Optional[P.MessageRecord]:
        """Like :meth:`get_message` but ``None`` when this mailbox has no such message."""
        try:
            return self.get_message(message_id)
        except P.RelayError as exc:
            if exc.code == "message_not_found":
                return None
            raise

    def messages_to(self, address: str, *, limit: int = 50) -> list[P.MessageRecord]:
        """Messages to ``address`` with their replies, oldest first, at most the newest ``limit``."""
        P.parse_address(address)
        if limit < 1:
            raise _bad("limit must be positive")
        with self._read() as conn:
            rows = conn.execute(
                "SELECT * FROM (SELECT *, rowid AS rid FROM messages WHERE to_addr = ?"
                " ORDER BY created_at DESC, rowid DESC LIMIT ?) ORDER BY created_at, rid",
                (address, limit),
            ).fetchall()
            return [self._record(conn, r) for r in rows]

    def _record(self, conn: sqlite3.Connection, row: sqlite3.Row) -> P.MessageRecord:
        record: P.MessageRecord = {
            "id": row["id"], "to": row["to_addr"], "sender": row["sender"], "kind": row["kind"],
            "body": row["body"], "hop": row["hop"], "created_at": row["created_at"],
            "expires_at": row["expires_at"], "updated_at": row["updated_at"], "state": row["state"],
            "attempts": row["attempts"],
        }
        if row["task"] is not None:
            record["task"] = row["task"]
        if row["state_reason"] is not None:
            record["state_reason"] = row["state_reason"]
        if row["forwarded_by"] is not None:
            record["forwarded_by"] = row["forwarded_by"]
        reply = conn.execute("SELECT * FROM replies WHERE in_reply_to = ?", (row["id"],)).fetchone()
        if reply is not None:
            record["reply"] = _reply(reply)
        return record

    # -- opt-ins and resume claims (amendment 3) ----------------------------------------

    def record_opt_in(self, address: str) -> None:
        """Remember that ``address`` registered with a non-empty ``accepts`` (durable; never removed)."""
        P.parse_address(address)
        now = self._clock()
        with self._write() as conn:
            conn.execute(
                "INSERT INTO opt_ins (address, first_at, last_at) VALUES (?, ?, ?)"
                " ON CONFLICT (address) DO UPDATE SET last_at = excluded.last_at",
                (address, now, now),
            )

    def opted_in(self, address: str) -> bool:
        """Whether ``address`` ever registered with a non-empty ``accepts``."""
        with self._read() as conn:
            return conn.execute("SELECT 1 FROM opt_ins WHERE address = ?", (address,)).fetchone() is not None

    def _claim_in(self, conn: sqlite3.Connection, row: sqlite3.Row, lease: LeaseClaim, now: float) -> None:
        self._transition(conn, row, "delivered", verb="continue_session", origin=self._relay_identity, now=now,
                         delivered_at=now, attempts=row["attempts"] + 1, reason="claimed for a resume run")
        conn.execute("UPDATE messages SET claimed_by = ? WHERE id = ?", (lease.lease_id, row["id"]))
        conn.execute(
            "INSERT INTO resume_leases (address, message_id, lease_id, proc_tag, relay_instance, phase, created_at,"
            " expires_at) VALUES (?, ?, ?, ?, ?, 'claimed', ?, ?)",
            (row["to_addr"], row["id"], lease.lease_id, lease.tag, lease.relay_instance, now, now + lease.lease_s),
        )

    def claim_message(self, message_id: str, *, lease_id: str, relay_instance: str, lease_s: float) -> P.DeliveredMessage:
        """Claim exactly the ``queued`` message ``message_id`` for a resume run, with a durable lease.

        ``rate_limited`` (``what: resume_running``) when its address already holds a lease;
        ``invalid_transition`` when the message is not ``queued``.
        """
        lease = LeaseClaim(lease_id, relay_instance, lease_s)
        with self._write() as conn:
            now = self._clock()
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            if row is None:
                raise P.RelayError("message_not_found", f"no message {message_id}")
            if row["state"] != "queued" or row["claimed_by"] is not None:
                raise P.RelayError("invalid_transition", f"message {message_id} is {row['state']}, not queued",
                                   details={"from_state": row["state"], "to_state": "delivered"})
            if conn.execute("SELECT 1 FROM resume_leases WHERE address = ?", (row["to_addr"],)).fetchone():
                raise P.RelayError("rate_limited", f"a continue_session run for {row['to_addr']} is already running",
                                   details={"what": "resume_running"})
            self._claim_in(conn, row, lease, now)
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            return {
                "id": row["id"], "sender": row["sender"], "kind": row["kind"], "body": row["body"], "hop": row["hop"],
                "created_at": row["created_at"], "expires_at": row["expires_at"], "attempt": row["attempts"],
                **({"task": row["task"]} if row["task"] is not None else {}),
            }

    @staticmethod
    def _owned_lease(conn: sqlite3.Connection, lease_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM resume_leases WHERE lease_id = ?", (lease_id,)).fetchone()
        if row is None:
            raise LeaseLost(f"lease {lease_id} is no longer held")
        return row

    def set_lease_phase(self, lease_id: str, phase: str, *, child_pid: Optional[int] = None,
                        child_start: Optional[float] = None, pgid: Optional[int] = None,
                        lease_s: Optional[float] = None) -> None:
        """Move the lease to ``phase`` (fenced by ``lease_id``); ``running`` records the child's identity."""
        if phase not in LEASE_PHASES:
            raise _bad(f"unknown lease phase {phase!r}")
        if phase == "running" and (child_pid is None or child_start is None or pgid is None):
            raise _bad("a running lease records the child's pid, create time and process group")
        with self._write() as conn:
            now = self._clock()
            row = self._owned_lease(conn, lease_id)
            conn.execute(
                "UPDATE resume_leases SET phase = ?, child_pid = COALESCE(?, child_pid),"
                " child_start = COALESCE(?, child_start), pgid = COALESCE(?, pgid), expires_at = ? WHERE lease_id = ?",
                (phase, child_pid, child_start, pgid,
                 row["expires_at"] if lease_s is None else now + lease_s, lease_id),
            )

    def renew_lease(self, lease_id: str, lease_s: float) -> float:
        """Push the lease's expiry to now + ``lease_s``; :class:`LeaseLost` when ``lease_id`` lost it."""
        with self._write() as conn:
            now = self._clock()
            self._owned_lease(conn, lease_id)
            conn.execute("UPDATE resume_leases SET expires_at = ? WHERE lease_id = ?", (now + lease_s, lease_id))
            return now + lease_s

    def finish_claim(self, lease_id: str, state: str, body: str) -> P.MessageRecord:
        """End a claim in ONE transaction: reply ``answered`` or ``declined`` with ``body``, terminal state,
        ``claimed_by`` cleared, lease deleted, transition audited. :class:`LeaseLost` (nothing written) when
        ``lease_id`` no longer owns the lease. The only path that clears a claim."""
        if state not in ("answered", "declined"):
            raise _bad("state must be answered or declined")
        if not isinstance(body, str) or not body:
            raise _bad("body must be a non-empty string")
        data = body.encode("utf-8")
        if len(data) > P.MAX_BODY_BYTES:
            body = data[:P.MAX_BODY_BYTES - 16].decode("utf-8", "ignore") + "\n[truncated]"
        with self._write() as conn:
            now = self._clock()
            lease = self._owned_lease(conn, lease_id)
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (lease["message_id"],)).fetchone()
            if row is None or row["claimed_by"] != lease_id:
                raise P.RelayError("internal", f"relay defect: lease {lease_id} names message {lease['message_id']}"
                                   " that it does not claim")
            self._transition(conn, row, state, verb="continue_session", origin=self._relay_identity, now=now)
            reply_id = "rpl_" + secrets.token_hex(12)
            conn.execute(
                "INSERT INTO replies (id, in_reply_to, sender, state, body, at) VALUES (?, ?, ?, ?, ?, ?)",
                (reply_id, row["id"], self._relay_identity, state, body, now),
            )
            conn.execute("UPDATE messages SET claimed_by = NULL WHERE id = ?", (row["id"],))
            conn.execute("DELETE FROM resume_leases WHERE lease_id = ?", (lease_id,))
            return self._record(conn, conn.execute("SELECT * FROM messages WHERE id = ?", (row["id"],)).fetchone())

    def take_lease(self, lease_id: str, *, new_lease_id: str, relay_instance: str) -> None:
        """Fence an orphaned lease before recovering it: ``lease_id`` becomes ``new_lease_id`` owned by
        ``relay_instance``, on the lease row and the message's claim together. Every later write with the old
        id (a runner that outlived its relay) raises :class:`LeaseLost`; so does this call if the old id is gone.
        ``proc_tag`` never changes: the run's processes are found by it after any takeover."""
        with self._write() as conn:
            lease = self._owned_lease(conn, lease_id)
            conn.execute("UPDATE resume_leases SET lease_id = ?, relay_instance = ? WHERE lease_id = ?",
                         (new_lease_id, relay_instance, lease_id))
            cursor = conn.execute("UPDATE messages SET claimed_by = ? WHERE id = ? AND claimed_by = ?",
                                  (new_lease_id, lease["message_id"], lease_id))
            if cursor.rowcount != 1:
                raise P.RelayError("internal", f"relay defect: lease {lease_id} names message {lease['message_id']}"
                                   " that it does not claim")

    def recover_lease(self, lease_id: str, reason: str) -> P.MessageRecord:
        """Decline an orphaned claim with ``reason`` (finish_claim's transaction shape)."""
        return self.finish_claim(lease_id, "declined", reason)

    def leases(self) -> list[dict[str, Any]]:
        """Every resume lease row, oldest first."""
        with self._read() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM resume_leases ORDER BY created_at, rowid")]

    def lease_for(self, address: str) -> Optional[dict[str, Any]]:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM resume_leases WHERE address = ?", (address,)).fetchone()
            return dict(row) if row is not None else None

    def claim_problems(self) -> list[str]:
        """Violations of the claim invariant: every claimed message has its lease row and every lease its message."""
        problems: list[str] = []
        with self._read() as conn:
            for row in conn.execute(
                "SELECT m.id, m.claimed_by FROM messages m LEFT JOIN resume_leases l ON l.lease_id = m.claimed_by"
                " WHERE m.claimed_by IS NOT NULL AND (l.lease_id IS NULL OR l.message_id != m.id)"
            ):
                problems.append(f"message {row['id']} is claimed by {row['claimed_by']} with no matching lease")
            for row in conn.execute(
                "SELECT l.lease_id, l.message_id FROM resume_leases l LEFT JOIN messages m ON m.id = l.message_id"
                " WHERE m.id IS NULL OR m.claimed_by IS NOT l.lease_id"
            ):
                problems.append(f"lease {row['lease_id']} names message {row['message_id']}, which it does not claim")
        return problems

    # -- grants ------------------------------------------------------------------

    def mint_grant(
        self,
        *,
        origin: Union[str, P.Identity],
        session: str,
        verb: str,
        targets: Sequence[str],
        expires_in_s: int,
        uses: int,
        origin_proof: Mapping[str, Any],
        command: Optional[str] = None,
    ) -> P.Grant:
        """Mint a grant for the calling ``/direct`` session (protocol section 14).

        ``origin`` must be ``direct:<session>``: a session mints only for
        itself. ``targets`` must already be resolved (a selector is
        ``target_invalid``). Only ``{"type": "none"}`` proofs are accepted
        (alternative A); the reserved ``enclave-signature`` is
        ``proof_unsupported``.
        """
        who = _identity(origin)
        if who.kind != "direct" or who.subject != session:
            raise P.RelayError("origin_not_allowed", "a session mints grants only for itself, from a direct origin")
        if P.parse_address(session).harness != "claude":
            raise _bad("a /direct session is a claude session")
        if verb not in P.GRANTABLE_VERBS:
            raise _bad(f"{verb!r} is not a grantable verb")
        for name, value, low, high in (("expires_in_s", expires_in_s, 1, P.MAX_GRANT_TTL_S), ("uses", uses, 1, P.MAX_GRANT_USES)):
            if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
                raise _bad(f"{name} must be {low}..{high}")
        proof_type = origin_proof.get("type") if isinstance(origin_proof, Mapping) else None
        if proof_type == "enclave-signature":
            raise P.RelayError("proof_unsupported", "this relay accepts only origin_proof type none")
        if proof_type != "none" or set(origin_proof) != {"type"}:
            raise _bad('origin_proof must be {"type": "none"}')
        if not isinstance(targets, (list, tuple)) or len(targets) > 16:
            raise _bad("targets must be a list of at most 16 resolved targets")
        resolved: list[str] = []
        for target in targets:
            if not isinstance(target, str) or not 1 <= len(target) <= 1100:
                raise _bad("every target must be a non-empty string")
            try:
                parsed = P.parse_target(target)
            except P.RelayError:
                parsed = None  # a machine label, q:<id> or msg_ id: valid for its verb
            if isinstance(parsed, P.Selector):
                raise P.RelayError("target_invalid", f"{target!r} is a selector; resolve it before minting")
            resolved.append(target)
        if command is not None and (not isinstance(command, str) or not 1 <= len(command) <= 1024):
            raise _bad("command must be 1..1024 characters")
        now = self._clock()
        grant_id = "grt_" + secrets.token_hex(16)
        with self._write() as conn:
            conn.execute("DELETE FROM grants WHERE expires_at < ?", (now - _GRANT_PRUNE_AFTER_S,))
            conn.execute(
                "INSERT INTO grants (grant_id, session, verb, targets, issued_at, expires_at, uses,"
                " uses_remaining, origin_proof, origin, command) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (grant_id, session, verb, json.dumps(resolved), now, now + expires_in_s, uses, uses,
                 json.dumps({"type": "none"}), str(who), command),
            )
            return _grant(conn.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone(), now)

    def get_grant(self, grant_id: str) -> P.Grant:
        now = self._clock()
        with self._read() as conn:
            row = conn.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
            if row is None:
                raise P.RelayError("grant_unknown", f"no grant {grant_id}")
            return _grant(row, now)

    def check_grant(
        self, *, session: str, verb: str, target: Optional[str] = None, grant_id: Optional[str] = None
    ) -> P.GrantCheckResult:
        """Whether a live grant covers the call, WITHOUT consuming it (``grant_check``)."""
        now = self._clock()
        try:
            with self._read() as conn:
                row = self._cover(conn, session, verb, target, grant_id, now)
        except P.RelayError as exc:
            if not exc.code.startswith("grant_"):
                raise
            return {"covered": False, "grant_id": None, "code": exc.code}
        return {"covered": True, "grant_id": row["grant_id"], "code": None}

    def consume_grant(
        self, *, session: str, verb: str, target: Optional[str] = None, grant_id: Optional[str] = None
    ) -> P.Grant:
        """Check coverage and consume ONE use in a single immediate transaction.

        Raises the first failing ``grant_*`` code and consumes nothing on any
        refusal. The decrement is a conditional UPDATE (``uses_remaining > 0``
        and not expired), so of two concurrent consumers of a one-use grant
        exactly one succeeds. Use this for verbs whose effect is not a mailbox
        write; ``send_message(grant=...)`` consumes in the same transaction
        as its insert, which is what "consumed only when the call is ok" needs.
        """
        with self._write() as conn:
            now = self._clock()  # read once the write lock is held: waiting for it must not stale the expiry check
            return self._consume_in(conn, session, verb, target, grant_id, now)

    def refund_grant(self, grant_id: str) -> P.Grant:
        """Give back ONE use that :meth:`consume_grant` reserved, for an operation that did not take effect.

        The use count never rises above the minted ``uses``; with nothing to
        give back this is ``invalid_transition``, and an unknown grant is
        ``grant_unknown``. Expiry is not consulted: a grant that lapsed while
        its call ran gets its use back and stays expired.
        """
        with self._write() as conn:
            row = conn.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
            if row is None:
                raise P.RelayError("grant_unknown", f"no grant {grant_id}")
            cursor = conn.execute(
                "UPDATE grants SET uses_remaining = uses_remaining + 1 WHERE grant_id = ? AND uses_remaining < uses",
                (grant_id,),
            )
            if cursor.rowcount != 1:
                raise P.RelayError("invalid_transition", f"grant {grant_id} has no reserved use to give back")
            return _grant(conn.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone(), self._clock())

    def _check_grant_in(self, conn, session, verb, target, grant_id, now) -> sqlite3.Row:
        return self._cover(conn, session, verb, target, grant_id, now)

    def _consume_in(self, conn, session, verb, target, grant_id, now) -> P.Grant:
        row = self._cover(conn, session, verb, target, grant_id, now)
        cursor = conn.execute(
            "UPDATE grants SET uses_remaining = uses_remaining - 1"
            " WHERE grant_id = ? AND uses_remaining > 0 AND expires_at > ?",
            (row["grant_id"], now),
        )
        if cursor.rowcount != 1:
            raise P.RelayError("grant_used_up", f"grant {row['grant_id']} has no uses left")
        return _grant(conn.execute("SELECT * FROM grants WHERE grant_id = ?", (row["grant_id"],)).fetchone(), now)

    def _cover(self, conn, session, verb, target, grant_id, now) -> sqlite3.Row:
        """The grant row that covers the call, else the F0 refusal (named: ordered; unnamed: grant_required)."""
        if grant_id is not None:
            row = conn.execute("SELECT * FROM grants WHERE grant_id = ?", (grant_id,)).fetchone()
            if row is None:
                raise P.RelayError("grant_unknown", f"no grant {grant_id}")
            failed = _first_failure(row, session, verb, target, now)
            if failed is not None:
                raise P.RelayError(failed, f"grant {grant_id} does not cover this call: {failed}")
            return row
        rows = conn.execute(
            "SELECT * FROM grants WHERE session = ? AND verb = ? ORDER BY expires_at, issued_at, rowid",
            (session, verb),
        ).fetchall()
        for row in rows:
            if _first_failure(row, session, verb, target, now) is None:
                return row
        raise P.RelayError("grant_required", f"no live grant of {session} covers {verb}")

    # -- named conversations (ask_llm) -----------------------------------------------

    def append_turn(self, endpoint: str, conversation: str, role: str, content: str) -> int:
        """Append one turn to a named conversation; returns its 1-based turn number."""
        return self.append_turns(endpoint, conversation, [(role, content)])

    def append_turns(self, endpoint: str, conversation: str, turns: Sequence[tuple[str, str]]) -> int:
        """Append ``(role, content)`` turns atomically (user + assistant after a completion);
        returns the last turn number. The oldest turns beyond
        ``limits.max_conversation_turns`` are dropped.
        """
        self._conversation_key(endpoint, conversation)
        if not turns:
            raise _bad("turns must not be empty")
        for role, content in turns:
            if role not in P.CHAT_ROLES:
                raise _bad(f"role must be one of {P.CHAT_ROLES}")
            if not isinstance(content, str) or len(content) > MAX_TURN_CHARS:
                raise _bad(f"content must be a string of at most {MAX_TURN_CHARS} characters")
        now = self._clock()
        with self._write() as conn:
            last = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) FROM conversation_turns WHERE endpoint = ? AND conversation = ?",
                (endpoint, conversation),
            ).fetchone()[0]
            for role, content in turns:
                last += 1
                conn.execute(
                    "INSERT INTO conversation_turns (endpoint, conversation, seq, role, content, at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (endpoint, conversation, last, role, content, now),
                )
            conn.execute(
                "DELETE FROM conversation_turns WHERE endpoint = ? AND conversation = ? AND seq <= ?",
                (endpoint, conversation, last - self.limits.max_conversation_turns),
            )
            return last

    def load_conversation(
        self, endpoint: str, conversation: str, *, max_turns: Optional[int] = None
    ) -> list[P.ChatMessage]:
        """The conversation's turns, oldest first (the newest ``max_turns`` when given); ``[]`` if none."""
        self._conversation_key(endpoint, conversation)
        if max_turns is not None and max_turns < 1:
            raise _bad("max_turns must be positive")
        with self._read() as conn:
            rows = conn.execute(
                "SELECT role, content FROM conversation_turns WHERE endpoint = ? AND conversation = ?"
                " ORDER BY seq DESC LIMIT ?",
                (endpoint, conversation, -1 if max_turns is None else max_turns),
            ).fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    @staticmethod
    def _conversation_key(endpoint: str, conversation: str) -> None:
        address = P.parse_address(endpoint)
        if address.harness != "llm":
            raise P.RelayError("target_invalid", f"{endpoint!r} is not an llm endpoint address")
        if not isinstance(conversation, str) or _CONVERSATION_RE.fullmatch(conversation) is None:
            raise _bad(f"conversation must match [A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_CONVERSATION_NAME_CHARS - 1}}}")


# -- helpers --------------------------------------------------------------------------


def _split_script(script: str) -> Iterator[str]:
    """Split the DDL script into statements (triggers carry an inner ``;``)."""
    buffer: list[str] = []
    for line in script.splitlines():
        buffer.append(line)
        text = "\n".join(buffer).strip()
        if not text:
            buffer = []
            continue
        if sqlite3.complete_statement(text):
            yield text
            buffer = []
    if "".join(buffer).strip():
        raise ValueError("incomplete statement in the mailbox schema")


def _first_failure(row: sqlite3.Row, session: str, verb: str, target: Optional[str], now: float) -> Optional[str]:
    """The first F0 grant refusal for a NAMED grant, ``None`` when it covers the call."""
    targets = json.loads(row["targets"])
    checks = {
        "grant_wrong_session": row["session"] != session,
        "grant_wrong_verb": row["verb"] != verb,
        "grant_wrong_target": (target not in targets) if target is not None else bool(targets),
        "grant_expired": now >= row["expires_at"],
        "grant_used_up": row["uses_remaining"] <= 0,
    }
    for code in P.GRANT_REFUSAL_ORDER:
        if checks.get(code):
            return code
    return None


def _grant(row: sqlite3.Row, now: float) -> P.Grant:
    if now >= row["expires_at"]:
        state = "expired"
    elif row["uses_remaining"] <= 0:
        state = "used-up"
    else:
        state = "live"
    return {
        "grant_id": row["grant_id"], "session": row["session"], "verb": row["verb"],
        "targets": json.loads(row["targets"]), "issued_at": row["issued_at"],
        "expires_at": row["expires_at"], "uses": row["uses"], "uses_remaining": row["uses_remaining"],
        "origin_proof": json.loads(row["origin_proof"]), "state": state,
    }


def _reply(row: sqlite3.Row) -> P.Reply:
    return {
        "id": row["id"], "in_reply_to": row["in_reply_to"], "sender": row["sender"],
        "state": row["state"], "body": row["body"], "at": row["at"],
    }


def _audit_row(row: sqlite3.Row) -> P.AuditRow:
    return {key: row[key] for key in row.keys()}  # type: ignore[return-value]

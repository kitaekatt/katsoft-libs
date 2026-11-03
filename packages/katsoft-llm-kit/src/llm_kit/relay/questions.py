"""The relay's operator-question service (unit Q10).

``list_questions`` and ``answer_question`` (``relay-protocol/1``,
``docs/reference/llm-kit-relay-protocol.md`` section 13) over the job ledgers
the relay watches (``RelayConfig.ledgers``). A question is one job interrupt
(``llm_kit.jobs``): ``list_questions`` reads ``JobStore.list_interrupts`` and
``answer_question`` calls ``JobStore.resolve_interrupt`` with the same
arguments, in the same order, that ``llm-kit jobs resolve`` uses, so the
stored resolution, the job's state change and the execution events are the
ones the CLI would have written. Like the CLI, the answer never resumes the
run.

Question id encoding (inside the F0 ``q:<machine>/<local id>`` grammar)::

    q:<machine>/<ledger-key>:<run-id>:<interrupt-id>

* ``<machine>`` is the serving relay's machine label.
* ``<ledger-key>`` is the watched ledger's configured name. The config lists
  ledger paths only, so the name is derived: the file stem with every
  character outside ``[A-Za-z0-9_.-]`` replaced by ``-``. Ledgers whose stems
  collide each get ``-<first 8 hex of sha1(path)>`` appended; a ledger
  config change that introduces a collision therefore renames the affected
  keys, and a question id from before the change answers ``question_not_found``
  (list again).
* ``<run-id>`` is the run id verbatim (it may contain ``:`` or ``/``).
* ``<interrupt-id>`` is the ledger's decimal interrupt id.

Parsing splits the ledger key at the first ``:`` and the interrupt id at the
last ``:``; the run id is the text between.

Question states: ``open`` (unresolved), ``expired`` (unresolved and past
``expires_at``, or resolved ``expired``), ``answered``, ``rejected``
(``cancelled`` resolutions, a ledger schema 13 outcome the F0 enum has no name
for, are reported ``rejected``). The default listing (``state`` omitted or
``open``) returns ``open`` questions only; ``all`` returns every interrupt.

Refusals (F0 codes): a malformed id or params is ``bad_request``; an id naming
another machine, an unknown ledger key, run or interrupt, or an unreadable
ledger is ``question_not_found``; an input failing the request schema is
``answer_invalid`` (``details.errors`` carries the validator's
``[json_pointer, keyword]`` pairs); a different resolution of a resolved
question, an expired interrupt and a job no longer waiting are
``invalid_transition``; a machine without the schema validator is
``internal``. Nothing is written on any refusal except that a lapsed
interrupt is recorded expired, as the store does.
"""
from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from llm_kit.jobs import interrupts as _interrupts
from llm_kit.jobs.model import InterruptRecord
from llm_kit.jobs.store import (
    InterruptExpiredError,
    JobStore,
    ResolutionConflictError,
    ResolutionInputError,
    StoreError,
    StoreNotFoundError,
    UnknownInterruptError,
    UnknownRunError,
)
from llm_kit.relay import protocol as P

#: Longest question id the F0 schema admits (``question_id.maxLength``).
MAX_QUESTION_ID_CHARS = 266

_KEY_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_STATES = ("open", "all")
_MAX_ERRORS = 20


def ledger_keys(ledgers: Sequence[Path]) -> dict[str, Path]:
    """Map each watched ledger path to its key (see the module docstring)."""
    stems = {path: (_KEY_UNSAFE.sub("-", path.stem).strip("-") or "ledger") for path in ledgers}
    counts: dict[str, int] = {}
    for stem in stems.values():
        counts[stem] = counts.get(stem, 0) + 1
    keys: dict[str, Path] = {}
    for path, stem in stems.items():
        key = stem
        if counts[stem] > 1:
            key = f"{stem}-{hashlib.sha1(str(path).encode('utf-8')).hexdigest()[:8]}"
        keys[key] = path
    return keys


def encode_question_id(machine: str, ledger_key: str, run_id: str, interrupt_id: str) -> str:
    """The F0 question id for one interrupt."""
    return f"q:{machine}/{ledger_key}:{run_id}:{interrupt_id}"


def parse_question_id(text: Any) -> tuple[str, str, str, str]:
    """Split a question id into ``(machine, ledger_key, run_id, interrupt_id)``.

    Raises ``RelayError("bad_request")`` when ``text`` is not of the form
    ``q:<machine>/<ledger-key>:<run-id>:<interrupt-id>``.
    """
    if not isinstance(text, str) or not text.startswith("q:") or len(text) > MAX_QUESTION_ID_CHARS:
        raise _bad(f"{text!r} is not a question id (q:<machine>/<ledger>:<run>:<interrupt>)")
    machine, slash, rest = text[2:].partition("/")
    key, colon, tail = rest.partition(":")
    run_id, colon2, interrupt_id = tail.rpartition(":")
    if (
        not slash or not colon or not colon2 or not P.is_machine_label(machine)
        or not key or not run_id or not interrupt_id.isascii() or not interrupt_id.isdecimal()
    ):
        raise _bad(f"{text!r} is not a question id (q:<machine>/<ledger>:<run>:<interrupt>)")
    return machine, key, run_id, interrupt_id


def _bad(message: str, **details: Any) -> P.RelayError:
    return P.RelayError("bad_request", message, details=details or None)


def _not_found(message: str, **details: Any) -> P.RelayError:
    return P.RelayError("question_not_found", message, details=details or None)


def _state_of(record: InterruptRecord, now: float) -> str:
    resolution = record.resolution
    if resolution is None:
        return "expired" if record.lapsed(now) else "open"
    if resolution.outcome == "cancelled":
        return "rejected"
    return resolution.outcome


class QuestionService:
    """The ``list_questions`` / ``answer_question`` handlers over the watched ledgers.

    ``machine`` is this relay's label, ``ledgers`` the absolute ledger paths
    (``RelayConfig.ledgers``). ``reach`` returns this machine's
    ``machine_reach`` object for a listing (the service passes its own so a
    paused relay reads ``paused``); the default is ``status: up``. Pause,
    origin and grant checks belong to the caller.
    """

    def __init__(
        self,
        *,
        machine: str,
        ledgers: Sequence[Path],
        clock: Callable[[], float] = time.time,
        reach: Optional[Callable[[], Mapping[str, Any]]] = None,
    ) -> None:
        if not P.is_machine_label(machine):
            raise ValueError(f"{machine!r} is not a machine label")
        self.machine = machine
        self._keys = ledger_keys([Path(p) for p in ledgers])
        self._clock = clock
        self._reach = reach or (lambda: {"machine": machine, "status": "up"})

    @classmethod
    def from_config(cls, config: Any, **kw: Any) -> "QuestionService":
        """Build the service for a ``RelayConfig`` (its machine label and watched ledgers)."""
        return cls(machine=config.machine_label, ledgers=config.ledgers, **kw)

    @property
    def ledger_keys(self) -> Mapping[str, Path]:
        return dict(self._keys)

    # -- list_questions -----------------------------------------------------------------

    def list_questions(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Answer ``list_questions``: ``{"questions": [...], "machines": [...], "ledgers": [...]}``.

        ``ledgers`` (an F0-permitted extra field) reports each watched ledger as
        ``ok`` or ``unavailable`` with the reason, and any interrupt omitted
        because its id would exceed the id limit, so a missing ledger is never
        silent.
        """
        state = params.get("state", "open")
        if state not in _STATES:
            raise _bad(f"state {state!r} is not one of {list(_STATES)}")
        machine = params.get("machine")
        if machine is not None and machine != self.machine:
            error = P.RelayError(
                "machine_unreachable",
                f"this relay serves {self.machine!r} only; relay-protocol/1 defines no forwarding to {machine!r}",
            )
            return {
                "questions": [],
                "machines": [{"machine": machine, "status": "unreachable", "error": error.to_error_object()}],
                "ledgers": [],
            }
        now = self._clock()
        questions: list[dict[str, Any]] = []
        ledgers: list[dict[str, Any]] = []
        for key, path in self._keys.items():
            status: dict[str, Any] = {"ledger": key, "status": "ok"}
            ledgers.append(status)
            try:
                store = JobStore(path, create=False)
                records = [
                    record
                    for run_id in store.list_run_ids()
                    for record in store.list_interrupts(run_id)
                ]
            except Exception as exc:  # noqa: BLE001 -- a bad ledger is reported, never hides the others
                status.update(status="unavailable", error=f"{type(exc).__name__}: {str(exc)[:300]}")
                continue
            skipped = []
            for record in records:
                question_state = _state_of(record, now)
                if state == "open" and question_state != "open":
                    continue
                qid = encode_question_id(self.machine, key, record.run_id, record.id)
                if len(qid) > MAX_QUESTION_ID_CHARS:
                    skipped.append({"run_id": record.run_id, "interrupt_id": record.id, "reason": "id_too_long"})
                    continue
                questions.append(self._question(qid, key, record, question_state))
            if skipped:
                status["skipped"] = skipped
        questions.sort(key=lambda q: (q["created_at"], q["id"]))
        return {"questions": questions, "machines": [dict(self._reach())], "ledgers": ledgers}

    def _question(self, qid: str, key: str, record: InterruptRecord, state: str) -> dict[str, Any]:
        return {
            "id": qid,
            "machine": self.machine,
            "source": {
                "kind": "job",
                "ledger": key,
                "run_id": record.run_id,
                "job_id": record.job_id,
                "interrupt_id": record.id,
            },
            "kind": record.kind,
            "payload": dict(record.payload),
            "request_schema": dict(record.request_schema),
            "created_at": record.created_at,
            "expires_at": record.expires_at,
            "state": state,
        }

    # -- answer_question ----------------------------------------------------------------

    def answer_question(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Answer ``answer_question``: resolve the interrupt as ``llm-kit jobs resolve`` does."""
        machine, key, run_id, interrupt_id = parse_question_id(params.get("id"))
        decision = params.get("decision")
        if decision not in ("answer", "reject"):
            raise _bad(f"decision {decision!r} is not 'answer' or 'reject'")
        value: object = None
        if decision == "answer":
            if "input" not in params or "reason" in params:
                raise _bad("decision 'answer' takes 'input' and no 'reason'")
            value = params["input"]
        elif "input" in params:
            raise _bad("decision 'reject' takes no 'input'")
        reason = params.get("reason")
        if reason is not None and (not isinstance(reason, str) or not 1 <= len(reason) <= 1000):
            raise _bad("reason must be a string of 1 to 1000 characters")

        if machine != self.machine:
            raise _not_found(f"this relay serves {self.machine!r} only, not {machine!r}", id=params["id"])
        path = self._keys.get(key)
        if path is None:
            raise _not_found(f"no watched ledger is named {key!r}", id=params["id"], ledgers=sorted(self._keys))
        qid = params["id"]
        try:
            _interrupts._schema_validator()
        except ImportError as exc:  # JsonSchemaSupportError, InterruptContractSupportError
            raise P.RelayError("internal", f"this machine cannot validate answers: {exc}") from exc
        try:
            store = JobStore(path, create=False)
        except StoreNotFoundError as exc:
            raise _not_found(f"ledger {key!r} does not exist: {exc}", id=qid) from exc
        try:
            resolution = store.resolve_interrupt(
                run_id,
                interrupt_id,
                decision=decision,
                input=value,
                reason=reason,
                now=self._clock(),
            )
        except (UnknownRunError, UnknownInterruptError) as exc:
            raise _not_found(str(exc), id=qid) from exc
        except ResolutionInputError as exc:
            errors = [[str(p), str(k)] for p, k in exc.errors][:_MAX_ERRORS]
            raise P.RelayError(
                "answer_invalid", str(exc), details={"errors": errors} if errors else None
            ) from exc
        except ResolutionConflictError as exc:
            raise P.RelayError("invalid_transition", str(exc), details={"stored": exc.stored_outcome}) from exc
        except InterruptExpiredError as exc:
            raise P.RelayError("invalid_transition", str(exc), details={"stored": "expired"}) from exc
        except StoreError as exc:  # the job is no longer waiting; the store wrote nothing
            raise P.RelayError("invalid_transition", str(exc)) from exc
        record = next(r for r in store.list_interrupts(run_id) if r.id == resolution.interrupt_id)
        job = store.get_job(run_id, record.job_id)
        return {
            "id": qid,
            "decision": decision,
            "state": "rejected" if resolution.outcome == "cancelled" else resolution.outcome,
            "replayed": resolution.replayed,
            "resolved_at": resolution.resolved_at,
            "job_id": job.id if job is not None else None,
            "job_state": job.state.value if job is not None else None,
        }


__all__ = [
    "MAX_QUESTION_ID_CHARS",
    "QuestionService",
    "encode_question_id",
    "ledger_keys",
    "parse_question_id",
]

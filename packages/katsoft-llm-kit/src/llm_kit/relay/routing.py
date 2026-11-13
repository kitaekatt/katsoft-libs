"""Routing shared by the hub and a forwarding relay: label checks, fan-out and single-holder resolution.

Both the hub (:class:`llm_kit.relay.hub.HubRouter`) and a relay that forwards
``/direct`` calls to its peers (amendment 2, :mod:`llm_kit.relay.service`)
send one verb to several relays and must refuse any result in which a relay
speaks for a machine it is not. This module holds that logic once.

The label check (protocol section 13, "Label check"). :data:`LABELED_FIELDS`
lists, per verb, every field of the result that names a machine, with its kind:

- destination and ownership fields (``label``, ``address``, ``question_id``
  and the ``identity`` of a reply's sender) must name exactly the relay that
  answered, as in katsoft-llm-kit 1.19.0;
- sender provenance (``sender``, ``forwarded_by``) is checked by
  :func:`sender_problem`: a message stored on relay ``R`` may come from the hub
  (``claude.ai/<user>``), from a ``/direct`` session on ``R``, or (amendment 2)
  from a ``/direct`` session on another machine ``M`` that relay ``M``
  forwarded, which the receiving relay stamps ``forwarded_by: relay:<M>``.
  ``M`` must be a machine the checker knows (the hub: its configured relays; a
  forwarding relay: itself and its peers). A channel, operator or relay
  identity as sender, a foreign sender without the stamp, a stamp that does
  not match the sender's machine, and a stamp naming an unknown machine are
  refused.

A refused result raises ``internal`` naming both machines and the field, and
nothing of it is aggregated, routed or shown. The check is fail closed: a test
compares :data:`LABELED_FIELDS` to the response schemas, so a machine-bearing
field that is not listed fails there. Free text (turns, bodies) is never read.
"""
from __future__ import annotations

import concurrent.futures
from typing import Any, Callable, Collection, Mapping, Optional, Sequence, TypeVar

from llm_kit.relay import protocol as P

#: Kinds of a destination or ownership field: the value must name the answering relay's machine.
STRICT_KINDS = frozenset({"label", "address", "identity", "question_id"})
#: Kinds of a sender provenance field, checked per message record by :func:`sender_problem`.
PROVENANCE_KINDS = frozenset({"sender", "forwarded_by"})

#: Per verb, every field of the verb's result that names a machine, as ``(path, kind)``. ``[]`` iterates a
#: list. Strict kinds: ``label`` (a machine label), ``address`` (``<machine>/...``), ``identity`` (a relay
#: identity), ``question_id`` (``q:<machine>/...``). Provenance kinds: ``sender`` and ``forwarded_by`` of a
#: message record. A test compares this table to the response schemas (fail closed).
LABELED_FIELDS: Mapping[str, tuple[tuple[str, str], ...]] = {
    "list_machines": (("machines[].machine", "label"), ("machines[].endpoints[]", "address")),
    "list_sessions": (("sessions[].machine", "label"), ("sessions[].address", "address"), ("machines[].machine", "label")),
    "list_questions": (("questions[].machine", "label"), ("questions[].id", "question_id"), ("machines[].machine", "label")),
    "read_session": (("address", "address"),),
    "ask_llm": (("endpoint", "address"),),
    "send_message": (("message.to", "address"), ("message.sender", "sender"), ("message.forwarded_by", "forwarded_by"),
                     ("message.reply.sender", "identity")),
    "get_replies": (("messages[].to", "address"), ("messages[].sender", "sender"),
                    ("messages[].forwarded_by", "forwarded_by"), ("messages[].reply.sender", "identity")),
    "answer_question": (("id", "question_id"),),
}


def field_values(result: Any, path: str) -> list[Any]:
    """The values at ``path`` (``a.b[].c``) in ``result``; nothing where the shape does not match."""
    nodes = [result]
    for segment in path.split("."):
        each = segment.endswith("[]")
        key = segment[:-2] if each else segment
        stepped = []
        for node in nodes:
            if isinstance(node, Mapping) and key in node:
                value = node[key]
                if each:
                    stepped.extend(value if isinstance(value, list) else [])
                else:
                    stepped.append(value)
        nodes = stepped
    return nodes


def question_machine(question_id: object) -> str:
    """The machine label a question id ``q:<machine>/<local id>`` names; ``bad_request`` when it names none."""
    if isinstance(question_id, str) and question_id.startswith("q:"):
        machine, slash, local = question_id[2:].partition("/")
        if slash and local and P.is_machine_label(machine):
            return machine
    raise P.RelayError("bad_request", f"{question_id!r} is not a question id (q:<machine>/<local id>)")


def _identity(value: object) -> Optional[P.Identity]:
    if not isinstance(value, str):
        return None
    try:
        return P.parse_identity(value)
    except P.RelayError:
        return None


def is_hub_identity(value: object) -> bool:
    identity = _identity(value)
    return identity is not None and identity.kind == "hub"


def machine_in(kind: str, value: object) -> Optional[str]:
    """The machine a strict field's value names, or None when it names none or is malformed."""
    if not isinstance(value, str):
        return None
    if kind == "label":
        return value
    if kind == "address":
        return value.split("/", 1)[0]
    if kind == "question_id":
        try:
            return question_machine(value)
        except P.RelayError:
            return None
    identity = _identity(value)
    if identity is None or identity.kind == "hub":
        return None
    return identity.subject.split("/", 1)[0]


def sender_problem(record: Mapping[str, Any], *, storing: str, known: Collection[str]) -> Optional[tuple[str, str]]:
    """Why a message record's ``sender`` / ``forwarded_by`` cannot have been stored on relay ``storing``.

    Returns ``(field, reported value)`` for the first offending field, or None when the provenance is valid
    (module docstring). ``known`` are the machines the checker knows; ``storing`` is always one of them.
    """
    sender = record.get("sender")
    stamped = "forwarded_by" in record
    stamp = record.get("forwarded_by")
    identity = _identity(sender)
    if identity is None:
        return "sender", str(sender)[:80]
    if identity.kind == "hub":
        return ("forwarded_by", str(stamp)[:80]) if stamped else None
    if identity.kind != "direct":
        return "sender", str(sender)
    machine = identity.subject.split("/", 1)[0]
    if machine == storing:
        return ("forwarded_by", str(stamp)[:80]) if stamped else None
    if not stamped:
        return "sender", str(sender)
    forwarder = _identity(stamp)
    if forwarder is None or forwarder.kind != "relay" or forwarder.subject != machine or machine not in known:
        return "forwarded_by", str(stamp)[:80]
    return None


def _refuse(verb: str, path: str, configured: str, reported: Optional[str], value: object, why: str = "") -> P.RelayError:
    shown = repr(reported) if reported is not None else f"{str(value)[:80]!r} (no machine)"
    return P.RelayError(
        "internal",
        f"the relay configured as machine {configured!r} reports machine {shown} in {verb}.{path}{why}; "
        "check its URL and the label in relay.yaml",
        details={"machine": configured, "reported": reported if reported is not None else str(value)[:80],
                 "field": f"{verb}.{path}"},
    )


def check_result(verb: str, result: Mapping[str, Any], *, machine: str, known: Collection[str]) -> None:
    """Every machine relay ``machine``'s ``verb`` result names must be allowed there (``internal`` otherwise).

    Strict fields must name ``machine``; a value that is present but not a well-formed machine-bearing
    string is refused too. Sender provenance follows :func:`sender_problem`.
    """
    for path, kind in LABELED_FIELDS.get(verb, ()):
        if kind in PROVENANCE_KINDS:
            parent, _, _ = path.rpartition(".")
            for record in field_values(result, parent):
                if not isinstance(record, Mapping):
                    continue
                problem = sender_problem(record, storing=machine, known=known)
                if problem is not None:
                    name, value = problem
                    identity = _identity(value)
                    reported = None if identity is None or identity.kind == "hub" else identity.subject.split("/", 1)[0]
                    why = f" ({name} {value!r} is not a provenance relay {machine!r} can store)"
                    raise _refuse(verb, f"{parent}.{name}" if parent else name, machine, reported, value, why)
            continue
        for value in field_values(result, path):
            reported = machine_in(kind, value)
            if reported != machine and not (reported is None and kind == "identity" and is_hub_identity(value)):
                raise _refuse(verb, path, machine, reported, value)


T = TypeVar("T")


def fan_out(executor: concurrent.futures.Executor, items: Sequence[T],
            call: Callable[[T], dict[str, Any]]) -> list[tuple[T, Any]]:
    """``call(item)`` for every item in parallel: ``(item, result)`` or ``(item, RelayError)``, in order.

    Any other exception is a defect and propagates.
    """
    pending = [(item, executor.submit(call, item)) for item in items]
    outcomes: list[tuple[T, Any]] = []
    for item, future in pending:
        try:
            outcomes.append((item, future.result()))
        except P.RelayError as exc:
            outcomes.append((item, exc))
    return outcomes


def resolve_one(outcomes: Sequence[tuple[str, Any]], target: str) -> tuple[str, dict[str, Any]]:
    """Exactly one of ``(machine, result | RelayError)`` may have resolved ``target``; that one, else the refusal.

    More than one is ``target_ambiguous`` with the candidates (each result's ``address``, else its machine);
    none with some ``machine_unreachable`` is ``machine_unreachable`` naming those machines; none otherwise
    is the first error.
    """
    found = [(m, o) for m, o in outcomes if not isinstance(o, P.RelayError)]
    errors = [(m, o) for m, o in outcomes if isinstance(o, P.RelayError)]
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise P.RelayError(
            "target_ambiguous", f"{target!r} resolves on more than one machine; name one",
            details={"candidates": [str(o.get("address", m)) for m, o in found]},
        )
    unreachable = [m for m, e in errors if e.code == "machine_unreachable"]
    if unreachable:
        raise P.RelayError(
            "machine_unreachable", f"no reachable relay resolved {target!r}; unreachable: {', '.join(unreachable)}",
            details={"machines": unreachable},
        )
    if not errors:
        raise P.RelayError("internal", f"routing defect: no relay was asked to resolve {target!r}")
    raise errors[0][1]


__all__ = [
    "LABELED_FIELDS",
    "PROVENANCE_KINDS",
    "STRICT_KINDS",
    "check_result",
    "fan_out",
    "field_values",
    "is_hub_identity",
    "machine_in",
    "question_machine",
    "resolve_one",
    "sender_problem",
]

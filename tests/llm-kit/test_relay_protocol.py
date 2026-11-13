"""Contract tests for relay-protocol/1: llm_kit.relay.protocol and its schemas.

The schemas are validated with ``llm_kit.schema.json_schema`` (the library's
own standard-library validator for a frozen JSON Schema subset), because no
JSON Schema package is in the dev environment and the relay schemas are
written in that subset so the relay can validate wire messages without one.

The frozen verb, client-verb and object-schema lists are spelled out HERE
rather than read from the module, so dropping a verb from ``protocol.VERBS``
together with its schema files still fails.
"""
from __future__ import annotations

import ast
import copy
import json
import sys
from collections import deque
from pathlib import Path

import pytest

from llm_kit.relay import protocol as P
from llm_kit.schema import json_schema as JS

REPO = Path(__file__).resolve().parents[2]
RELAY_DIR = REPO / "packages" / "katsoft-llm-kit" / "src" / "llm_kit" / "relay"
SCHEMA_DIR = RELAY_DIR / "schemas"
DOC = REPO / "docs" / "reference" / "llm-kit-relay-protocol.md"

CHANNEL_VERBS = ("register", "heartbeat", "deregister", "poll", "ack", "reply")
SECTION_7_VERBS = (
    "list_machines", "list_sessions", "read_session", "ask_llm",
    "send_message", "get_replies", "list_questions", "answer_question",
)
GRANT_VERBS = ("grant_mint", "grant_check")
CONTROL_VERBS = ("pause", "resume", "status")
FROZEN_VERBS = CHANNEL_VERBS + SECTION_7_VERBS + GRANT_VERBS + CONTROL_VERBS
FROZEN_CLIENT_VERBS = {
    "machines": "list_machines", "sessions": "list_sessions", "read": "read_session",
    "ask": "ask_llm", "send": "send_message", "replies": "get_replies",
    "questions": "list_questions", "answer": "answer_question",
}
FROZEN_OBJECTS = (
    "error", "signed", "origin_proof", "grant", "audit_row", "message_record",
    "delivered_message", "reply_record", "session", "machine", "pause_state", "question",
)
FROZEN_ENVELOPES = ("envelope.request", "envelope.response")
FROZEN_SCHEMAS = frozenset(
    {"common", *FROZEN_ENVELOPES, *FROZEN_OBJECTS}
    | {f"{verb}.{side}" for verb in FROZEN_VERBS for side in ("request", "response")}
)
REQUEST_SCHEMAS = tuple(f"{verb}.request" for verb in FROZEN_VERBS) + ("envelope.request",)
RESPONSE_SCHEMAS = tuple(f"{verb}.response" for verb in FROZEN_VERBS) + ("envelope.response",)
VALIDATED_SCHEMAS = tuple(sorted(FROZEN_SCHEMAS - {"common"}))

FROZEN_STATES = (
    "queued", "delivered", "acked", "answered", "declined", "lapsed", "session-ended", "refused",
)
FROZEN_TRANSITIONS = frozenset({
    ("queued", "delivered"), ("queued", "lapsed"), ("queued", "session-ended"),
    ("delivered", "delivered"), ("delivered", "acked"), ("delivered", "answered"),
    ("delivered", "declined"), ("delivered", "lapsed"), ("delivered", "session-ended"),
    ("acked", "answered"), ("acked", "declined"), ("acked", "session-ended"),
})


def schema(name: str) -> dict:
    return P.load_schema(name)


def examples(name: str) -> list:
    found = schema(name).get("examples")
    assert isinstance(found, list) and found, f"{name}.json carries no examples"
    return copy.deepcopy(found)


def errors(name: str, value) -> tuple:
    return JS.validate(schema(name), value)


def effective_root(composed: dict) -> dict:
    """The root node, or the $defs entry a bare root $ref names."""
    if "$ref" in composed:
        return JS.resolve_local_ref(composed, composed["$ref"])
    return composed


# --------------------------------------------------------------------------
# identity, verb table, files
# --------------------------------------------------------------------------


def test_protocol_and_subset_literals():
    assert P.PROTOCOL == "relay-protocol/1"
    assert P.SCHEMA_SUBSET == JS.SUBSET_V1
    assert P.SCHEMA_SUBSET in JS.SUPPORTED_SUBSETS


def test_verb_table_is_the_frozen_set():
    assert tuple(P.VERBS) == FROZEN_VERBS
    assert dict(P.CLIENT_VERBS) == FROZEN_CLIENT_VERBS
    assert P.HUB_TOOLS == SECTION_7_VERBS
    assert P.GRANTABLE_VERBS == frozenset(SECTION_7_VERBS)
    assert P.OBJECT_SCHEMAS == FROZEN_OBJECTS
    assert P.ENVELOPE_SCHEMAS == FROZEN_ENVELOPES
    for name, spec in P.VERBS.items():
        assert spec.name == name and spec.tier in P.TIERS and spec.summary


def test_schema_directory_holds_exactly_the_frozen_files():
    on_disk = {path.name.removesuffix(".json") for path in SCHEMA_DIR.glob("*.json")}
    assert on_disk == FROZEN_SCHEMAS, (
        f"missing {sorted(FROZEN_SCHEMAS - on_disk)}, unexpected {sorted(on_disk - FROZEN_SCHEMAS)}"
    )
    assert set(P.schema_names()) == FROZEN_SCHEMAS
    assert len(P.schema_names()) == len(FROZEN_SCHEMAS)


@pytest.mark.parametrize("verb", FROZEN_VERBS)
def test_every_verb_has_request_and_response_schemas(verb):
    for name in (P.request_schema_name(verb), P.response_schema_name(verb)):
        assert (SCHEMA_DIR / f"{name}.json").is_file(), f"{name}.json is missing"
        composed = schema(name)
        JS.check_schema(composed)
        assert composed.get("type") == "object", f"{name} root must be an object schema"
        for example in examples(name):
            assert JS.validate(composed, example) == (), (name, example)


@pytest.mark.parametrize("name", sorted(FROZEN_SCHEMAS))
def test_schema_checks_under_the_frozen_subset(name):
    JS.check_schema(schema(name))


@pytest.mark.parametrize("name", sorted(FROZEN_SCHEMAS))
def test_schema_file_is_ascii_json_with_annotations(name):
    raw = (SCHEMA_DIR / f"{name}.json").read_bytes()
    text = raw.decode("ascii")
    assert text.endswith("\n")
    data = json.loads(text)
    assert data["$id"] == f"{P.PROTOCOL}/{name}.json"
    assert data["title"] and data["description"]


def test_load_schema_refuses_an_unknown_name_and_a_common_redefinition(monkeypatch):
    with pytest.raises(KeyError):
        P.load_schema("../../escape")
    with pytest.raises(KeyError):
        P.request_schema_name("delete_everything")
    real = P._read_schema_file

    def clashing(name):
        data = real(name)
        if name == "ack.request":
            data["$defs"] = {"address": {"type": "string"}}
        return data

    monkeypatch.setattr(P, "_read_schema_file", clashing)
    with pytest.raises(ValueError, match="redefines common"):
        P.load_schema("ack.request")


# --------------------------------------------------------------------------
# round trip and rejection
# --------------------------------------------------------------------------


@pytest.mark.parametrize("name", VALIDATED_SCHEMAS)
def test_examples_round_trip_and_validate(name):
    composed = schema(name)
    for example in examples(name):
        back = json.loads(json.dumps(example, ensure_ascii=True))
        assert back == example
        assert JS.validate(composed, back) == (), (name, back)


@pytest.mark.parametrize("name", VALIDATED_SCHEMAS)
def test_a_non_object_is_rejected(name):
    for value in ([], "text", 7, None):
        assert errors(name, value), (name, value)


@pytest.mark.parametrize("name", VALIDATED_SCHEMAS)
def test_dropping_a_required_field_is_rejected(name):
    composed = schema(name)
    root = effective_root(composed)
    required = root.get("required") or []
    if not required:
        pytest.skip(f"{name} has no root-required fields (covered by INVALID_CASES)")
    for example in examples(name):
        for field in required:
            broken = {k: v for k, v in example.items() if k != field}
            assert JS.validate(composed, broken), f"{name} accepted an instance without {field!r}"


@pytest.mark.parametrize("name", REQUEST_SCHEMAS)
def test_request_schemas_reject_unknown_fields(name):
    for example in examples(name):
        assert errors(name, {**example, "zz_unknown": 1}), f"{name} accepted an unknown field"


@pytest.mark.parametrize("name", RESPONSE_SCHEMAS + tuple(o for o in FROZEN_OBJECTS if o != "origin_proof"))
def test_response_schemas_accept_unknown_fields(name):
    for example in examples(name):
        assert errors(name, {**example, "zz_added_later": 1}) == (), f"{name} must ignore new fields"


def _ex(name, index=0, **changes):
    value = examples(name)[index]
    for key, new in changes.items():
        if new is _DROP:
            value.pop(key)
        else:
            value[key] = new
    return value


_DROP = object()
_LONG_BODY = "x" * (P.MAX_BODY_BYTES + 1)

INVALID_CASES = [
    ("envelope.request", lambda: _ex("envelope.request", protocol="relay-protocol/2"), "wrong protocol"),
    ("envelope.request", lambda: _ex("envelope.request", origin=_DROP), "no origin"),
    ("envelope.request", lambda: _ex("envelope.request", verb="delete"), "unknown verb"),
    ("envelope.response", lambda: _ex("envelope.response", 0, result=_DROP), "ok without result"),
    ("envelope.response", lambda: _ex("envelope.response", 1, error=_DROP), "error without error"),
    ("envelope.response", lambda: _ex("envelope.response", 1, ok=True), "ok true without result"),
    ("envelope.response", lambda: _ex("envelope.response", 0, id=None), "ok response with null id"),
    ("envelope.response", lambda: {**_ex("envelope.response", 1),
                                   "error": {"code": "paused", "message": "m", "retryable": "yes"}}, "retryable"),
    ("send_message.request", lambda: _ex("send_message.request", kind="command"), "kind"),
    ("send_message.request", lambda: _ex("send_message.request", body=""), "empty body"),
    ("send_message.request", lambda: _ex("send_message.request", body=_LONG_BODY), "body cap"),
    ("send_message.request", lambda: _ex("send_message.request", expires_in_s=0), "expiry floor"),
    ("send_message.request", lambda: _ex("send_message.request", expires_in_s=P.MAX_EXPIRES_IN_S + 1), "expiry cap"),
    ("send_message.request", lambda: _ex("send_message.request", hop=-1), "hop"),
    ("poll.request", lambda: _ex("poll.request", wait_s=P.MAX_POLL_WAIT_S + 1), "long-poll cap"),
    ("poll.request", lambda: _ex("poll.request", 1, max_messages=P.MAX_POLL_BATCH + 1), "batch cap"),
    ("poll.request", lambda: _ex("poll.request", channel_nonce=_DROP), "nonce"),
    ("register.request", lambda: _ex("register.request", harness="llm"), "channel harness"),
    ("register.request", lambda: _ex("register.request", harness="gemini"), "undefined channel harness"),
    ("register.request", lambda: _ex("register.request", harness="Codex"), "harness is case-sensitive"),
    ("status.response", lambda: _ex("status.response", harnesses=["llm"]), "status harness"),
    ("status.response", lambda: _ex("status.response", harnesses="codex"), "status harnesses type"),
    ("register.request", lambda: _ex("register.request", channel_nonce="short"), "nonce length"),
    ("register.request", lambda: _ex("register.request", pid=0), "pid"),
    ("register.request", lambda: _ex("register.request", accepts=["command"]), "accepts"),
    ("reply.request", lambda: _ex("reply.request", state="lapsed"), "reply state"),
    ("ack.request", lambda: _ex("ack.request", message_id=_DROP), "ack id"),
    ("get_replies.request", lambda: {}, "neither id nor address"),
    ("get_replies.request", lambda: {"wait_s": 3}, "only wait_s"),
    ("get_replies.request", lambda: {"message_id": "msg_1", "address": "latest"}, "both forms"),
    ("get_replies.request", lambda: {"message_id": "msg_1", "wait_s": P.MAX_REPLY_WAIT_S + 1}, "wait cap"),
    ("ask_llm.request", lambda: {**_ex("ask_llm.request", 2), "prompt": "hi"}, "prompt and messages"),
    ("ask_llm.request", lambda: _ex("ask_llm.request", prompt=_DROP), "neither prompt nor messages"),
    ("ask_llm.request", lambda: {**_ex("ask_llm.request", 2), "conversation": "c"}, "messages in a conversation"),
    ("ask_llm.request", lambda: _ex("ask_llm.request", temperature=3), "temperature"),
    ("ask_llm.request", lambda: _ex("ask_llm.request", endpoint=_DROP), "endpoint"),
    ("answer_question.request", lambda: _ex("answer_question.request", 0, input=_DROP), "answer without input"),
    ("answer_question.request", lambda: {**_ex("answer_question.request", 1), "input": 1}, "reject with input"),
    ("answer_question.request", lambda: {**_ex("answer_question.request", 0), "reason": "r"}, "answer with reason"),
    ("answer_question.request", lambda: _ex("answer_question.request", 0, decision="maybe"), "decision"),
    ("grant_mint.request", lambda: _ex("grant_mint.request", uses=0), "uses floor"),
    ("grant_mint.request", lambda: _ex("grant_mint.request", uses=P.MAX_GRANT_USES + 1), "uses cap"),
    ("grant_mint.request", lambda: _ex("grant_mint.request", expires_in_s=P.MAX_GRANT_TTL_S + 1), "ttl cap"),
    ("grant_mint.request", lambda: _ex("grant_mint.request", verb="pause"), "ungrantable verb"),
    ("grant_mint.request", lambda: _ex("grant_mint.request", verb="grant_mint"), "self-minting verb"),
    ("grant_mint.request", lambda: _ex("grant_mint.request", origin_proof={"type": "password"}), "proof type"),
    ("grant_mint.request", lambda: _ex("grant_mint.request", origin_proof={"type": "none", "x": 1}), "loose A proof"),
    ("grant_mint.request", lambda: _ex("grant_mint.request", origin_proof=_DROP), "no proof"),
    ("grant_mint.request", lambda: _ex("grant_mint.request", targets=["t"] * 17), "target cap"),
    ("grant_check.request", lambda: _ex("grant_check.request", verb="grant_mint"), "check verb"),
    ("pause.request", lambda: _ex("pause.request", scope="all"), "scope"),
    ("resume.request", lambda: {}, "resume scope"),
    ("list_sessions.request", lambda: {"state": "running"}, "session state"),
    ("list_sessions.request", lambda: {"harness": "llm"}, "session harness"),
    ("list_machines.request", lambda: {"machine": "m5pro"}, "no params"),
    ("read_session.request", lambda: _ex("read_session.request", limit=P.MAX_READ_TURNS + 1), "turn cap"),
    ("status.response", lambda: _ex("status.response", protocol="relay-protocol/2"), "status protocol"),
    ("heartbeat.response", lambda: _ex("heartbeat.response", ended_messages=-1), "ended count"),
    ("grant", lambda: _ex("grant", uses_remaining=-1), "uses remaining"),
    ("grant", lambda: _ex("grant", state="revoked"), "grant state"),
    ("grant", lambda: _ex("grant", origin_proof={"type": "pin"}), "grant proof"),
    ("message_record", lambda: _ex("message_record", state="pending"), "mailbox state"),
    ("message_record", lambda: _ex("message_record", reply={"id": "rpl_1"}), "reply record"),
    ("delivered_message", lambda: _ex("delivered_message", attempt=0), "attempt"),
    ("audit_row", lambda: _ex("audit_row", outcome="maybe"), "outcome"),
    ("audit_row", lambda: _ex("audit_row", verb="rm"), "audit verb"),
    ("audit_row", lambda: _ex("audit_row", to_state="pending"), "audit state"),
    ("session", lambda: _ex("session", harness="llm"), "session harness"),
    ("session", lambda: _ex("session", state="running"), "session state"),
    ("machine", lambda: _ex("machine", status="down"), "machine status"),
    ("pause_state", lambda: _ex("pause_state", hub=_DROP), "hub flag"),
    ("pause_state", lambda: {**_ex("pause_state"), "relay": {"paused": True}}, "pause flag fields"),
    ("question", lambda: {**_ex("question"), "source": {**_ex("question")["source"], "kind": "session"}}, "source"),
    ("error", lambda: _ex("error", code="oops"), "error code"),
    ("signed", lambda: {"issued_at": "now"}, "signed field type"),
    ("origin_proof", lambda: {"type": "none", "signature": "s"}, "A proof stays bare"),
    ("origin_proof", lambda: {}, "proof type required"),
]


@pytest.mark.parametrize(("name", "build", "why"), INVALID_CASES, ids=[f"{n}-{w}" for n, _, w in INVALID_CASES])
def test_invalid_instances_are_rejected(name, build, why):
    assert errors(name, build()), f"{name} accepted an invalid instance ({why})"


def test_typed_dicts_match_their_schemas():
    pairs = []
    for verb, spec in P.VERBS.items():
        pairs.append((P.request_schema_name(verb), spec.params_type))
        pairs.append((P.response_schema_name(verb), spec.result_type))
    pairs += [
        ("envelope.request", P.RequestEnvelope), ("error", P.ErrorObject), ("signed", P.Signed),
        ("grant", P.Grant), ("audit_row", P.AuditRow), ("message_record", P.MessageRecord),
        ("delivered_message", P.DeliveredMessage), ("reply_record", P.Reply), ("session", P.Session),
        ("machine", P.Machine), ("pause_state", P.PauseState), ("question", P.Question),
    ]
    common = schema("common")["$defs"]
    defs = [
        ("origin_proof_none", P.OriginProofNone), ("origin_proof_enclave_signature", P.OriginProofEnclaveSignature),
        ("pause_flag", P.PauseFlag), ("machine_reach", P.MachineReach), ("turn", P.Turn),
        ("chat_message", P.ChatMessage), ("question_source", P.QuestionSource),
    ]
    problems = []
    nodes = [(name, effective_root(schema(name)), td) for name, td in pairs]
    nodes += [(f"$defs/{name}", common[name], td) for name, td in defs]
    for label, node, td in nodes:
        keys = td.__required_keys__ | td.__optional_keys__
        if keys != set(node.get("properties", {})):
            problems.append(f"{label}: keys {sorted(keys ^ set(node.get('properties', {})))} differ")
        if td.__required_keys__ != set(node.get("required", [])):
            problems.append(f"{label}: required {sorted(td.__required_keys__ ^ set(node.get('required', [])))} differ")
    response = effective_root(schema("envelope.response"))
    union = (P.OkResponse.__required_keys__ | P.OkResponse.__optional_keys__
             | P.ErrorResponse.__required_keys__ | P.ErrorResponse.__optional_keys__)
    if union != set(response["properties"]):
        problems.append("envelope.response: keys differ from OkResponse | ErrorResponse")
    assert not problems, "\n".join(problems)


def test_schema_vocabularies_match_the_module_constants():
    defs = schema("common")["$defs"]
    assert defs["protocol"]["const"] == P.PROTOCOL
    assert defs["verb"]["enum"] == list(P.VERBS)
    assert set(defs["grantable_verb"]["enum"]) == P.GRANTABLE_VERBS
    assert defs["error_code"]["enum"] == list(P.ERROR_CODES)
    assert defs["mailbox_state"]["enum"] == list(P.MAILBOX_STATES)
    assert defs["session_state"]["enum"] == list(P.SESSION_STATES)
    assert defs["message_kind"]["enum"] == list(P.MESSAGE_KINDS)
    assert defs["session_harness"]["enum"] == list(P.SESSION_HARNESSES)
    assert defs["machine_status"]["enum"] == list(P.MACHINE_STATUSES)
    assert defs["question_state"]["enum"] == list(P.QUESTION_STATES)
    assert defs["grant_state"]["enum"] == list(P.GRANT_STATES)
    assert defs["pause_scope"]["enum"] == list(P.PAUSE_SCOPES)
    assert defs["audit_row"]["properties"]["verb"]["enum"] == [*P.VERBS, *P.AUDIT_INTERNAL_VERBS]
    assert defs["body"]["maxLength"] == P.MAX_BODY_BYTES
    assert defs["grant"]["properties"]["uses"]["maximum"] == P.MAX_GRANT_USES
    caps = {
        ("poll.request", "wait_s"): P.MAX_POLL_WAIT_S,
        ("poll.request", "max_messages"): P.MAX_POLL_BATCH,
        ("get_replies.request", "wait_s"): P.MAX_REPLY_WAIT_S,
        ("send_message.request", "expires_in_s"): P.MAX_EXPIRES_IN_S,
        ("grant_mint.request", "expires_in_s"): P.MAX_GRANT_TTL_S,
        ("grant_mint.request", "uses"): P.MAX_GRANT_USES,
        ("read_session.request", "limit"): P.MAX_READ_TURNS,
    }
    for (name, field), cap in caps.items():
        assert schema(name)["properties"][field]["maximum"] == cap, (name, field)


# --------------------------------------------------------------------------
# errors
# --------------------------------------------------------------------------


def test_error_codes_are_stable_with_fixed_retryability():
    for code, (retryable, status) in P.ERROR_CODES.items():
        assert isinstance(retryable, bool) and isinstance(status, int) and 400 <= status <= 599, code
    retryable = {code for code, (flag, _) in P.ERROR_CODES.items() if flag}
    assert retryable == {"registration_conflict", "rate_limited", "paused", "machine_unreachable",
                         "upstream_unavailable"}
    plan_refusals = {"grant_required", "grant_wrong_verb", "grant_wrong_target", "grant_expired",
                     "grant_used_up", "grant_wrong_session"}
    assert plan_refusals <= set(P.ERROR_CODES)
    assert set(P.GRANT_REFUSAL_ORDER) == plan_refusals - {"grant_required"} | {"grant_unknown"}
    assert P.CHECK_ORDER == ("envelope", "auth", "verb", "params", "origin", "pause", "grant", "semantics")


def test_relay_error_round_trips_and_refuses_drift():
    error = P.RelayError("target_ambiguous", "two matches", details={"candidates": ["a", "b"]})
    assert error.retryable is False and error.http_status == 409
    obj = error.to_error_object()
    assert JS.validate(schema("error"), obj) == ()
    again = P.RelayError.from_error_object(obj)
    assert (again.code, again.message, again.details) == ("target_ambiguous", "two matches", {"candidates": ["a", "b"]})
    with pytest.raises(ValueError):
        P.RelayError.from_error_object({**obj, "retryable": True})
    with pytest.raises(ValueError):
        P.RelayError("no_such_code", "x")
    for name in VALIDATED_SCHEMAS:
        for example in examples(name):
            for found in _walk(example):
                if isinstance(found, dict) and set(found) >= {"code", "message", "retryable"}:
                    assert found["retryable"] is P.ERROR_CODES[found["code"]][0], (name, found)


def test_envelope_builders_produce_valid_envelopes():
    params = examples("send_message.request")[0]
    request = P.make_request("send_message", params, origin="direct:m5pro/claude/abc", request_id="req-9")
    assert errors("envelope.request", request) == ()
    assert errors("send_message.request", request["params"]) == ()
    ok = P.make_ok("req-9", examples("send_message.response")[0])
    assert errors("envelope.response", ok) == ()
    failed = P.make_error(None, P.RelayError("bad_request", "not JSON"))
    assert errors("envelope.response", failed) == ()
    with pytest.raises(KeyError):
        P.make_request("nope", {}, origin="operator:m5pro", request_id="r")


# --------------------------------------------------------------------------
# mailbox state machine
# --------------------------------------------------------------------------


def test_mailbox_states_and_transitions_are_frozen():
    assert P.MAILBOX_STATES == FROZEN_STATES
    assert set(P.TRANSITIONS) == FROZEN_TRANSITIONS
    assert P.INITIAL_STATES == {"queued", "refused"}
    assert P.TERMINAL_STATES == {"answered", "declined", "lapsed", "session-ended", "refused"}
    assert not [pair for pair in P.TRANSITIONS if pair[0] in P.TERMINAL_STATES]
    assert P.KIND_EXCLUDED_TRANSITIONS == {
        "note": {("acked", "session-ended")}, "instruction": set(), "question": set(),
    }
    assert set(P.KIND_EXCLUDED_TRANSITIONS) == set(P.MESSAGE_KINDS)


def test_every_state_is_reachable_and_can_finish():
    edges = {}
    for a, b in P.TRANSITIONS:
        edges.setdefault(a, set()).add(b)
    seen, todo = set(P.INITIAL_STATES), deque(P.INITIAL_STATES)
    while todo:
        for nxt in edges.get(todo.popleft(), ()):
            if nxt not in seen:
                seen.add(nxt)
                todo.append(nxt)
    assert seen == set(P.MAILBOX_STATES)
    for state in set(P.MAILBOX_STATES) - P.TERMINAL_STATES:
        assert edges.get(state, set()) & P.TERMINAL_STATES or any(
            edges.get(n, set()) & P.TERMINAL_STATES for n in edges.get(state, ())
        ), state


@pytest.mark.parametrize("kind", ["note", "instruction", "question"])
def test_transition_allowed_matches_the_frozen_table(kind):
    for a in FROZEN_STATES:
        for b in FROZEN_STATES:
            expected = (a, b) in FROZEN_TRANSITIONS and not (kind == "note" and (a, b) == ("acked", "session-ended"))
            assert P.transition_allowed(a, b, kind) is expected, (kind, a, b)
            if not expected:
                with pytest.raises(P.RelayError) as caught:
                    P.require_transition(a, b, kind)
                assert caught.value.code == "invalid_transition"
    with pytest.raises(ValueError):
        P.transition_allowed("queued", "pending", kind)
    with pytest.raises(ValueError):
        P.transition_allowed("queued", "delivered", "command")


# --------------------------------------------------------------------------
# grants, origins, pause
# --------------------------------------------------------------------------


def test_grants_are_for_direct_and_a_hub_write_rests_on_the_sessions_opt_in():
    for name, spec in P.VERBS.items():
        assert spec.grant_for <= spec.origins, name
        assert spec.origins <= set(P.REQUEST_ORIGIN_KINDS), name
        props = schema(P.request_schema_name(name))["properties"]
        assert set(spec.target_params) <= set(props), name
        # Hub reads are policy-only (design 9.4); a hub write's permission is the target session's
        # channel opt-in, enforced by the relay's messaging refusals (owner ruling 2026-11-11).
        assert "hub" not in spec.grant_for, name
        if spec.tier == "write":
            # amendment 2: a peer relay forwards for a /direct session; only direct needs a grant
            assert spec.origins == {"direct", "hub", "relay"} and spec.grant_for == {"direct"}, name
    for client, verb in P.CLIENT_VERBS.items():
        assert "direct" in P.VERBS[verb].grant_for, f"/direct {client} must need a grant (ruling 9)"
    assert {v for v, s in P.VERBS.items() if s.tier == "write"} == {"send_message", "answer_question"}
    for verb in CHANNEL_VERBS:
        assert P.VERBS[verb].origins == {"channel"} and not P.VERBS[verb].grant_for
    for verb in GRANT_VERBS:
        assert P.VERBS[verb].origins == {"direct"} and not P.VERBS[verb].grant_for
    for verb in ("pause", "resume"):
        assert P.VERBS[verb].origins == {"operator"}
    assert set(schema("grant_mint.request")["$defs"]["grantable_verb"]["enum"]) == P.GRANTABLE_VERBS


def test_pause_effects_are_frozen():
    assert P.RELAY_PAUSE_REFUSES == {v for v, s in P.VERBS.items() if s.tier == "write"}
    assert P.RELAY_PAUSE_EMPTIES == {"poll"}
    assert P.HUB_PAUSE_REFUSES_ORIGINS == {"hub"}
    assert P.PAUSE_SCOPES == ("relay", "hub")


def test_origin_proof_is_extensible_and_a_is_type_none():
    assert dict(P.ORIGIN_PROOF_TYPES) == {"none": "accepted", "enclave-signature": "reserved"}
    defs = schema("origin_proof")["$defs"]
    branch_types = {
        JS.resolve_local_ref(schema("origin_proof"), branch["$ref"])["properties"]["type"]["const"]
        for branch in defs["origin_proof"]["anyOf"]
    }
    assert branch_types == set(P.ORIGIN_PROOF_TYPES)
    assert errors("origin_proof", {"type": "none"}) == ()
    # The reserved branch admits fields B adds later, so B can extend it.
    assert errors("origin_proof", {"type": "enclave-signature", "added_by_b": {"x": 1}}) == ()
    assert errors("origin_proof", {"type": "touch-id"})


def test_signed_envelope_fields_are_reserved_and_optional():
    defs = schema("common")["$defs"]
    assert set(defs["signed"]["properties"]) == {"audience", "issued_at", "not_after", "nonce", "key_id", "alg",
                                                 "signature"}
    assert defs["signed"]["required"] == []
    for holder in ("envelope.request", "envelope.response"):
        assert schema(holder)["properties"]["signed"] == {"$ref": "#/$defs/signed"}
    assert defs["delivered_message"]["properties"]["signed"] == {"$ref": "#/$defs/signed"}
    with_signed = [e for e in examples("envelope.request") if "signed" in e]
    without = [e for e in examples("envelope.request") if "signed" not in e]
    assert with_signed and without


# --------------------------------------------------------------------------
# grammar
# --------------------------------------------------------------------------

SID = "7f3c2a10-5b7e-4c1d-9a8e-0d6f4b2c9e11"


@pytest.mark.parametrize("text", [
    f"m5pro/claude/{SID}", "5090w/llm/qwen", "m5pro/codex/thr_19abc", "m5pro/llm/qwen38-m5-32k-local",
    "claudebox/claude/x1",
])
def test_addresses_parse_and_round_trip(text):
    address = P.parse_address(text)
    assert str(address) == text and P.parse_target(text) == address


@pytest.mark.parametrize(("text", "expected"), [
    ("latest", ("latest", "", None, None)),
    ("m5pro/latest", ("latest", "", "m5pro", None)),
    ("m5pro/codex/latest", ("latest", "", "m5pro", "codex")),
    ("m5pro/claude/latest", ("latest", "", "m5pro", "claude")),
    ("claude/latest", ("latest", "", None, "claude")),
    ("task:plugins-kit/presence-kit", ("task", "plugins-kit/presence-kit", None, None)),
    ("m5pro/claude/task:plugins-kit/harness-messaging", ("task", "plugins-kit/harness-messaging", "m5pro", "claude")),
    ("cwd:katsoft-libs", ("cwd", "katsoft-libs", None, None)),
    ("m5pro/cwd:/Users/christina/Dev/katsoft-libs", ("cwd", "/Users/christina/Dev/katsoft-libs", "m5pro", None)),
])
def test_selectors_parse_and_round_trip(text, expected):
    selector = P.parse_target(text)
    assert isinstance(selector, P.Selector)
    assert (selector.kind, selector.value, selector.machine, selector.harness) == expected
    assert str(selector) == text


@pytest.mark.parametrize("text", [
    "", "m5pro", "m5pro/claude", "M5PRO/claude/x", "m5pro/bash/x", "m5pro/claude/a:b", "m5pro/claude/x/y",
    "claude/claude/x", "latest/claude/x", "task:only-project", "cwd:", "m5pro/llm/latest", "-m5/claude/x",
    "m5pro/claude/.hidden", "m5pro//x", "m5pro/claude/ x",
])
def test_invalid_targets_are_refused(text):
    with pytest.raises(P.RelayError) as caught:
        P.parse_target(text)
    assert caught.value.code == "target_invalid"


def test_latest_is_never_a_session_id():
    with pytest.raises(P.RelayError):
        P.parse_address("m5pro/claude/latest")


@pytest.mark.parametrize(("text", "kind", "subject"), [
    (f"channel:m5pro/claude/{SID}", "channel", f"m5pro/claude/{SID}"),
    (f"direct:m5pro/claude/{SID}", "direct", f"m5pro/claude/{SID}"),
    (f"channel:m5pro/codex/{SID}", "channel", f"m5pro/codex/{SID}"),
    ("operator:m5pro", "operator", "m5pro"),
    ("relay:m5pro", "relay", "m5pro"),
    ("claude.ai/owner", "hub", "owner"),
])
def test_identities_parse_and_round_trip(text, kind, subject):
    identity = P.parse_identity(text)
    assert (identity.kind, identity.subject) == (kind, subject) and str(identity) == text
    assert identity.kind in P.IDENTITY_KINDS


@pytest.mark.parametrize("text", [
    "direct:m5pro/codex/x", f"direct:m5pro/codex/{SID}", "channel:m5pro/llm/x", "channel:m5pro/gemini/x",
    "channel:m5pro/codex/", "channel:codex/x", "operator:M5", "claude.ai/", "hub:m5pro", "direct:latest",
    "m5pro/claude/x",
])
def test_invalid_identities_are_refused(text):
    with pytest.raises(P.RelayError):
        P.parse_identity(text)


@pytest.mark.parametrize(("kind", "good", "bad"), [
    ("message", "msg_01JBX3V9Q8K2M4N6P7R8S9T0VW", "rpl_01"),
    ("reply", "rpl_01JBX3VA2C4E6G8J0K2M4N6P8Q", "msg_01"),
    ("grant", "grt_01JBX3T7Z1Y2X3W4V5U6T7S8R9", "grt_"),
    ("registration", "reg_01JBX3S0A1B2C3D4E5F6G7H8J9", "reg_a b"),
    ("question", "q:m5pro/jobs-main:run-1:7", "q:M5/x"),
    ("request", "req-0001", "req 0001"),
])
def test_id_grammars(kind, good, bad):
    assert P.is_valid_id(kind, good) and not P.is_valid_id(kind, bad)


def _walk(value):
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from _walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk(item)


def _pairs(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield key, item
            yield from _pairs(item)
    elif isinstance(value, list):
        for item in value:
            yield from _pairs(item)


def test_examples_obey_the_grammars():
    id_keys = {"message_id": "message", "in_reply_to": "message", "grant_id": "grant",
               "registration_id": "registration"}
    target_keys = {"to", "address", "endpoint", "session", "target"}
    identity_keys = {"origin", "sender"}
    checked = 0
    for name in VALIDATED_SCHEMAS:
        for example in examples(name):
            for key, value in _pairs(example):
                if not isinstance(value, str):
                    continue
                if key in id_keys:
                    assert P.is_valid_id(id_keys[key], value), (name, key, value)
                elif key in target_keys:
                    P.parse_target(value)
                elif key in identity_keys:
                    P.parse_identity(value)
                elif key == "machine":
                    assert P.is_machine_label(value), (name, value)
                else:
                    continue
                checked += 1
    assert checked > 50


# --------------------------------------------------------------------------
# the client seam, the import rule, the document
# --------------------------------------------------------------------------


def test_relay_client_protocol_has_one_method_per_verb():
    for verb in FROZEN_VERBS:
        assert callable(getattr(P.RelayClient, verb, None)), verb
    assert callable(P.RelayClient.call)
    members = {"machine": property(lambda self: "m5pro"), "call": lambda self, *a, **k: {}}
    members.update({verb: (lambda self, params, **kw: {}) for verb in FROZEN_VERBS})
    assert isinstance(type("Full", (), members)(), P.RelayClient)
    partial = {k: v for k, v in members.items() if k != "send_message"}
    assert not isinstance(type("Partial", (), partial)(), P.RelayClient)


@pytest.mark.parametrize("module", ["__init__.py", "protocol.py"])
def test_contract_modules_import_only_the_standard_library(module):
    tree = ast.parse((RELAY_DIR / module).read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, f"{module}: relative import"
            imported.add(node.module)
    foreign = sorted(m for m in imported if m.split(".")[0] not in sys.stdlib_module_names)
    assert not foreign, f"{module} imports non-stdlib modules: {foreign}"


# -- amendment 1 (katsoft-llm-kit 1.19.0): the Codex channel origin -----------

#: Every pre-amendment Claude request the examples carried, frozen here as
#: literals. They must keep validating, unchanged, as long as relay-protocol/1
#: lasts (section 1, condition 1). Do not regenerate them from the schemas.
_LEGACY_SID = "7f3c2a10-5b7e-4c1d-9a8e-0d6f4b2c9e11"
_LEGACY_CHANNEL = {
    "registration_id": "reg_01JBX3S0A1B2C3D4E5F6G7H8J9", "session_id": _LEGACY_SID,
    "channel_nonce": "c2hhbm5lbC1ub25jZS0wMDAx",
}
LEGACY_CLAUDE_REQUESTS = (
    ("register", f"channel:m5pro/claude/{_LEGACY_SID}", {
        "session_id": _LEGACY_SID, "harness": "claude", "pid": 41234,
        "cwd": "/Users/christina/Dev/katsoft-libs", "version": "2.1.317",
        "accepts": ["note", "instruction", "question"], "channel_nonce": "c2hhbm5lbC1ub25jZS0wMDAx",
        "title": "relay F0", "task": "plugins-kit/harness-messaging"}),
    ("register", f"channel:m5pro/claude/{_LEGACY_SID}", {
        "session_id": _LEGACY_SID, "harness": "claude", "pid": 41234, "cwd": "/tmp", "version": "2.1.317",
        "accepts": [], "channel_nonce": "c2hhbm5lbC1ub25jZS0wMDAx"}),
    ("heartbeat", f"channel:m5pro/claude/{_LEGACY_SID}", dict(_LEGACY_CHANNEL)),
    ("poll", f"channel:m5pro/claude/{_LEGACY_SID}", {**_LEGACY_CHANNEL, "wait_s": 25}),
    ("ack", f"channel:m5pro/claude/{_LEGACY_SID}", {**_LEGACY_CHANNEL, "message_id": "msg_01JBX3V9Q8K2M4N6P7R8S9T0VW"}),
    ("reply", f"channel:m5pro/claude/{_LEGACY_SID}", {
        **_LEGACY_CHANNEL, "in_reply_to": "msg_01JBX3V9Q8K2M4N6P7R8S9T0VW", "state": "answered",
        "body": "Done: tests pass.", "id": "rpl_01JBX3VA2C4E6G8J0K2M4N6P8Q"}),
    ("grant_mint", "direct:m5pro/claude/b91e0c44-2f8a-4d3b-8c6e-71a5d0e3f2a9", {
        "session": "m5pro/claude/b91e0c44-2f8a-4d3b-8c6e-71a5d0e3f2a9", "verb": "send_message",
        "targets": [f"m5pro/claude/{_LEGACY_SID}"], "expires_in_s": 300, "uses": 1,
        "origin_proof": {"type": "none"}, "command": "/direct send"}),
    ("send_message", "direct:m5pro/claude/b91e0c44-2f8a-4d3b-8c6e-71a5d0e3f2a9", {
        "to": f"m5pro/claude/{_LEGACY_SID}", "kind": "question", "body": "Are the relay tests green?",
        "id": "msg_01JBX3V9Q8K2M4N6P7R8S9T0VW", "expires_in_s": 3600, "task": "plugins-kit/harness-messaging"}),
    ("send_message", "claude.ai/owner@example.com", {
        "to": f"m5pro/claude/{_LEGACY_SID}", "kind": "note", "body": "FYI: 1.11.0 is out."}),
)


@pytest.mark.parametrize(("verb", "origin", "params"), LEGACY_CLAUDE_REQUESTS,
                         ids=[f"{v}-{i}" for i, (v, _, _) in enumerate(LEGACY_CLAUDE_REQUESTS)])
def test_legacy_claude_requests_still_validate_and_parse(verb, origin, params):
    assert errors(P.request_schema_name(verb), params) == ()
    assert errors("envelope.request", {"protocol": P.PROTOCOL, "id": "req-legacy", "verb": verb,
                                       "origin": origin, "params": params}) == ()
    assert str(P.parse_identity(origin)) == origin


def test_codex_register_and_origin_round_trip():
    params = _ex("register.request", 1)
    assert params["harness"] == "codex"
    assert errors("register.request", params) == ()
    origin = f"channel:m5pro/codex/{params['session_id']}"
    assert errors("envelope.request", {"protocol": P.PROTOCOL, "id": "req-codex", "verb": "register",
                                       "origin": origin, "params": params}) == ()
    identity = P.parse_identity(origin)
    assert (identity.kind, identity.subject) == ("channel", f"m5pro/codex/{params['session_id']}")
    assert str(identity) == origin
    assert errors("register.request", {**params, "previous_session_id": "x"}), "register never carries previous_session_id"


def test_direct_stays_claude_only():
    with pytest.raises(P.RelayError) as caught:
        P.parse_identity(f"direct:m5pro/codex/{SID}")
    assert caught.value.code == "bad_request"


def test_channel_harness_vocabulary_agrees_across_code_schemas_and_document():
    assert P.CHANNEL_HARNESSES == ("claude", "codex")
    assert set(P.CHANNEL_HARNESSES) <= set(P.SESSION_HARNESSES)
    assert schema("register.request")["properties"]["harness"]["enum"] == list(P.CHANNEL_HARNESSES)
    assert schema("status.response")["properties"]["harnesses"]["items"]["enum"] == list(P.CHANNEL_HARNESSES)
    text = DOC.read_bytes().decode("ascii")
    assert "channel:<machine>/(claude|codex)/<session>" in text
    assert "`register.harness` is one of `claude`, `codex`" in text
    assert "`status.harnesses`" in text and "katsoft-llm-kit 1.19.0" in text
    for condition in ("Every request that was valid before stays valid", "refuses the new request non-retryably",
                      "says so in a response field", "listed here with its date"):
        assert condition in text, condition


def test_status_example_advertises_the_channel_harnesses():
    assert _ex("status.response")["harnesses"] == list(P.CHANNEL_HARNESSES)
    legacy_status = {k: v for k, v in _ex("status.response").items() if k != "harnesses"}
    assert errors("status.response", legacy_status) == (), "a relay without the field stays a valid status"


def test_reference_document_names_every_verb_code_and_state():
    text = DOC.read_bytes().decode("ascii")
    missing = [word for word in (*FROZEN_VERBS, *FROZEN_CLIENT_VERBS, *P.ERROR_CODES, *FROZEN_STATES,
                                 P.PROTOCOL, P.HEADER_RELAY_TOKEN, P.HEADER_GRANT, "origin_proof")
               if f"`{word}`" not in text]
    assert not missing, f"{DOC.name} does not name: {missing}"


def test_not_implemented_is_a_non_retryable_501():
    assert P.ERROR_CODES["not_implemented"] == (False, 501)
    assert P.RelayError("not_implemented", "x").http_status == 501

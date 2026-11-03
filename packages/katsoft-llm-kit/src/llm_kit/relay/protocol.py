"""relay-protocol/1: the frozen wire contract of the relay, hub and clients.

This module and the JSON Schemas beside it (``llm_kit/relay/schemas/``) are
the contract every relay unit builds against: the relay server, the hub, the
``llm-kit relay client`` CLI, the Claude Code channel server and the ``/direct``
grant hooks. The prose specification is
``docs/reference/llm-kit-relay-protocol.md``; where this module, the schemas and
the document disagree, that is a defect to fix, not a choice.

What is frozen here:

* the request, response and error envelopes, and the stable error codes;
* the transport binding and the auth headers (relay token, hub bearer, grant
  reference), plus reserved signed-envelope fields for a later proof scheme;
* the address grammar and the friendly selectors;
* every verb as a request/response pair: the channel handshake and delivery
  verbs, the hub tools of design section 7 (which the ``/direct`` client verbs
  mirror one to one), the grant verbs and the pause controls;
* the mailbox states and their allowed transitions;
* the grant object (with an extensible ``origin_proof``) and the audit row.

The module imports only the standard library. It validates nothing against
the schemas itself; a consumer that validates uses
``llm_kit.schema.json_schema`` (every relay schema is written in its frozen
subset, :data:`SCHEMA_SUBSET`) on the composed schema :func:`load_schema`
returns.

Versioning: within ``relay-protocol/1`` a change may only ADD -- a new verb, a
new optional request field (accepted by relays before clients send it), a new
response field (clients ignore unknown response fields), a new error code, a
new ``origin_proof`` type. Removing or renaming anything, or changing what an
existing field means, is ``relay-protocol/2``.

No ``from __future__ import annotations`` here: it would leave the TypedDict
``NotRequired`` markers unevaluated, so ``__required_keys__`` would list every
key as required.
"""
import json
import re
from dataclasses import dataclass
from importlib import resources
from typing import (
    Any,
    Literal,
    Mapping,
    NotRequired,
    Optional,
    Protocol,
    TypedDict,
    Union,
    runtime_checkable,
)

# --------------------------------------------------------------------------
# identity of the protocol and its schema dialect
# --------------------------------------------------------------------------

#: The protocol literal every envelope carries.
PROTOCOL = "relay-protocol/1"

#: The JSON Schema subset every relay schema is written in (the literal of
#: ``llm_kit.schema.json_schema.SUBSET_V1``; the test pins the two equal).
SCHEMA_SUBSET = "llm-scripting-kit.json-schema-subset/v1"

# --------------------------------------------------------------------------
# transport binding and auth headers
# --------------------------------------------------------------------------

#: Every relay verb is ``POST /v1/<verb>`` with a request envelope as the JSON
#: body; the reply body is always a response envelope.
HTTP_METHOD = "POST"
HTTP_PATH_TEMPLATE = "/v1/{verb}"
CONTENT_TYPE = "application/json"

#: The relay token: required on every request to a relay.
HEADER_RELAY_TOKEN = "X-Relay-Token"
#: The hub bearer: required on every request to the hub's MCP endpoint, as
#: ``Authorization: Bearer <hub bearer>``. It never authorizes a relay call.
HEADER_AUTHORIZATION = "Authorization"
BEARER_SCHEME = "Bearer"
#: The grant reference: optional on a granted verb. When present the named
#: grant must cover the call; when absent the relay picks a covering live grant
#: of the origin's session, and refuses ``grant_required`` if there is none.
HEADER_GRANT = "X-Relay-Grant"

#: Largest request envelope a relay reads, in UTF-8 bytes.
MAX_REQUEST_BYTES = 65536

# --------------------------------------------------------------------------
# limits shared by both ends
# --------------------------------------------------------------------------

#: Largest message or reply body, in UTF-8 bytes (schemas cap characters at
#: the same number; the relay enforces bytes and refuses ``too_large``).
MAX_BODY_BYTES = 8192
#: A message whose hop count exceeds this is refused ``hop_limit``.
MAX_HOP = 1
#: Channel heartbeat interval the relay returns at registration.
HEARTBEAT_INTERVAL_S = 30
#: A registration lapses when no register, heartbeat or poll renewed it for this long.
REGISTRATION_LEASE_S = 90
#: A delivered message not acked within this is redelivered on a later poll.
ACK_TIMEOUT_S = 60
#: Long-poll caps (seconds) for ``poll`` and ``get_replies``.
MAX_POLL_WAIT_S = 25
MAX_REPLY_WAIT_S = 25
#: Most messages one poll returns.
MAX_POLL_BATCH = 16
#: Message expiry: default and cap for ``send_message.expires_in_s``.
DEFAULT_EXPIRES_IN_S = 3600
MAX_EXPIRES_IN_S = 86400
#: Grant lifetime and use caps for ``grant_mint``.
MAX_GRANT_TTL_S = 600
MAX_GRANT_USES = 32
#: ``read_session.limit`` default and cap, in turns.
DEFAULT_READ_TURNS = 20
MAX_READ_TURNS = 200

# --------------------------------------------------------------------------
# vocabularies
# --------------------------------------------------------------------------

HARNESSES = ("claude", "codex", "llm")
SESSION_HARNESSES = ("claude", "codex")
MESSAGE_KINDS = ("note", "instruction", "question")
SESSION_STATES = ("live-accepting", "live-read-only", "idle", "ended")
MACHINE_STATUSES = ("up", "paused", "unreachable")
QUESTION_STATES = ("open", "answered", "rejected", "expired")
GRANT_STATES = ("live", "used-up", "expired")
PAUSE_SCOPES = ("relay", "hub")
TURN_ROLES = ("user", "assistant", "tool", "system", "channel")
CHAT_ROLES = ("system", "user", "assistant")
AUDIT_INGRESSES = ("relay", "hub")
AUDIT_EVENTS = ("call", "transition")
AUDIT_OUTCOMES = ("ok", "refused", "error")
#: Relay-internal causes an audit row may name in place of a verb.
AUDIT_INTERNAL_VERBS = ("expire", "scan")

#: Identity kinds. ``channel``, ``direct``, ``hub`` and ``operator`` may
#: originate a request; ``relay`` names the relay itself in audit rows and is
#: reserved for peer forwarding, which relay-protocol/1 does not define.
IDENTITY_KINDS = ("channel", "direct", "hub", "operator", "relay")
REQUEST_ORIGIN_KINDS = ("channel", "direct", "hub", "operator")

# --------------------------------------------------------------------------
# error codes: code -> (retryable, HTTP status)
# --------------------------------------------------------------------------

#: The stable error codes. ``retryable`` in an error object MUST equal the
#: value here. HTTP status is 200 for every ok response and the status here for
#: an error; the envelope, not the status, is authoritative.
ERROR_CODES: Mapping[str, tuple[bool, int]] = {
    "bad_request": (False, 400),
    "unsupported_protocol": (False, 400),
    "unknown_verb": (False, 404),
    "unauthenticated": (False, 401),
    "origin_not_allowed": (False, 403),
    "grant_required": (False, 403),
    "grant_unknown": (False, 403),
    "grant_wrong_session": (False, 403),
    "grant_wrong_verb": (False, 403),
    "grant_wrong_target": (False, 403),
    "grant_expired": (False, 403),
    "grant_used_up": (False, 403),
    "proof_unsupported": (False, 403),
    "target_invalid": (False, 400),
    "target_not_found": (False, 404),
    "target_ambiguous": (False, 409),
    "not_opted_in": (False, 403),
    "session_ended": (False, 410),
    "hop_limit": (False, 403),
    "too_large": (False, 413),
    "idempotency_conflict": (False, 409),
    "invalid_transition": (False, 409),
    "message_not_found": (False, 404),
    "question_not_found": (False, 404),
    "answer_invalid": (False, 422),
    "registration_unknown": (False, 404),
    "registration_conflict": (True, 409),
    "nonce_mismatch": (False, 403),
    "consent_refused": (False, 403),
    "endpoint_refused": (False, 403),
    "rate_limited": (True, 429),
    "paused": (True, 503),
    "machine_unreachable": (True, 502),
    "upstream_unavailable": (True, 502),
    "internal": (False, 500),
}

#: When a request names a grant, the first failing check in this order is the
#: refusal code.
GRANT_REFUSAL_ORDER = (
    "grant_unknown",
    "grant_wrong_session",
    "grant_wrong_verb",
    "grant_wrong_target",
    "grant_expired",
    "grant_used_up",
)

#: The order a relay (and the hub, for its own ingress) applies its checks;
#: the first failing stage decides the error.
CHECK_ORDER = (
    "envelope",   # parseable JSON, protocol literal  -> bad_request / unsupported_protocol
    "auth",       # relay token or hub bearer         -> unauthenticated
    "verb",       # known verb                        -> unknown_verb
    "params",     # request schema                    -> bad_request
    "origin",     # origin kind allowed for the verb  -> origin_not_allowed
    "pause",      # relay or hub pause                -> paused
    "grant",      # grant covers (verb, target)       -> grant_*
    "semantics",  # the verb's own refusals
)


class RelayError(Exception):
    """An error envelope's ``error`` object, raised by clients and servers."""

    def __init__(self, code: str, message: str, *, details: Optional[Mapping[str, Any]] = None) -> None:
        if code not in ERROR_CODES:
            raise ValueError(f"unknown relay error code {code!r}")
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.retryable = ERROR_CODES[code][0]
        self.http_status = ERROR_CODES[code][1]
        self.details = dict(details) if details else None

    def to_error_object(self) -> "ErrorObject":
        error: ErrorObject = {"code": self.code, "message": self.message, "retryable": self.retryable}
        if self.details is not None:
            error["details"] = dict(self.details)
        return error

    @classmethod
    def from_error_object(cls, error: Mapping[str, Any]) -> "RelayError":
        code = error.get("code")
        if code not in ERROR_CODES:
            raise ValueError(f"error object names unknown code {code!r}")
        if error.get("retryable") is not ERROR_CODES[code][0]:
            raise ValueError(f"error object for {code!r} has retryable={error.get('retryable')!r}")
        return cls(code, str(error.get("message", "")), details=error.get("details"))


# --------------------------------------------------------------------------
# mailbox state machine
# --------------------------------------------------------------------------

MAILBOX_STATES = (
    "queued",
    "delivered",
    "acked",
    "answered",
    "declined",
    "lapsed",
    "session-ended",
    "refused",
)
#: A message row is created in one of these.
INITIAL_STATES = frozenset({"queued", "refused"})
#: No transition leaves these.
TERMINAL_STATES = frozenset({"answered", "declined", "lapsed", "session-ended", "refused"})

#: Every allowed (from, to) transition and the trigger that causes it.
TRANSITIONS: Mapping[tuple[str, str], str] = {
    ("queued", "delivered"): "poll",
    ("queued", "lapsed"): "expire",
    ("queued", "session-ended"): "session_end",
    ("delivered", "delivered"): "redeliver",
    ("delivered", "acked"): "ack",
    ("delivered", "answered"): "reply",
    ("delivered", "declined"): "reply",
    ("delivered", "lapsed"): "expire",
    ("delivered", "session-ended"): "session_end",
    ("acked", "answered"): "reply",
    ("acked", "declined"): "reply",
    ("acked", "session-ended"): "session_end",
}

#: A ``note`` expects no reply, so an acked note is complete: it never moves to
#: ``session-ended``.
KIND_EXCLUDED_TRANSITIONS: Mapping[str, frozenset[tuple[str, str]]] = {
    "note": frozenset({("acked", "session-ended")}),
    "instruction": frozenset(),
    "question": frozenset(),
}


def transition_allowed(from_state: str, to_state: str, kind: str) -> bool:
    """Whether a message of ``kind`` may move from ``from_state`` to ``to_state``."""
    if from_state not in MAILBOX_STATES or to_state not in MAILBOX_STATES:
        raise ValueError(f"unknown mailbox state in {from_state!r} -> {to_state!r}")
    if kind not in KIND_EXCLUDED_TRANSITIONS:
        raise ValueError(f"unknown message kind {kind!r}")
    pair = (from_state, to_state)
    return pair in TRANSITIONS and pair not in KIND_EXCLUDED_TRANSITIONS[kind]


def require_transition(from_state: str, to_state: str, kind: str) -> None:
    """Raise ``invalid_transition`` unless the transition is allowed."""
    if not transition_allowed(from_state, to_state, kind):
        raise RelayError(
            "invalid_transition",
            f"a {kind} message cannot move from {from_state} to {to_state}",
            details={"from_state": from_state, "to_state": to_state},
        )


# --------------------------------------------------------------------------
# address grammar, selectors, identities, ids
# --------------------------------------------------------------------------

#: Words a machine label may not be, so a scoped selector parses one way.
RESERVED_LABELS = ("claude", "codex", "llm", "latest", "task", "cwd", "q")

_RESERVED_ALT = "|".join(RESERVED_LABELS)
MACHINE_LABEL = rf"(?!(?:{_RESERVED_ALT})(?![a-z0-9-]))[a-z0-9](?:[a-z0-9-]{{0,61}}[a-z0-9])?"
SESSION_ID = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_SEGMENT = r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}"
_HARNESS_ALT = "|".join(HARNESSES)
_SESSION_HARNESS_ALT = "|".join(SESSION_HARNESSES)

MACHINE_RE = re.compile(MACHINE_LABEL)
ADDRESS_RE = re.compile(
    rf"(?P<machine>{MACHINE_LABEL})/(?P<harness>{_HARNESS_ALT})/(?P<session>{SESSION_ID})"
)
SELECTOR_RE = re.compile(
    rf"(?:(?P<machine>{MACHINE_LABEL})/)?(?:(?P<harness>{_SESSION_HARNESS_ALT})/)?"
    rf"(?:(?P<latest>latest)"
    rf"|task:(?P<task>{_SEGMENT}/{_SEGMENT}(?:/{_SEGMENT})*)"
    rf"|cwd:(?P<cwd>/[^\x00-\x1f\x7f]{{0,1023}}|{_SEGMENT}))"
)
HUB_USER = r"[A-Za-z0-9._@+-]{1,128}"
IDENTITY_RE = re.compile(
    rf"(?:(?P<kind>channel|direct):(?P<address>{MACHINE_LABEL}/claude/{SESSION_ID})"
    rf"|(?P<mkind>operator|relay):(?P<machine>{MACHINE_LABEL})"
    rf"|claude\.ai/(?P<user>{HUB_USER}))"
)

#: Id grammars by id kind. Message and reply ids are chosen by their sender
#: (idempotency keys); grant, registration and question ids by the relay.
ID_PATTERNS: Mapping[str, re.Pattern[str]] = {
    "request": re.compile(r"[A-Za-z0-9._:-]{1,128}"),
    "message": re.compile(r"msg_[A-Za-z0-9_-]{1,64}"),
    "reply": re.compile(r"rpl_[A-Za-z0-9_-]{1,64}"),
    "grant": re.compile(r"grt_[A-Za-z0-9_-]{1,64}"),
    "registration": re.compile(r"reg_[A-Za-z0-9_-]{1,64}"),
    "question": re.compile(rf"q:{MACHINE_LABEL}/[A-Za-z0-9._:-]{{1,200}}"),
}


@dataclass(frozen=True)
class Address:
    """``<machine>/<harness>/<session>``: one session or one LLM endpoint."""

    machine: str
    harness: str
    session: str

    def __str__(self) -> str:
        return f"{self.machine}/{self.harness}/{self.session}"


@dataclass(frozen=True)
class Selector:
    """A friendly target that the relay resolves to exactly one address.

    ``kind`` is ``latest``, ``task`` (value ``<project>/<stub>``) or ``cwd``
    (value a project name matched against the cwd's last component, or an
    absolute path matched exactly). ``machine`` and ``harness`` narrow it.
    """

    kind: str
    value: str
    machine: Optional[str] = None
    harness: Optional[str] = None

    def __str__(self) -> str:
        scope = "".join(f"{part}/" for part in (self.machine, self.harness) if part)
        body = "latest" if self.kind == "latest" else f"{self.kind}:{self.value}"
        return scope + body


@dataclass(frozen=True)
class Identity:
    """Who a request (or audit row) is from.

    ``kind`` is one of :data:`IDENTITY_KINDS`; ``subject`` is the session
    address (channel, direct), the machine label (operator, relay) or the
    connector user (hub).
    """

    kind: str
    subject: str

    def __str__(self) -> str:
        if self.kind == "hub":
            return f"claude.ai/{self.subject}"
        return f"{self.kind}:{self.subject}"


def parse_address(text: str) -> Address:
    """The address ``text`` names; :class:`RelayError` ``target_invalid`` otherwise."""
    match = ADDRESS_RE.fullmatch(text) if isinstance(text, str) else None
    if match is None or match["session"] == "latest":
        raise RelayError("target_invalid", f"{text!r} is not <machine>/<harness>/<session>")
    return Address(match["machine"], match["harness"], match["session"])


def parse_target(text: str) -> Union[Address, Selector]:
    """An address or a selector; :class:`RelayError` ``target_invalid`` otherwise.

    The two forms are disjoint: a session id never contains ``:`` and is never
    ``latest``, and every selector is ``latest`` or contains ``:``.
    """
    if not isinstance(text, str):
        raise RelayError("target_invalid", f"{text!r} is not a target string")
    match = SELECTOR_RE.fullmatch(text)
    if match is not None:
        if match["latest"]:
            kind, value = "latest", ""
        elif match["task"] is not None:
            kind, value = "task", match["task"]
        else:
            kind, value = "cwd", match["cwd"]
        return Selector(kind, value, match["machine"], match["harness"])
    return parse_address(text)


def parse_identity(text: str) -> Identity:
    """The identity ``text`` names; :class:`RelayError` ``bad_request`` otherwise."""
    match = IDENTITY_RE.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise RelayError("bad_request", f"{text!r} is not a relay identity")
    if match["kind"]:
        return Identity(match["kind"], match["address"])
    if match["mkind"]:
        return Identity(match["mkind"], match["machine"])
    return Identity("hub", match["user"])


def is_machine_label(text: str) -> bool:
    return isinstance(text, str) and MACHINE_RE.fullmatch(text) is not None


def is_valid_id(kind: str, text: str) -> bool:
    """Whether ``text`` is a well-formed id of ``kind`` (a key of :data:`ID_PATTERNS`)."""
    pattern = ID_PATTERNS[kind]
    return isinstance(text, str) and pattern.fullmatch(text) is not None


# --------------------------------------------------------------------------
# envelopes and shared objects
# --------------------------------------------------------------------------


class ErrorObject(TypedDict):
    code: str
    message: str
    retryable: bool
    details: NotRequired[dict[str, Any]]


class Signed(TypedDict, total=False):
    """Reserved for a signing proof scheme (build-plan alternative B).

    relay-protocol/1 defines the fields and no verification: a peer that does
    not verify signatures accepts a well-formed block and MUST NOT treat any of
    it as authentication.
    """

    audience: str
    issued_at: float
    not_after: float
    nonce: str
    key_id: str
    alg: str
    signature: str


class RequestEnvelope(TypedDict):
    protocol: str
    id: str
    verb: str
    origin: str
    params: dict[str, Any]
    signed: NotRequired[Signed]


class OkResponse(TypedDict):
    protocol: str
    id: str
    ok: Literal[True]
    result: dict[str, Any]
    signed: NotRequired[Signed]


class ErrorResponse(TypedDict):
    protocol: str
    id: Optional[str]
    ok: Literal[False]
    error: ErrorObject
    signed: NotRequired[Signed]


ResponseEnvelope = Union[OkResponse, ErrorResponse]


class OriginProofNone(TypedDict):
    """Alternative A: the grant came through the guardrail hook. Proves nothing."""

    type: Literal["none"]


class OriginProofEnclaveSignature(TypedDict):
    """Reserved for alternative B; a relay-protocol/1 relay refuses ``proof_unsupported``."""

    type: Literal["enclave-signature"]
    key_id: NotRequired[str]
    alg: NotRequired[str]
    challenge: NotRequired[str]
    signature: NotRequired[str]
    signed: NotRequired[Signed]


OriginProof = Union[OriginProofNone, OriginProofEnclaveSignature]

#: Every origin_proof type the schema admits and its status in relay-protocol/1.
ORIGIN_PROOF_TYPES: Mapping[str, str] = {
    "none": "accepted",
    "enclave-signature": "reserved",
}


class Reply(TypedDict):
    id: str
    in_reply_to: str
    sender: str
    state: Literal["answered", "declined"]
    body: str
    at: float


class MessageRecord(TypedDict):
    id: str
    to: str
    sender: str
    kind: str
    body: str
    hop: int
    created_at: float
    expires_at: float
    updated_at: float
    state: str
    attempts: int
    task: NotRequired[str]
    state_reason: NotRequired[str]
    reply: NotRequired[Reply]


class DeliveredMessage(TypedDict):
    id: str
    sender: str
    kind: str
    body: str
    hop: int
    created_at: float
    expires_at: float
    attempt: int
    task: NotRequired[str]
    signed: NotRequired[Signed]


class Session(TypedDict):
    address: str
    machine: str
    harness: str
    session_id: str
    state: str
    registered: bool
    accepts: list[str]
    cwd: NotRequired[str]
    title: NotRequired[str]
    task: NotRequired[str]
    pid: NotRequired[int]
    version: NotRequired[str]
    last_activity_at: NotRequired[float]


class PauseFlag(TypedDict):
    paused: bool
    paused_since: Optional[float]
    reason: Optional[str]


class PauseState(TypedDict):
    relay: PauseFlag
    hub: PauseFlag


class MachineReach(TypedDict):
    """Reachability of one machine in a fan-out result; unreachable is listed, never omitted."""

    machine: str
    status: str
    error: NotRequired[ErrorObject]


class Machine(TypedDict):
    machine: str
    status: str
    protocol: NotRequired[str]
    last_contact_at: NotRequired[float]
    pause: NotRequired[PauseState]
    endpoints: NotRequired[list[str]]
    error: NotRequired[ErrorObject]


class Turn(TypedDict):
    role: str
    text: str
    at: NotRequired[float]
    truncated: NotRequired[bool]


class ChatMessage(TypedDict):
    role: str
    content: str


class QuestionSource(TypedDict):
    kind: Literal["job"]
    ledger: str
    run_id: str
    job_id: str
    interrupt_id: str


class Question(TypedDict):
    id: str
    machine: str
    source: QuestionSource
    kind: str
    payload: dict[str, Any]
    request_schema: dict[str, Any]
    created_at: float
    expires_at: Optional[float]
    state: str


class Grant(TypedDict):
    grant_id: str
    session: str
    verb: str
    targets: list[str]
    issued_at: float
    expires_at: float
    uses: int
    uses_remaining: int
    origin_proof: OriginProof
    state: str


class AuditRow(TypedDict):
    seq: int
    at: float
    machine: str
    ingress: str
    event: str
    origin: str
    verb: str
    outcome: str
    request_id: NotRequired[Optional[str]]
    target: NotRequired[Optional[str]]
    grant_id: NotRequired[Optional[str]]
    message_id: NotRequired[Optional[str]]
    from_state: NotRequired[Optional[str]]
    to_state: NotRequired[Optional[str]]
    code: NotRequired[Optional[str]]
    detail: NotRequired[Optional[str]]


# --------------------------------------------------------------------------
# verb params and results
# --------------------------------------------------------------------------

# -- channel handshake and delivery (origin channel:<address>) ------------


class RegisterParams(TypedDict):
    session_id: str
    harness: Literal["claude"]
    pid: int
    cwd: str
    version: str
    accepts: list[str]
    channel_nonce: str
    title: NotRequired[str]
    task: NotRequired[str]
    transcript_path: NotRequired[str]


class RegisterResult(TypedDict):
    registration_id: str
    address: str
    machine: str
    heartbeat_s: int
    lease_s: int
    lease_expires_at: float
    ack_timeout_s: int
    max_wait_s: int
    paused: bool
    replaced: bool


class HeartbeatParams(TypedDict):
    registration_id: str
    session_id: str
    channel_nonce: str
    previous_session_id: NotRequired[str]
    cwd: NotRequired[str]
    title: NotRequired[str]
    task: NotRequired[str]
    accepts: NotRequired[list[str]]


class HeartbeatResult(TypedDict):
    registration_id: str
    address: str
    lease_expires_at: float
    paused: bool
    session_changed: bool
    ended_messages: int


class DeregisterParams(TypedDict):
    registration_id: str
    session_id: str
    channel_nonce: str
    reason: NotRequired[str]


class DeregisterResult(TypedDict):
    registration_id: str
    address: str
    ended_messages: int


class PollParams(TypedDict):
    registration_id: str
    session_id: str
    channel_nonce: str
    wait_s: int
    max_messages: NotRequired[int]


class PollResult(TypedDict):
    messages: list[DeliveredMessage]
    paused: bool
    lease_expires_at: float


class AckParams(TypedDict):
    registration_id: str
    session_id: str
    channel_nonce: str
    message_id: str


class AckResult(TypedDict):
    message_id: str
    state: str


class ReplyParams(TypedDict):
    registration_id: str
    session_id: str
    channel_nonce: str
    in_reply_to: str
    state: Literal["answered", "declined"]
    body: str
    id: NotRequired[str]


class ReplyResult(TypedDict):
    reply: Reply
    message_state: str
    replayed: bool


# -- hub tools (design section 7) and their /direct client verbs ----------


class ListMachinesParams(TypedDict):
    pass


class ListMachinesResult(TypedDict):
    machines: list[Machine]


class ListSessionsParams(TypedDict, total=False):
    machine: str
    harness: str
    state: str


class ListSessionsResult(TypedDict):
    sessions: list[Session]
    machines: list[MachineReach]


class ReadSessionParams(TypedDict):
    address: str
    since: NotRequired[str]
    limit: NotRequired[int]


class ReadSessionResult(TypedDict):
    address: str
    turns: list[Turn]
    cursor: Optional[str]
    truncated: bool
    redactions: int


class AskLlmParams(TypedDict):
    endpoint: str
    prompt: NotRequired[str]
    messages: NotRequired[list[ChatMessage]]
    conversation: NotRequired[str]
    max_tokens: NotRequired[int]
    temperature: NotRequired[float]


class AskLlmResult(TypedDict):
    endpoint: str
    text: str
    finish_reason: NotRequired[Optional[str]]
    conversation: NotRequired[str]
    turn: NotRequired[int]
    usage: NotRequired[dict[str, int]]


class SendMessageParams(TypedDict):
    to: str
    kind: str
    body: str
    id: NotRequired[str]
    expires_in_s: NotRequired[int]
    task: NotRequired[str]
    hop: NotRequired[int]


class SendMessageResult(TypedDict):
    message: MessageRecord
    replayed: bool


class GetRepliesParams(TypedDict, total=False):
    message_id: str
    address: str
    wait_s: int
    limit: int


class GetRepliesResult(TypedDict):
    messages: list[MessageRecord]


class ListQuestionsParams(TypedDict, total=False):
    machine: str
    state: str


class ListQuestionsResult(TypedDict):
    questions: list[Question]
    machines: list[MachineReach]


class AnswerQuestionParams(TypedDict):
    id: str
    decision: Literal["answer", "reject"]
    input: NotRequired[Any]
    reason: NotRequired[str]


class AnswerQuestionResult(TypedDict):
    id: str
    decision: str
    state: str
    replayed: bool
    resolved_at: float


# -- grants (origin direct:<address>) --------------------------------------


class GrantMintParams(TypedDict):
    session: str
    verb: str
    targets: list[str]
    expires_in_s: int
    uses: int
    origin_proof: OriginProof
    command: NotRequired[str]


class GrantMintResult(TypedDict):
    grant: Grant


class GrantCheckParams(TypedDict):
    session: str
    verb: str
    target: NotRequired[str]
    grant_id: NotRequired[str]


class GrantCheckResult(TypedDict):
    covered: bool
    grant_id: Optional[str]
    code: Optional[str]


# -- control ----------------------------------------------------------------


class PauseParams(TypedDict):
    scope: Literal["relay", "hub"]
    reason: NotRequired[str]


class PauseResult(TypedDict):
    pause: PauseState


class ResumeParams(TypedDict):
    scope: Literal["relay", "hub"]


class ResumeResult(TypedDict):
    pause: PauseState


class StatusParams(TypedDict):
    pass


class StatusResult(TypedDict):
    protocol: str
    machine: str
    now: float
    pause: PauseState
    library_version: NotRequired[str]
    registrations: NotRequired[int]


# --------------------------------------------------------------------------
# the verb table
# --------------------------------------------------------------------------

TIERS = ("channel", "read", "llm", "write", "grant", "control")


@dataclass(frozen=True)
class VerbSpec:
    """One verb: who may call it, whether a grant is needed, and its types.

    ``origins`` are the identity kinds that may originate the verb (anything
    else is ``origin_not_allowed``). ``grant_for`` are the origin kinds for
    which a covering grant is required; it is a subset of ``origins``.
    ``target_params`` name the params whose value is the grant target (at most
    one is present in a request); a verb without one is covered only by a
    grant with an empty ``targets`` list. ``client_verb`` is the ``/direct``
    and ``llm-kit relay client`` name; ``hub_tool`` marks a hub MCP tool, whose
    arguments and result are exactly this verb's params and result.
    """

    name: str
    tier: str
    origins: frozenset[str]
    grant_for: frozenset[str]
    target_params: tuple[str, ...]
    client_verb: Optional[str]
    hub_tool: bool
    params_type: type
    result_type: type
    summary: str


def _verb(name, tier, origins, grant_for, target_params, client_verb, hub_tool, params, result, summary):
    return VerbSpec(
        name, tier, frozenset(origins), frozenset(grant_for), tuple(target_params),
        client_verb, hub_tool, params, result, summary,
    )


_CHANNEL = ("channel",)
_HUB_SIDE = ("direct", "hub")

VERBS: Mapping[str, VerbSpec] = {
    spec.name: spec
    for spec in (
        _verb("register", "channel", _CHANNEL, (), (), None, False, RegisterParams, RegisterResult,
              "A channel server registers its session; returns the registration and lease."),
        _verb("heartbeat", "channel", _CHANNEL, (), (), None, False, HeartbeatParams, HeartbeatResult,
              "Renew the lease and report metadata, including a session-id change."),
        _verb("deregister", "channel", _CHANNEL, (), (), None, False, DeregisterParams, DeregisterResult,
              "End the registration; unfinished messages become session-ended."),
        _verb("poll", "channel", _CHANNEL, (), (), None, False, PollParams, PollResult,
              "Long-poll for messages to deliver into the session."),
        _verb("ack", "channel", _CHANNEL, (), (), None, False, AckParams, AckResult,
              "Acknowledge one delivered message after pushing it into the session."),
        _verb("reply", "channel", _CHANNEL, (), (), None, False, ReplyParams, ReplyResult,
              "Answer or decline one delivered message."),
        _verb("list_machines", "read", _HUB_SIDE, ("direct",), (), "machines", True,
              ListMachinesParams, ListMachinesResult, "Machines with relay status and last contact."),
        _verb("list_sessions", "read", _HUB_SIDE, ("direct",), ("machine",), "sessions", True,
              ListSessionsParams, ListSessionsResult, "Sessions with address, cwd, title, task and state."),
        _verb("read_session", "read", _HUB_SIDE, ("direct",), ("address",), "read", True,
              ReadSessionParams, ReadSessionResult, "A capped, redacted tail of one session."),
        _verb("ask_llm", "llm", _HUB_SIDE, ("direct",), ("endpoint",), "ask", True,
              AskLlmParams, AskLlmResult, "A completion from a local LLM endpoint."),
        _verb("send_message", "write", _HUB_SIDE, _HUB_SIDE, ("to",), "send", True,
              SendMessageParams, SendMessageResult, "Queue a message for an opted-in session."),
        _verb("get_replies", "read", _HUB_SIDE, ("direct",), ("message_id", "address"), "replies", True,
              GetRepliesParams, GetRepliesResult, "Messages and their replies, with a capped long-poll."),
        _verb("list_questions", "read", _HUB_SIDE, ("direct",), ("machine",), "questions", True,
              ListQuestionsParams, ListQuestionsResult, "Pending operator questions (job interrupts)."),
        _verb("answer_question", "write", _HUB_SIDE, _HUB_SIDE, ("id",), "answer", True,
              AnswerQuestionParams, AnswerQuestionResult,
              "Answer or reject one question, as `llm-kit jobs resolve` would."),
        _verb("grant_mint", "grant", ("direct",), (), (), None, False, GrantMintParams, GrantMintResult,
              "Mint a grant for the calling /direct session."),
        _verb("grant_check", "grant", ("direct",), (), (), None, False, GrantCheckParams, GrantCheckResult,
              "Whether a live grant covers (session, verb, target), without consuming it."),
        _verb("pause", "control", ("operator",), (), (), None, False, PauseParams, PauseResult,
              "Set the relay or hub pause flag."),
        _verb("resume", "control", ("operator",), (), (), None, False, ResumeParams, ResumeResult,
              "Clear the relay or hub pause flag."),
        _verb("status", "control", REQUEST_ORIGIN_KINDS, (), (), None, False, StatusParams, StatusResult,
              "Protocol, machine, pause state."),
    )
}

#: ``/direct`` and ``llm-kit relay client`` verb -> wire verb (design 7b).
CLIENT_VERBS: Mapping[str, str] = {
    spec.client_verb: spec.name for spec in VERBS.values() if spec.client_verb is not None
}
#: The hub's MCP tools (design section 7), in table order.
HUB_TOOLS: tuple[str, ...] = tuple(spec.name for spec in VERBS.values() if spec.hub_tool)
#: Verbs a grant may name: exactly the client verbs' wire verbs.
GRANTABLE_VERBS: frozenset[str] = frozenset(CLIENT_VERBS.values())

#: While the relay is paused: these verbs are refused ``paused`` from every
#: origin, and ``poll`` returns no messages (with ``paused: true``). Expiry
#: keeps running, so a queued message can lapse during a pause.
RELAY_PAUSE_REFUSES: frozenset[str] = frozenset({"send_message", "answer_question"})
RELAY_PAUSE_EMPTIES: frozenset[str] = frozenset({"poll"})
#: While the hub is paused: the hub refuses every tool call, and relays refuse
#: every request whose origin kind is listed here, with ``paused``.
HUB_PAUSE_REFUSES_ORIGINS: frozenset[str] = frozenset({"hub"})

#: Message states a session end or session-id change moves to session-ended
#: (``acked`` only for kinds that expect a reply).
SESSION_END_FROM_STATES: frozenset[str] = frozenset({"queued", "delivered", "acked"})


# --------------------------------------------------------------------------
# schemas
# --------------------------------------------------------------------------

#: The shared ``$defs`` bundle every other schema file references.
COMMON_SCHEMA = "common"
ENVELOPE_SCHEMAS = ("envelope.request", "envelope.response")
#: Standalone schemas for the shared objects (each a ``$ref`` into common).
OBJECT_SCHEMAS = (
    "error",
    "signed",
    "origin_proof",
    "grant",
    "audit_row",
    "message_record",
    "delivered_message",
    "reply_record",
    "session",
    "machine",
    "pause_state",
    "question",
)


def request_schema_name(verb: str) -> str:
    if verb not in VERBS:
        raise KeyError(f"unknown relay verb {verb!r}")
    return f"{verb}.request"


def response_schema_name(verb: str) -> str:
    if verb not in VERBS:
        raise KeyError(f"unknown relay verb {verb!r}")
    return f"{verb}.response"


def schema_names() -> tuple[str, ...]:
    """Every schema file (without ``.json``) the protocol defines."""
    names = [COMMON_SCHEMA, *ENVELOPE_SCHEMAS, *OBJECT_SCHEMAS]
    for verb in VERBS:
        names += [request_schema_name(verb), response_schema_name(verb)]
    return tuple(names)


def _read_schema_file(name: str) -> dict[str, Any]:
    if name not in schema_names():
        raise KeyError(f"unknown relay schema {name!r}")
    path = resources.files(__package__).joinpath("schemas", f"{name}.json")
    return json.loads(path.read_text(encoding="utf-8"))


def load_schema(name: str) -> dict[str, Any]:
    """The named schema, self-contained: ``common``'s ``$defs`` merged in.

    A schema file references shared definitions as ``#/$defs/<name>``; the
    definitions live once, in ``common.json``. The composed result resolves
    every such reference locally, so it can be checked and validated by
    ``llm_kit.schema.json_schema`` under :data:`SCHEMA_SUBSET`. A file that
    defines a ``$defs`` name common also defines is refused.
    """
    schema = _read_schema_file(name)
    if name == COMMON_SCHEMA:
        return schema
    common = _read_schema_file(COMMON_SCHEMA)["$defs"]
    own = schema.get("$defs", {})
    clash = sorted(set(own) & set(common))
    if clash:
        raise ValueError(f"relay schema {name!r} redefines common $defs {clash}")
    schema["$defs"] = {**common, **own}
    return schema


# --------------------------------------------------------------------------
# envelope builders
# --------------------------------------------------------------------------


def make_request(
    verb: str,
    params: Mapping[str, Any],
    *,
    origin: str,
    request_id: str,
    signed: Optional[Signed] = None,
) -> RequestEnvelope:
    if verb not in VERBS:
        raise KeyError(f"unknown relay verb {verb!r}")
    envelope: RequestEnvelope = {
        "protocol": PROTOCOL,
        "id": request_id,
        "verb": verb,
        "origin": origin,
        "params": dict(params),
    }
    if signed is not None:
        envelope["signed"] = signed
    return envelope


def make_ok(request_id: str, result: Mapping[str, Any]) -> OkResponse:
    return {"protocol": PROTOCOL, "id": request_id, "ok": True, "result": dict(result)}


def make_error(request_id: Optional[str], error: RelayError) -> ErrorResponse:
    return {"protocol": PROTOCOL, "id": request_id, "ok": False, "error": error.to_error_object()}


# --------------------------------------------------------------------------
# the client seam
# --------------------------------------------------------------------------


@runtime_checkable
class RelayClient(Protocol):
    """A connection to one relay. The hub holds one per (machine, URL); the CLI one.

    Every method sends one verb as ``origin`` (an identity string) and returns
    its result, or raises :class:`RelayError` carrying the error envelope's
    code. A transport failure raises ``machine_unreachable``. ``grant`` is the
    optional grant reference sent as :data:`HEADER_GRANT`.
    """

    @property
    def machine(self) -> str: ...

    def call(self, verb: str, params: Mapping[str, Any], *, origin: str,
             grant: Optional[str] = None) -> dict[str, Any]: ...

    def register(self, params: RegisterParams, *, origin: str,
                 grant: Optional[str] = None) -> RegisterResult: ...

    def heartbeat(self, params: HeartbeatParams, *, origin: str,
                  grant: Optional[str] = None) -> HeartbeatResult: ...

    def deregister(self, params: DeregisterParams, *, origin: str,
                   grant: Optional[str] = None) -> DeregisterResult: ...

    def poll(self, params: PollParams, *, origin: str,
             grant: Optional[str] = None) -> PollResult: ...

    def ack(self, params: AckParams, *, origin: str,
            grant: Optional[str] = None) -> AckResult: ...

    def reply(self, params: ReplyParams, *, origin: str,
              grant: Optional[str] = None) -> ReplyResult: ...

    def list_machines(self, params: ListMachinesParams, *, origin: str,
                      grant: Optional[str] = None) -> ListMachinesResult: ...

    def list_sessions(self, params: ListSessionsParams, *, origin: str,
                      grant: Optional[str] = None) -> ListSessionsResult: ...

    def read_session(self, params: ReadSessionParams, *, origin: str,
                     grant: Optional[str] = None) -> ReadSessionResult: ...

    def ask_llm(self, params: AskLlmParams, *, origin: str,
                grant: Optional[str] = None) -> AskLlmResult: ...

    def send_message(self, params: SendMessageParams, *, origin: str,
                     grant: Optional[str] = None) -> SendMessageResult: ...

    def get_replies(self, params: GetRepliesParams, *, origin: str,
                    grant: Optional[str] = None) -> GetRepliesResult: ...

    def list_questions(self, params: ListQuestionsParams, *, origin: str,
                       grant: Optional[str] = None) -> ListQuestionsResult: ...

    def answer_question(self, params: AnswerQuestionParams, *, origin: str,
                        grant: Optional[str] = None) -> AnswerQuestionResult: ...

    def grant_mint(self, params: GrantMintParams, *, origin: str,
                   grant: Optional[str] = None) -> GrantMintResult: ...

    def grant_check(self, params: GrantCheckParams, *, origin: str,
                    grant: Optional[str] = None) -> GrantCheckResult: ...

    def pause(self, params: PauseParams, *, origin: str,
              grant: Optional[str] = None) -> PauseResult: ...

    def resume(self, params: ResumeParams, *, origin: str,
               grant: Optional[str] = None) -> ResumeResult: ...

    def status(self, params: StatusParams, *, origin: str,
               grant: Optional[str] = None) -> StatusResult: ...

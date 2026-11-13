"""Tests for llm_kit.relay.mailbox (unit M1).

Every test uses tmp directories and an injected clock: nothing reads or writes
the real state directory, and nothing sleeps.
"""
from __future__ import annotations

import sqlite3
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from llm_kit.relay import mailbox as M
from llm_kit.relay import protocol as P

MACHINE = "m5pro"
ALICE = f"{MACHINE}/claude/alice"      # the /direct session
BOB = f"{MACHINE}/claude/bob"          # the recipient
DIRECT_ALICE = f"direct:{ALICE}"
DIRECT_EVE = f"direct:{MACHINE}/claude/eve"
LLM = f"{MACHINE}/llm/qwen38-m5-32k-local"
NO_PROOF = {"type": "none"}


class Clock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def box(tmp_path: Path, clock: Clock) -> M.Mailbox:
    return M.Mailbox(tmp_path / "state" / "mailbox.sqlite3", machine=MACHINE, clock=clock)


def send(box: M.Mailbox, **kw):
    kw.setdefault("origin", DIRECT_ALICE)
    kw.setdefault("to", BOB)
    kw.setdefault("kind", "instruction")
    kw.setdefault("body", "run the tests")
    return box.send_message(**kw)


def mint(box: M.Mailbox, *, session: str = ALICE, verb: str = "send_message", targets=(BOB,), ttl: int = 300, uses: int = 1):
    return box.mint_grant(
        origin=f"direct:{session}", session=session, verb=verb, targets=list(targets),
        expires_in_s=ttl, uses=uses, origin_proof=NO_PROOF, command="/direct send",
    )


def code_of(call) -> str:
    with pytest.raises(P.RelayError) as info:
        call()
    return info.value.code


# --------------------------------------------------------------------------
# schema, file mode
# --------------------------------------------------------------------------

def test_schema_version_2_and_private_file(box):
    assert box.schema_version() == 2
    assert (box.db_path.stat().st_mode & 0o777) == 0o600
    again = M.Mailbox(box.db_path, machine=MACHINE)  # reopening is a no-op
    assert again.schema_version() == 2


def test_newer_schema_version_is_refused(tmp_path):
    path = tmp_path / "m.sqlite3"
    M.Mailbox(path, machine=MACHINE)
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE schema_version SET version = 7")
    assert code_of(lambda: M.Mailbox(path, machine=MACHINE)) == "internal"


# --------------------------------------------------------------------------
# send: idempotency, size, hop
# --------------------------------------------------------------------------

def test_resend_of_same_id_is_not_delivered_twice(box):
    first = send(box, id="msg_one")
    again = send(box, id="msg_one")
    assert (first["replayed"], again["replayed"]) == (False, True)
    assert again["message"] == first["message"]
    delivered = box.poll(BOB)
    assert [m["id"] for m in delivered] == ["msg_one"]
    # a resend after delivery and ack still creates nothing new
    box.ack(BOB, "msg_one")
    assert send(box, id="msg_one")["replayed"] is True
    assert box.poll(BOB) == []
    with sqlite3.connect(box.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_same_id_other_content_is_idempotency_conflict(box):
    send(box, id="msg_one")
    assert code_of(lambda: send(box, id="msg_one", body="something else")) == "idempotency_conflict"
    assert code_of(lambda: send(box, id="msg_one", origin=DIRECT_EVE)) == "idempotency_conflict"


def test_oversize_body_is_refused_and_row_readable(box):
    body = "x" * (P.MAX_BODY_BYTES + 1)
    assert code_of(lambda: send(box, id="msg_big", body=body)) == "too_large"
    record = box.get_message("msg_big")
    assert record["state"] == "refused" and record["state_reason"] == "too_large"
    assert len(record["body"]) < 100  # the oversize body is not stored
    assert box.poll(BOB) == []
    # the cap is in UTF-8 bytes: 4097 two-byte characters exceed it
    assert code_of(lambda: send(box, id="msg_wide", body="\u00e9" * 4097)) == "too_large"
    # exactly the cap is fine
    assert send(box, id="msg_edge", body="y" * P.MAX_BODY_BYTES)["replayed"] is False
    # replaying a refused id re-raises the refusal and writes nothing
    before = len(box.audit_rows(limit=1000))
    assert code_of(lambda: send(box, id="msg_big", body=body)) == "too_large"
    assert len(box.audit_rows(limit=1000)) == before


def test_hop_two_is_refused(box):
    assert send(box, id="msg_h1", hop=1)["message"]["hop"] == 1
    assert code_of(lambda: send(box, id="msg_h2", hop=2)) == "hop_limit"
    assert box.get_message("msg_h2")["state"] == "refused"
    assert [m["id"] for m in box.poll(BOB)] == ["msg_h1"]


def test_caller_refusals_store_a_refused_row(box):
    assert code_of(lambda: send(box, id="msg_ro", refuse="not_opted_in")) == "not_opted_in"
    assert box.get_message("msg_ro")["state"] == "refused"
    assert code_of(lambda: send(box, id="msg_x", refuse="paused")) == "bad_request"


@pytest.mark.parametrize("kw", [
    {"kind": "shout"}, {"body": ""}, {"id": "nope"}, {"expires_in_s": 0},
    {"expires_in_s": P.MAX_EXPIRES_IN_S + 1}, {"hop": -1}, {"to": "latest"},
])
def test_malformed_sends_create_no_row(box, kw):
    with pytest.raises(P.RelayError):
        send(box, **kw)
    assert box.audit_rows() == []


def test_only_direct_and_hub_origins_send(box):
    assert code_of(lambda: send(box, origin=f"channel:{BOB}")) == "origin_not_allowed"
    assert send(box, origin="claude.ai/christina")["replayed"] is False


# --------------------------------------------------------------------------
# state machine and delivery
# --------------------------------------------------------------------------

def test_full_life_queued_delivered_acked_answered(box, clock):
    send(box, id="msg_a")
    (msg,) = box.poll(BOB)
    assert msg["attempt"] == 1 and msg["kind"] == "instruction"
    assert box.ack(BOB, "msg_a") == {"message_id": "msg_a", "state": "acked"}
    assert box.ack(BOB, "msg_a")["state"] == "acked"  # idempotent
    result = box.reply(BOB, "msg_a", "answered", "done", id="rpl_a")
    assert result["message_state"] == "answered" and result["replayed"] is False
    assert box.reply(BOB, "msg_a", "answered", "done", id="rpl_a")["replayed"] is True
    record = box.get_message("msg_a")
    assert record["state"] == "answered" and record["reply"]["body"] == "done"
    transitions = [(r["from_state"], r["to_state"]) for r in box.audit_rows(message_id="msg_a")]
    assert transitions == [
        (None, "queued"), ("queued", "delivered"), ("delivered", "acked"), ("acked", "answered"),
    ]


def test_illegal_transition_raises(box):
    send(box, id="msg_a")
    # not yet delivered: cannot be acked or replied to
    assert code_of(lambda: box.ack(BOB, "msg_a")) == "invalid_transition"
    assert code_of(lambda: box.reply(BOB, "msg_a", "answered", "x")) == "invalid_transition"
    box.poll(BOB)
    box.reply(BOB, "msg_a", "declined", "no")
    # terminal: a second, different reply is invalid, and the row did not change
    assert code_of(lambda: box.reply(BOB, "msg_a", "answered", "yes", id="rpl_other")) == "invalid_transition"
    assert box.get_message("msg_a")["state"] == "declined"
    # the state machine itself refuses every move out of a terminal state
    row = {"state": "answered", "kind": "instruction", "id": "msg_a", "to_addr": BOB}
    with sqlite3.connect(box.db_path) as conn:
        conn.row_factory = sqlite3.Row
        for target in P.MAILBOX_STATES:
            with pytest.raises(P.RelayError) as info:
                box._transition(conn, row, target, verb="poll", origin=f"channel:{BOB}", now=1.0)
            assert info.value.code == "invalid_transition"


def test_note_acked_is_complete_and_never_session_ended(box):
    send(box, id="msg_n", kind="note")
    box.poll(BOB)
    box.ack(BOB, "msg_n")
    send(box, id="msg_i")
    box.poll(BOB)
    box.ack(BOB, "msg_i")
    assert box.end_session(BOB, reason="session_changed") == 1
    assert box.get_message("msg_n")["state"] == "acked"
    ended = box.get_message("msg_i")
    assert ended["state"] == "session-ended" and ended["state_reason"] == "session_changed"


def test_reply_and_ack_belong_to_the_recipient(box):
    send(box, id="msg_a")
    box.poll(BOB)
    other = f"{MACHINE}/claude/carol"
    assert code_of(lambda: box.ack(other, "msg_a")) == "message_not_found"
    assert code_of(lambda: box.reply(other, "msg_a", "answered", "x")) == "message_not_found"
    assert code_of(lambda: box.ack(BOB, "msg_missing")) == "message_not_found"


def test_reply_id_reuse_with_other_content_conflicts(box):
    send(box, id="msg_a")
    box.poll(BOB)
    box.reply(BOB, "msg_a", "answered", "one", id="rpl_x")
    assert code_of(lambda: box.reply(BOB, "msg_a", "answered", "two", id="rpl_x")) == "idempotency_conflict"
    assert code_of(lambda: box.reply(BOB, "msg_a", "answered", "x" * (P.MAX_BODY_BYTES + 1))) == "too_large"


def test_stale_message_lapses(box, clock):
    send(box, id="msg_q", expires_in_s=60)
    send(box, id="msg_d", expires_in_s=120)
    assert [m["id"] for m in box.poll(BOB, max_messages=1)] == ["msg_q"]  # msg_q delivered, never acked
    clock.advance(61)
    assert box.expire() == 1                                        # delivered-never-acked lapses
    assert box.get_message("msg_q")["state"] == "lapsed"
    clock.advance(120)
    assert box.poll(BOB) == []                                      # the queued one lapsed, not delivered stale
    assert box.get_message("msg_d")["state"] == "lapsed"
    assert box.get_message("msg_d")["state_reason"] == "expired"
    origins = {r["origin"] for r in box.audit_rows() if r["verb"] == "expire"}
    assert origins == {f"relay:{MACHINE}"}
    assert code_of(lambda: box.ack(BOB, "msg_q")) == "invalid_transition"


def test_lease_expiry_triggers_redelivery(box, clock):
    send(box, id="msg_a")
    assert [m["attempt"] for m in box.poll(BOB)] == [1]
    clock.advance(P.ACK_TIMEOUT_S - 1)
    assert box.poll(BOB) == []                                      # still inside the ack window
    clock.advance(1)
    redelivered = box.poll(BOB)
    assert [(m["id"], m["attempt"]) for m in redelivered] == [("msg_a", 2)]
    assert box.get_message("msg_a")["attempts"] == 2
    assert box.poll(BOB) == []                                      # a fresh window
    box.ack(BOB, "msg_a")
    clock.advance(10 * P.ACK_TIMEOUT_S)
    assert box.poll(BOB) == []                                      # acked: never redelivered
    moves = [(r["from_state"], r["to_state"]) for r in box.audit_rows(message_id="msg_a")]
    assert ("delivered", "delivered") in moves


def test_poll_is_per_address_oldest_first_and_capped(box, clock):
    for i in range(5):
        send(box, id=f"msg_{i}")
        clock.advance(1)
    send(box, id="msg_other", to=f"{MACHINE}/claude/carol")
    got = box.poll(BOB, max_messages=3)
    assert [m["id"] for m in got] == ["msg_0", "msg_1", "msg_2"]
    assert code_of(lambda: box.poll(BOB, max_messages=P.MAX_POLL_BATCH + 1)) == "bad_request"


# --------------------------------------------------------------------------
# rate limits
# --------------------------------------------------------------------------

def test_sender_global_and_target_rate_limits(tmp_path, clock):
    limits = M.MailboxLimits(window_s=60, sender_per_window=3, global_per_window=5, max_pending_per_target=100)
    box = M.Mailbox(tmp_path / "m.sqlite3", machine=MACHINE, clock=clock, limits=limits)
    for i in range(3):
        send(box, id=f"msg_a{i}")
    assert code_of(lambda: send(box, id="msg_a3")) == "rate_limited"
    assert box.find_message("msg_a3") is None                       # no row for a rate-limited send
    send(box, id="msg_e0", origin=DIRECT_EVE)                       # another sender still fits
    send(box, id="msg_e1", origin=DIRECT_EVE)
    assert code_of(lambda: send(box, id="msg_e2", origin=DIRECT_EVE)) == "rate_limited"  # global 5
    assert send(box, id="msg_a0")["replayed"] is True               # a replay is never limited
    clock.advance(61)
    assert send(box, id="msg_a9")["replayed"] is False              # the window moved

    tight = M.Mailbox(tmp_path / "t.sqlite3", machine=MACHINE, clock=clock,
                      limits=M.MailboxLimits(max_pending_per_target=2))
    send(tight, id="msg_t0")
    send(tight, id="msg_t1")
    assert code_of(lambda: send(tight, id="msg_t2")) == "rate_limited"
    tight.poll(BOB)
    tight.reply(BOB, "msg_t0", "answered", "ok")
    assert send(tight, id="msg_t2")["replayed"] is False            # finished messages free the queue


def test_limits_reject_nonpositive_values():
    with pytest.raises(ValueError):
        M.MailboxLimits(sender_per_window=0)


# --------------------------------------------------------------------------
# audit
# --------------------------------------------------------------------------

def test_audit_rows_are_immutable_via_the_api(box):
    send(box, id="msg_a")
    box.poll(BOB)
    call = box.append_audit(event="call", origin=DIRECT_ALICE, verb="send_message", outcome="ok",
                            request_id="req-1", target=BOB, message_id="msg_a")
    assert call["origin"] == DIRECT_ALICE and call["machine"] == MACHINE
    # the API offers no way to change or remove a row
    public = {name for name in dir(box) if not name.startswith("_")}
    assert not [n for n in public if "audit" in n and n not in ("append_audit", "audit_rows")]
    assert not [n for n in public if n.startswith(("update", "delete", "remove", "purge", "clear", "truncate"))]
    # and the table itself refuses raw rewrites
    with sqlite3.connect(box.db_path) as conn:
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("UPDATE audit SET outcome = 'error'")
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            conn.execute("DELETE FROM audit")
    rows = box.audit_rows()
    assert [r["seq"] for r in rows] == sorted({r["seq"] for r in rows})  # strictly increasing
    assert box.audit_rows(since_seq=rows[0]["seq"])[0]["seq"] > rows[0]["seq"]


def test_every_transition_carries_origin_identity(box, clock):
    send(box, id="msg_a", origin=DIRECT_ALICE)
    send(box, id="msg_b", origin="claude.ai/christina")
    box.poll(BOB)
    by_msg = {}
    for r in box.audit_rows():
        by_msg.setdefault((r["message_id"], r["to_state"]), r["origin"])
    assert by_msg[("msg_a", "queued")] == DIRECT_ALICE
    assert by_msg[("msg_b", "queued")] == "claude.ai/christina"
    assert by_msg[("msg_a", "delivered")] == f"channel:{BOB}"
    for r in box.audit_rows():
        P.parse_identity(r["origin"])
        assert r["event"] == "transition" and r["ingress"] == "relay"


def test_audit_rejects_unknown_vocabulary(box):
    for bad in ({"event": "boom"}, {"outcome": "meh"}, {"verb": "fly"}, {"code": "nope"}, {"origin": "garbage"}):
        kw = dict(event="call", origin=DIRECT_ALICE, verb="send_message", outcome="ok")
        kw.update(bad)
        with pytest.raises(P.RelayError):
            box.append_audit(**kw)
    assert box.audit_rows() == []


# --------------------------------------------------------------------------
# grants
# --------------------------------------------------------------------------

def test_mint_check_consume_and_state(box, clock):
    g = mint(box, uses=2)
    assert g["state"] == "live" and g["uses_remaining"] == 2 and g["origin_proof"] == NO_PROOF
    covered = box.check_grant(session=ALICE, verb="send_message", target=BOB)
    assert covered == {"covered": True, "grant_id": g["grant_id"], "code": None}
    assert box.get_grant(g["grant_id"])["uses_remaining"] == 2      # check does not consume
    box.consume_grant(session=ALICE, verb="send_message", target=BOB)
    assert box.consume_grant(session=ALICE, verb="send_message", target=BOB, grant_id=g["grant_id"])["state"] == "used-up"
    assert code_of(lambda: box.consume_grant(session=ALICE, verb="send_message", target=BOB)) == "grant_required"
    assert box.check_grant(session=ALICE, verb="send_message", target=BOB, grant_id=g["grant_id"])["code"] == "grant_used_up"


def test_named_grant_refusal_order(box, clock):
    g = mint(box, targets=(BOB,), ttl=10, uses=1)
    gid = g["grant_id"]
    check = lambda **kw: box.check_grant(**{"session": ALICE, "verb": "send_message", "target": BOB, "grant_id": gid, **kw})
    assert box.check_grant(session=ALICE, verb="send_message", target=BOB, grant_id="grt_missing")["code"] == "grant_unknown"
    # several things wrong at once: the first in F0 order wins
    assert check(session=f"{MACHINE}/claude/eve", verb="ask_llm", target="elsewhere")["code"] == "grant_wrong_session"
    assert check(verb="ask_llm", target="elsewhere")["code"] == "grant_wrong_verb"
    assert check(target=f"{MACHINE}/claude/carol")["code"] == "grant_wrong_target"
    assert check()["covered"] is True
    box.consume_grant(session=ALICE, verb="send_message", target=BOB, grant_id=gid)
    clock.advance(11)
    assert check()["code"] == "grant_expired"                       # expired outranks used-up


def test_no_target_covered_only_by_empty_targets(box):
    scoped = mint(box, verb="list_machines", targets=(BOB,))
    assert box.check_grant(session=ALICE, verb="list_machines", grant_id=scoped["grant_id"])["code"] == "grant_wrong_target"
    assert box.check_grant(session=ALICE, verb="list_machines")["code"] == "grant_required"
    open_grant = mint(box, verb="list_machines", targets=())
    assert box.check_grant(session=ALICE, verb="list_machines")["grant_id"] == open_grant["grant_id"]
    # an empty-target grant does not cover a call that names a target
    assert box.check_grant(session=ALICE, verb="list_machines", target=BOB)["grant_id"] == scoped["grant_id"]


def test_cross_session_replay_is_refused(box):
    g = mint(box, session=ALICE)
    eve = f"{MACHINE}/claude/eve"
    # named: wrong session
    assert code_of(lambda: box.consume_grant(session=eve, verb="send_message", target=BOB, grant_id=g["grant_id"])) == "grant_wrong_session"
    # unnamed: eve has no grant of her own
    assert code_of(lambda: box.consume_grant(session=eve, verb="send_message", target=BOB)) == "grant_required"
    assert code_of(lambda: send(box, origin=f"direct:{eve}", grant=M.GrantUse(eve, g["grant_id"]))) == "grant_wrong_session"
    assert box.get_grant(g["grant_id"])["uses_remaining"] == 1       # nothing consumed
    assert box.find_message("msg_nothing") is None


def test_mint_validation(box):
    ok = dict(origin=DIRECT_ALICE, session=ALICE, verb="send_message", targets=[BOB], expires_in_s=60, uses=1, origin_proof=NO_PROOF)
    for patch, code in [
        ({"origin": DIRECT_EVE}, "origin_not_allowed"),                        # mints only for itself
        ({"origin": f"channel:{ALICE}"}, "origin_not_allowed"),
        ({"origin": "claude.ai/christina"}, "origin_not_allowed"),
        ({"verb": "pause"}, "bad_request"),
        ({"expires_in_s": 0}, "bad_request"),
        ({"expires_in_s": P.MAX_GRANT_TTL_S + 1}, "bad_request"),
        ({"uses": 0}, "bad_request"),
        ({"uses": P.MAX_GRANT_USES + 1}, "bad_request"),
        ({"targets": [f"{MACHINE}/claude/latest"]}, "target_invalid"),
        ({"targets": ["latest"]}, "target_invalid"),
        ({"targets": [f"cwd:/tmp/x"]}, "target_invalid"),
        ({"origin_proof": {"type": "none", "extra": 1}}, "bad_request"),
        ({"origin_proof": {"type": "enclave-signature", "key_id": "k"}}, "proof_unsupported"),
    ]:
        assert code_of(lambda: box.mint_grant(**{**ok, **patch})) == code, patch
    assert box.mint_grant(**ok)["state"] == "live"


def test_send_consumes_only_on_success(box, clock):
    g = mint(box, uses=3)
    use = M.GrantUse(ALICE)
    first = send(box, id="msg_a", grant=use)
    assert box.get_grant(g["grant_id"])["uses_remaining"] == 2
    assert send(box, id="msg_a", grant=use)["replayed"] is True       # replay: covered but free
    assert box.get_grant(g["grant_id"])["uses_remaining"] == 2
    assert code_of(lambda: send(box, id="msg_h", hop=2, grant=use)) == "hop_limit"   # refusal: free
    assert code_of(lambda: send(box, id="msg_c", body="other", to=f"{MACHINE}/claude/carol", grant=use)) == "grant_required"
    assert box.get_grant(g["grant_id"])["uses_remaining"] == 2
    assert first["message"]["state"] == "queued"
    # a replay still needs a covering grant
    box.consume_grant(session=ALICE, verb="send_message", target=BOB)
    box.consume_grant(session=ALICE, verb="send_message", target=BOB)
    assert code_of(lambda: send(box, id="msg_a", grant=use)) == "grant_required"


def _race(fn, count: int) -> list:
    barrier = threading.Barrier(count)
    results: list = [None] * count

    def run(i: int) -> None:
        barrier.wait()
        try:
            results[i] = ("ok", fn(i))
        except P.RelayError as exc:
            results[i] = ("err", exc.code)
        except BaseException as exc:  # a lost race must not be hidden as a refusal
            results[i] = ("crash", repr(exc))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(count)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    return results


def test_two_concurrent_threads_one_winner(box):
    g = mint(box, uses=1)
    results = _race(lambda i: box.consume_grant(session=ALICE, verb="send_message", target=BOB, grant_id=g["grant_id"]), 2)
    assert sorted(r[0] for r in results) == ["err", "ok"], results
    assert [r[1] for r in results if r[0] == "err"] == ["grant_used_up"]
    assert box.get_grant(g["grant_id"])["uses_remaining"] == 0


def test_many_concurrent_consumers_win_exactly_uses(tmp_path, clock):
    path = tmp_path / "m.sqlite3"
    seed = M.Mailbox(path, machine=MACHINE, clock=clock)
    g = mint(seed, uses=3)
    # each thread has its own Mailbox object, as separate relay workers would
    results = _race(
        lambda i: M.Mailbox(path, machine=MACHINE, clock=clock).consume_grant(
            session=ALICE, verb="send_message", target=BOB, grant_id=g["grant_id"]),
        10,
    )
    assert [r[0] for r in results].count("ok") == 3, results
    assert not [r for r in results if r[0] == "crash"], results
    assert seed.get_grant(g["grant_id"])["uses_remaining"] == 0


_CHILD = """
import sys
from llm_kit.relay import mailbox as M, protocol as P
box = M.Mailbox(sys.argv[1], machine="m5pro", clock=lambda: 1000000.0)
try:
    box.consume_grant(session=sys.argv[2], verb="send_message", target=sys.argv[3], grant_id=sys.argv[4])
    print("ok")
except P.RelayError as exc:
    print(exc.code)
"""


def test_two_concurrent_processes_one_winner(tmp_path):
    path = tmp_path / "m.sqlite3"
    seed = M.Mailbox(path, machine=MACHINE, clock=lambda: 1_000_000.0)
    g = mint(seed, uses=1)
    argv = [sys.executable, "-c", _CHILD, str(path), ALICE, BOB, g["grant_id"]]
    src = str(Path(M.__file__).resolve().parents[2])  # the directory holding llm_kit
    env = {**os.environ, "PYTHONPATH": src + os.pathsep + os.environ.get("PYTHONPATH", "")}
    procs = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for _ in range(2)]
    outs = [p.communicate(timeout=60) for p in procs]
    assert all(p.returncode == 0 for p in procs), outs
    assert sorted(o[0].strip() for o in outs) == ["grant_used_up", "ok"]


# --------------------------------------------------------------------------
# named conversations
# --------------------------------------------------------------------------

def test_conversation_append_and_load(box):
    assert box.load_conversation(LLM, "chat-1") == []
    assert box.append_turn(LLM, "chat-1", "user", "hello") == 1
    assert box.append_turns(LLM, "chat-1", [("assistant", "hi"), ("user", "again")]) == 3
    assert box.load_conversation(LLM, "chat-1") == [
        {"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi"}, {"role": "user", "content": "again"},
    ]
    assert [t["content"] for t in box.load_conversation(LLM, "chat-1", max_turns=2)] == ["hi", "again"]
    other = f"{MACHINE}/llm/other"
    assert box.load_conversation(other, "chat-1") == []             # keyed by endpoint too


def test_conversation_trims_and_validates(tmp_path, clock):
    box = M.Mailbox(tmp_path / "m.sqlite3", machine=MACHINE, clock=clock, limits=M.MailboxLimits(max_conversation_turns=4))
    for i in range(7):
        box.append_turn(LLM, "c", "user", f"t{i}")
    assert [t["content"] for t in box.load_conversation(LLM, "c")] == ["t3", "t4", "t5", "t6"]
    assert box.append_turn(LLM, "c", "user", "t7") == 8             # numbering never restarts
    assert code_of(lambda: box.append_turn(BOB, "c", "user", "x")) == "target_invalid"
    assert code_of(lambda: box.append_turn(LLM, "bad name", "user", "x")) == "bad_request"
    assert code_of(lambda: box.append_turn(LLM, "c", "tool", "x")) == "bad_request"
    assert code_of(lambda: box.append_turn(LLM, "c", "user", "x" * (M.MAX_TURN_CHARS + 1))) == "bad_request"


# --------------------------------------------------------------------------
# listing and session end
# --------------------------------------------------------------------------

def test_messages_to_and_end_session(box, clock):
    send(box, id="msg_a")
    clock.advance(1)
    send(box, id="msg_b", kind="question")
    box.poll(BOB, max_messages=1)
    assert [m["id"] for m in box.messages_to(BOB)] == ["msg_a", "msg_b"]
    assert [m["id"] for m in box.messages_to(BOB, limit=1)] == ["msg_b"]
    assert box.end_session(BOB) == 2
    states = {m["id"]: (m["state"], m["state_reason"]) for m in box.messages_to(BOB)}
    assert states == {"msg_a": ("session-ended", "session_ended"), "msg_b": ("session-ended", "session_ended")}
    assert box.end_session(BOB) == 0                                # terminal rows stay put
    assert box.poll(BOB) == []


def test_mailbox_db_path_is_under_the_state_root(tmp_path):
    from llm_kit.relay.config import StatePaths

    assert M.mailbox_db_path(StatePaths(tmp_path)) == tmp_path / "mailbox.sqlite3"


def test_empty_poll_takes_no_write_transaction(box: M.Mailbox, monkeypatch) -> None:
    writes = []
    real = box._write
    monkeypatch.setattr(box, "_write", lambda: writes.append(1) or real())
    assert box.poll(BOB) == []
    assert writes == []
    # a poll with something to deliver still writes
    send(box)
    writes.clear()
    assert len(box.poll(BOB)) == 1
    assert writes == [1]


# --------------------------------------------------------------------------
# expiry is judged on a clock read inside the write transaction
# --------------------------------------------------------------------------

class LockProbeClock(Clock):
    """A clock that records, on every reading, whether another connection could take the write lock."""

    def __init__(self, db_path: Path, start: float = 1_000_000.0) -> None:
        super().__init__(start)
        self.db_path = db_path
        self.watching = False
        self.held: list[bool] = []

    def __call__(self) -> float:
        if self.watching:
            probe = sqlite3.connect(self.db_path, timeout=0, isolation_level=None)
            try:
                try:
                    probe.execute("BEGIN IMMEDIATE")
                except sqlite3.OperationalError:
                    self.held.append(True)
                else:
                    self.held.append(False)
                    probe.execute("ROLLBACK")
            finally:
                probe.close()
        return self.now


def test_send_message_and_consume_grant_read_the_clock_after_taking_the_write_lock(tmp_path):
    # A writer waits for the lock for up to busy_timeout; a timestamp read before that wait can
    # judge a grant live that has expired by the time the transaction runs.
    clock = LockProbeClock(tmp_path / "state" / "mailbox.sqlite3")
    box = M.Mailbox(clock.db_path, machine=MACHINE, clock=clock)
    mint(box, uses=3)
    clock.watching = True
    send(box, id="msg_a", grant=M.GrantUse(ALICE, None))
    sent = list(clock.held)
    box.consume_grant(session=ALICE, verb="send_message", target=BOB)
    clock.watching = False
    assert sent and all(sent), "send_message judged its grant on a clock read outside the write transaction"
    consumed = clock.held[len(sent):]
    assert consumed and all(consumed), "consume_grant judged its grant on a clock read outside the write transaction"


# --------------------------------------------------------------------------
# a reserved grant use can be given back
# --------------------------------------------------------------------------

def test_refund_grant_returns_one_use_and_never_exceeds_the_minted_uses(box):
    g = mint(box, uses=2)
    gid = g["grant_id"]
    box.consume_grant(session=ALICE, verb="send_message", target=BOB, grant_id=gid)
    assert box.get_grant(gid)["uses_remaining"] == 1
    assert box.refund_grant(gid)["uses_remaining"] == 2
    assert code_of(lambda: box.refund_grant(gid)) == "invalid_transition"    # nothing to give back
    assert box.get_grant(gid)["uses_remaining"] == 2
    assert code_of(lambda: box.refund_grant("grt_missing")) == "grant_unknown"

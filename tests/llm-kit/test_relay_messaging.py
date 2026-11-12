"""Messaging semantics over a real relay (unit M9): get_replies long-poll, pause, restart, expiry, audit.

Served like tests/llm-kit/test_relay_server.py (real uvicorn on an ephemeral loopback port, tmp state, real HTTP
client); the fixtures are shared from that module. The channel server's side of the same flows, with the
real channel process and a scripted Claude Code, is plugins-kit's tests/llm-kit/test_relay_channel_e2e.py.
"""
from __future__ import annotations

import json
import threading
import time

import pytest

from llm_kit.relay import protocol as P

from test_relay_server import (  # noqa: F401 -- fixtures and helpers
    DIRECT, MACHINE, Relay, addr, backend, config, make_server, relay, roots, sid,
)


def _in_thread(fn):
    out = {}

    def run():
        started = time.monotonic()
        try:
            out["result"] = fn()
        except BaseException as exc:  # reported to the test thread
            out["error"] = exc
        out["took"] = time.monotonic() - started

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


def _delivered(relay, channel, registration):
    """A channel's view of its next message: poll, ack."""
    creds = relay.creds(registration, channel)
    polled = relay.channel(channel, "poll", dict(creds, wait_s=0))
    for message in polled["messages"]:
        relay.channel(channel, "ack", dict(creds, message_id=message["id"]))
    return polled["messages"]


# --------------------------------------------------------------------------
# get_replies: a capped long-poll
# --------------------------------------------------------------------------

def test_get_replies_by_message_id_waits_for_the_reply_and_returns_unchanged_at_the_cap(relay):
    channel = sid()
    registration = relay.register(channel)
    message = relay.send(addr(channel), kind="question", body="ready?", grant_uses=1)
    _delivered(relay, channel, registration)
    creds = relay.creds(registration, channel)

    relay.mint("get_replies", [message["id"]], uses=4)
    started = time.monotonic()
    unchanged = relay.direct("get_replies", {"message_id": message["id"], "wait_s": 1})["messages"]
    assert 0.8 < time.monotonic() - started < 4 and unchanged[0]["state"] == "acked"

    thread, out = _in_thread(lambda: relay.direct("get_replies", {"message_id": message["id"], "wait_s": 20})["messages"])
    time.sleep(0.4)
    relay.channel(channel, "reply", dict(creds, in_reply_to=message["id"], state="answered", body="ready"))
    thread.join(timeout=10)
    assert not thread.is_alive() and "error" not in out, out
    assert out["took"] < 5 and out["result"][0]["state"] == "answered" and out["result"][0]["reply"]["body"] == "ready"


def test_get_replies_by_address_wakes_on_a_new_message_and_the_wait_is_capped_at_25_seconds(relay):
    channel = sid()
    relay.register(channel)
    relay.mint("get_replies", [addr(channel)], uses=3)
    thread, out = _in_thread(lambda: relay.direct("get_replies", {"address": addr(channel), "wait_s": 20})["messages"])
    time.sleep(0.4)
    relay.send(addr(channel), kind="note", body="hello")
    thread.join(timeout=10)
    assert not thread.is_alive() and out["took"] < 5 and [m["body"] for m in out["result"]] == ["hello"]

    err = relay.refused("bad_request", relay.direct, "get_replies", {"address": addr(channel), "wait_s": P.MAX_REPLY_WAIT_S + 1})
    assert "wait_s" in json.dumps(err.details)


# --------------------------------------------------------------------------
# pause: a waiting poll, expiry, restart
# --------------------------------------------------------------------------

def test_a_waiting_poll_returns_when_the_relay_is_paused_and_nothing_is_delivered_after(relay):
    channel = sid()
    registration = relay.register(channel)
    creds = relay.creds(registration, channel)
    thread, out = _in_thread(lambda: relay.channel(channel, "poll", dict(creds, wait_s=20)))
    time.sleep(0.4)
    paused_at = time.monotonic()
    relay.operator("pause", {"scope": "relay"})
    thread.join(timeout=10)
    assert not thread.is_alive() and "error" not in out, out
    assert out["result"]["paused"] is True and out["result"]["messages"] == []
    assert time.monotonic() - paused_at < P.HEARTBEAT_INTERVAL_S


@pytest.fixture
def sweeping_relay(config, roots, backend, tmp_path):
    """A relay whose maintenance sweep runs every 0.2 s (the shared fixture's runs hourly)."""
    server = make_server(config, roots, backend, tmp_path, maintain_interval_s=0.2).start()
    try:
        yield Relay(server, roots)
    finally:
        server.stop()


def test_a_queued_message_lapses_during_a_pause_and_is_never_delivered(sweeping_relay):
    relay = sweeping_relay
    channel = sid()
    registration = relay.register(channel)
    keep = relay.send(addr(channel), kind="note", body="kept")
    relay.mint("send_message", [addr(channel)])
    stale = relay.direct("send_message", {"to": addr(channel), "kind": "note", "body": "short lived", "expires_in_s": 1})["message"]
    relay.operator("pause", {"scope": "relay"})
    # the server's maintenance sweep lapses it; a pause does not stop expiry
    deadline = time.monotonic() + 8
    while relay.mailbox.get_message(stale["id"])["state"] != "lapsed" and time.monotonic() < deadline:
        time.sleep(0.1)
    assert relay.mailbox.get_message(stale["id"])["state"] == "lapsed"
    assert relay.mailbox.get_message(keep["id"])["state"] == "queued"
    relay.operator("resume", {"scope": "relay"})
    assert [m["body"] for m in _delivered(relay, channel, registration)] == ["kept"]


def test_the_pause_flag_survives_a_relay_restart(config, roots, backend, tmp_path):
    first = make_server(config, roots, backend, tmp_path).start()
    try:
        Relay(first, roots).operator("pause", {"scope": "relay", "reason": "owner away"})
    finally:
        first.stop()
    second = make_server(config, roots, backend, tmp_path).start()
    try:
        again = Relay(second, roots)
        state = again.operator("status", {})["pause"]["relay"]
        assert state["paused"] is True and state["reason"] == "owner away"
        channel = sid()
        again.register(channel)
        again.mint("send_message", [addr(channel)])
        again.refused("paused", again.direct, "send_message", {"to": addr(channel), "kind": "note", "body": "x"})
    finally:
        second.stop()


# --------------------------------------------------------------------------
# audit identities
# --------------------------------------------------------------------------

def test_every_messaging_call_is_audited_under_its_callers_identity(relay):
    channel = sid()
    registration = relay.register(channel)
    creds = relay.creds(registration, channel)
    message = relay.send(addr(channel), kind="question", body="who are you?")
    _delivered(relay, channel, registration)
    relay.channel(channel, "reply", dict(creds, in_reply_to=message["id"], state="answered", body="a session"))
    relay.operator("pause", {"scope": "relay"})
    relay.mint("send_message", [addr(channel)])
    relay.refused("paused", relay.direct, "send_message", {"to": addr(channel), "kind": "note", "body": "x"})

    direct = f"direct:{addr(DIRECT)}"
    rows = relay.mailbox.audit_rows(limit=1000)
    by = lambda verb, outcome="ok": [r for r in rows if r["event"] == "call" and r["verb"] == verb and r["outcome"] == outcome]
    assert [r["origin"] for r in by("send_message")] == [direct]
    assert [r["origin"] for r in by("reply")] == [f"channel:{addr(channel)}"]
    assert [r["origin"] for r in by("pause")] == [f"operator:{MACHINE}"]
    refused = by("send_message", "refused")
    assert [(r["origin"], r["code"]) for r in refused] == [(direct, "paused")]
    moves = [(r["from_state"], r["to_state"], r["origin"]) for r in rows
             if r["event"] == "transition" and r["message_id"] == message["id"]]
    assert moves == [(None, "queued", direct), ("queued", "delivered", f"channel:{addr(channel)}"),
                     ("delivered", "acked", f"channel:{addr(channel)}"), ("acked", "answered", f"channel:{addr(channel)}")]

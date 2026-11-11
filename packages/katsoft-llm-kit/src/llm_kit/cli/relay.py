"""``llm-kit relay``: serve this machine's relay or hub, pause or resume it, show its status, call it as a client.

Verbs:

``serve``
    Run the relay in the foreground on ``RelayConfig.bind("relay")``
    (``127.0.0.1:4710`` unless ``relay.yaml`` says otherwise; loopback only).
    Prints one JSON object once the socket listens, then serves until SIGINT
    or SIGTERM. A second ``serve`` on the same state directory exits 1 with
    ``already_running``.
``hub [--host H] [--port N]``
    Run the hub (the MCP server the claude.ai connector talks to) in the
    foreground on ``RelayConfig.bind("hub")`` (``127.0.0.1:4711`` unless
    ``relay.yaml`` says otherwise; loopback only), in front of this machine's
    relay. Prints one JSON object (``serving`` is the MCP endpoint URL) once
    the socket listens, then serves until SIGINT or SIGTERM. A second ``hub``
    on the same state directory exits 1 with ``already_running``. The hub
    creates its bearer token if missing; it does not need the relay to be up
    (an unreachable relay is reported per call).
``pause [--scope relay|hub] [--reason TEXT]`` / ``resume [--scope relay|hub]``
    Set or clear a pause flag through the running relay, as
    ``operator:<machine>`` (audited). ``--offline`` writes the pause file in the
    state directory instead, for when no relay is running (no audit row).
``status``
    The relay's ``status`` result, as ``operator:<machine>``; ``--offline``
    prints the persisted pause state only.
``client <verb> --session <id> [--grant <grant id>] ...``
    One ``/direct`` client verb: ``machines``, ``sessions``, ``read``, ``ask``,
    ``send``, ``replies``, ``questions``, ``answer``. The origin is
    ``direct:<machine>/claude/<id>``; ``--grant`` is sent as ``X-Relay-Grant``.
    Every client verb needs a covering grant at the relay. There is no mint
    verb here: under alternative A grants are minted by the ``/direct`` hooks,
    and the CLI offers no convenience path to mint one. ``sessions`` shows the
    live sessions (``live-accepting`` and ``live-read-only``) by default and
    counts the rest under ``omitted``; ``--state all`` lists every session and
    ``--state <state>`` exactly one state.

Every verb takes ``--env-json`` (the fleet machine registry), ``--config``
(``relay.yaml``) and ``--state-dir``. Output is one JSON object on stdout.
Errors are one JSON object on stderr: ``{"error": <relay error object>}`` for a
relay refusal or failure, ``{"error": {"kind": "configuration", ...}}`` for a
configuration error.

Exit codes: 0 success; 1 the relay refused or failed the call, did not answer,
or is already running; 2 usage or configuration.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from llm_kit.relay import protocol as P
from llm_kit.relay.config import RelayConfig, RelayConfigError, load_config

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2

#: Session states ``client sessions`` shows by default (running sessions).
LIVE_SESSION_STATES = ("live-accepting", "live-read-only")
#: ``client sessions --state`` values: ``live`` (the default), ``all``, or one session state.
SESSIONS_STATE_CHOICES = ("live", "all", *P.SESSION_STATES)

#: client verb -> one-line help (the /direct client verbs, protocol section 8).
CLIENT_VERBS = {
    "machines": "List machines with relay status (list_machines).",
    "sessions": "List sessions with address, cwd, title, task and state (list_sessions).",
    "read": "Read a capped, redacted tail of one session (read_session).",
    "ask": "Ask a local LLM endpoint (ask_llm).",
    "send": "Send a message to an opted-in session (send_message).",
    "replies": "Messages and their replies (get_replies).",
    "questions": "Pending operator questions (list_questions).",
    "answer": "Answer or reject one question (answer_question).",
}


def _emit(value: Any, *, stream: Any = None) -> None:
    print(json.dumps(value, sort_keys=True), file=stream or sys.stdout, flush=True)


def _relay_error(exc: P.RelayError) -> int:
    _emit({"error": exc.to_error_object()}, stream=sys.stderr)
    return EXIT_REFUSED


def _config_error(message: str) -> int:
    _emit({"error": {"kind": "configuration", "message": message}}, stream=sys.stderr)
    return EXIT_USAGE


def _json_arg(text: str, what: str) -> Any:
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{what} is not valid JSON: {exc}") from exc


# --------------------------------------------------------------------------
# argument parsing
# --------------------------------------------------------------------------

def _common() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--env-json", type=Path, help="Fleet machine registry env.json (default: the one in the Claude Code home directory).")
    parent.add_argument("--config", type=Path, help="relay.yaml (default $XDG_CONFIG_HOME/llm-kit/relay.yaml).")
    parent.add_argument("--state-dir", type=Path, help="Relay state directory (default $XDG_STATE_HOME/llm-kit/relay).")
    return parent


def _client_common() -> argparse.ArgumentParser:
    parent = argparse.ArgumentParser(add_help=False, parents=[_common()])
    parent.add_argument("--session", required=True, help="The /direct session id this call is for.")
    parent.add_argument("--grant", help="Grant id to send as X-Relay-Grant (default: the relay picks a covering grant).")
    return parent


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="llm-kit relay",
        description="Serve this machine's relay or hub, pause or resume it, show its status, or call it as a /direct client.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True, metavar="verb")
    common = _common()
    serve = sub.add_parser("serve", parents=[common], help="Run the relay in the foreground (loopback only).")
    serve.add_argument("--host", help="Loopback host to bind instead of relay.yaml's (a literal loopback address).")
    serve.add_argument("--port", type=int, help="Port to bind instead of relay.yaml's.")
    hub = sub.add_parser("hub", parents=[common], help="Run the hub MCP server in the foreground (loopback only).")
    hub.add_argument("--host", help="Loopback host to bind instead of relay.yaml's (a literal loopback address).")
    hub.add_argument("--port", type=int, help="Port to bind instead of relay.yaml's.")
    for name, text in (("pause", "Set a pause flag."), ("resume", "Clear a pause flag.")):
        verb = sub.add_parser(name, parents=[common], help=text)
        verb.add_argument("--scope", choices=P.PAUSE_SCOPES, default="relay", help="relay (default) or hub.")
        if name == "pause":
            verb.add_argument("--reason", help="Why; kept with the flag.")
        verb.add_argument("--offline", action="store_true", help="Write the pause file directly; no relay needed, no audit row.")
    status = sub.add_parser("status", parents=[common], help="The relay's status.")
    status.add_argument("--offline", action="store_true", help="Print the persisted pause state; no relay needed.")

    client = sub.add_parser("client", help="Call one /direct client verb.")
    verbs = client.add_subparsers(dest="client_verb", required=True, metavar="client-verb")
    cc = _client_common()
    verbs.add_parser("machines", parents=[cc], help=CLIENT_VERBS["machines"])
    sessions = verbs.add_parser("sessions", parents=[cc], help=CLIENT_VERBS["sessions"])
    sessions.add_argument("--machine")
    sessions.add_argument("--harness", choices=P.SESSION_HARNESSES)
    sessions.add_argument(
        "--state", choices=SESSIONS_STATE_CHOICES, default="live",
        help="live (default: running sessions only), all, or one session state.",
    )
    read = verbs.add_parser("read", parents=[cc], help=CLIENT_VERBS["read"])
    read.add_argument("address", help="A session address or selector.")
    read.add_argument("--since", help="Cursor from a previous read.")
    read.add_argument("--limit", type=int)
    ask = verbs.add_parser("ask", parents=[cc], help=CLIENT_VERBS["ask"])
    ask.add_argument("endpoint", help="An LLM address <machine>/llm/<entry>, or a bare entry name on this machine.")
    form = ask.add_mutually_exclusive_group(required=True)
    form.add_argument("--prompt")
    form.add_argument("--messages", help="A JSON list of {role, content} messages.")
    ask.add_argument("--conversation")
    ask.add_argument("--max-tokens", type=int)
    ask.add_argument("--temperature", type=float)
    send = verbs.add_parser("send", parents=[cc], help=CLIENT_VERBS["send"])
    send.add_argument("to", help="A session address or selector.")
    send.add_argument("--kind", choices=P.MESSAGE_KINDS, required=True)
    send.add_argument("--body", required=True, help="Message text; '-' reads standard input.")
    send.add_argument("--id", help="msg_ id (the idempotency key).")
    send.add_argument("--expires-in", type=int)
    send.add_argument("--task")
    send.add_argument("--hop", type=int)
    replies = verbs.add_parser("replies", parents=[cc], help=CLIENT_VERBS["replies"])
    which = replies.add_mutually_exclusive_group(required=True)
    which.add_argument("--message-id")
    which.add_argument("--address")
    replies.add_argument("--wait", type=int, help="Long-poll seconds (at most 25).")
    replies.add_argument("--limit", type=int)
    questions = verbs.add_parser("questions", parents=[cc], help=CLIENT_VERBS["questions"])
    questions.add_argument("--machine")
    questions.add_argument("--state", choices=("open", "all"))
    answer = verbs.add_parser("answer", parents=[cc], help=CLIENT_VERBS["answer"])
    answer.add_argument("id", help="Question id q:<machine>/<local id>.")
    answer.add_argument("--decision", choices=("answer", "reject"), required=True)
    answer.add_argument("--input", help="The answer input as JSON (with --decision answer).")
    answer.add_argument("--reason")
    return parser


# --------------------------------------------------------------------------
# verbs
# --------------------------------------------------------------------------

def _config(args: argparse.Namespace, **overrides: Any) -> RelayConfig:
    return load_config(env_json=args.env_json, config_path=args.config, state_root=args.state_dir, **overrides)


def _local_client(config: RelayConfig):
    from llm_kit.relay.client import HttpRelayClient

    if not config.paths.relay_token.exists():
        raise RelayConfigError(
            f"relay token {config.paths.relay_token} does not exist: no relay has run on this state directory "
            "(start one with `llm-kit relay serve`)"
        )
    return HttpRelayClient.local(config)


def _cmd_serve(args: argparse.Namespace) -> int:
    from llm_kit.relay.server import RelayServer

    overrides = {key: value for key, value in (("relay_host", args.host), ("relay_port", args.port)) if value is not None}
    config = _config(args, **overrides)

    def ready(server: RelayServer) -> None:
        _emit({
            "serving": server.url, "machine": config.machine_label, "protocol": P.PROTOCOL,
            "pid": os.getpid(), "state_dir": str(config.paths.root),
        })

    RelayServer(config).serve_forever(on_ready=ready)
    return EXIT_OK


def _cmd_hub(args: argparse.Namespace) -> int:
    from llm_kit.relay.hub import HubServer

    overrides = {key: value for key, value in (("hub_host", args.host), ("hub_port", args.port)) if value is not None}
    config = _config(args, **overrides)

    def ready(server: HubServer) -> None:
        _emit({"serving": server.url, "machine": config.machine_label, "protocol": P.PROTOCOL})

    HubServer(config).serve_forever(on_ready=ready)
    return EXIT_OK


def _cmd_control(args: argparse.Namespace) -> int:
    config = _config(args)
    if args.offline:
        from llm_kit.relay.lifecycle import PauseStore, ensure_state_dir

        if args.cmd != "status":
            ensure_state_dir(config.paths)
        store = PauseStore(config.paths)
        if args.cmd == "pause":
            _emit({"pause": store.pause(args.scope, args.reason), "via": "state"})
        elif args.cmd == "resume":
            _emit({"pause": store.resume(args.scope), "via": "state"})
        else:
            _emit({"machine": config.machine_label, "pause": store.state(), "via": "state"})
        return EXIT_OK
    origin = f"operator:{config.machine_label}"
    if args.cmd == "pause":
        params: dict[str, Any] = {"scope": args.scope}
        if args.reason is not None:
            params["reason"] = args.reason
    elif args.cmd == "resume":
        params = {"scope": args.scope}
    else:
        params = {}
    with _local_client(config) as client:
        _emit(client.call(args.cmd, params, origin=origin))
    return EXIT_OK


def _client_params(args: argparse.Namespace, machine: str) -> tuple[str, dict[str, Any]]:
    verb = args.client_verb
    params: dict[str, Any] = {}

    def put(key: str, value: Any) -> None:
        if value is not None:
            params[key] = value

    if verb == "sessions":
        put("machine", args.machine)
        put("harness", args.harness)
        if args.state in P.SESSION_STATES:
            params["state"] = args.state
    elif verb == "read":
        params["address"] = args.address
        put("since", args.since)
        put("limit", args.limit)
    elif verb == "ask":
        params["endpoint"] = args.endpoint if "/" in args.endpoint else f"{machine}/llm/{args.endpoint}"
        if args.messages is not None:
            params["messages"] = _json_arg(args.messages, "--messages")
        else:
            params["prompt"] = args.prompt
        put("conversation", args.conversation)
        put("max_tokens", args.max_tokens)
        put("temperature", args.temperature)
    elif verb == "send":
        params.update({"to": args.to, "kind": args.kind, "body": sys.stdin.read() if args.body == "-" else args.body})
        put("id", args.id)
        put("expires_in_s", args.expires_in)
        put("task", args.task)
        put("hop", args.hop)
    elif verb == "replies":
        put("message_id", args.message_id)
        put("address", args.address)
        put("wait_s", args.wait)
        put("limit", args.limit)
    elif verb == "questions":
        put("machine", args.machine)
        put("state", args.state)
    elif verb == "answer":
        params.update({"id": args.id, "decision": args.decision})
        if args.input is not None:
            params["input"] = _json_arg(args.input, "--input")
        put("reason", args.reason)
    return P.CLIENT_VERBS[verb], params


def _cmd_client(args: argparse.Namespace) -> int:
    config = _config(args)
    machine = config.machine_label
    if re.fullmatch(P.SESSION_ID, args.session) is None or args.session == "latest":
        return _config_error(f"--session {args.session!r} is not a session id")
    origin = f"direct:{machine}/claude/{args.session}"
    wire, params = _client_params(args, machine)
    with _local_client(config) as client:
        result = client.call(wire, params, origin=origin, grant=args.grant)
    if args.client_verb == "sessions" and args.state == "live":
        result = live_sessions_only(result)
    _emit(result)
    return EXIT_OK


def live_sessions_only(result: dict[str, Any]) -> dict[str, Any]:
    """A ``list_sessions`` result narrowed to running sessions, with the others counted per state.

    An unfiltered listing is mostly ended sessions (about 158 KB on the owner's
    machine), so ``client sessions`` shows the live ones unless ``--state`` asks
    for more. ``omitted`` counts what was left out, by state, so a caller knows
    to ask with ``--state all``.
    """
    kept: list[Any] = []
    omitted = {state: 0 for state in P.SESSION_STATES if state not in LIVE_SESSION_STATES}
    for session in result["sessions"]:
        if session.get("state") in LIVE_SESSION_STATES:
            kept.append(session)
        else:
            omitted[session["state"]] = omitted.get(session["state"], 0) + 1
    return {**result, "sessions": kept, "omitted": omitted}


_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "serve": _cmd_serve,
    "hub": _cmd_hub,
    "pause": _cmd_control,
    "resume": _cmd_control,
    "status": _cmd_control,
    "client": _cmd_client,
}


def run(argv: Optional[Sequence[str]] = None) -> int:
    """Run one ``relay`` verb and return its exit code."""
    args = _parser().parse_args(list(sys.argv[1:] if argv is None else argv))
    try:
        return _COMMANDS[args.cmd](args)
    except P.RelayError as exc:
        return _relay_error(exc)
    except (RelayConfigError, ValueError, OSError) as exc:
        return _config_error(str(exc))


__all__ = [
    "CLIENT_VERBS", "EXIT_OK", "EXIT_REFUSED", "EXIT_USAGE", "LIVE_SESSION_STATES", "SESSIONS_STATE_CHOICES",
    "live_sessions_only", "run",
]

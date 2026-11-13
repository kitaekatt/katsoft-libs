"""``llm-kit relay``: serve this machine's relay or hub, pause or resume it, show its status, call it as a client.

Verbs:

``serve``
    Run the relay in the foreground on ``RelayConfig.bind("relay")``
    (``127.0.0.1:4710`` unless ``relay.yaml`` says otherwise; loopback only).
    Prints one JSON object once the socket listens, then serves until SIGINT
    or SIGTERM. A second ``serve`` on the same state directory exits 1 with
    ``already_running``.
``hub [--host H] [--port N] [--public-url https://<host>]``
    Run the hub (the MCP server the claude.ai connector talks to) in the
    foreground on ``RelayConfig.bind("hub")`` (``127.0.0.1:4711`` unless
    ``relay.yaml`` says otherwise; loopback only), in front of this machine's
    relay and of the peers ``relay: {peers: [...]}`` names (each over https with
    its own credentials from ``peers/<machine>/``; a missing credential stops
    the hub at start and names ``peer import``). Prints one JSON object (``serving`` is the loopback MCP endpoint
    URL, ``public_url`` the connector URL or null) once the socket listens,
    then serves until SIGINT or SIGTERM. A second ``hub`` on the same state
    directory exits 1 with ``already_running``. The hub creates its bearer
    token if missing; it does not need the relay to be up (an unreachable
    relay is reported per call). ``--public-url`` (or relay.yaml
    ``hub.public_url``) names the https origin a tunnel serves the hub at and
    turns on the OAuth sign-in; the bind stays loopback. The approval code for
    each claude.ai Connect is printed on this command's stderr.
``pause [--scope relay|hub] [--reason TEXT]`` / ``resume [--scope relay|hub]``
    Set or clear a pause flag through the running relay, as
    ``operator:<machine>`` (audited). ``--offline`` writes the pause file in the
    state directory instead, for when no relay is running (no audit row).
``rotate-credentials``
    Replace this machine's relay token and hub bearer with new ones, each
    written atomically as a private file, and print the two files and the
    local consumers that must restart to use them (never a credential). Run
    it on a machine that ran ``peer export`` before katsoft-llm-kit 1.21.0:
    those exports handed out the general credentials, which stay valid until
    rotated. Never done implicitly.
``status``
    The relay's ``status`` result, as ``operator:<machine>``; ``--offline``
    prints the persisted pause state only. With ``relay.peers`` configured the
    output also carries ``peers``: each peer's machine, URL and whether its
    peer key is stored (never the key).
``peer export --for <machine>``
    Mint, or reuse, the peer key this relay issues to ``<machine>``
    (``accept/<machine>/peer.key``) and print it as one JSON object, for that
    machine to import (``ssh <this machine> llm-kit relay peer export --for
    <that machine> | llm-kit relay peer import <this machine>``). ``<machine>``
    must be listed in ``relay.accept_from``. The key is the only credential a
    peer ever holds: it authenticates the principal ``peer:<machine>``, which
    may claim only ``relay:<machine>`` and the hub origin (protocol section 3);
    this machine's relay token and hub bearer are never exported. Refuses a
    terminal on standard output, so a key is never printed to a screen or a
    scrollback. A relay reads its issued keys at start, so restart it after
    minting.
``peer import <machine>``
    Read that JSON object from standard input (never a terminal) and store the
    key as ``peers/<machine>/peer.key`` in the state directory, a private file
    in private directories; this machine's hub and relay then reach that peer
    with it (after a restart). The object's ``machine`` must equal
    ``<machine>``, which must be an enabled fleet machine other than this one.
    The ``llm-kit-relay-peer/1`` object a 1.19.0 or 1.20.0 export printed (a
    general relay token and hub bearer) is refused with the re-export
    command.
``codex --cd DIR [--resume THREAD_ID] [--sandbox read-only|workspace-write] [--network] [--allow-tmp] [--hooks] ...``
    Run a Codex channel in the foreground (unit CH): own one ``codex app-server``
    child over stdio, register its thread with this machine's relay as
    ``channel:<machine>/codex/<thread>``, run one turn per delivered message and
    reply with the turn's final answer. The owner starts it at the keyboard; the
    relay launches nothing. Exits 2, spawning nothing, unless relay.yaml holds
    ``enabled: true``, ``--cd`` is not a directory, or ``--resume`` names a thread
    that never ran a turn; exits 1 when the relay does not answer, is older than
    katsoft-llm-kit 1.19.0 (its ``status`` lists no ``codex`` in ``harnesses``),
    refuses the registration, or the thread is open in another Codex process.
    The first stdout line is the effective policy, hooks included; later lines
    mirror the app-server's events. Defaults: sandbox ``workspace-write`` rooted
    at ``--cd``, network off, ``/tmp`` and ``$TMPDIR`` not writable
    (``--allow-tmp`` lifts it), the user's Codex hooks off (``--hooks`` turns them
    on), the notify program off, a turn timeout of 1800 s (``--turn-timeout``).
    Stops on SIGINT or SIGTERM: deregister, then close the app-server.
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
or is already running (``codex``: also a busy thread or a failed app-server);
2 usage or configuration (``codex``: also the relay not enabled).
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
from llm_kit.relay.codex_channel import DEFAULT_TURN_TIMEOUT_S
from llm_kit.relay.config import RelayConfig, RelayConfigError, fleet_machine_labels, load_config

#: The ``format`` of the object ``peer export`` prints and ``peer import`` reads.
PEER_FORMAT = "llm-kit-relay-peer/2"
#: What 1.19.0 and 1.20.0 printed (the general relay token and hub bearer); refused on import.
LEGACY_PEER_FORMAT = "llm-kit-relay-peer/1"
_PEER_OBJECT_KEYS = frozenset({"format", "machine", "peer_key"})
_MAX_PEER_OBJECT_BYTES = 4096

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


def _positive_seconds(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not a number of seconds") from None
    if not value > 0 or value == float("inf"):
        raise argparse.ArgumentTypeError(f"{text!r} is not a positive number of seconds")
    return value


def _message_kinds(text: str) -> tuple:
    kinds = tuple(part.strip() for part in text.split(",") if part.strip())
    for kind in kinds:
        if kind not in P.MESSAGE_KINDS:
            raise argparse.ArgumentTypeError(f"{kind!r} is not a message kind (one of {', '.join(P.MESSAGE_KINDS)})")
    return tuple(dict.fromkeys(kinds))


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
    hub.add_argument("--public-url", help="The https origin a tunnel serves the hub at (turns on the OAuth sign-in; "
                                          "the bind stays loopback). Default: relay.yaml hub.public_url.")
    for name, text in (("pause", "Set a pause flag."), ("resume", "Clear a pause flag.")):
        verb = sub.add_parser(name, parents=[common], help=text)
        verb.add_argument("--scope", choices=P.PAUSE_SCOPES, default="relay", help="relay (default) or hub.")
        if name == "pause":
            verb.add_argument("--reason", help="Why; kept with the flag.")
        verb.add_argument("--offline", action="store_true", help="Write the pause file directly; no relay needed, no audit row.")
    sub.add_parser("rotate-credentials", parents=[common],
                   help="Replace this machine's relay token and hub bearer (after a pre-1.21 peer export).")
    status = sub.add_parser("status", parents=[common], help="The relay's status.")
    status.add_argument("--offline", action="store_true", help="Print the persisted pause state; no relay needed.")
    peer = sub.add_parser("peer", help="Issue a peer key to another machine, or store the one a peer issued.")
    peer_verbs = peer.add_subparsers(dest="peer_verb", required=True, metavar="peer-verb")
    peer_export = peer_verbs.add_parser("export", parents=[common],
                                        help="Print the peer key this relay issues to MACHINE as JSON (refuses a terminal).")
    peer_export.add_argument("--for", dest="for_machine", metavar="MACHINE", required=True,
                             help="The machine the key is for; it must be listed in relay.accept_from.")
    peer_import = peer_verbs.add_parser("import", parents=[common],
                                        help="Store the peer key a peer issued to this machine, read as JSON from standard input.")
    peer_import.add_argument("machine", help="The peer's machine label; must equal the JSON object's machine.")

    codex = sub.add_parser("codex", parents=[common],
                           help="Run a Codex channel: one codex app-server thread that answers relay messages.")
    codex.add_argument("--cd", required=True, type=Path, help="The directory the Codex thread works in (an existing directory).")
    codex.add_argument("--resume", metavar="THREAD_ID", help="Resume this Codex thread (it must have run a turn).")
    codex.add_argument("--sandbox", choices=("read-only", "workspace-write"), default="workspace-write",
                       help="workspace-write (default; writable only under --cd) or read-only.")
    codex.add_argument("--network", action="store_true", help="Allow network access in the sandbox (default: off).")
    codex.add_argument("--allow-tmp", action="store_true",
                       help="Let the sandbox write /tmp and $TMPDIR (default: excluded).")
    codex.add_argument("--hooks", action="store_true", help="Run the user's Codex hooks in this session (default: off).")
    codex.add_argument("--model", help="Codex model for this server.")
    codex.add_argument("--effort", help="Codex reasoning effort for this server.")
    codex.add_argument("--turn-timeout", type=_positive_seconds, default=DEFAULT_TURN_TIMEOUT_S, metavar="SECONDS",
                       help="Interrupt and decline a turn that runs longer than this (default: %(default)s).")
    codex.add_argument("--accepts", type=_message_kinds, default=tuple(P.MESSAGE_KINDS), metavar="KINDS",
                       help="Comma list of message kinds to accept (default: note,instruction,question; empty is read-only).")

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

    overrides = {key: value for key, value in (("hub_host", args.host), ("hub_port", args.port),
                                               ("hub_public_url", args.public_url)) if value is not None}
    config = _config(args, **overrides)

    def ready(server: HubServer) -> None:
        _emit({"serving": server.url, "machine": config.machine_label, "protocol": P.PROTOCOL,
               "public_url": server.public_mcp_url, "oauth": server.oauth is not None})
        if server.oauth is not None:
            server.oauth.log(f"sign-in is on for {server.public_mcp_url}; the approval code for each claude.ai "
                             "Connect is printed here")

    HubServer(config).serve_forever(on_ready=ready)
    return EXIT_OK


def _peer_status(config: RelayConfig) -> list[dict[str, Any]]:
    """Each configured peer with whether its credentials are stored; never a credential."""
    paths = config.paths
    return [{
        "machine": peer.machine, "url": peer.url,
        "credentials": {"peer_key": "stored" if paths.peer_key(peer.machine).exists() else "missing"},
    } for peer in config.peers]


def _cmd_control(args: argparse.Namespace) -> int:
    config = _config(args)
    peers = {"peers": _peer_status(config)} if args.cmd == "status" and config.peers else {}
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
            _emit({"machine": config.machine_label, "pause": store.state(), "via": "state", **peers})
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
        _emit({**client.call(args.cmd, params, origin=origin), **peers})
    return EXIT_OK


def _is_tty(stream: Any) -> bool:
    return bool(getattr(stream, "isatty", lambda: False)())


def _cmd_codex(args: argparse.Namespace) -> int:
    from llm_kit.relay.codex_channel import ChannelExit, CodexChannel, CodexChannelOptions

    config = _config(args)
    options = CodexChannelOptions(
        cd=os.path.realpath(os.path.expanduser(str(args.cd))), resume=args.resume, sandbox=args.sandbox,
        network=args.network, allow_tmp=args.allow_tmp, hooks=args.hooks, model=args.model, effort=args.effort,
        turn_timeout_s=args.turn_timeout, accepts=args.accepts,
    )
    try:
        return CodexChannel(config, options, hard_exit=True).run()
    except ChannelExit as exc:
        if exc.code == EXIT_USAGE:
            return _config_error(exc.message)
        _emit({"error": {"kind": "codex_channel", "message": exc.message}}, stream=sys.stderr)
        return exc.code


#: What reads the relay token or hub bearer once, at start, and so must restart after a rotation.
ROTATION_RESTARTS = (
    {"consumer": "llm-kit relay serve", "reads": ["relay.token", "hub.bearer"]},
    {"consumer": "llm-kit relay hub", "reads": ["relay.token", "hub.bearer"]},
    {"consumer": "llm-kit relay codex (each Codex channel)", "reads": ["relay.token"]},
)
#: What re-reads the relay token on every call, and so needs nothing.
ROTATION_UNCHANGED = ("the llm-kit plugin's Claude channel server", "the /direct hooks", "llm-kit relay client")


def _cmd_rotate(args: argparse.Namespace) -> int:
    from llm_kit.relay.lifecycle import rotate_credentials

    config = _config(args)
    files = rotate_credentials(config.paths)
    _emit({
        "rotated": [str(file) for file in files],
        "restart": [dict(item) for item in ROTATION_RESTARTS],
        "reconnect": "the claude.ai connector, only when the hub runs without hub.public_url (bearer sign-in): "
                     "give it the new hub bearer",
        "unchanged": list(ROTATION_UNCHANGED),
        "note": "a running relay and hub accept the old credentials until they restart",
    })
    return EXIT_OK


def _cmd_peer(args: argparse.Namespace) -> int:
    from llm_kit.relay.lifecycle import ensure_accept_peer_key, peer_reexport_command, write_peer_key

    config = _config(args)
    if args.peer_verb == "export":
        requester = args.for_machine
        if _is_tty(sys.stdout):
            return _config_error("refusing to print a peer key to a terminal; pipe the output "
                                 f"(for example `ssh {config.machine_label} llm-kit relay peer export --for <machine> | "
                                 f"llm-kit relay peer import {config.machine_label}`)")
        if not P.is_machine_label(requester):
            raise RelayConfigError(f"--for {requester!r} is not a machine label")
        if requester == config.machine_label:
            raise RelayConfigError(f"--for {requester!r} is this machine; a peer key is for another machine")
        if requester not in config.accept_from:
            raise RelayConfigError(
                f"--for {requester!r}: that machine is not in relay.accept_from in {config.config_path}; add it "
                "there first (this relay accepts peer calls only from the machines listed)"
            )
        _emit({"format": PEER_FORMAT, "machine": config.machine_label,
               "peer_key": ensure_accept_peer_key(config.paths, requester)})
        return EXIT_OK
    machine = args.machine
    if not P.is_machine_label(machine):
        raise RelayConfigError(f"{machine!r} is not a machine label")
    if machine == config.machine_label:
        raise RelayConfigError(f"{machine!r} is this machine; a peer is another machine")
    known = fleet_machine_labels(args.env_json)
    if machine not in known:
        raise RelayConfigError(f"{machine!r} is not a machine in the fleet registry (no env.json machine has that tailscale_hostname)")
    if known[machine]:
        raise RelayConfigError(f"{machine!r} is disabled in the fleet registry")
    if _is_tty(sys.stdin):
        raise RelayConfigError("peer import reads the exported JSON object from standard input; pipe it in, do not type it")
    raw = sys.stdin.read(_MAX_PEER_OBJECT_BYTES + 1)
    if len(raw) > _MAX_PEER_OBJECT_BYTES:
        raise RelayConfigError("peer import input is not a peer export object (too large)")
    try:
        data = json.loads(raw)
    except ValueError:
        raise RelayConfigError("peer import input is not a JSON object (the output of `llm-kit relay peer export`)") from None
    if isinstance(data, dict) and data.get("format") == LEGACY_PEER_FORMAT:
        raise RelayConfigError(
            f"peer import input is a {LEGACY_PEER_FORMAT} object (a general relay token and hub bearer, as "
            "katsoft-llm-kit 1.19.0 and 1.20.0 exported); a peer is no longer reached with those, and nothing was "
            f"stored. Re-export with katsoft-llm-kit 1.21.0 or later on both: "
            f"`{peer_reexport_command(machine, config.machine_label)}`"
        )
    if not isinstance(data, dict) or set(data) != _PEER_OBJECT_KEYS or data.get("format") != PEER_FORMAT:
        raise RelayConfigError(f"peer import input is not a {PEER_FORMAT} object")
    if data["machine"] != machine:
        raise RelayConfigError(
            f"the exported peer key is from machine {data['machine']!r}, not {machine!r}; nothing was stored"
        )
    write_peer_key(config.paths, machine, data["peer_key"])
    _emit({"imported": machine, "files": [str(config.paths.peer_key(machine))],
           "configured": any(peer.machine == machine for peer in config.peers)})
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
    "peer": _cmd_peer,
    "rotate-credentials": _cmd_rotate,
    "codex": _cmd_codex,
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

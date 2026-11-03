"""Relay configuration: machine identity, bind addresses, paths, ledgers, LLM endpoints.

This module is pure. It reads ``env.json`` and the optional ``relay.yaml`` and
returns frozen values; it creates no directory, file, token or lock (the
lifecycle module does that). The wire contract it serves is
``docs/reference/llm-kit-relay-protocol.md`` (``relay-protocol/1``); this
module owns what that document leaves to configuration: machine labels, ports,
state and config paths, and the relay's own loopback model registry.

Machine identity. The relay's machine label is a fleet fact, not a free
setting. The host name must match a key of ``env.json``'s ``machines`` mapping
(case-insensitively, whole name); the label is that machine's
``tailscale_hostname`` lowercased (``m5pro.local`` -> ``m5pro``). An unknown
host, a disabled machine, a machine without a ``tailscale_hostname`` or a
hostname that is not a protocol machine label is a :class:`RelayConfigError`;
there is no fallback label.

Bind addresses. The relay listens on ``127.0.0.1:4710`` and the hub on
``127.0.0.1:4711`` by default, both overridable. A host that is not a literal
loopback address is refused; ``localhost`` is refused too because a hosts file
can point it elsewhere.

Paths follow the XDG convention and read the environment on every call:

    state   $XDG_STATE_HOME/llm-kit/relay   (fallback ~/.local/state/llm-kit/relay)
    config  $XDG_CONFIG_HOME/llm-kit/relay.yaml

An ``XDG_*`` value is honoured only when absolute.

Loopback model registry. ``ask_llm`` may reach only endpoints on this
machine. The relay therefore owns a registry file of its own (served to the
registry reader through ``MODEL_ENDPOINTS_REGISTRY``), never the user's
fleet registry, whose default entry is a remote front door. Every entry must
be a keyless-or-keyed transport entry whose ``base_url`` host is a literal
loopback address; anything else, and any request that names no endpoint, is
refused with the protocol error ``endpoint_refused``. There is no default
entry and no fallback to the fleet registry.

Security posture. Under alternative A (owner ruling 2026-11-03) the relay
token, the hub bearer and every origin are claims a same-user process can make:
files in the state directory are a guardrail against accidental calls, not
protection against a same-user model that can read them. Nothing here is a
security boundary.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from llm_kit.relay import protocol as P

#: Directory name under the state and config homes (the registry's neutral name).
NEUTRAL_DIRNAME = "llm-kit"
RELAY_STATE_DIRNAME = "relay"
CONFIG_FILENAME = "relay.yaml"
REGISTRY_FILENAME = "model-endpoints.yaml"

DEFAULT_RELAY_HOST = "127.0.0.1"
DEFAULT_RELAY_PORT = 4710
DEFAULT_HUB_HOST = "127.0.0.1"
DEFAULT_HUB_PORT = 4711

#: Entries the relay's loopback registry carries unless relay.yaml replaces them.
#: Same weights as the fleet registry's m5pro entries, reached through the
#: local llama-swap server on 127.0.0.1:8000.
DEFAULT_LLM_ENDPOINTS: Mapping[str, Mapping[str, Any]] = {
    "qwen38-m5-32k-local": {
        "name": "Qwen3.8-27B Unleashed on this machine, 32K context",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "qwen3.8-27b-unleashed",
        "billing": {"mode": "unmetered", "owner": "self"},
        "context_window": 32768,
        "reasoning_effort": "medium",
        "effort_style": "chat_template_kwargs",
    },
    "qwen36-m5-local": {
        "name": "Qwen3.6-35B-A3B OptiQ 4-bit on this machine",
        "base_url": "http://127.0.0.1:8000/v1",
        "model": "mlx-community/Qwen3.6-35B-A3B-OptiQ-4bit",
        "billing": {"mode": "unmetered", "owner": "self"},
        "context_window": 262144,
        "effort_style": "chat_template_kwargs",
    },
}

#: Keys an endpoint entry in relay.yaml or in the registry file may carry.
_ENDPOINT_KEYS = frozenset(
    {"name", "base_url", "model", "billing", "context_window", "reasoning_effort", "effort_style"}
)


class RelayConfigError(Exception):
    """The relay cannot start from this configuration. Always loud, never recovered."""


# --------------------------------------------------------------------------
# machine identity
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class MachineIdentity:
    """Who this machine is in the fleet.

    ``key`` is the env.json ``machines`` key that matched (``m5pro.local``),
    ``label`` the protocol machine label (``m5pro``).
    """

    key: str
    label: str
    tailscale_hostname: str


def default_env_json_path() -> Path:
    """The fleet machine registry, ``~/.claude/env.json`` (read only)."""
    return Path.home() / ".claude" / "env.json"


def resolve_machine(
    env_json: Optional[Path] = None,
    *,
    hostname: Optional[Callable[[], str]] = None,
) -> MachineIdentity:
    """Resolve this machine from ``env.json``.

    ``env_json`` is injectable (default :func:`default_env_json_path`);
    ``hostname`` is a callable returning the host name (default
    ``socket.gethostname``).

    Raises:
        RelayConfigError: the file is missing or not a JSON object with a
            ``machines`` mapping, the host matches no key, the match is
            ``disabled``, or it carries no usable ``tailscale_hostname``.
    """
    path = Path(env_json) if env_json is not None else default_env_json_path()
    host = (hostname or socket.gethostname)()
    if not isinstance(host, str) or not host.strip():
        raise RelayConfigError("the host name is empty; cannot resolve the machine label")
    host = host.strip()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RelayConfigError(f"machine registry {path} does not exist") from None
    except (OSError, ValueError) as exc:
        raise RelayConfigError(f"cannot read machine registry {path}: {exc}") from exc
    machines = raw.get("machines") if isinstance(raw, dict) else None
    if not isinstance(machines, dict):
        raise RelayConfigError(f"machine registry {path} has no 'machines' mapping")

    matches = [key for key in machines if isinstance(key, str) and key.lower() == host.lower()]
    if not matches:
        raise RelayConfigError(
            f"host {host!r} is not a machine in {path}; add it to 'machines' before running a relay here"
        )
    if len(matches) > 1:
        raise RelayConfigError(f"host {host!r} matches several machines in {path} ({', '.join(sorted(matches))})")
    key = matches[0]
    entry = machines[key]
    if not isinstance(entry, dict):
        raise RelayConfigError(f"machine {key!r} in {path} is not a mapping")
    if entry.get("disabled") is True:
        raise RelayConfigError(f"machine {key!r} is disabled in {path}; a relay does not run on a disabled machine")
    tailscale = entry.get("tailscale_hostname")
    if not isinstance(tailscale, str) or not tailscale.strip():
        raise RelayConfigError(f"machine {key!r} in {path} has no tailscale_hostname to use as its label")
    label = tailscale.strip().lower()
    if not P.is_machine_label(label):
        raise RelayConfigError(
            f"machine {key!r} has tailscale_hostname {tailscale!r}, which is not a valid relay machine label"
        )
    return MachineIdentity(key=key, label=label, tailscale_hostname=tailscale.strip())


# --------------------------------------------------------------------------
# loopback checks
# --------------------------------------------------------------------------

def is_loopback_host(host: object) -> bool:
    """True only for a literal loopback IP address (``127.0.0.0/8`` or ``::1``)."""
    if not isinstance(host, str):
        return False
    try:
        return ipaddress.ip_address(host.strip().strip("[]")).is_loopback
    except ValueError:
        return False


def require_loopback_host(host: object, *, what: str) -> str:
    """``host`` when it is a literal loopback address, else :class:`RelayConfigError`."""
    if not is_loopback_host(host):
        raise RelayConfigError(
            f"{what} host {host!r} is not a loopback address; the relay binds loopback only"
        )
    return str(host).strip()


def _require_port(port: object, *, what: str) -> int:
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise RelayConfigError(f"{what} port {port!r} is not an integer from 1 to 65535")
    return port


def is_loopback_url(url: object) -> bool:
    """True for an http(s) URL whose host is a literal loopback address."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url.strip())
        return parts.scheme in ("http", "https") and is_loopback_host(parts.hostname)
    except ValueError:
        return False


# --------------------------------------------------------------------------
# paths
# --------------------------------------------------------------------------

def _absolute_env(environ: Mapping[str, str], name: str) -> Optional[Path]:
    value = (environ.get(name) or "").strip()
    return Path(value) if value and os.path.isabs(value) else None


def state_dir(environ: Optional[Mapping[str, str]] = None) -> Path:
    """``$XDG_STATE_HOME/llm-kit/relay`` (absolute only), else ``~/.local/state/llm-kit/relay``."""
    env = os.environ if environ is None else environ
    home = _absolute_env(env, "XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return home / NEUTRAL_DIRNAME / RELAY_STATE_DIRNAME


def config_file(environ: Optional[Mapping[str, str]] = None) -> Path:
    """``$XDG_CONFIG_HOME/llm-kit/relay.yaml`` (absolute only), else ``~/.config/llm-kit/relay.yaml``."""
    env = os.environ if environ is None else environ
    home = _absolute_env(env, "XDG_CONFIG_HOME") or Path.home() / ".config"
    return home / NEUTRAL_DIRNAME / CONFIG_FILENAME


@dataclass(frozen=True)
class StatePaths:
    """Every file the relay and hub keep under the state directory."""

    root: Path

    @property
    def relay_token(self) -> Path:
        return self.root / "relay.token"

    @property
    def hub_bearer(self) -> Path:
        return self.root / "hub.bearer"

    @property
    def pause_file(self) -> Path:
        return self.root / "pause.json"

    @property
    def registry_file(self) -> Path:
        return self.root / REGISTRY_FILENAME

    def pidfile(self, role: str) -> Path:
        _require_role(role)
        return self.root / f"{role}.pid"


ROLES = ("relay", "hub")


def _require_role(role: str) -> str:
    if role not in ROLES:
        raise RelayConfigError(f"unknown relay role {role!r}; expected one of {', '.join(ROLES)}")
    return role


# --------------------------------------------------------------------------
# loopback model registry
# --------------------------------------------------------------------------

def validate_llm_endpoints(endpoints: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Check endpoint declarations; return a plain copy.

    Each entry needs ``base_url`` (a loopback http(s) URL) and ``model``; the
    other keys are those the registry reader understands. A non-loopback or
    unknown-key entry raises :class:`llm_kit.relay.protocol.RelayError` with
    code ``endpoint_refused``. An empty mapping is refused: with no endpoint
    there is nothing for ``ask_llm`` to reach and no fallback to supply one.
    """
    if not isinstance(endpoints, Mapping) or not endpoints:
        raise P.RelayError("endpoint_refused", "the relay has no LLM endpoint entries; there is no default endpoint")
    checked: dict[str, dict[str, Any]] = {}
    for name, entry in endpoints.items():
        if not isinstance(name, str) or not name.strip():
            raise P.RelayError("endpoint_refused", f"endpoint name {name!r} is not a non-empty string")
        if not isinstance(entry, Mapping):
            raise P.RelayError("endpoint_refused", f"endpoint {name!r} is not a mapping")
        unknown = sorted(set(entry) - _ENDPOINT_KEYS)
        if unknown:
            raise P.RelayError("endpoint_refused", f"endpoint {name!r} has unsupported keys: {', '.join(map(str, unknown))}")
        base_url = entry.get("base_url")
        if not is_loopback_url(base_url):
            raise P.RelayError(
                "endpoint_refused",
                f"endpoint {name!r} base_url {base_url!r} is not a loopback http(s) URL; ask_llm reaches this machine only",
                details={"endpoint": name},
            )
        model = entry.get("model")
        if not isinstance(model, str) or not model.strip():
            raise P.RelayError("endpoint_refused", f"endpoint {name!r} has no model")
        checked[name] = {key: value for key, value in entry.items()}
    return checked


def render_registry_yaml(endpoints: Mapping[str, Mapping[str, Any]]) -> str:
    """The registry file text for ``endpoints`` (validated first; no ``default:`` key)."""
    import yaml

    checked = validate_llm_endpoints(endpoints)
    return yaml.safe_dump({"version": 1, "models": checked}, sort_keys=False)


def load_loopback_registry(path: Path):
    """Read the relay's registry file and refuse anything that is not loopback.

    Returns the :class:`llm_kit.registry.model_endpoints.EndpointRegistry`.
    The file is read through ``MODEL_ENDPOINTS_REGISTRY`` semantics (a missing
    file is an error), never through the convention lookup, so the fleet
    registry cannot be substituted. ``endpoint_refused`` for a harness entry,
    a front-door entry, a non-loopback ``base_url``, a ``default:`` key (a
    default is a fallback), or any entry the reader skipped.
    """
    from llm_kit.registry import model_endpoints as ME

    try:
        registry = ME.load_endpoint_registry({ME.REGISTRY_ENV: str(path)})
    except ME.EndpointRegistryError as exc:
        raise P.RelayError("endpoint_refused", f"relay registry unreadable: {exc}") from exc
    if registry.notes:
        raise P.RelayError("endpoint_refused", f"relay registry {path} has unusable entries: {'; '.join(registry.notes)}")
    if registry.default_id is not None:
        raise P.RelayError("endpoint_refused", f"relay registry {path} names a default entry; the relay has no default endpoint")
    if not registry.entries:
        raise P.RelayError("endpoint_refused", f"relay registry {path} has no entries")
    for name, entry in registry.entries.items():
        if entry.kind != ME.TRANSPORT_KIND or entry.frontdoor:
            raise P.RelayError("endpoint_refused", f"relay registry entry {name!r} is not a plain transport entry")
        if not is_loopback_url(entry.base_url):
            raise P.RelayError(
                "endpoint_refused",
                f"relay registry entry {name!r} base_url {entry.base_url!r} is not loopback",
                details={"endpoint": name},
            )
    return registry


def require_loopback_endpoint(registry, name: Optional[str]):
    """The named entry of ``registry`` (from :func:`load_loopback_registry`).

    ``name`` must be given: ``None`` or empty is ``endpoint_refused`` (never
    the registry default, never a fleet entry). An unknown name is
    ``endpoint_refused`` too, and a non-loopback entry is re-checked.
    """
    if not isinstance(name, str) or not name.strip():
        raise P.RelayError("endpoint_refused", "no explicit endpoint was named; the relay has no default endpoint")
    entry = registry.entries.get(name)
    if entry is None:
        raise P.RelayError("endpoint_refused", f"endpoint {name!r} is not in the relay's loopback registry", details={"endpoint": name})
    if not is_loopback_url(entry.base_url):
        raise P.RelayError("endpoint_refused", f"endpoint {name!r} is not loopback", details={"endpoint": name})
    return entry


# --------------------------------------------------------------------------
# the configuration
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class RelayConfig:
    """Everything a relay or hub process needs to know before it starts."""

    machine: MachineIdentity
    relay_host: str
    relay_port: int
    hub_host: str
    hub_port: int
    paths: StatePaths
    config_path: Path
    ledgers: tuple[Path, ...] = ()
    llm_endpoints: Mapping[str, Mapping[str, Any]] = field(default_factory=lambda: dict(DEFAULT_LLM_ENDPOINTS))

    def bind(self, role: str) -> tuple[str, int]:
        """The ``(host, port)`` ``role`` listens on."""
        return (self.relay_host, self.relay_port) if _require_role(role) == "relay" else (self.hub_host, self.hub_port)

    @property
    def machine_label(self) -> str:
        return self.machine.label


_CONFIG_KEYS = frozenset({"relay", "hub", "ledgers", "llm_endpoints"})
_LISTENER_KEYS = frozenset({"host", "port"})


def _read_config_file(path: Path) -> dict[str, Any]:
    """The parsed relay.yaml, or ``{}`` when the convention file is absent."""
    if not path.is_file():
        return {}
    import yaml

    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise RelayConfigError(f"cannot read relay config {path}: {exc}") from exc
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise RelayConfigError(f"relay config {path} is not a YAML mapping")
    unknown = sorted(set(data) - _CONFIG_KEYS)
    if unknown:
        raise RelayConfigError(f"relay config {path} has unknown keys: {', '.join(map(str, unknown))}")
    return data


def _listener(data: Mapping[str, Any], key: str, default_host: str, default_port: int, path: Path) -> tuple[str, int]:
    block = data.get(key, {})
    if block is None:
        block = {}
    if not isinstance(block, dict):
        raise RelayConfigError(f"relay config {path}: '{key}' is not a mapping")
    unknown = sorted(set(block) - _LISTENER_KEYS)
    if unknown:
        raise RelayConfigError(f"relay config {path}: '{key}' has unknown keys: {', '.join(map(str, unknown))}")
    return block.get("host", default_host), block.get("port", default_port)


def _ledgers(raw: object, path: Path) -> tuple[Path, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise RelayConfigError(f"relay config {path}: 'ledgers' is not a list")
    out: list[Path] = []
    for item in raw:
        if not isinstance(item, (str, os.PathLike)) or not str(item).strip():
            raise RelayConfigError(f"relay config {path}: ledger {item!r} is not a path")
        expanded = Path(os.path.expanduser(str(item).strip()))
        if not expanded.is_absolute():
            raise RelayConfigError(f"relay config {path}: ledger {item!r} is not absolute")
        if expanded not in out:
            out.append(expanded)
    return tuple(out)


def load_config(
    *,
    env_json: Optional[Path] = None,
    hostname: Optional[Callable[[], str]] = None,
    environ: Optional[Mapping[str, str]] = None,
    config_path: Optional[Path] = None,
    state_root: Optional[Path] = None,
    relay_host: Optional[str] = None,
    relay_port: Optional[int] = None,
    hub_host: Optional[str] = None,
    hub_port: Optional[int] = None,
    ledgers: Optional[Sequence[Path]] = None,
    llm_endpoints: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> RelayConfig:
    """Resolve the relay configuration. Precedence: argument, relay.yaml, default.

    A missing ``relay.yaml`` at the convention path is the normal unconfigured
    state; one that exists must parse and carry only known keys. Every bind
    host must be a literal loopback address and every endpoint entry loopback
    (:class:`RelayConfigError` for the former, ``endpoint_refused`` for the
    latter). Reads files; writes nothing.
    """
    env = os.environ if environ is None else environ
    machine = resolve_machine(env_json, hostname=hostname)
    cfg_path = Path(config_path) if config_path is not None else config_file(env)
    data = _read_config_file(cfg_path)

    file_relay_host, file_relay_port = _listener(data, "relay", DEFAULT_RELAY_HOST, DEFAULT_RELAY_PORT, cfg_path)
    file_hub_host, file_hub_port = _listener(data, "hub", DEFAULT_HUB_HOST, DEFAULT_HUB_PORT, cfg_path)
    r_host = require_loopback_host(relay_host if relay_host is not None else file_relay_host, what="relay")
    r_port = _require_port(relay_port if relay_port is not None else file_relay_port, what="relay")
    h_host = require_loopback_host(hub_host if hub_host is not None else file_hub_host, what="hub")
    h_port = _require_port(hub_port if hub_port is not None else file_hub_port, what="hub")
    if r_port == h_port and r_host == h_host:
        raise RelayConfigError(f"relay and hub would both bind {r_host}:{r_port}")

    if ledgers is not None:
        watched = _ledgers([str(p) for p in ledgers], cfg_path)
    else:
        watched = _ledgers(data.get("ledgers"), cfg_path)

    if llm_endpoints is not None:
        endpoints: Mapping[str, Mapping[str, Any]] = llm_endpoints
    elif data.get("llm_endpoints") is not None:
        endpoints = data["llm_endpoints"]
    else:
        endpoints = DEFAULT_LLM_ENDPOINTS
    endpoints = validate_llm_endpoints(endpoints)

    root = Path(state_root) if state_root is not None else state_dir(env)
    return RelayConfig(
        machine=machine,
        relay_host=r_host,
        relay_port=r_port,
        hub_host=h_host,
        hub_port=h_port,
        paths=StatePaths(root),
        config_path=cfg_path,
        ledgers=watched,
        llm_endpoints=endpoints,
    )


__all__ = [
    "CONFIG_FILENAME",
    "DEFAULT_HUB_HOST",
    "DEFAULT_HUB_PORT",
    "DEFAULT_LLM_ENDPOINTS",
    "DEFAULT_RELAY_HOST",
    "DEFAULT_RELAY_PORT",
    "MachineIdentity",
    "REGISTRY_FILENAME",
    "ROLES",
    "RelayConfig",
    "RelayConfigError",
    "StatePaths",
    "config_file",
    "default_env_json_path",
    "is_loopback_host",
    "is_loopback_url",
    "load_config",
    "load_loopback_registry",
    "render_registry_yaml",
    "require_loopback_endpoint",
    "require_loopback_host",
    "resolve_machine",
    "state_dir",
    "validate_llm_endpoints",
]

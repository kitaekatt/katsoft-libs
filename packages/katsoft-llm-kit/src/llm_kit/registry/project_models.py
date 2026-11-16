"""The project model file: one tracked catalogue plus named selections.

A project keeps one YAML file (default ``.llm-kit/models.yaml``, tracked in the
project's own repository):

    version: 1
    endpoints:                   # optional; how this project places an effort on a transport
      openrouter: {effort_style: reasoning-object}   # a row's endpoint; wins over the registry
    catalog:                     # ordered; the order is the project's order
      - id: luna                 # opaque, unique, chosen by the project
        label: Luna              # presentation text
        endpoint: luna           # a registry endpoint (credentials, adapter)
        model: gpt-6-luna        # the exact wire model for that endpoint
        efforts: [low, high]     # optional; a menu-ordered subset of the library menu
                                 # (efforts: [] declares NO effort for the row)
        max_output_tokens: 65536 # optional; positive integer, the output cap this model's
                                 # calls carry when the call names none (see below)
      - id: flash
        label: Flash
        endpoint: openrouter
        model: vendor/flash-1
        traits: [long_drafts]    # optional; the consumer's labels, never sent
        request_defaults:        # optional; typed defaults and wire extras (a JSON mapping)
          temperature: 0.7
          extras: {provider: {sort: latency}}
    requests:                    # optional; named request profiles a selection points at
      classify: {temperature: 0, max_output_tokens: 60}
    selections:                  # opaque consumer keys -> {model, effort, request}
      draft.generate.primary: {model: luna, effort: high}
      draft.label: {model: flash, effort: none, request: classify}

The request settings (``temperature``, ``top_p``, ``max_output_tokens``,
``reasoning_budget``, ``extras``) layer row < selection < call; the resolved
request and its rules are in :mod:`llm_kit.registry.request_settings`.

The file holds no keys, key files, base URLs, consent or vendor capability
claims: endpoints, credentials and billing resolve through the endpoint
registry, and the effort menu is the library's, measured per exact model
(``model_profiles``). A project may narrow a menu and never widen it; a harness
model the library has not measured has no menu, so its row declares no
``efforts`` and its selections carry a null effort. ``efforts: []`` declares that a
row has no effort dial at all (any model; the shipped profile of a model the vendor
lists without one has the same empty menu): every selection of it carries a null
effort and a request for it refuses any effort. ``null`` effort means "omit"; the string ``none`` is an explicit
disable value accepted only where the library menu lists it. Nothing here
translates ``off`` or any other word.

``max_output_tokens`` is the project's configured output cap for that model. A
protocol 2 call to a transport (OpenAI-compatible) row uses it when the request
names no ``max_tokens``; an explicit request ``max_tokens`` always wins, and the
row's ``request_defaults.extras.max_tokens`` is the next fallback. A row that
declares neither sends no cap to openrouter.ai, so the provider's own limit for the
model applies. The claude and codex harnesses expose no output-cap control: a harness
row may declare the field, and it is not applied (the catalogue says so in its notes).
Nothing clamps the value to a model limit; the library ships no per-model limit table.

This module is harness-independent: standard library plus PyYAML (the ``config``
extra), no ``bootstrap_lib``, no plugin path, no home-directory convention of
its own. Parsing, strict validation, canonical rendering, hashing and the
atomic save all live here; the CLI (``llm-kit models project``) is a thin shell.

Canonical form (what ``write`` stores and ``render_yaml`` returns): UTF-8, LF,
two-space indentation, one final newline, catalogue order preserved, top-level
keys in the order version/endpoints/catalog/requests/selections (endpoints and
requests only when the file has them), row fields in the fixed order
id/label/endpoint/model/efforts/max_output_tokens/traits/request_defaults, typed
settings in the order temperature/top_p/max_output_tokens/reasoning_budget/extras,
every free-form mapping (``endpoints``, ``requests``, ``selections``, ``extras``)
sorted by key. Comments are not
round-tripped. The digest of a file is the SHA-256 of its bytes as stored.

A second, personal layer sits beside the file: ``<file-stem>.local.yaml``
(default ``.llm-kit/models.local.yaml``), ``{version: 1, selections: {...}}``,
meant to be untracked. Its entries override the tracked selections per key; see
"The personal layer" below. The library never edits ``.gitignore``.

``write`` is optimistic: ``if_match`` compares the digest and the replacement
follows, with no lock between the two. A concurrent writer in that window wins
silently; callers that need more serialize their own writes.

Portable validation and binding. Validation has two parts. The PORTABLE part
checks what any reader in any language can check from the file alone: the
structure, the JSON values, and the references (a selection names a catalogue
id; an effort is in the row's declared ``efforts``). The BINDING part checks
what only this machine knows: for each row whose ``endpoint`` resolves in the
endpoint registry, that each effort is in the library menu for that endpoint and
model, and that ``extras`` holds no reserved key. A row whose endpoint resolves
to no registry endpoint is UNBOUND: the endpoint is the consumer's name, so the
row loads with every check that needs no endpoint (its declared ``efforts``, the
endpoint-independent reserved extras), and the result names it
(:attr:`ProjectModels.unbound`, :attr:`Validation.unbound`). A call or a plan that
resolves to an unbound row refuses at that point, naming the row and the
endpoint. :func:`validate_portable_document`
and :func:`validate_portable_local_document` run the portable part only;
:func:`validate_document` and every read and write run both. The shared format
rules, the JSON Schemas (``llm_kit.schema.formats``) and the cases every reader runs
are in ``docs/reference/llm-kit-formats.md``.

The YAML reader (:func:`parse_yaml_document`) reads YAML 1.1 as PyYAML's safe
loader resolves it, and refuses duplicate keys, merge keys (``<<``) and the
``!!omap`` and ``!!pairs`` types. Anchors and aliases are allowed; an alias
cycle is refused. Every value must be JSON data, and an integer must be in the
range -(2**53 - 1) to 2**53 - 1, which every JSON reader holds exactly.
"""
from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import re
import stat
import uuid
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Mapping, Optional

from .effort import EFFORT_KEY, EFFORT_STYLES, REASONING_KEY, REASONING_OBJECT, TEMPLATE_KEY, style_effort_menu
from .model_profiles import (
    MENU_ENDPOINT_STYLE,
    MENU_EXACT_MODEL,
    MENU_NONE,
    exact_effort_menu,
    route_of_base_url,
)

__all__ = [
    "SCHEMA", "FILE_VERSION", "DEFAULT_FILE",
    "CODE_INVALID", "CODE_STALE", "CODE_PATH", "CODE_SYMLINK", "CODE_MISSING", "CODE_DEPENDENCY",
    "CODE_UNKNOWN_SELECTION", "REQUEST_SETTINGS", "PROFILE_RESERVED_EXTRAS", "LEGACY_ROW_EXTRAS",
    "endpoint_styles", "check_setting", "validate_profile", "KEEP_REQUEST",
    "LOCAL_SUFFIX", "LAYER_TRACKED", "LAYER_LOCAL", "SOURCE_DEFAULT", "SOURCE_OVERRIDE",
    "ProjectModelsError", "EndpointInfo", "EffortMenuResolver",
    "library_effort_menu_with_source", "library_effort_menu",
    "parse_json_document", "parse_yaml_document",
    "validate_document", "validate_portable_document",
    "render_yaml", "digest",
    "ProjectFile", "resolve_project_file",
    "ProjectModels", "Validation", "WriteResult",
    "read_project_models", "validate_project_models", "write_project_models",
    "local_file_name", "resolve_local_file",
    "validate_local_document", "validate_portable_local_document", "render_local_yaml",
    "resolve_selections", "LocalOverrides", "ResolvedModels",
    "read_local_overrides", "resolve_models", "read_resolved_models",
    "LocalValidation", "LocalWriteResult",
    "validate_local_overrides", "write_local_overrides", "set_local_override", "clear_local_override",
]

#: The format id of the project model file (and the ``schema`` field of every result's JSON).
SCHEMA = "llm-kit.project-models/v1"
#: The ``version`` value a file must hold.
FILE_VERSION = 1
#: The project-relative file read and written when a call names no ``file``.
DEFAULT_FILE = ".llm-kit/models.yaml"

#: ProjectModelsError.code values: see :class:`ProjectModelsError`.
CODE_INVALID = "invalid"
CODE_STALE = "stale"
CODE_PATH = "path"
CODE_SYMLINK = "symlink"
CODE_MISSING = "missing"
CODE_DEPENDENCY = "dependency"
CODE_UNKNOWN_SELECTION = "unknown-selection"

#: The personal layer: ``<file-stem>.local.yaml`` beside the project file.
LOCAL_SUFFIX = ".local.yaml"
#: The ``layer`` values: the tracked file and the personal layer.
LAYER_TRACKED = "tracked"
LAYER_LOCAL = "local"
#: The ``source`` values of a resolved selection (:func:`resolve_selections`).
SOURCE_DEFAULT = "default"
SOURCE_OVERRIDE = "override"

_TOP_KEYS = ("version", "endpoints", "catalog", "requests", "selections")
_LOCAL_TOP_KEYS = ("version", "selections")
_ROW_KEYS = ("id", "label", "endpoint", "model", "efforts", "max_output_tokens", "traits", "request_defaults")
_ROW_REQUIRED = ("id", "label", "endpoint", "model")
_SELECTION_KEYS = ("model", "effort", "request")
_ENDPOINT_KEYS = ("effort_style",)
#: The typed request settings, in canonical order. ``max_output_tokens`` and
#: ``reasoning_budget`` are counts; ``temperature`` is 0 or more; ``top_p`` is 0 to 1.
REQUEST_SETTINGS = ("temperature", "top_p", "max_output_tokens", "reasoning_budget")
#: The keys of a row's ``request_defaults`` (its output cap default is the row's own ``max_output_tokens``).
_DEFAULTS_KEYS = ("temperature", "top_p", "reasoning_budget", "extras")
#: The keys of a named request profile (``requests``).
_PROFILE_KEYS = REQUEST_SETTINGS + ("extras",)
#: Extras names a request profile or a call may not hold: a typed setting or the
#: effort owns them, or the call itself does. Row extras keep the older rules.
PROFILE_RESERVED_EXTRAS = frozenset({
    "model", "messages", "stream", "api_key", EFFORT_KEY, REASONING_KEY,
    "temperature", "top_p", "max_tokens", "max_completion_tokens",
})
#: In a row's extras, these keys are the older spellings of the row's typed defaults.
LEGACY_ROW_EXTRAS = (("temperature", "temperature"), ("top_p", "top_p"), ("max_tokens", "max_output_tokens"))
# Extras a project default must not carry: the effort belongs to ``efforts`` and
# selections (so the menu check cannot be bypassed), and these name what the
# call itself owns.
_RESERVED_EXTRAS = frozenset({"model", "messages", "stream", "api_key", EFFORT_KEY})
_SHA256 = re.compile(r"[0-9a-f]{64}")
# The largest integer every JSON reader holds exactly (an IEEE 754 double).
_MAX_INTEGER = 2**53 - 1


class ProjectModelsError(ValueError):
    """The project model file, a document for it, or a path to it was refused.

    ``code`` is one of ``invalid`` (the document or an argument is wrong),
    ``stale`` (``if_match`` does not match the stored file), ``path`` (the file
    escapes the project root), ``symlink`` (a path component is a symlink),
    ``missing`` (the file or root does not exist), ``dependency`` (PyYAML is
    not installed), ``unknown-selection`` (no layer names the selection key a
    request was resolved for, :mod:`llm_kit.registry.request_settings`).

    ``where`` is the path of the first problem in the document, such as
    ``catalog[1].efforts[0]`` or ``selections['draft'].effort``; it is ``""``
    when the problem is the file as a whole (not UTF-8, not YAML, a duplicate
    key) or not in the document.
    """

    def __init__(self, code: str, message: str, *, where: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.where = where

    def to_json(self) -> dict[str, str]:
        return {"kind": "project-models", "code": self.code, "message": str(self)}


def _invalid(where: str, message: str, *, path: Optional[str] = None) -> ProjectModelsError:
    """An ``invalid`` error; ``path`` is the document path when it differs from the ``where`` text."""
    return ProjectModelsError(
        CODE_INVALID, f"{where}: {message}" if where else message,
        where=where if path is None else path,
    )


# ---------------------------------------------------------------------------
# Endpoints and the effort menu
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EndpointInfo:
    """What validation needs to know about one registry endpoint.

    ``harness`` is set for a harness entry (``codex``, ``claude``, ...);
    ``effort_style`` for a transport or wrapper endpoint that resolves one.
    ``route`` keys the measured exact-model profiles: the harness name for a
    harness entry, ``openrouter`` for a transport whose base URL is
    openrouter.ai, None for any other endpoint.
    """

    name: str
    kind: str
    harness: Optional[str] = None
    effort_style: Optional[str] = None
    route: Optional[str] = None


#: A function ``(endpoint, model) -> menu`` that returns the ordered effort menu
#: the binding check accepts; :func:`library_effort_menu` is the default.
EffortMenuResolver = Callable[[EndpointInfo, str], "tuple[str, ...]"]


def library_effort_menu_with_source(
    endpoint: EndpointInfo, model: str
) -> "tuple[tuple[str, ...], str]":
    """``(menu, source)``: the ordered effort menu the library accepts, and where it came from.

    EXACT-MODEL FIRST. A model with a measured profile
    (:mod:`llm_kit.registry.model_profiles`) takes that menu, for a harness
    entry and for an openrouter.ai transport alike (``library-model``). A profile
    with a required wire style has an empty menu on other endpoint styles. A harness
    model with no profile has NO menu: the library does not invent one
    (``none``). A transport that resolves an effort style and has no profile
    takes that style's wire menu (``library-endpoint-style``); a transport with
    neither delivers no effort (``none``). An empty menu means a row may declare
    no ``efforts`` and a selection must carry a null effort.
    """
    exact = exact_effort_menu(endpoint.route, model, effort_style=endpoint.effort_style)
    if exact is not None:
        return exact, MENU_EXACT_MODEL
    if endpoint.harness is not None:
        return (), MENU_NONE
    menu = style_effort_menu(endpoint.effort_style)
    return menu, (MENU_ENDPOINT_STYLE if menu else MENU_NONE)


def library_effort_menu(endpoint: EndpointInfo, model: str) -> "tuple[str, ...]":
    """The ordered effort menu the library accepts for ``endpoint`` and ``model``.

    Validation's default ``menu_for`` and the seam a test may replace; see
    :func:`library_effort_menu_with_source` for the rule.
    """
    return library_effort_menu_with_source(endpoint, model)[0]


def _route_of(base_url: Optional[str]) -> Optional[str]:
    """``openrouter`` when ``base_url`` is on openrouter.ai, else None."""
    return route_of_base_url(base_url)


def endpoint_styles(document: Mapping) -> "dict[str, str]":
    """Endpoint name -> the effort style the project file declares for it (``endpoints``).

    ``document`` is a normalized project model document. A style declared here wins
    over the machine's endpoint registry for this project's calls. Returns ``{}``
    when the file declares none; raises nothing.
    """
    return {name: entry["effort_style"] for name, entry in (document.get("endpoints") or {}).items()}


class _Endpoints:
    """Registry lookups for one validation pass, loaded on first use.

    ``styles`` (endpoint name -> effort style, see :func:`endpoint_styles`) replaces the
    registry's effort style of a transport or wrapper endpoint; a harness keeps its own.
    """

    def __init__(self, project_root: Optional[str], styles: "Optional[Mapping[str, str]]" = None) -> None:
        self._project_root = project_root
        self._loaded: Optional["tuple[dict, Any]"] = None
        self._seen: dict[str, Optional[EndpointInfo]] = {}
        self.styles: "dict[str, str]" = dict(styles or {})

    def use_styles(self, styles: "Mapping[str, str]") -> None:
        """Replace the declared effort styles; lookups made before are forgotten."""
        self.styles = dict(styles)
        self._seen.clear()

    @classmethod
    def for_document(cls, project_root: Optional[str], document: Mapping) -> "_Endpoints":
        """The lookups for ``document``, with the effort styles its ``endpoints`` declares."""
        return cls(project_root, endpoint_styles(document))

    def _load(self) -> "tuple[dict, Any]":
        if self._loaded is None:
            from .models import discover_model_entries, load_model_config  # noqa: PLC0415

            config = load_model_config(project_root=self._project_root)
            discovery = discover_model_entries(config=config, project_root=self._project_root)
            self._loaded = (config, discovery)
        return self._loaded

    def info(self, name: str) -> Optional[EndpointInfo]:
        if name not in self._seen:
            info = self._resolve(name)
            if info is not None and info.harness is None and name in self.styles:
                info = replace(info, effort_style=self.styles[name])
            self._seen[name] = info
        return self._seen[name]

    def _resolve(self, name: str) -> Optional[EndpointInfo]:
        from .model_endpoints import HARNESS_KIND, resolve_effort_style  # noqa: PLC0415

        config, discovery = self._load()
        entry = discovery.get(name)
        if entry is not None:
            if entry.kind == HARNESS_KIND:
                return EndpointInfo(name, HARNESS_KIND, harness=entry.harness, route=entry.harness)
            return EndpointInfo(
                name,
                entry.kind,
                effort_style=resolve_effort_style(entry).style,
                route=_route_of(entry.base_url),
            )
        endpoints = config.get("endpoints") or {}
        if isinstance(endpoints, Mapping) and name in endpoints:
            return self._wrapper(name, config)
        return None

    def _wrapper(self, name: str, config: dict) -> EndpointInfo:
        """A legacy wrapper endpoint (the shipped ``openrouter`` record): its resolved base URL and style."""
        from .models import EndpointResolveError, resolve_endpoint  # noqa: PLC0415

        try:
            resolved = resolve_endpoint(name, config=config, project_root=self._project_root)
        except EndpointResolveError:
            return EndpointInfo(name, "wrapper")
        return EndpointInfo(
            name,
            "wrapper",
            effort_style=resolved.get("effort_style"),
            route=_route_of(resolved.get("base_url")),
        )

    def entry(self, name: str) -> Any:
        """The discovered registry entry for ``name``, or None (a wrapper endpoint has none)."""
        return self._load()[1].get(name)

    def notes(self) -> "list[str]":
        """The registry's non-fatal loader notes (skipped declarations)."""
        return list(self._load()[1].notes)

    def entries(self) -> "Mapping[str, Any]":
        """Every discovered registry entry by id (the cost classifier's tier lookup)."""
        return self._load()[1].entries

    def known(self) -> list[str]:
        config, discovery = self._load()
        names = set(discovery)
        endpoints = config.get("endpoints") or {}
        if isinstance(endpoints, Mapping):
            names.update(str(n) for n in endpoints)
        return sorted(names)


# ---------------------------------------------------------------------------
# Strict parsing
# ---------------------------------------------------------------------------


def _json_pairs(pairs: list) -> dict:
    out: dict = {}
    for key, value in pairs:
        if key in out:
            raise _invalid("", f"duplicate key {key!r} in JSON input")
        out[key] = value
    return out


def _json_constant(token: str) -> Any:
    raise _invalid("", f"JSON input holds {token}, which is not a JSON value")


def parse_json_document(text: str) -> Any:
    """Parse JSON ``text`` strictly: no duplicate keys, no NaN or Infinity.

    Returns the parsed value. Raises :class:`ProjectModelsError` (``invalid``)
    for text that is not JSON, a duplicate key, NaN or Infinity.
    """
    try:
        return json.loads(text, object_pairs_hook=_json_pairs, parse_constant=_json_constant)
    except json.JSONDecodeError as exc:
        raise _invalid("", f"input is not valid JSON: {exc}") from None


def _import_yaml() -> Any:
    try:
        import yaml  # noqa: PLC0415
    except ImportError:
        raise ProjectModelsError(
            CODE_DEPENDENCY,
            "PyYAML is required for the project model file; "
            "install katsoft-llm-kit[config]",
        ) from None
    return yaml


@functools.lru_cache(maxsize=1)
def _yaml_types() -> "tuple[type, type, type]":
    """``(Loader, Dumper, FlowList)``: a duplicate-key-rejecting loader and the canonical dumper."""
    yaml = _import_yaml()

    class Loader(yaml.SafeLoader):
        def construct_mapping(self, node: Any, deep: bool = False) -> dict:
            mapping = super().construct_mapping(node, deep=deep)
            if len(mapping) != len(node.value):
                raise yaml.constructor.ConstructorError(
                    None, None, "duplicate mapping key", node.start_mark
                )
            return mapping

        def flatten_mapping(self, node: Any) -> None:
            for key_node, _value_node in node.value:
                if key_node.tag == "tag:yaml.org,2002:merge":
                    raise yaml.constructor.ConstructorError(
                        None, None, "merge keys (<<) are not supported; write the keys out",
                        key_node.start_mark,
                    )
            super().flatten_mapping(node)

    def refuse(loader: Any, node: Any) -> Any:
        raise yaml.constructor.ConstructorError(
            None, None, f"{node.tag} is not JSON data", node.start_mark
        )

    # PyYAML builds these as lists of tuples, which would pass as nested lists.
    Loader.add_constructor("tag:yaml.org,2002:omap", refuse)
    Loader.add_constructor("tag:yaml.org,2002:pairs", refuse)

    class FlowList(list):
        """A list rendered in flow style (``[a, b]``)."""

    class Dumper(yaml.SafeDumper):
        def increase_indent(self, flow: bool = False, indentless: bool = False) -> Any:
            return super().increase_indent(flow, False)

        def ignore_aliases(self, data: Any) -> bool:
            return True

    Dumper.add_representer(
        FlowList,
        lambda dumper, value: dumper.represent_sequence(
            "tag:yaml.org,2002:seq", list(value), flow_style=True
        ),
    )
    return Loader, Dumper, FlowList


def parse_yaml_document(data: bytes) -> Any:
    """Parse the stored file's bytes: one UTF-8 YAML document, duplicate keys refused.

    The bytes must be UTF-8; a leading byte order mark is skipped. YAML 1.1
    resolves plain words as PyYAML's safe loader does (``on`` is true, ``0x10``
    is 16). Merge keys (``<<``), ``!!omap`` and ``!!pairs`` are refused. Returns
    the document as Python data; raises :class:`ProjectModelsError`
    (``invalid``, ``where`` ``""``) for bytes that are not UTF-8, text that is
    not one YAML document, and an empty document, and (``dependency``) when
    PyYAML is not installed.
    """
    yaml = _import_yaml()
    loader = _yaml_types()[0]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise _invalid("", f"the file is not UTF-8: {exc}") from None
    try:
        document = yaml.load(text, Loader=loader)  # noqa: S506 -- a SafeLoader subclass
    except yaml.YAMLError as exc:
        raise _invalid("", f"the file is not valid YAML: {exc}") from None
    if document is None:
        raise _invalid("", "the file is empty")
    return document


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _text(value: Any, where: str, *, path: Optional[str] = None) -> str:
    if not isinstance(value, str):
        raise _invalid(where, f"must be a string, not {type(value).__name__}", path=path)
    if not value or value != value.strip():
        raise _invalid(where, "must be non-empty with no leading or trailing whitespace", path=path)
    return value


def _mapping(value: Any, where: str, allowed: "tuple[str, ...]") -> Mapping:
    if not isinstance(value, Mapping):
        raise _invalid(where, f"must be a mapping, not {type(value).__name__}")
    for key in value:
        if not isinstance(key, str):
            raise _invalid(where, f"key {key!r} must be a string")
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise _invalid(where, f"unknown key(s) {unknown}; allowed: {list(allowed)}")
    return value


def _json_value(value: Any, where: str, _open: "frozenset[int]" = frozenset()) -> Any:
    """A deep, key-sorted copy of a JSON value; anything else is refused.

    ``_open`` holds the containers on the path to ``value``: a container that
    holds itself (a YAML alias cycle) is refused instead of recursing forever.
    """
    if value is None or isinstance(value, (bool, str)):
        return value
    if isinstance(value, int):
        if abs(value) > _MAX_INTEGER:
            raise _invalid(
                where, f"must be an integer from -{_MAX_INTEGER} to {_MAX_INTEGER}, not {value}"
            )
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise _invalid(where, "must be a finite number")
        return value
    if isinstance(value, (list, tuple, Mapping)):
        if id(value) in _open:
            raise _invalid(where, "holds itself (a YAML alias cycle)")
        inner = _open | {id(value)}
        if not isinstance(value, Mapping):
            return [_json_value(item, f"{where}[{i}]", inner) for i, item in enumerate(value)]
        out = {}
        for key in sorted(value, key=lambda k: (not isinstance(k, str), str(k))):
            if not isinstance(key, str):
                raise _invalid(where, f"key {key!r} must be a string")
            out[key] = _json_value(value[key], f"{where}.{key}", inner)
        return out
    raise _invalid(where, f"must be JSON data, not {type(value).__name__}")


def _number(value: Any, where: str) -> Any:
    """A JSON number (an int in the exact range, or a finite float); a boolean is refused."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _invalid(where, f"must be a number, not {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise _invalid(where, "must be a finite number")
    if isinstance(value, int) and abs(value) > _MAX_INTEGER:
        raise _invalid(where, f"must be a number from -{_MAX_INTEGER} to {_MAX_INTEGER}, not {value}")
    return value


def check_setting(name: str, value: Any, where: str) -> Any:
    """Validate one typed request setting and return it.

    ``name`` is one of :data:`REQUEST_SETTINGS`: ``max_output_tokens`` and
    ``reasoning_budget`` are integers from 1 to 2**53 - 1 (a decimal such as
    ``60.0`` is refused), ``temperature`` is a number 0 or more, ``top_p`` a
    number from 0 to 1. Raises :class:`ProjectModelsError` (``invalid``) at
    ``where``.
    """
    if name in ("max_output_tokens", "reasoning_budget"):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_INTEGER:
            raise _invalid(where, f"must be an integer from 1 to {_MAX_INTEGER}, not {value!r}")
        return value
    number = _number(value, where)
    if name == "temperature" and number < 0:
        raise _invalid(where, f"must be 0 or more, not {number!r}")
    if name == "top_p" and not 0 <= number <= 1:
        raise _invalid(where, f"must be from 0 to 1, not {number!r}")
    return number


def _check_profile_extras(extras: Mapping, where: str) -> None:
    """Refuse an extras name a typed setting, the effort or the call owns (:data:`PROFILE_RESERVED_EXTRAS`)."""
    for key in extras:
        if key in PROFILE_RESERVED_EXTRAS:
            raise _invalid(
                where, f"{key!r} is reserved; set it as a typed setting, through the effort, or in the call"
            )
    nested = extras.get(TEMPLATE_KEY)
    if isinstance(nested, Mapping) and EFFORT_KEY in nested:
        raise _invalid(f"{where}.{TEMPLATE_KEY}", f"{EFFORT_KEY!r} is reserved; set the effort instead")


def validate_profile(value: Any, where: str, *, call: bool = False) -> dict:
    """Validate a request profile (an entry of ``requests``) or, with ``call``, a call layer.

    A profile holds any of :data:`REQUEST_SETTINGS` and ``extras`` (a JSON
    mapping with no :data:`PROFILE_RESERVED_EXTRAS` key). A call also holds
    ``effort`` (null or non-empty text), and may not hold both ``effort`` and
    ``reasoning_budget``. Returns the normalized copy with only the given keys,
    in canonical order. Raises :class:`ProjectModelsError` (``invalid``) for the
    first problem, with its path in ``where`` (``requests['p'].temperature``,
    ``call.extras``).
    """
    allowed = _PROFILE_KEYS + (("effort",) if call else ())
    given = _mapping(value, where, allowed)
    out: dict = {}
    for name in REQUEST_SETTINGS:
        if name in given:
            out[name] = check_setting(name, given[name], f"{where}.{name}")
    if "extras" in given:
        extras = given["extras"]
        if not isinstance(extras, Mapping):
            raise _invalid(f"{where}.extras", "must be a mapping")
        copy = _json_value(extras, f"{where}.extras")
        _check_profile_extras(copy, f"{where}.extras")
        out["extras"] = copy
    if call and "effort" in given:
        effort = given["effort"]
        if effort is not None and (not isinstance(effort, str) or not effort):
            raise _invalid(f"{where}.effort", f"must be a non-empty string or null, not {effort!r}")
        if "reasoning_budget" in given:
            raise _invalid(where, "sets both effort and reasoning_budget; one layer sets one reasoning control")
        out["effort"] = effort
    return out


def _request_defaults(
    value: Any, where: str, *, bound: bool = True, reasoning_reserved: bool = False
) -> dict:
    """Validate a row's request defaults: its typed defaults and its extras.

    ``bound`` (binding) refuses the reserved extras; portable validation passes
    False. ``reasoning_reserved`` is set for a ``reasoning-object`` endpoint only:
    there a raw ``reasoning`` extra would override the selected effort and bypass
    the menu. A typed default may not also be spelled in extras (``temperature``,
    ``top_p``; ``reasoning`` beside ``reasoning_budget``).
    """
    defaults = _mapping(value, where, _DEFAULTS_KEYS)
    out: dict = {}
    for name in ("temperature", "top_p", "reasoning_budget"):
        if name in defaults:
            out[name] = check_setting(name, defaults[name], f"{where}.{name}")
    if "extras" in defaults:
        extras = defaults["extras"]
        if not isinstance(extras, Mapping):
            raise _invalid(f"{where}.extras", "must be a mapping")
        copy = _json_value(extras, f"{where}.extras")
        if bound:
            for key in copy:
                if key in _RESERVED_EXTRAS or (reasoning_reserved and key == REASONING_KEY):
                    raise _invalid(
                        f"{where}.extras", f"{key!r} is reserved; set effort through efforts and selections"
                    )
            nested = copy.get(TEMPLATE_KEY)
            if isinstance(nested, Mapping) and EFFORT_KEY in nested:
                raise _invalid(
                    f"{where}.extras.{TEMPLATE_KEY}",
                    f"{EFFORT_KEY!r} is reserved; set effort through efforts and selections",
                )
        for name, wire in (("temperature", "temperature"), ("top_p", "top_p"), ("reasoning_budget", REASONING_KEY)):
            if name in out and wire in copy:
                raise _invalid(f"{where}.extras", f"{wire!r} repeats the typed {name}; keep one")
        out["extras"] = copy
    return out


def _max_output_tokens(value: Any, where: str, row_id: str, model: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_INTEGER:
        raise _invalid(
            where,
            f"must be a positive integer up to {_MAX_INTEGER}, not {value!r} "
            f"(row {row_id!r}, model {model!r})",
        )
    return value


def _efforts(
    value: Any, where: str, menu: "Optional[tuple[str, ...]]", endpoint: str, model: str
) -> list[str]:
    """A row's declared efforts; ``menu`` None (portable) checks only strings and repeats."""
    if not isinstance(value, (list, tuple)):
        raise _invalid(where, f"must be a list, not {value!r} (an empty list declares no effort)")
    out: list[str] = []
    last = -1
    for i, item in enumerate(value):
        item_where = f"{where}[{i}]"
        if not isinstance(item, str) or not item:
            raise _invalid(
                item_where,
                f"must be a non-empty string, not {item!r} (YAML reads on/off/yes/no as booleans; quote the word)",
            )
        if menu is None:
            if item in out:
                raise _invalid(item_where, f"{item!r} repeats")
            out.append(item)
            continue
        if not menu:
            raise _invalid(
                item_where,
                f"the library knows no effort menu for endpoint {endpoint!r} model {model!r}; omit efforts",
            )
        if item not in menu:
            raise _invalid(item_where, f"{item!r} is not in the library menu {list(menu)}")
        index = menu.index(item)
        if index <= last:
            raise _invalid(
                item_where, f"{item!r} repeats or breaks the library order {list(menu)}"
            )
        last = index
        out.append(item)
    return out


def _traits(value: Any, where: str) -> list[str]:
    """A row's traits: non-empty strings with no repeats. Consumer labels; never sent."""
    if not isinstance(value, (list, tuple)):
        raise _invalid(where, f"must be a list, not {value!r}")
    out: list[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str) or not item:
            raise _invalid(f"{where}[{i}]", f"must be a non-empty string, not {item!r}")
        if item in out:
            raise _invalid(f"{where}[{i}]", f"{item!r} repeats")
        out.append(item)
    return out


def _validate_endpoints(value: Any) -> "dict[str, dict]":
    """The key-sorted ``endpoints`` block: endpoint name -> ``{effort_style}``.

    Checks the structure only; that each name is a row's endpoint is checked
    after the catalogue (:func:`_check_endpoint_names`).
    """
    if not isinstance(value, Mapping):
        raise _invalid("endpoints", f"must be a mapping, not {type(value).__name__}")
    out: dict[str, dict] = {}
    for name in value:
        if not isinstance(name, str):
            raise _invalid("endpoints", f"key {name!r} must be a string")
        where = f"endpoints[{name!r}]"
        _text(name, f"endpoints key {name!r}", path=where)
        entry = _mapping(value[name], where, _ENDPOINT_KEYS)
        if "effort_style" not in entry:
            raise _invalid(where, "'effort_style' is required")
        style = entry["effort_style"]
        if style not in EFFORT_STYLES:
            raise _invalid(f"{where}.effort_style", f"must be one of {list(EFFORT_STYLES)}, not {style!r}")
        out[name] = {"effort_style": style}
    return {name: out[name] for name in sorted(out)}


def _check_endpoint_names(names: "list[str]", rows: "list[dict]", endpoints: "Optional[_Endpoints]") -> None:
    """Each declared endpoint is a row's endpoint; binding also refuses a style on a harness."""
    used = {row["endpoint"] for row in rows}
    for name in names:
        where = f"endpoints[{name!r}]"
        if name not in used:
            raise _invalid(where, f"no catalog row names endpoint {name!r}")
        info = endpoints.info(name) if endpoints is not None else None
        if info is not None and info.harness is not None:
            raise _invalid(f"{where}.effort_style", f"{name!r} is a {info.harness} harness, which takes no effort style")


def _validate_requests(value: Any) -> "dict[str, dict]":
    """The key-sorted ``requests`` block: profile name -> normalized profile (:func:`validate_profile`)."""
    if not isinstance(value, Mapping):
        raise _invalid("requests", f"must be a mapping, not {type(value).__name__}")
    out: dict[str, dict] = {}
    for name in value:
        if not isinstance(name, str):
            raise _invalid("requests", f"key {name!r} must be a string")
        where = f"requests[{name!r}]"
        _text(name, f"requests key {name!r}", path=where)
        out[name] = validate_profile(value[name], where)
    return {name: out[name] for name in sorted(out)}


def _validate_catalog(
    catalog_in: Any,
    endpoints: "Optional[_Endpoints]",
    menu_of: Optional[EffortMenuResolver],
    unbound: "Optional[list[str]]" = None,
) -> "tuple[list[dict], dict[str, Optional[tuple[str, ...]]], dict[str, Optional[list[str]]]]":
    """``(rows, menus, efforts_of)``: the normalized catalogue and each row's library menu and declared efforts.

    ``endpoints`` None is portable validation: no endpoint is resolved, every
    menu is None and no extra is reserved. With ``endpoints``, a row whose
    endpoint resolves to no registry endpoint is unbound: its menu is None, the
    endpoint-independent reserved extras are refused, and its id is appended to
    ``unbound`` when given.
    """
    if not isinstance(catalog_in, (list, tuple)):
        raise _invalid("catalog", f"must be a list, not {type(catalog_in).__name__}")
    rows: list[dict] = []
    menus: dict[str, "Optional[tuple[str, ...]]"] = {}
    efforts_of: dict[str, Optional[list[str]]] = {}
    for i, raw in enumerate(catalog_in):
        where = f"catalog[{i}]"
        row = _mapping(raw, where, _ROW_KEYS)
        for key in _ROW_REQUIRED:
            if key not in row:
                raise _invalid(where, f"{key!r} is required")
        row_id = _text(row["id"], f"{where}.id")
        if row_id in menus:
            raise _invalid(f"{where}.id", f"duplicate id {row_id!r}")
        label = _text(row["label"], f"{where}.label")
        endpoint = _text(row["endpoint"], f"{where}.endpoint")
        model = _text(row["model"], f"{where}.model")
        info: Optional[EndpointInfo] = None
        menu: "Optional[tuple[str, ...]]" = None
        if endpoints is not None:
            info = endpoints.info(endpoint)
            if info is None:
                if unbound is not None:
                    unbound.append(row_id)
            else:
                menu = tuple((menu_of or library_effort_menu)(info, model))
        out: dict = {"id": row_id, "label": label, "endpoint": endpoint, "model": model}
        efforts: Optional[list[str]] = None
        if "efforts" in row:
            efforts = _efforts(row["efforts"], f"{where}.efforts", menu, endpoint, model)
            out["efforts"] = efforts
        if "max_output_tokens" in row:
            out["max_output_tokens"] = _max_output_tokens(
                row["max_output_tokens"], f"{where}.max_output_tokens", row_id, model
            )
        if "traits" in row:
            out["traits"] = _traits(row["traits"], f"{where}.traits")
        if "request_defaults" in row:
            out["request_defaults"] = _request_defaults(
                row["request_defaults"],
                f"{where}.request_defaults",
                bound=endpoints is not None,
                reasoning_reserved=info is not None and info.effort_style == REASONING_OBJECT,
            )
        menus[row_id] = menu
        efforts_of[row_id] = efforts
        rows.append(out)
    return rows, menus, efforts_of


def _validate_selections(
    selections_in: Any,
    menus: "Mapping[str, Optional[tuple[str, ...]]]",
    efforts_of: "Mapping[str, Optional[list[str]]]",
    requests: "Mapping[str, Mapping]",
) -> "dict[str, dict]":
    """The key-sorted normalized selections; each must name a catalog id and an accepted effort.

    The accepted efforts are the row's declared ``efforts``; a row that declares
    none accepts its library menu, or (portable, menu None) any non-empty string.
    A ``request`` names a key of ``requests``; a selection with an effort names no
    request that sets ``reasoning_budget`` (one layer, one reasoning control).
    """
    if not isinstance(selections_in, Mapping):
        raise _invalid("selections", f"must be a mapping, not {type(selections_in).__name__}")
    selections: dict[str, dict] = {}
    for key in selections_in:
        where = f"selections[{key!r}]" if isinstance(key, str) else "selections"
        if not isinstance(key, str):
            raise _invalid(where, f"key {key!r} must be a string")
        _text(key, f"selections key {key!r}", path=where)
        picked = _mapping(selections_in[key], where, _SELECTION_KEYS)
        if "model" not in picked:
            raise _invalid(where, "'model' is required")
        target = _text(picked["model"], f"{where}.model")
        if target not in menus:
            raise _invalid(f"{where}.model", f"{target!r} is not a catalog id")
        effort = picked.get("effort")
        if effort is not None:
            if not isinstance(effort, str) or not effort:
                raise _invalid(
                    f"{where}.effort",
                    f"must be a non-empty string or null, not {effort!r} "
                    "(YAML reads on/off/yes/no as booleans; quote the word)",
                )
            declared, menu = efforts_of[target], menus[target]
            allowed = declared if declared is not None else (None if menu is None else list(menu))
            if allowed is not None and effort not in allowed:
                raise _invalid(
                    f"{where}.effort",
                    f"{effort!r} is not accepted for {target!r} (accepted: {list(allowed)})",
                )
        selection: dict = {"model": target, "effort": effort}
        if "request" in picked:
            name = _text(picked["request"], f"{where}.request")
            if name not in requests:
                raise _invalid(
                    f"{where}.request",
                    f"{name!r} is not a key of requests (known: {', '.join(requests) or '<none>'})",
                )
            if effort is not None and "reasoning_budget" in requests[name]:
                raise _invalid(
                    f"{where}.effort",
                    f"effort {effort!r} and the reasoning_budget of request {name!r} are one layer; set one",
                )
            selection["request"] = name
        selections[key] = selection
    return {key: selections[key] for key in sorted(selections)}


def validate_document(
    data: Any,
    *,
    project_root: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> dict:
    """Validate ``data`` and return its normalized, canonical-order copy.

    Raises :class:`ProjectModelsError` (``invalid``) naming the first problem,
    with a path such as ``catalog[1].efforts[0]``. Nothing is written.
    ``menu_for`` replaces :func:`library_effort_menu` (the exact-model seam).
    """
    return _validate_bound(data, project_root, menu_for)[0]


def _validate_bound(
    data: Any, project_root: Optional[str], menu_for: Optional[EffortMenuResolver]
) -> "tuple[dict, tuple[str, ...]]":
    """``(document, unbound)``: :func:`validate_document` and the ids of its unbound rows."""
    _import_yaml()  # the registry and the canonical form both need it: name the extra first
    unbound: list[str] = []
    document = _validate_top(data, _Endpoints(project_root), menu_for or library_effort_menu, unbound)
    return document, tuple(unbound)


def validate_portable_document(data: Any) -> dict:
    """Validate ``data`` as a project model file without this machine's registry.

    ``data`` is the parsed document (see :func:`parse_yaml_document`). The
    check is the portable part of :func:`validate_document`: structure, JSON
    values and references. It does not resolve endpoints, does not check
    efforts against the library menu, and does not refuse reserved extras; a
    row that declares no ``efforts`` accepts any non-empty effort string here.
    It needs no PyYAML and reads no file. Returns the normalized document (the
    same shape :func:`validate_document` returns). Raises
    :class:`ProjectModelsError` (``invalid``) for the first problem, with its
    path in ``where``.
    """
    return _validate_top(data, None, None)


def _check_version(top: Mapping) -> None:
    if "version" not in top:
        raise _invalid("version", "is required")
    version = top["version"]
    if not isinstance(version, int) or isinstance(version, bool) or version != FILE_VERSION:
        raise _invalid("version", f"must be {FILE_VERSION}, not {version!r}")


def _validate_top(
    data: Any,
    endpoints: "Optional[_Endpoints]",
    menu_of: Optional[EffortMenuResolver],
    unbound: "Optional[list[str]]" = None,
) -> dict:
    """The normalized tracked document; ``endpoints`` None is portable validation.

    Order of the checks (it fixes which problem is reported first): the version,
    the ``endpoints`` structure, the catalogue (with the declared effort styles),
    that each ``endpoints`` name is a row's endpoint, ``requests``, ``selections``.
    """
    top = _mapping(data, "document", _TOP_KEYS)
    _check_version(top)
    declared = _validate_endpoints(top["endpoints"]) if "endpoints" in top else None
    if endpoints is not None:
        endpoints.use_styles(endpoint_styles({"endpoints": declared or {}}))
    rows, menus, efforts_of = _validate_catalog(top.get("catalog", []), endpoints, menu_of, unbound)
    if declared is not None:
        _check_endpoint_names(list(top["endpoints"]), rows, endpoints)
    requests = _validate_requests(top["requests"]) if "requests" in top else None
    selections = _validate_selections(top.get("selections", {}), menus, efforts_of, requests or {})
    document: dict = {"version": FILE_VERSION}
    if declared is not None:
        document["endpoints"] = declared
    document["catalog"] = rows
    if requests is not None:
        document["requests"] = requests
    document["selections"] = selections
    return document


# ---------------------------------------------------------------------------
# Canonical rendering and hashing
# ---------------------------------------------------------------------------


def render_yaml(document: Mapping) -> bytes:
    """The canonical bytes of an already-validated document.

    ``document`` is a result of :func:`validate_document` (or
    :func:`validate_portable_document`). Returns UTF-8 YAML in the canonical
    form the module docstring describes. Raises :class:`ProjectModelsError`
    (``dependency``) when PyYAML is not installed.
    """
    yaml = _import_yaml()
    _loader, dumper, flow_list = _yaml_types()
    rows = []
    for row in document["catalog"]:
        shown = dict(row)
        for key in ("efforts", "traits"):
            if key in shown:
                shown[key] = flow_list(shown[key])
        rows.append(shown)
    shown_document: dict = {"version": document["version"]}
    if "endpoints" in document:
        shown_document["endpoints"] = document["endpoints"]
    shown_document["catalog"] = rows
    if "requests" in document:
        shown_document["requests"] = document["requests"]
    shown_document["selections"] = document["selections"]
    text = yaml.dump(
        shown_document,
        Dumper=dumper,
        sort_keys=False,
        default_flow_style=False,
        indent=2,
        allow_unicode=True,
        width=1_000_000,
    )
    return text.encode("utf-8")


def digest(data: bytes) -> str:
    """The lowercase SHA-256 hex digest of ``data``, as the ``if_match`` guard compares it."""
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# The file: confinement, read, atomic write
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectFile:
    """A confined project file: the resolved root, the absolute path, the root-relative POSIX name."""

    root: Path
    path: Path
    relative: str


def resolve_project_file(
    project_root: Optional[str] = None, file: Optional[str] = None
) -> ProjectFile:
    """Resolve ``file`` (default :data:`DEFAULT_FILE`) beneath ``project_root`` (default cwd).

    Refuses a root that is not a directory, a name that leaves the root
    (``..`` or an absolute path outside it), and any symlink among the
    components beneath the root, the file itself included. Touches nothing.
    """
    try:
        root = Path(project_root if project_root else os.getcwd()).resolve(strict=True)
    except OSError as exc:
        raise ProjectModelsError(CODE_MISSING, f"project root does not exist: {exc.filename or project_root}") from None
    if not root.is_dir():
        raise ProjectModelsError(CODE_MISSING, f"project root is not a directory: {root}")
    raw = DEFAULT_FILE if file is None else file
    if not raw or "\0" in raw:
        raise _invalid("--file", "must be a non-empty path")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    lexical = Path(os.path.normpath(candidate))
    if not lexical.is_relative_to(root) and Path(raw).is_absolute():
        # An absolute name spelled through a symlinked prefix of the root
        # (macOS /tmp, a mounted home): compare through its parent's real path.
        lexical = Path(os.path.realpath(lexical.parent)) / lexical.name
    if not lexical.is_relative_to(root):
        raise ProjectModelsError(CODE_PATH, f"{raw!r} resolves outside the project root {root}")
    relative = lexical.relative_to(root)
    if relative == Path("."):
        raise ProjectModelsError(CODE_PATH, f"{raw!r} names the project root, not a file")
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise ProjectModelsError(
                CODE_SYMLINK, f"{cursor} is a symlink; the project model file must not be reached through one"
            )
    return ProjectFile(root=root, path=lexical, relative=relative.as_posix())


def _read_existing(project_file: ProjectFile) -> Optional[bytes]:
    """The stored bytes, or None when the file is absent; a non-regular file is refused."""
    try:
        info = project_file.path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise _invalid(project_file.relative, "exists and is not a regular file")
    return project_file.path.read_bytes()


@dataclass(frozen=True)
class ProjectModels:
    """The stored file, validated: its normalized document and the stored bytes' digest.

    ``unbound`` is the ids, in catalogue order, of the rows whose endpoint
    resolves to no registry endpoint on this machine; a call to one refuses.
    """

    path: str
    sha256: str
    document: dict
    unbound: "tuple[str, ...]" = ()

    def to_json(self) -> dict:
        return {"schema": SCHEMA, "path": self.path, "sha256": self.sha256, "document": self.document,
                "unbound": list(self.unbound)}


@dataclass(frozen=True)
class Validation:
    """A document that passed validation, and the digest ``write`` would store.

    ``unbound`` names the rows whose endpoint resolves to no registry endpoint
    (see :class:`ProjectModels`).
    """

    path: str
    canonical_sha256: str
    catalog_rows: int
    selections: int
    unbound: "tuple[str, ...]" = ()

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA,
            "valid": True,
            "path": self.path,
            "canonical_sha256": self.canonical_sha256,
            "catalog_rows": self.catalog_rows,
            "selections": self.selections,
            "unbound": list(self.unbound),
        }


@dataclass(frozen=True)
class WriteResult:
    """A completed write: the confined name, the stored digest and the counts."""

    path: str
    sha256: str
    changed: bool
    catalog_rows: int
    selections: int

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA,
            "path": self.path,
            "sha256": self.sha256,
            "changed": self.changed,
            "catalog_rows": self.catalog_rows,
            "selections": self.selections,
        }


def _read_tracked(
    project_file: ProjectFile, menu_for: Optional[EffortMenuResolver] = None
) -> ProjectModels:
    data = _read_existing(project_file)
    if data is None:
        raise ProjectModelsError(CODE_MISSING, f"{project_file.relative} does not exist under {project_file.root}")
    document, unbound = _validate_bound(parse_yaml_document(data), str(project_file.root), menu_for)
    return ProjectModels(project_file.relative, digest(data), document, unbound)


def read_project_models(
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> ProjectModels:
    """Read, strictly parse and validate the stored file.

    Raises ``missing`` when it does not exist and ``invalid`` when it fails
    validation; a file that cannot be shown is never silently repaired. The
    personal layer is not read here; see :func:`read_resolved_models`.
    """
    return _read_tracked(resolve_project_file(project_root, file), menu_for)


def validate_project_models(
    document: Any,
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> Validation:
    """Validate ``document`` and confine ``file``; nothing is read or written.

    Runs :func:`validate_document` with the endpoint registry of
    ``project_root``. Returns a :class:`Validation` with the digest a write
    would store. Raises :class:`ProjectModelsError`: ``invalid`` for the first
    problem (path in ``where``), ``path``, ``symlink`` or ``missing`` for the
    file name, ``dependency`` without PyYAML.
    """
    project_file = resolve_project_file(project_root, file)
    normalized, unbound = _validate_bound(document, str(project_file.root), menu_for)
    return Validation(
        project_file.relative,
        digest(render_yaml(normalized)),
        len(normalized["catalog"]),
        len(normalized["selections"]),
        unbound,
    )


def _fsync_directory(path: Path) -> None:
    if os.name == "posix":
        directory = os.open(path, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def _atomic_write(path: Path, data: bytes, mode: Optional[int]) -> None:
    """Write ``data`` to a same-directory temporary file, fsync it, and replace ``path``.

    A created file takes the mode the umask leaves; a replaced file keeps its mode.
    """
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass
        raise
    _fsync_directory(path.parent)


def _check_if_match(project_file: ProjectFile, existing: Optional[bytes], if_match: Optional[str]) -> None:
    """Refuse a write whose ``if_match`` digest does not match the stored bytes."""
    if if_match is None:
        return
    if not _SHA256.fullmatch(if_match):
        raise _invalid("if_match", "must be a lowercase 64-character SHA-256 hex digest")
    if existing is None:
        raise ProjectModelsError(
            CODE_STALE, f"{project_file.relative} does not exist, so it cannot match {if_match}"
        )
    if digest(existing) != if_match:
        raise ProjectModelsError(
            CODE_STALE, f"{project_file.relative} changed: stored digest is {digest(existing)}, not {if_match}"
        )


def write_project_models(
    document: Any,
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    if_match: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> WriteResult:
    """Validate ``document`` and store its canonical form atomically.

    Every refusal (path, symlink, validation, stale digest) happens before the
    file or its directory is touched. ``if_match`` is the digest a prior
    ``read_project_models`` returned; the file must exist and still hash to it.
    Without ``if_match`` the file is created or overwritten. A document whose
    canonical bytes equal the stored bytes is not rewritten (``changed`` False).
    """
    project_file = resolve_project_file(project_root, file)
    normalized = validate_document(document, project_root=str(project_file.root), menu_for=menu_for)
    data = render_yaml(normalized)
    existing = _read_existing(project_file)
    _check_if_match(project_file, existing, if_match)
    result = WriteResult(
        project_file.relative, digest(data), existing != data,
        len(normalized["catalog"]), len(normalized["selections"]),
    )
    if existing == data:
        return result
    mode: Optional[int] = None
    if existing is None:
        project_file.path.parent.mkdir(parents=True, exist_ok=True)
    else:
        mode = stat.S_IMODE(project_file.path.lstat().st_mode)
    _atomic_write(project_file.path, data, mode)
    return result


# ---------------------------------------------------------------------------
# The personal layer: overrides beside the tracked file
# ---------------------------------------------------------------------------
#
# ``<file-stem>.local.yaml`` (default ``.llm-kit/models.local.yaml``) holds
#
#     version: 1
#     selections:
#       draft.generate.primary: {model: luna, effort: low}
#
# and is meant to be untracked. This library never edits ``.gitignore``; that
# is the consumer's call. Each key in the local file wins over the same key in
# the tracked file's ``selections``; a key only the local file names is an
# override with no default. The local file is validated exactly as the tracked
# one is, against the TRACKED catalogue (it carries none of its own), and every
# error names the local file. An override equal to the tracked default is not
# stored: a write drops it, and a file left with no override is deleted.


def local_file_name(relative: str) -> str:
    """The root-relative POSIX name of the personal layer for the tracked file ``relative``.

    ``.llm-kit/models.yaml`` gives ``.llm-kit/models.local.yaml``. Pure; raises nothing.
    """
    path = PurePosixPath(relative)
    return path.with_name(path.stem + LOCAL_SUFFIX).as_posix()


def resolve_local_file(project_file: ProjectFile) -> ProjectFile:
    """The confined personal-layer file beside ``project_file`` (same confinement rules). Touches nothing.

    Returns a :class:`ProjectFile`. Raises :class:`ProjectModelsError` as
    :func:`resolve_project_file` does.
    """
    return resolve_project_file(str(project_file.root), local_file_name(project_file.relative))


def _name_local(local_file: ProjectFile, exc: ProjectModelsError) -> ProjectModelsError:
    if exc.code != CODE_INVALID:
        return exc
    return ProjectModelsError(exc.code, f"{local_file.relative}: {exc}", where=exc.where)


def validate_local_document(
    data: Any,
    tracked: ProjectModels,
    local_file: ProjectFile,
    *,
    menu_for: Optional[EffortMenuResolver] = None,
) -> dict:
    """Validate a personal-layer document against ``tracked``'s catalogue; return its normalized copy.

    Same model and effort checks as the tracked file's selections. A refusal
    is a ``ProjectModelsError`` (``invalid``) whose message starts with the
    local file's name.
    """
    try:
        _import_yaml()
        return _validate_local(
            data, tracked.document, _Endpoints.for_document(str(local_file.root), tracked.document),
            menu_for or library_effort_menu,
        )
    except ProjectModelsError as exc:
        raise _name_local(local_file, exc) from None


def validate_portable_local_document(data: Any, tracked_document: Any) -> dict:
    """Validate ``data`` as a personal layer over ``tracked_document``, without this machine's registry.

    ``tracked_document`` is the tracked file's parsed or normalized document;
    it is validated first with :func:`validate_portable_document`, so a fault
    in it is reported with its own path. ``data`` must then pass the same
    portable checks as the tracked selections, against the tracked catalogue.
    It needs no PyYAML and reads no file. Returns the normalized personal layer
    ``{"version": 1, "selections": {...}}``. Raises :class:`ProjectModelsError`
    (``invalid``) for the first problem, with its path in ``where``.
    """
    tracked = validate_portable_document(tracked_document)
    return _validate_local(data, tracked, None, None)


def _validate_local(
    data: Any, tracked: Mapping, endpoints: "Optional[_Endpoints]", menu_of: Optional[EffortMenuResolver]
) -> dict:
    """The normalized personal layer against the normalized ``tracked`` document; ``endpoints`` None is portable."""
    top = _mapping(data, "document", _LOCAL_TOP_KEYS)
    _check_version(top)
    _rows, menus, efforts_of = _validate_catalog(tracked["catalog"], endpoints, menu_of)
    selections = _validate_selections(top.get("selections", {}), menus, efforts_of, tracked.get("requests") or {})
    return {"version": FILE_VERSION, "selections": selections}


def render_local_yaml(document: Mapping) -> bytes:
    """The canonical bytes of an already-validated personal-layer document.

    ``document`` is ``{"version": 1, "selections": {...}}`` as
    :func:`validate_local_document` returns it. Returns UTF-8 YAML. Raises
    :class:`ProjectModelsError` (``dependency``) when PyYAML is not installed.
    """
    yaml = _import_yaml()
    dumper = _yaml_types()[1]
    text = yaml.dump(
        {"version": document["version"], "selections": document["selections"]},
        Dumper=dumper,
        sort_keys=False,
        default_flow_style=False,
        indent=2,
        allow_unicode=True,
        width=1_000_000,
    )
    return text.encode("utf-8")


def resolve_selections(
    defaults: Mapping[str, Mapping], overrides: Mapping[str, Mapping]
) -> "dict[str, dict]":
    """Per selection key: ``{"effective", "default", "source"}``, sorted by key.

    ``effective`` is the override when the local layer names the key with a
    value that differs from the default, else the default; ``default`` is the
    tracked selection, or ``None`` for a key only the local layer names;
    ``source`` is ``override`` when the effective value comes from the local
    layer, else ``default``. An entry equal to the default is no override.
    """
    out: dict[str, dict] = {}
    for key in sorted(set(defaults) | set(overrides)):
        default = dict(defaults[key]) if key in defaults else None
        local = dict(overrides[key]) if key in overrides else None
        if local is not None and local != default:
            out[key] = {"effective": local, "default": default, "source": SOURCE_OVERRIDE}
        else:
            out[key] = {"effective": default, "default": default, "source": SOURCE_DEFAULT}
    return out


@dataclass(frozen=True)
class LocalOverrides:
    """The personal layer as stored: its name, whether it exists, its digest and its overrides."""

    path: str
    present: bool
    sha256: Optional[str]
    selections: "dict[str, dict]"

    def to_json(self) -> dict:
        return {
            "path": self.path,
            "present": self.present,
            "sha256": self.sha256,
            "selections": self.selections,
        }


@dataclass(frozen=True)
class ResolvedModels:
    """The tracked file, the personal layer over it, and the per-selection resolution."""

    project: ProjectModels
    local: LocalOverrides
    selections: "dict[str, dict]"

    def to_json(self) -> dict:
        out = self.project.to_json()
        out["local"] = self.local.to_json()
        out["resolved_selections"] = self.selections
        return out


def _read_local(
    tracked: ProjectModels, local_file: ProjectFile, menu_for: Optional[EffortMenuResolver]
) -> LocalOverrides:
    data = _read_existing(local_file)
    if data is None:
        return LocalOverrides(local_file.relative, False, None, {})
    try:
        parsed = parse_yaml_document(data)
    except ProjectModelsError as exc:
        raise _name_local(local_file, exc) from None
    document = validate_local_document(parsed, tracked, local_file, menu_for=menu_for)
    return LocalOverrides(local_file.relative, True, digest(data), document["selections"])


def read_local_overrides(
    tracked: ProjectModels,
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> LocalOverrides:
    """Read and validate the personal layer for ``tracked``; an absent file is no overrides.

    Returns :class:`LocalOverrides`. Raises :class:`ProjectModelsError`
    (``invalid``, message prefixed with the local file's name) for a bad file,
    and as :func:`resolve_project_file` does for the file name.
    """
    local_file = resolve_local_file(resolve_project_file(project_root, file))
    return _read_local(tracked, local_file, menu_for)


def resolve_models(tracked: ProjectModels, local: LocalOverrides) -> ResolvedModels:
    """Layer ``local`` over ``tracked``; returns :class:`ResolvedModels` (see :func:`resolve_selections`)."""
    return ResolvedModels(
        tracked, local, resolve_selections(tracked.document["selections"], local.selections)
    )


def read_resolved_models(
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> ResolvedModels:
    """Read the tracked file and the personal layer over it, both validated.

    Returns :class:`ResolvedModels`. Raises :class:`ProjectModelsError` as
    :func:`read_project_models` and :func:`read_local_overrides` do.
    """
    project_file = resolve_project_file(project_root, file)
    tracked = _read_tracked(project_file, menu_for)
    local = _read_local(tracked, resolve_local_file(project_file), menu_for)
    return resolve_models(tracked, local)


@dataclass(frozen=True)
class LocalValidation:
    """A personal-layer document that passed validation, and the digest ``write`` would store.

    ``canonical_sha256`` is ``None`` when nothing would be stored (no override
    survives), in which case a write deletes the file.
    """

    path: str
    canonical_sha256: Optional[str]
    selections: int
    dropped_defaults: "list[str]"

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA,
            "layer": LAYER_LOCAL,
            "valid": True,
            "path": self.path,
            "canonical_sha256": self.canonical_sha256,
            "selections": self.selections,
            "dropped_defaults": self.dropped_defaults,
        }


@dataclass(frozen=True)
class LocalWriteResult:
    """A completed personal-layer write or clear.

    ``sha256`` is the stored digest, ``None`` when no file is stored;
    ``deleted`` is true when the write removed the file because no override
    remained; ``dropped_defaults`` lists keys not stored because they equal the
    tracked default.
    """

    path: str
    sha256: Optional[str]
    changed: bool
    deleted: bool
    selections: int
    dropped_defaults: "list[str]"

    def to_json(self) -> dict:
        return {
            "schema": SCHEMA,
            "layer": LAYER_LOCAL,
            "path": self.path,
            "sha256": self.sha256,
            "changed": self.changed,
            "deleted": self.deleted,
            "selections": self.selections,
            "dropped_defaults": self.dropped_defaults,
        }


def _tidy_local(
    document: Any,
    project_root: Optional[str],
    file: Optional[str],
    menu_for: Optional[EffortMenuResolver],
) -> "tuple[ProjectFile, dict, dict, list[str]]":
    """``(local_file, kept, normalized, dropped)``: validated, with default-equal entries removed."""
    project_file = resolve_project_file(project_root, file)
    tracked = _read_tracked(project_file, menu_for)
    local_file = resolve_local_file(project_file)
    normalized = validate_local_document(document, tracked, local_file, menu_for=menu_for)
    defaults = tracked.document["selections"]
    kept = {k: v for k, v in normalized["selections"].items() if defaults.get(k) != v}
    dropped = sorted(set(normalized["selections"]) - set(kept))
    return local_file, {"version": FILE_VERSION, "selections": kept}, normalized, dropped


def validate_local_overrides(
    document: Any,
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> LocalValidation:
    """Validate a personal-layer ``document`` against the tracked file; nothing is written.

    Reads and validates the tracked file first. Returns a
    :class:`LocalValidation` (entries equal to the tracked default are listed
    in ``dropped_defaults``). Raises :class:`ProjectModelsError` as
    :func:`read_project_models` does, and ``invalid`` for the document.
    """
    local_file, kept, _normalized, dropped = _tidy_local(document, project_root, file, menu_for)
    stored = digest(render_local_yaml(kept)) if kept["selections"] else None
    return LocalValidation(local_file.relative, stored, len(kept["selections"]), dropped)


def write_local_overrides(
    document: Any,
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    if_match: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> LocalWriteResult:
    """Validate ``document`` and store the personal layer atomically, replacing it whole.

    Entries equal to the tracked default are dropped; when no override
    remains, the file is deleted (or stays absent). ``if_match`` is the
    ``sha256`` of the stored personal file, as for the tracked file. Every
    refusal happens before the file or its directory is touched.
    """
    local_file, kept, _normalized, dropped = _tidy_local(document, project_root, file, menu_for)
    existing = _read_existing(local_file)
    _check_if_match(local_file, existing, if_match)
    count = len(kept["selections"])
    if not count:
        if existing is None:
            return LocalWriteResult(local_file.relative, None, False, False, 0, dropped)
        local_file.path.unlink()
        _fsync_directory(local_file.path.parent)
        return LocalWriteResult(local_file.relative, None, True, True, 0, dropped)
    data = render_local_yaml(kept)
    result = LocalWriteResult(local_file.relative, digest(data), existing != data, False, count, dropped)
    if existing == data:
        return result
    mode: Optional[int] = None
    if existing is None:
        local_file.path.parent.mkdir(parents=True, exist_ok=True)
    else:
        mode = stat.S_IMODE(local_file.path.lstat().st_mode)
    _atomic_write(local_file.path, data, mode)
    return result


#: The ``request`` default of :func:`set_local_override`: keep the tracked selection's request profile.
KEEP_REQUEST = "<keep>"


def set_local_override(
    key: str,
    model: str,
    effort: Optional[str] = None,
    *,
    request: Optional[str] = KEEP_REQUEST,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    if_match: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> LocalWriteResult:
    """Set one override (``effort`` None omits the effort); equal to the default, it clears the key.

    An override replaces the whole selection, so ``request`` names its request
    profile: :data:`KEEP_REQUEST` (the default) copies the tracked selection's
    ``request``, so overriding only the model keeps the role's settings; None
    omits it; a name sets it. Returns the :class:`LocalWriteResult` of the
    write. Raises :class:`ProjectModelsError` as :func:`write_local_overrides`
    does.
    """
    project_file = resolve_project_file(project_root, file)
    tracked = _read_tracked(project_file, menu_for)
    current = _read_local(tracked, resolve_local_file(project_file), menu_for).selections
    entry: dict = {"model": model, "effort": effort}
    if request == KEEP_REQUEST:
        request = (tracked.document["selections"].get(key) or {}).get("request")
    if request is not None:
        entry["request"] = request
    current[key] = entry
    return write_local_overrides(
        {"version": FILE_VERSION, "selections": current},
        project_root=project_root, file=file, if_match=if_match, menu_for=menu_for,
    )


def clear_local_override(
    key: str,
    *,
    project_root: Optional[str] = None,
    file: Optional[str] = None,
    if_match: Optional[str] = None,
    menu_for: Optional[EffortMenuResolver] = None,
) -> LocalWriteResult:
    """Remove one override; the file is deleted when none remains.

    The stored file is read structurally, not validated whole, so a stale
    override (for example one naming a model the tracked catalogue dropped) can
    be cleared; the entries that remain are still validated before anything is
    written. A key the file does not hold changes nothing.
    """
    project_file = resolve_project_file(project_root, file)
    tracked = _read_tracked(project_file, menu_for)
    local_file = resolve_local_file(project_file)
    data = _read_existing(local_file)
    if data is None:
        _check_if_match(local_file, None, if_match)
        return LocalWriteResult(local_file.relative, None, False, False, 0, [])
    try:
        parsed = parse_yaml_document(data)
        if not isinstance(parsed, Mapping) or not isinstance(parsed.get("selections", {}), Mapping):
            raise _invalid("document", "must be a mapping with a selections mapping")
    except ProjectModelsError as exc:
        raise _name_local(local_file, exc) from None
    selections = dict(parsed.get("selections", {}))
    if key not in selections:
        _check_if_match(local_file, data, if_match)
        return LocalWriteResult(
            local_file.relative, digest(data), False, False, len(selections), []
        )
    del selections[key]
    return write_local_overrides(
        {"version": FILE_VERSION, "selections": selections},
        project_root=project_root, file=file, if_match=if_match, menu_for=menu_for,
    )

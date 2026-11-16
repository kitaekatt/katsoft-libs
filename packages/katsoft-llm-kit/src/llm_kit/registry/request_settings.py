"""The request a selection resolves to: layered, typed request settings from the project model file.

A project model file (:mod:`llm_kit.registry.project_models`) describes the whole
request of a selection, not only its model and effort. The settings come in three
layers, lowest first:

1. the catalogue row: its ``request_defaults`` (``temperature``, ``top_p``,
   ``reasoning_budget``, ``extras``) and its own ``max_output_tokens``. In the row's
   extras, ``temperature``, ``top_p`` and ``max_tokens`` are the older spellings of
   those defaults and are read as them (a typed value wins over ``max_tokens``);
2. the selection: its ``effort``, and the request profile its ``request`` names
   (an entry of ``requests``);
3. the call: the same keys as a profile, plus ``effort``.

Per typed setting the highest layer that sets it wins; ``extras`` merge per
top-level key in the same order. Reasoning is one control with two forms,
``effort`` and ``reasoning_budget``: the highest layer that sets either wins, and a
call that names ``effort`` (null included) decides it, so a call ``effort: null``
sends no reasoning control at all. ``traits`` are the row's consumer labels; they
are returned and never sent.

The result of :func:`resolve_request` is provider-neutral JSON with every key
present (null when unset)::

    {"row", "endpoint", "model", "effort", "effort_style", "temperature", "top_p",
     "max_output_tokens", "reasoning_budget", "extras", "traits"}

``effort_style`` is the style the project file declares for the row's endpoint
(``endpoints``), else null: the caller then uses its own (Python: the endpoint
registry). The TypeScript reader ``@katsoft/llm-kit-config`` returns the same
value, and the shared cases in ``tests/formats/project-models/requests.json`` fix
both, with the OpenAI-compatible body each request must put on the wire. Contract:
``docs/reference/llm-kit-formats.md``, "Request settings".

Stdlib only; no endpoint registry is read here.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping, Optional

from .project_models import (
    CODE_UNKNOWN_SELECTION,
    LEGACY_ROW_EXTRAS,
    ProjectModelsError,
    _invalid,
    check_setting,
    validate_profile,
)

__all__ = ["REQUEST_KEYS", "request_for", "resolve_request"]

#: The keys of a resolved request, in order.
REQUEST_KEYS = (
    "row", "endpoint", "model", "effort", "effort_style", "temperature", "top_p",
    "max_output_tokens", "reasoning_budget", "extras", "traits",
)


def _row_layer(row: Mapping, index: int) -> "tuple[dict, dict]":
    """``(settings, extras)`` of a row: its typed defaults, with the older extras spellings read as them."""
    defaults = row.get("request_defaults") or {}
    extras = copy.deepcopy(dict(defaults.get("extras") or {}))
    settings: dict = {}
    for wire, name in LEGACY_ROW_EXTRAS:
        if wire in extras:
            where = f"catalog[{index}].request_defaults.extras.{wire}"
            try:
                settings[name] = check_setting(name, extras.pop(wire), where)
            except ProjectModelsError as exc:
                raise ProjectModelsError(exc.code, f"{exc} (row {row['id']!r})", where=exc.where) from None
    for name in ("temperature", "top_p", "reasoning_budget"):
        if name in defaults:
            settings[name] = defaults[name]
    if "max_output_tokens" in row:
        settings["max_output_tokens"] = row["max_output_tokens"]
    return settings, extras


def _call_effort(call: Mapping, row: Mapping) -> None:
    """A call effort is held to the row's declared efforts, as a selection's is."""
    effort = call.get("effort")
    declared = row.get("efforts")
    if effort is not None and declared is not None and effort not in declared:
        raise _invalid("call.effort", f"{effort!r} is not accepted for {row['id']!r} (accepted: {list(declared)})")


def request_for(document: Mapping, selection: Mapping, *, call: Optional[Mapping] = None) -> dict:
    """The resolved request of one effective ``selection`` (``{model, effort, request?}``).

    ``document`` is a normalized project model document (the result of
    :func:`~llm_kit.registry.project_models.validate_portable_document` or of any
    read). ``call`` is the call layer (see the module docstring); None is an
    empty call. Returns the resolved request as a new dict of JSON data. Raises
    :class:`~llm_kit.registry.project_models.ProjectModelsError` (``invalid``):
    at ``call`` and ``call.<key>`` for a bad call; at
    ``catalog[<i>].request_defaults.extras.<key>`` for an older extras spelling
    that is not a valid setting; at ``selection.model``, ``selection.request``
    or ``selection.effort`` for a selection the document does not support.
    """
    catalog = list(document["catalog"])
    index = next((i for i, row in enumerate(catalog) if row["id"] == selection.get("model")), None)
    if index is None:
        raise _invalid("selection.model", f"{selection.get('model')!r} is not a catalog id")
    row = catalog[index]
    profile: Mapping = {}
    if selection.get("request") is not None:
        profiles = document.get("requests") or {}
        if selection["request"] not in profiles:
            raise _invalid("selection.request", f"{selection['request']!r} is not a key of requests")
        profile = profiles[selection["request"]]
    layer = validate_profile({} if call is None else call, "call", call=True)
    _call_effort(layer, row)
    row_settings, row_extras = _row_layer(row, index)

    effort = selection.get("effort")
    if "effort" in layer or "reasoning_budget" in layer:
        effort, budget = layer.get("effort"), layer.get("reasoning_budget")
    elif effort is not None or "reasoning_budget" in profile:
        if effort is not None and "reasoning_budget" in profile:
            raise _invalid("selection.effort", "an effort and its request's reasoning_budget are one layer; set one")
        budget = profile.get("reasoning_budget")
    else:
        budget = row_settings.get("reasoning_budget")

    resolved: dict = {
        "row": row["id"],
        "endpoint": row["endpoint"],
        "model": row["model"],
        "effort": effort,
        "effort_style": ((document.get("endpoints") or {}).get(row["endpoint"]) or {}).get("effort_style"),
    }
    for name in ("temperature", "top_p", "max_output_tokens"):
        resolved[name] = next((source[name] for source in (layer, profile, row_settings) if name in source), None)
    resolved["reasoning_budget"] = budget
    resolved["extras"] = {
        **row_extras,
        **copy.deepcopy(dict(profile.get("extras") or {})),
        **copy.deepcopy(dict(layer.get("extras") or {})),
    }
    resolved["traits"] = list(row.get("traits") or [])
    return resolved


def resolve_request(
    document: Mapping,
    key: str,
    *,
    overrides: Optional[Mapping[str, Mapping]] = None,
    call: Optional[Mapping] = None,
) -> dict:
    """The resolved request of selection ``key``: the personal layer over the tracked selections, then :func:`request_for`.

    ``document`` is a normalized project model document; ``overrides`` the
    normalized selections of a personal layer or overlay (a key there replaces
    the tracked selection whole); ``call`` the call layer. Returns the resolved
    request (see the module docstring). Raises
    :class:`~llm_kit.registry.project_models.ProjectModelsError`
    ``unknown-selection`` (``where`` ``selections['<key>']``) when no layer names
    ``key``, and as :func:`request_for` does.
    """
    layers = dict(document["selections"])
    layers.update(overrides or {})
    if key not in layers:
        raise ProjectModelsError(
            CODE_UNKNOWN_SELECTION, f"no layer names the selection key {key!r}", where=f"selections[{key!r}]"
        )
    return request_for(document, layers[key], call=call)

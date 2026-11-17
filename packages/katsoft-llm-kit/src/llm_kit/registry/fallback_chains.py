"""Fallback chain files (``llm-kit.fallback-chain/v1``): hosts in order and a model's copy on each host.

A fallback chain file names, per chain, the hosts that serve a request in order
and the failure classes that move on to the next host (``no_key`` skips a host
with no key), and, per model, the model's endpoint on each host and the rules
that build one host's input from another's. The TypeScript reader is
``@katsoft/llm-kit-send`` (``readFallbackChains``, ``mapInput``); the shared cases
in ``tests/formats/fallback-chain/cases.json`` fix both readers. Contract:
``docs/reference/llm-kit-formats.md``, "llm-kit.fallback-chain/v1".

This module reads and checks a file and maps an input; it sends nothing (the
TypeScript package runs a chain). It checks the whole format itself, as the
TypeScript reader does; the tests validate the shared cases against the schema
``llm_kit.schema.formats.FALLBACK_CHAIN_V1`` too. Stdlib only.
"""
from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

__all__ = ["FallbackChainError", "check_fallback_chains", "map_input", "read_fallback_chains"]

_HOST_ID = re.compile(r"[a-z0-9][a-z0-9_.-]*")
_CLASS_ID = re.compile(r"[a-z][a-z0-9_]*")
_ALNUM = re.compile(r"[A-Za-z0-9]+")
_FORMAT = "llm-kit.fallback-chain/v1"
_RULE_KEYS = ("key", "from", "value", "number", "max", "id_key", "id_match")


class FallbackChainError(ValueError):
    """A fallback chain document that breaks the format.

    ``path`` names the place (``models.m.hosts.h.input[1].max``, or ``$`` for the
    whole document); the message starts with it.
    """

    def __init__(self, path: str, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def check_fallback_chains(document: Any) -> dict[str, Any]:
    """Check a parsed fallback chain document and return it.

    The document must have the shape of the JSON Schema of
    ``llm-kit.fallback-chain/v1`` (checked here, field by field) and the reader
    rules: host ids match ``[a-z0-9][a-z0-9_.-]*`` and class ids
    ``[a-z][a-z0-9_]*``; the hosts of one chain and the classes of one step are
    unique; the output fields (``key``, ``id_key``) of one rule list are unique;
    ``max`` needs ``number``; ``id_key`` and ``id_match`` go together and not with
    ``number``; ``input`` needs ``from``; ``from`` names another host of the same
    model, which has no ``from`` itself.

    Raises :class:`FallbackChainError` with the path of the first broken rule.
    """
    if not isinstance(document, dict):
        raise FallbackChainError("$", "the document is an object")
    _only(document, ("format", "chains", "models"), "$")
    if document.get("format") != _FORMAT:
        raise FallbackChainError("format", f"must be {_FORMAT!r}")
    for name in ("chains", "models"):
        if not isinstance(document.get(name), dict):
            raise FallbackChainError(name, "an object")
    for chain_name, chain in document["chains"].items():
        at = f"chains.{chain_name}"
        if not isinstance(chain, dict):
            raise FallbackChainError(at, "an object")
        _only(chain, ("steps",), at)
        if not isinstance(chain.get("steps"), list) or not chain["steps"]:
            raise FallbackChainError(f"{at}.steps", "a non-empty array")
        for index, step in enumerate(chain["steps"]):
            if not isinstance(step, dict):
                raise FallbackChainError(f"{at}.steps[{index}]", "an object")
            _only(step, ("host", "next_on"), f"{at}.steps[{index}]")
            if not isinstance(step.get("host"), str):
                raise FallbackChainError(f"{at}.steps[{index}].host", "a host id (a-z, 0-9, _ . -)")
            if not isinstance(step.get("next_on"), list) or not all(isinstance(item, str) for item in step["next_on"]):
                raise FallbackChainError(f"{at}.steps[{index}].next_on", "an array of class ids")
    for model_id, model in document["models"].items():
        at = f"models.{model_id}"
        if not isinstance(model, dict):
            raise FallbackChainError(at, "an object")
        _only(model, ("hosts",), at)
        if not isinstance(model.get("hosts"), dict):
            raise FallbackChainError(f"{at}.hosts", "a non-empty object")
        for host, entry in model["hosts"].items():
            if not isinstance(entry, dict):
                raise FallbackChainError(f"{at}.hosts.{host}", "an object")
            _only(entry, ("endpoint", "from", "input"), f"{at}.hosts.{host}")
            if not isinstance(entry.get("endpoint"), str) or not entry["endpoint"]:
                raise FallbackChainError(f"{at}.hosts.{host}.endpoint", "a non-empty string")
            if "from" in entry and (not isinstance(entry["from"], str) or not entry["from"]):
                raise FallbackChainError(f"{at}.hosts.{host}.from", "a non-empty string")
            if "input" in entry and not isinstance(entry["input"], list):
                raise FallbackChainError(f"{at}.hosts.{host}.input", "an array")
    for chain_name, chain in document["chains"].items():
        hosts: set[str] = set()
        for index, step in enumerate(chain["steps"]):
            at = f"chains.{chain_name}.steps[{index}]"
            if not _HOST_ID.fullmatch(step["host"]):
                raise FallbackChainError(f"{at}.host", "a host id (a-z, 0-9, _ . -)")
            if step["host"] in hosts:
                raise FallbackChainError(f"{at}.host", f"{step['host']!r} is already a step of this chain")
            hosts.add(step["host"])
            classes: set[str] = set()
            for place, item in enumerate(step["next_on"]):
                if not _CLASS_ID.fullmatch(item):
                    raise FallbackChainError(f"{at}.next_on[{place}]", "a class id (a-z, 0-9, _)")
                if item in classes:
                    raise FallbackChainError(f"{at}.next_on[{place}]", f"{item!r} twice")
                classes.add(item)
    for model_id, model in document["models"].items():
        model_hosts = model["hosts"]
        if not model_hosts:
            raise FallbackChainError(f"models.{model_id}.hosts", "a non-empty object")
        for host, entry in model_hosts.items():
            at = f"models.{model_id}.hosts.{host}"
            if not _HOST_ID.fullmatch(host):
                raise FallbackChainError(at, "a host id (a-z, 0-9, _ . -)")
            if "from" in entry:
                source = entry["from"]
                if source == host or source not in model_hosts:
                    raise FallbackChainError(f"{at}.from", "another host of this model")
                if "from" in model_hosts[source]:
                    raise FallbackChainError(f"{at}.from", f"{source!r} maps from a host itself; mappings do not compose")
            if "input" in entry:
                if "from" not in entry:
                    raise FallbackChainError(f"{at}.input", "needs from")
                outputs: set[str] = set()
                for index, rule in enumerate(entry["input"]):
                    _check_rule(rule, f"{at}.input[{index}]")
                    for output in (rule["key"], rule.get("id_key")):
                        if output is None:
                            continue
                        if output in outputs:
                            raise FallbackChainError(f"{at}.input[{index}]", f"the output field {output!r} twice")
                        outputs.add(output)
    return document


def _only(value: Mapping[str, Any], keys: Sequence[str], path: str) -> None:
    for key in value:
        if key not in keys:
            raise FallbackChainError(path, f"unknown field {key!r}")


def _name(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _check_rule(rule: Any, path: str) -> None:
    if not isinstance(rule, dict):
        raise FallbackChainError(path, "a rule is an object")
    _only(rule, _RULE_KEYS, path)
    if not _name(rule.get("key")):
        raise FallbackChainError(f"{path}.key", "a non-empty string is required")
    if "value" in rule:
        for other in ("from", "number", "max", "id_key", "id_match"):
            if other in rule:
                raise FallbackChainError(f"{path}.{other}", "not allowed with value")
        if not (rule["value"] is None or isinstance(rule["value"], (str, bool)) or _number(rule["value"])):
            raise FallbackChainError(f"{path}.value", "a string, finite number, boolean or null")
        return
    if "from" in rule and not _name(rule["from"]):
        raise FallbackChainError(f"{path}.from", "a non-empty string")
    if "number" in rule and rule["number"] is not True:
        raise FallbackChainError(f"{path}.number", "only true")
    if "max" in rule and not _number(rule["max"]):
        raise FallbackChainError(f"{path}.max", "a finite number")
    if "max" in rule and rule.get("number") is not True:
        raise FallbackChainError(f"{path}.max", "needs number: true")
    if ("id_key" in rule) != ("id_match" in rule):
        raise FallbackChainError(f"{path}.id_key", "id_key and id_match go together")
    if "id_key" in rule:
        if not _name(rule["id_key"]):
            raise FallbackChainError(f"{path}.id_key", "a non-empty string")
        if "number" in rule:
            raise FallbackChainError(f"{path}.id_key", "not allowed with number")
        match = rule["id_match"]
        if not isinstance(match, dict):
            raise FallbackChainError(f"{path}.id_match", "an object")
        _only(match, ("charset", "length"), f"{path}.id_match")
        if match.get("charset") != "alnum":
            raise FallbackChainError(f"{path}.id_match.charset", "only 'alnum'")
        length = match.get("length")
        if not isinstance(length, int) or isinstance(length, bool) or length < 1:
            raise FallbackChainError(f"{path}.id_match.length", "an integer of 1 or more")


def read_fallback_chains(path: str | Path) -> dict[str, Any]:
    """Read a fallback chain file (UTF-8 JSON) and check it with :func:`check_fallback_chains`.

    Returns the checked document. Raises :class:`FallbackChainError` (path ``$``)
    when the file is not JSON, and :class:`OSError` when it cannot be read.
    """
    text = Path(path).read_text(encoding="utf-8")
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise FallbackChainError("$", f"not JSON ({error.msg} at line {error.lineno})") from error
    return check_fallback_chains(document)


def map_input(rules: Sequence[Mapping[str, Any]], source: Mapping[str, Any]) -> dict[str, Any]:
    """Build a host's input from another host's input with checked rules, in rule order.

    A rule writes ``value``, or reads the field ``from`` (default ``key``). A
    missing field is left out; null is copied. With ``number`` only a number (not
    a boolean) is copied, at most ``max``. With ``id_key`` a string of exactly
    ``id_match.length`` ASCII letters and digits is written under ``id_key``
    instead of ``key``. Returns a new dict whose key order is the rule order.
    """
    output: dict[str, Any] = {}
    for rule in rules:
        key = rule["key"]
        if "value" in rule:
            output[key] = rule["value"]
            continue
        name = rule.get("from", key)
        if name not in source:
            continue
        field = source[name]
        if rule.get("number"):
            if not (isinstance(field, (int, float)) and not isinstance(field, bool)):
                continue
            output[key] = field if "max" not in rule else min(field, rule["max"])
            continue
        match = rule.get("id_match")
        if match and isinstance(field, str) and len(field) == match["length"] and _ALNUM.fullmatch(field):
            output[rule["id_key"]] = field
            continue
        output[key] = field
    return output

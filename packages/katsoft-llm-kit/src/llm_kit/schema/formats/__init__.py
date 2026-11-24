"""JSON Schemas of the llm-kit file formats that readers in other languages share.

Each format has one id and one JSON Schema file in this package:

- ``llm-kit.project-models/v1``: the tracked project model file
  (:mod:`llm_kit.registry.project_models`).
- ``llm-kit.project-models-local/v1``: its personal layer.
- ``llm-kit.prompt-file/v1``: the parsed form of a prompt file
  (:mod:`llm_kit.graph.prompt`).
- ``llm-kit.run-record/v1``: one attempt of one case on one model selection
  (:mod:`llm_kit.runs`).
- ``llm-kit.run-store/v1``: the policy file ``store.json`` of a run store.
- ``llm-kit.run-import/v1``: one line of a run import file.
- ``llm-kit.compare-case/v1``: one line of a comparison case file
  (:mod:`llm_kit.runs.compare`).
- ``llm-kit.fallback-chain/v1``: a fallback chain file: hosts in order and a
  model's copy on each host (:mod:`llm_kit.registry.fallback_chains`).
- ``llm-kit.call-record/v1``: one sent try of a model call, as the npm package
  ``@katsoft/llm-kit-send`` reports it.
- ``llm-kit.call-record/v2``: version 1 plus ``replayed`` (a reply cache answer).
- ``llm-kit.reply-entry/v1``: one stored answer of the reply cache
  (:mod:`llm_kit.registry.reply_cache`).
- ``llm-kit.consent/v2``: the user-level consent file, its cost-class policy and
  its connection approvals (:mod:`llm_kit.registry.consent`).
- ``llm-kit.graph-catalog/v1``: the graphs a rendered graph page draws
  (:mod:`llm_kit.graph.render`).

The schemas are written in ``llm-scripting-kit.json-schema-subset/v1``, so
:mod:`llm_kit.schema.json_schema` checks and validates them with no third-party
package. A rule the subset cannot state is a reader rule; the shared cases under
``tests/formats/`` in the source repository fix every reader rule. The contract
is in ``docs/reference/llm-kit-formats.md``. This module is standard library
only.
"""

from __future__ import annotations

import json
from importlib import resources
from typing import Any

__all__ = [
    "PROJECT_MODELS_V1",
    "PROJECT_MODELS_LOCAL_V1",
    "PROMPT_FILE_V1",
    "RUN_RECORD_V1",
    "RUN_STORE_V1",
    "RUN_IMPORT_V1",
    "COMPARE_CASE_V1",
    "FALLBACK_CHAIN_V1",
    "CALL_RECORD_V1",
    "CALL_RECORD_V2",
    "REPLY_ENTRY_V1",
    "CONSENT_V2",
    "GRAPH_CATALOG_V1",
    "SCHEMA_FILES",
    "load_schema",
]

#: The tracked project model file.
PROJECT_MODELS_V1 = "llm-kit.project-models/v1"
#: The personal layer beside the project model file.
PROJECT_MODELS_LOCAL_V1 = "llm-kit.project-models-local/v1"
#: The parsed form of a prompt file.
PROMPT_FILE_V1 = "llm-kit.prompt-file/v1"
#: One attempt of one case on one model selection.
RUN_RECORD_V1 = "llm-kit.run-record/v1"
#: The policy file ``store.json`` at the root of a run store.
RUN_STORE_V1 = "llm-kit.run-store/v1"
#: One line of a run import file (JSON Lines).
RUN_IMPORT_V1 = "llm-kit.run-import/v1"
#: One line of a comparison case file (JSON Lines): a case id and its slot values.
COMPARE_CASE_V1 = "llm-kit.compare-case/v1"
#: A fallback chain file: hosts in order and a model's copy on each host.
FALLBACK_CHAIN_V1 = "llm-kit.fallback-chain/v1"
#: One sent try of a model call (tokens, cost, time; never text).
CALL_RECORD_V1 = "llm-kit.call-record/v1"
#: Version 1 plus ``replayed``: a sent try, or an answer the reply cache replayed.
CALL_RECORD_V2 = "llm-kit.call-record/v2"
#: One stored answer of the reply cache: a step's answer, or an operation's final refusal.
REPLY_ENTRY_V1 = "llm-kit.reply-entry/v1"
#: The user-level consent file (``schema: 2``): the cost-class policy and the connection approvals.
CONSENT_V2 = "llm-kit.consent/v2"
#: The graphs a rendered graph page draws: entry graphs, graph files, who runs each file.
GRAPH_CATALOG_V1 = "llm-kit.graph-catalog/v1"

#: Format id -> the schema file name in this package. The file's ``$id`` is the format id.
SCHEMA_FILES = {
    PROJECT_MODELS_V1: "project-models.v1.schema.json",
    PROJECT_MODELS_LOCAL_V1: "project-models-local.v1.schema.json",
    PROMPT_FILE_V1: "prompt-file.v1.schema.json",
    RUN_RECORD_V1: "run-record.v1.schema.json",
    RUN_STORE_V1: "run-store.v1.schema.json",
    RUN_IMPORT_V1: "run-import.v1.schema.json",
    COMPARE_CASE_V1: "compare-case.v1.schema.json",
    FALLBACK_CHAIN_V1: "fallback-chain.v1.schema.json",
    CALL_RECORD_V1: "call-record.v1.schema.json",
    CALL_RECORD_V2: "call-record.v2.schema.json",
    REPLY_ENTRY_V1: "reply-entry.v1.schema.json",
    CONSENT_V2: "consent.v2.schema.json",
    GRAPH_CATALOG_V1: "graph-catalog.v1.schema.json",
}


def load_schema(format_id: str) -> dict[str, Any]:
    """The JSON Schema of the format ``format_id``, as a new dict.

    ``format_id`` is a key of :data:`SCHEMA_FILES`. The result passes
    :func:`llm_kit.schema.json_schema.check_schema`; validate data with
    :func:`llm_kit.schema.json_schema.validate`. Raises :class:`KeyError` for an
    unknown format id.
    """
    if format_id not in SCHEMA_FILES:
        raise KeyError(f"unknown llm-kit format {format_id!r}; known: {sorted(SCHEMA_FILES)}")
    text = resources.files(__package__).joinpath(SCHEMA_FILES[format_id]).read_text(encoding="utf-8")
    return json.loads(text)

"""The types of inferred graph labels: one model call, its reply, who makes it, and the errors.

A rendered graph page can carry labels a model inferred from the graph files and
their prompts: a title and description for each node, a text for each edge, a
gloss for each input and output. The labelling core (``llm_kit.graph.render.labels``)
plans :class:`LabelCall` values, hands each to a :class:`Labeller` and gets a
:class:`LabelReply` back. A catalog with labels has the format
:data:`LABELLED_CATALOG_FORMAT` (``llm_kit.schema.formats.GRAPH_CATALOG_V2``); the
cache entry of one request is ``llm-kit.graph-labels/v1``. This module holds only
these types; it makes no call and imports no model code.

Every error the labelling raises is a :class:`LabelError`. The ``llm-kit graph
render`` command maps all of them to exit status 3 and writes no page.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Protocol

__all__ = [
    "LABELS_PROMPT_ID",
    "LABELLED_CATALOG_FORMAT",
    "LabelCall",
    "LabelReply",
    "Labeller",
    "LabelError",
    "LabelRefused",
    "LabelFailed",
]

#: The version of the label prompt; it enters the cache key, so a new prompt makes new keys.
LABELS_PROMPT_ID = "llm-kit.graph-labels.prompt/1"

#: The format id of a catalog that carries labels (``llm_kit.schema.formats.GRAPH_CATALOG_V2``).
LABELLED_CATALOG_FORMAT = "llm-kit.graph-catalog/v2"


@dataclass(frozen=True)
class LabelCall:
    """One model call that labels part of one graph file.

    ``file`` is the graph file's key in the catalog and ``index`` the call's
    position in that file's plan. ``header`` is true for the call that also
    labels the graph itself, its inputs and its outputs. ``nodes``, ``edges``,
    ``inputs`` and ``outputs`` are the keys this call must answer: node paths,
    ``"<dep path>-><node path>"`` edge keys, and port names. ``system`` and
    ``prompt`` are the texts sent. ``schema`` is the structural schema of the
    answer (``llm-scripting-kit.json-schema-subset/v1``); completeness against
    the keys above is checked by the caller. ``key`` is the cache key of the
    primary call. A follow-up call (``followup`` true) asks only for keys the
    primary call left out, and ``entry`` names the model entry that served the
    primary call, which must serve the follow-up too.
    """

    file: str
    index: int
    header: bool
    nodes: tuple[str, ...]
    edges: tuple[str, ...]
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    system: str
    prompt: str
    schema: Mapping[str, Any]
    key: str
    followup: bool = False
    entry: Optional[str] = None


@dataclass(frozen=True)
class LabelReply:
    """The answer to a :class:`LabelCall` and who gave it.

    ``answer`` is the parsed JSON answer, already checked against the call's
    schema. ``entry`` is the id of the model entry that served the call and
    ``model`` the model that answered.
    """

    answer: Mapping[str, Any]
    entry: str
    model: str


class Labeller(Protocol):
    """Whatever makes a :class:`LabelCall` into a :class:`LabelReply`.

    ``declaration`` is the ordered model entry ids the labeller may use; it
    enters the cache key. Calling the labeller with a call returns the reply, or
    raises :class:`LabelRefused` when nothing was sent (consent refused, no
    usable model, evidence over the cap) or :class:`LabelFailed` when a call ran
    and gave no usable labels. A labeller may be called from several threads.
    """

    declaration: tuple[str, ...]

    def __call__(self, call: LabelCall) -> LabelReply:
        ...


class LabelError(Exception):
    """A graph label could not be made or read; ``llm-kit graph render`` exits 3 on it and writes no page."""


class LabelRefused(LabelError):
    """Labelling stopped before any model call was sent: consent, no usable model, or evidence over the cap."""


class LabelFailed(LabelError):
    """A model call ran and gave no usable labels: an unparsable answer, an unknown key or a key still missing."""

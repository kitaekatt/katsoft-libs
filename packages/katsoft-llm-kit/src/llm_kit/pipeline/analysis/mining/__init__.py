"""Open-label mining over a text corpus: the generic map-reduce half of an LLM corpus analysis.

A consumer splits its corpus into stable batches (``batches``), asks a model to tag each item with open-vocabulary
labels, counts the labels (``labels``), asks a model to merge them into families and applies that answer
(``taxonomy``), then reduces the tagged records per family (``families``). The model calls, prompts and corpus belong
to the consumer; this package holds only pure functions over plain data (standard library only).
"""
from __future__ import annotations

from .batches import Batch, stable_batches
from .families import family_stats
from .labels import LabelCount, label_inventory, normalize_label
from .taxonomy import OTHER, Family, Taxonomy, TaxonomyError

__all__ = [
    "OTHER",
    "Batch",
    "Family",
    "LabelCount",
    "Taxonomy",
    "TaxonomyError",
    "family_stats",
    "label_inventory",
    "normalize_label",
    "stable_batches",
]

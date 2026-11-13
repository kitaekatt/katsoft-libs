"""Open-label mining over a small made-up corpus with katsoft-llm-kit.

Run it in a plain Python environment; it needs no AI harness, model, network or extras:

    python -m venv .venv
    .venv/bin/python -m pip install katsoft-llm-kit
    .venv/bin/python mine_labels.py

A real analysis gets the tags and the taxonomy from model calls. Here both are written out, so the script shows the data
steps around those calls: stable batches, the label inventory, the taxonomy check and the per-family stats. It exits
non-zero with a message when a result is not the expected one.
"""

from __future__ import annotations

import sys

from llm_kit.pipeline.analysis.mining import Taxonomy, family_stats, label_inventory, stable_batches

NOTES = [
    {"id": "n1", "thread": "t1", "text": "Please reset my password"},
    {"id": "n2", "thread": "t1", "text": "Still locked out, reset again?"},
    {"id": "n3", "thread": "t2", "text": "Where is my invoice for March?"},
]
# What a tagging call might answer for the notes: open labels with details.
TAGS = [
    {"note": "n1", "thread": "t1", "label": "Reset password", "details": {"channel": "email"}},
    {"note": "n2", "thread": "t1", "label": "Reset password.", "details": {"channel": "email"}},
    {"note": "n3", "thread": "t2", "label": "find invoice", "details": {"month": "March"}},
]
# What a taxonomy call might answer for the inventory.
TAXONOMY = {"families": [
    {"id": "account", "name": "Account access", "definition": "Getting back into an account.",
     "labels": ["reset password", "password reset"]},
    {"id": "billing", "name": "Billing", "definition": "Invoices and payments.", "labels": ["find invoice"]},
]}


def main() -> int:
    batches = stable_batches(NOTES, group=lambda n: n["thread"], ident=lambda n: n["id"], size=10)
    inventory = label_inventory(t["label"] for t in TAGS)
    taxonomy = Taxonomy.from_answer(TAXONOMY)
    stats = family_stats(TAGS, family_of=lambda t: taxonomy.family_of(t["label"]), dims=["note", "thread"],
                         details="details", totals={"note": len(NOTES)})
    for entry in stats:
        print(f"{entry['family']}: {entry['records']} tags, {entry['distinct']['note']} notes, details {entry['details']}")
    expected = ([b.group for b in batches], [c.label for c in inventory][0], [e["family"] for e in stats])
    if expected != (["t1", "t2"], "reset password", ["account", "billing"]):
        print(f"unexpected result: {expected}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

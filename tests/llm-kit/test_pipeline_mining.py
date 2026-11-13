"""llm_kit.pipeline.analysis.mining: stable batches, label inventory, taxonomy and family stats."""
from __future__ import annotations

import pytest

from llm_kit.pipeline.analysis.mining.batches import KEY_CHARS, batch_key
from llm_kit.pipeline.analysis.mining import (
    OTHER, Taxonomy, TaxonomyError, family_stats, label_inventory, normalize_label, stable_batches,
)

ITEMS = [{"id": f"{g}{n}", "g": g} for g in "ab" for n in range(5)]


def batches(items, size=2):
    return stable_batches(items, group=lambda i: i["g"], ident=lambda i: i["id"], size=size)


def test_batches_group_in_order_and_chunk():
    out = batches(ITEMS)
    assert [(b.group, b.index, [i["id"] for i in b.items]) for b in out] == [
        ("a", 0, ["a0", "a1"]), ("a", 1, ["a2", "a3"]), ("a", 2, ["a4"]),
        ("b", 0, ["b0", "b1"]), ("b", 1, ["b2", "b3"]), ("b", 2, ["b4"])]


def test_appending_to_a_group_keeps_every_full_chunk_key():
    before = {b.key for b in batches(ITEMS)}
    after = {b.key for b in batches(ITEMS + [{"id": "a5", "g": "a"}])}
    assert len(before - after) == 1          # only a's last chunk (a4) changed
    assert len(after - before) == 1          # it became a4, a5


def test_batches_refuse_a_repeated_id_and_a_bad_size():
    with pytest.raises(ValueError, match="occurs twice"):
        batches(ITEMS + [ITEMS[0]])
    with pytest.raises(ValueError, match="at least 1"):
        batches(ITEMS, size=0)


def test_labels_normalize_and_count():
    assert normalize_label('  Change   Scene. ') == "change scene"
    with pytest.raises(ValueError):
        normalize_label(" .. ")
    inv = label_inventory(["Skip time", "change scene", "skip time.", "ask for photo"])
    assert [(c.label, c.count) for c in inv] == [("skip time", 2), ("ask for photo", 1), ("change scene", 1)]


ANSWER = {"families": [
    {"id": "scene", "name": "Change the scene", "definition": "where or when", "labels": ["Change scene", "skip time"]},
    {"id": "media", "name": "Ask for media", "definition": "a picture", "labels": ["ask for photo"]},
]}


def test_taxonomy_maps_labels_and_reports_coverage():
    tax = Taxonomy.from_answer(ANSWER)
    assert tax.family_of("change  scene") == "scene"
    assert tax.family_of("hug") == OTHER
    assert tax.coverage([("skip time", 3), ("hug", 1)]) == 0.75


def test_taxonomy_lists_every_fault():
    bad = {"families": [ANSWER["families"][0], {"id": "scene", "name": "x", "definition": "y", "labels": []},
                        {"id": "dup", "name": "x", "definition": "y", "labels": ["skip time"]}, {"id": "z"}]}
    with pytest.raises(TaxonomyError) as caught:
        Taxonomy.from_answer(bad)
    message = str(caught.value)
    assert "'scene' is reserved or repeated" in message
    assert "'skip time' is in 'scene' and 'dup'" in message
    assert "family 3: missing name, definition, labels" in message
    with pytest.raises(TaxonomyError, match="no families"):
        Taxonomy.from_answer({"families": []})


def test_family_stats_counts_distinct_flags_and_details():
    rows = [
        {"label": "skip time", "turn": 1, "who": "p1", "steer": False, "details": {"time": "Next morning"}},
        {"label": "change scene", "turn": 2, "who": "p1", "steer": True, "details": {"place": "Kitchen", "time": "next morning"}},
        {"label": "skip time", "turn": 2, "who": "p2", "steer": False, "details": {"time": "later"}},
        {"label": "hug", "turn": 3, "who": "p2", "steer": False, "details": {}},
    ]
    tax = Taxonomy.from_answer(ANSWER)
    stats = family_stats(rows, family_of=lambda r: tax.family_of(r["label"]), dims=["turn", "who"], flags=["steer"],
                         details="details", top=2, totals={"turn": 4, "who": 2})
    scene = stats[0]
    assert scene["family"] == "scene" and scene["records"] == 3
    assert scene["distinct"] == {"turn": 2, "who": 2} and scene["share"] == {"turn": 0.5, "who": 1.0}
    assert scene["flags"] == {"steer": 1 / 3}
    assert scene["details"]["time"] == {"count": 3, "top": [["next morning", 2], ["later", 1]]}
    assert scene["details"]["place"] == {"count": 1, "top": [["kitchen", 1]]}
    assert stats[1]["family"] == OTHER


def test_family_stats_refuses_a_non_mapping_details_field():
    with pytest.raises(TypeError, match="must be a mapping"):
        family_stats([{"turn": 1, "details": "x"}], family_of=lambda r: "f", details="details")


def test_batch_key_depends_on_ids_and_their_order():
    assert len(batch_key(["a", "b"])) == KEY_CHARS
    assert batch_key(["a", "b"]) == batch_key(["a", "b"]) != batch_key(["b", "a"])
    with pytest.raises(ValueError, match="at least one"):
        batch_key([])

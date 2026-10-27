"""Definition hashes (Q17): what changes a node's hash and what does not."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

import graph_support as gs
from llm_kit.graph.hashing import hash_graph, node_hash
from llm_kit.graph.loader import load_graph
from llm_kit.graph.model import Op, UnknownOpError

HEX64 = re.compile(r"^[0-9a-f]{64}$")


@pytest.fixture()
def beta(tmp_path) -> Path:
    root = tmp_path / "beta"
    shutil.copytree(gs.BETA, root)
    return root


def _stage(root: Path, registry=None):
    graph = load_graph(root / "stages" / "story-structure.graph.yaml")
    return hash_graph(graph, registry or gs.registry())


def _edit(path: Path, old: str, new: str) -> None:
    text = path.read_text(encoding="utf-8")
    assert old in text, old
    path.write_text(text.replace(old, new), encoding="utf-8")


def test_hashes_are_full_sha256_and_stable_across_loads(beta):
    first, second = _stage(beta), _stage(beta)
    assert first == second
    assert HEX64.match(first.graph) and all(HEX64.match(h) for h in first.nodes.values())
    assert set(first.nodes) == {"baseline", "loop", "keep"}
    assert set(first.bodies) == {"baseline", "loop"}
    assert set(first.bodies["loop"].nodes) == {"prep", "write", "check", "repair", "accept", "evaluate"}
    assert first.bodies["baseline"] == first.bodies["loop"].bodies["evaluate"]


def test_the_hash_does_not_depend_on_the_checkout_location(beta, tmp_path):
    other = tmp_path / "elsewhere" / "beta"
    shutil.copytree(gs.BETA, other)
    assert _stage(beta) == _stage(other)


def test_yaml_layout_and_key_order_do_not_count(beta):
    path = beta / "stages" / "story-structure.graph.yaml"
    _edit(path, "    in: {incumbent: inputs.incumbent, baseline: baseline.out, rounds: loop.rounds}",
          "    in:\n      rounds: loop.rounds\n      baseline: baseline.out\n      incumbent: inputs.incumbent  # c")
    assert _stage(beta) == _stage(gs.BETA)


def test_a_prompt_body_edit_changes_only_the_nodes_that_send_it(beta):
    before = _stage(beta)
    _edit(beta / "prompts" / "repair.md", "Fix these errors:", "Fix every error:")
    after = _stage(beta)
    changed = {k for k in before.bodies["loop"].nodes if before.bodies["loop"].nodes[k] != after.bodies["loop"].nodes[k]}
    assert changed == {"repair"}
    assert before.nodes["loop"] != after.nodes["loop"] and before.graph != after.graph
    assert before.nodes["keep"] == after.nodes["keep"] and before.nodes["baseline"] == after.nodes["baseline"]


def test_a_prompt_header_edit_changes_nothing(beta):
    before = _stage(beta)
    _edit(beta / "prompts" / "repair.md", "Fixture repair prompt.", "Fixture repair prompt, rewritten header.")
    assert _stage(beta) == before


def test_an_op_version_changes_the_nodes_that_call_it(beta):
    before = _stage(beta)
    bumped = gs.registry()
    old = bumped.get("afterplay.beta.keep_best")
    bumped._ops[old.name] = Op(old.name, "2", old.fn, inputs=old.inputs, outputs=old.outputs)
    after = _stage(beta, bumped)
    assert after.nodes["keep"] != before.nodes["keep"]
    assert after.nodes["loop"] == before.nodes["loop"] and after.nodes["baseline"] == before.nodes["baseline"]


def test_a_schema_file_edit_reaches_ports_params_and_subgraphs(beta):
    before = _stage(beta)
    _edit(beta / "contracts" / "plot-evaluation.v1.schema.json", '"maximum": 10', '"maximum": 9')
    after = _stage(beta)
    assert after.bodies["baseline"].nodes["judge"] != before.bodies["baseline"].nodes["judge"]
    assert after.nodes["baseline"] != before.nodes["baseline"]        # its subgraph hash changed
    assert after.nodes["loop"] != before.nodes["loop"]                # the body runs that subgraph too
    assert after.nodes["keep"] == before.nodes["keep"]                # keep's op schemas are registry values
    assert after.graph != before.graph


def test_human_and_commit_nodes(beta):
    registry = gs.registry()
    before = hash_graph(load_graph(beta / "beta.graph.yaml"), registry)
    _edit(beta / "contracts" / "played.v1.schema.json", '"boolean"', '"string"')
    after = hash_graph(load_graph(beta / "beta.graph.yaml"), registry)
    assert after.nodes["played"] != before.nodes["played"] and after.nodes["encode"] == before.nodes["encode"]
    old = registry.get("afterplay.beta.encode")
    registry._ops[old.name] = Op(old.name, "9", old.fn, inputs=old.inputs, outputs=old.outputs, commit=True)
    bumped = hash_graph(load_graph(beta / "beta.graph.yaml"), registry)
    assert bumped.nodes["encode"] != after.nodes["encode"]


def test_node_hash_and_unknown_names(beta):
    graph = load_graph(beta / "stages" / "story-structure.graph.yaml")
    registry = gs.registry()
    assert node_hash(graph, "keep", registry) == hash_graph(graph, registry).nodes["keep"]
    with pytest.raises(KeyError, match="no node 'nope'"):
        node_hash(graph, "nope", registry)
    registry._ops.pop("afterplay.beta.keep_best")
    with pytest.raises(UnknownOpError):
        hash_graph(graph, registry)

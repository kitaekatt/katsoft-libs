"""Definition hashes of graph nodes and graphs (Q17).

A node's definition hash covers what the node IS, not what flows into it:

- the node mapping as written (canonical JSON, so key order and YAML layout
  do not count);
- ``name@version`` of every operation it calls (an op node's op, a commit
  adapter, and every op inside an inline body);
- for an op parameter: a ``schema`` as resolved (file references bundled), a
  ``file`` as bytes, a ``template`` as its prompt text (the part that is sent;
  an edit to the header above the marker line does not count);
- a human schema and a commit contract as resolved;
- the graph hash of every subgraph and inline body it runs.

A graph hash covers the graph mapping without its nodes, its resolved input
and output schemas, and every node's definition hash. Inputs (the values that
flow into a node) are not part of either hash; the runner hashes them apart.

Every hash is a full 64-hex SHA-256 (``freshness.hashing.digest``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from llm_kit.pipeline.artifacts.freshness.hashing import FULL_DIGEST_LENGTH, digest, stable_json

from .loader import resolve_params
from .model import Graph, Node, OpRegistry

__all__ = ["GRAPH_HASH_VERSION", "GraphHashes", "hash_graph", "node_hash"]

GRAPH_HASH_VERSION = b"llm-kit.graph.hash/v1"


@dataclass(frozen=True)
class GraphHashes:
    """The hash of one graph, its nodes' definition hashes, and its bodies' hashes.

    ``bodies`` maps a node id to the hashes of the graph that node runs (a
    subgraph file, or a map or repeat body).
    """

    graph: str
    nodes: Mapping[str, str]
    bodies: Mapping[str, "GraphHashes"] = field(default_factory=dict)


def _ports_json(ports: Mapping) -> dict:
    return {name: port.to_json() for name, port in ports.items()}


class _Hasher:
    def __init__(self, registry: OpRegistry) -> None:
        self.registry = registry
        self.memo: dict[int, GraphHashes] = {}

    def graph(self, graph: Graph) -> GraphHashes:
        key = id(graph)
        if key in self.memo:
            return self.memo[key]
        nodes: dict[str, str] = {}
        bodies: dict[str, GraphHashes] = {}
        for node_id, node in graph.nodes.items():
            body_hashes = [self.graph(body) for body in node.bodies()]
            if body_hashes:
                bodies[node_id] = body_hashes[0]
            nodes[node_id] = self.node(graph, node, body_hashes)
        head = {k: v for k, v in graph.raw.items() if k != "nodes"}
        parts = [
            GRAPH_HASH_VERSION,
            stable_json(head),
            stable_json({"inputs": _ports_json(graph.inputs), "outputs": _ports_json(graph.outputs)}),
        ]
        for node_id in sorted(nodes):
            parts.append(stable_json([node_id, nodes[node_id]]))
        result = GraphHashes(digest(*parts, length=FULL_DIGEST_LENGTH), nodes, bodies)
        self.memo[key] = result
        return result

    def node(self, graph: Graph, node: Node, body_hashes: list[GraphHashes]) -> str:
        parts: list[bytes] = [GRAPH_HASH_VERSION, b"node", stable_json(node.raw)]
        if node.kind == "op":
            op = self.registry.get(node.op)
            parts.append(op.ref.encode("utf-8"))
            for name, param in sorted(resolve_params(node, op, graph).items()):
                if param.kind == "schema":
                    parts.append(stable_json([name, param.schema]))
                elif param.kind == "file":
                    parts.extend([name.encode("utf-8"), param.data])
                elif param.kind == "template":
                    parts.extend([name.encode("utf-8"), param.body.encode("utf-8")])
        elif node.kind == "human":
            parts.append(stable_json(node.human.schema))
        elif node.kind == "commit":
            parts.append(stable_json(node.commit.contract))
            parts.append(self.registry.get(node.commit.adapter).ref.encode("utf-8"))
        for hashes in body_hashes:
            parts.append(hashes.graph.encode("ascii"))
        return digest(*parts, length=FULL_DIGEST_LENGTH)


def hash_graph(graph: Graph, registry: OpRegistry) -> GraphHashes:
    """Hashes of ``graph``, its nodes and (recursively) the graphs they run.

    Raises :class:`llm_kit.graph.model.GraphError` when an op is unknown or a
    parameter cannot be resolved; validate the graph first.
    """
    return _Hasher(registry).graph(graph)


def node_hash(graph: Graph, node_id: str, registry: OpRegistry) -> str:
    """The definition hash of one node of ``graph``."""
    if node_id not in graph.nodes:
        raise KeyError(f"{graph.id} has no node {node_id!r}")
    return hash_graph(graph, registry).nodes[node_id]

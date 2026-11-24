"""Label two small graph files and draw them as one HTML page with katsoft-llm-kit, with no model.

Run it in a plain Python environment; it needs no AI harness, plugin, client
app, op registry, model or network:

    python -m venv .venv
    .venv/bin/python -m pip install "katsoft-llm-kit[graph]"
    .venv/bin/python label_graphs.py

``llm-kit graph render`` labels a page by default: a model reads the graph files
and writes a title and description for each node, a text for each edge and a
gloss for each input and output (``label_catalog``). This script shows the whole
path with a canned labeller in place of the model, so it makes no call and
spends nothing. To ask a model instead, replace ``CannedLabeller()`` with

    from llm_kit.graph.render import model_labeller
    labeller = model_labeller(project_root=root)

which asks ``haiku`` under the user's consent (``llm-kit consent set --scope
graph-labels ...``) and sends the graph definitions and the first lines of each
prompt file to the model's provider.

The script writes an outer graph and the subgraph file it runs into a temporary
directory, builds their catalog, labels it, checks the result against the
version 2 catalog JSON Schema, and writes labelled-graphs.html in the current
directory. Open that file in a browser: the labels are drawn in purple italic,
and the Labels button hides them. It exits non-zero with a message when a check
fails.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from llm_kit.graph.render import build_catalog, label_catalog, write_page
from llm_kit.graph.render.label_types import LabelCall, LabelReply
from llm_kit.schema.formats import GRAPH_CATALOG_V2, load_schema
from llm_kit.schema.json_schema import validate

OUTER = """\
id: example.article
description: Draft an article, then polish it.
inputs:
  topic: {schema: {type: string}, description: what the article is about}
outputs:
  article: {schema: {type: object}}
nodes:
  outline:
    op: example.outline
    in: {topic: inputs.topic}
  draft:
    graph: ./draft.graph.yaml
    in: {outline: outline.out}
  polish:
    op: example.polish
    in: {text: draft.out.text}
outputs_from:
  article: polish.out
"""

DRAFT = """\
id: example.draft
description: Write a draft and check it.
inputs:
  outline: {schema: {type: object}}
outputs:
  text: {schema: {type: string}}
nodes:
  write:
    op: example.write
    in: {outline: inputs.outline}
  check:
    op: example.check
    in: {text: write.out}
outputs_from:
  text: write.out
"""


class CannedLabeller:
    """Answers every call from the keys it asks for; a stand-in for ``model_labeller``."""

    declaration = ("canned",)

    def __call__(self, call: LabelCall) -> LabelReply:
        answer = {
            "nodes": {path: {"title": path.rsplit("/", 1)[-1].capitalize() + " step",
                             "description": f"Runs the {path} step of the graph."} for path in call.nodes},
            "edges": {edge: "its result" for edge in call.edges},
        }
        if call.header:
            answer["graph"] = {"title": "Article graph", "purpose": "Turns a topic into a polished article."}
            answer["inputs"] = {name: f"The {name} given to the graph." for name in call.inputs}
            answer["outputs"] = {name: f"The {name} the graph produces." for name in call.outputs}
        return LabelReply(answer, "canned", "canned-1")


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "article.graph.yaml").write_text(OUTER, encoding="utf-8")
        (root / "draft.graph.yaml").write_text(DRAFT, encoding="utf-8")
        catalog = build_catalog({"article": root / "article.graph.yaml"}, root=root, project="example")
        labelled = label_catalog(catalog, CannedLabeller(), root=root, progress=print)
    errors = validate(load_schema(GRAPH_CATALOG_V2), json.loads(json.dumps(labelled)))
    if errors:
        raise SystemExit(f"the labelled catalog does not match its schema: {errors}")
    labels = labelled["labels"]
    if sorted(labels) != ["article.graph.yaml", "draft.graph.yaml"] or sorted(labels["draft.graph.yaml"]["nodes"]) != [
            "check", "write"]:
        raise SystemExit(f"unexpected labels: {sorted(labels)}")
    page = write_page(labelled, Path("labelled-graphs.html"))
    print(f"wrote {page} ({len(labels)} graph files labelled)")


if __name__ == "__main__":
    main()

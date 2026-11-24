"""Draw two small graph files as one HTML page with katsoft-llm-kit.

Run it in a plain Python environment; it needs no AI harness, plugin, client
app, op registry or network:

    python -m venv .venv
    .venv/bin/python -m pip install "katsoft-llm-kit[graph]"
    .venv/bin/python render_graphs.py

The script writes an outer graph and the subgraph file it runs into a
temporary directory, builds their catalog, checks it against the catalog's
JSON Schema, and writes graphs.html in the current directory. Open that file in
a browser: the list shows the outer graph and the file it runs, and a double
click on the "draft" node moves into the subgraph. It exits non-zero with a
message when a check fails.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from llm_kit.graph.render import build_catalog, write_page
from llm_kit.schema.formats import GRAPH_CATALOG_V1, load_schema
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


def main() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "article.graph.yaml").write_text(OUTER, encoding="utf-8")
        (root / "draft.graph.yaml").write_text(DRAFT, encoding="utf-8")
        catalog = build_catalog({"article": root / "article.graph.yaml"}, root=root, project="example")
    errors = validate(load_schema(GRAPH_CATALOG_V1), json.loads(json.dumps(catalog)))
    if errors:
        raise SystemExit(f"the catalog does not match its schema: {errors}")
    if sorted(catalog["files"]) != ["article.graph.yaml", "draft.graph.yaml"]:
        raise SystemExit(f"unexpected graph files: {sorted(catalog['files'])}")
    page = write_page(catalog, Path("graphs.html"))
    print(f"wrote {page} ({len(catalog['files'])} graph files)")


if __name__ == "__main__":
    main()

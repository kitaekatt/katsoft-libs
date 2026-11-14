"""Record two model attempts in a run store, import one more, and query the index.

Run it in a plain Python environment; it needs no AI harness, plugin, client
app, endpoint registry, network or extra:

    python -m venv .venv
    .venv/bin/python -m pip install katsoft-llm-kit
    .venv/bin/python record_runs.py

The script makes a store in a temporary directory that accepts synthetic and
internal content, writes a started event and a result for one attempt, imports
a second attempt from a JSON Lines import file, shows that the store refuses a
sensitive record, and prints the index rows. It makes no model call. It exits
non-zero with a message when a check fails.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from llm_kit.runs import (
    RUN_IMPORT_V1,
    RunRecordStore,
    RunStoreError,
    blob_ref,
    finish_record,
    import_file,
    reuse_key,
    start_record,
)

QUESTION = "What is two plus two?"


def attempt(model: str, answer: str, content: str = "synthetic") -> tuple[dict, dict]:
    """A started event and an ok result for one answer of ``model``."""
    started = start_record(
        flow="arithmetic.answer",
        group={"id": "arithmetic-compare", "kind": "compare", "label": "two models, one question"},
        case={"id": "two-plus-two", "input_sha256": blob_ref(QUESTION)[len("blob:"):], "input": blob_ref(QUESTION)},
        selection={"model": model},
        request={"max_output_tokens": 50},
        content=content,
        tool={"name": "record_runs.py", "version": "1"},
    )
    result = finish_record(
        started, status="ok", output={"text": blob_ref(answer)},
        usage={"input_tokens": 7, "output_tokens": 2, "cached_tokens": None, "reasoning_tokens": None},
        cost={"usd": 0.0, "source": "local"},
    )
    return started, result


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        store = RunRecordStore.init(Path(directory) / "runs", accepts=["synthetic", "internal"])

        started, result = attempt("example/model-a", "4")
        store.put(started, {blob_ref(QUESTION): QUESTION})
        if store.get(started["id"])["status"] != "started":
            raise SystemExit("an unfinished attempt is not visible as started")
        store.put(result, {blob_ref("4"): "4"})

        _, other = attempt("example/model-b", "Four.")
        line = {"format": RUN_IMPORT_V1, "record": other,
                "blobs": {blob_ref(QUESTION): {"text": QUESTION}, blob_ref("Four."): {"text": "Four."}}}
        import_path = Path(directory) / "run.jsonl"
        import_path.write_text(json.dumps(line) + "\n", encoding="utf-8")
        report = import_file(store, import_path)
        print(f"imported {report.events_written} event(s), {report.blobs_written} new blob(s)")

        _, private = attempt("example/model-a", "4", content="sensitive")
        try:
            store.put(private, {blob_ref(QUESTION): QUESTION, blob_ref("4"): "4"})
        except RunStoreError as exc:
            print(f"refused as expected ({exc.code}): {exc}")
        else:
            raise SystemExit("the store took a sensitive record its policy does not accept")

        rows = store.index().query(group="arithmetic-compare")
        for row in rows:
            answer = store.get_blob(store.get(row["id"])["output"]["text"]).decode("utf-8")
            print(f"{row['model']}: {answer!r} status={row['status']} key={row['reuse_key'][:12]}")
        if len(rows) != 2 or rows[0]["reuse_key"] == rows[1]["reuse_key"]:
            raise SystemExit("expected two records with different reuse keys")
        if reuse_key(result) != rows[0]["reuse_key"]:
            raise SystemExit("the index key differs from reuse_key()")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

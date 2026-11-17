"""The fallback chain and call record formats: the shared cases against the Python reader and the schemas.

The cases in ``tests/formats/fallback-chain/cases.json`` and
``tests/formats/call-record/cases.json`` are the contract both readers run
(``@katsoft/llm-kit-send`` runs the same files in its tests).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from llm_kit.registry import fallback_chains as fc
from llm_kit.schema import formats
from llm_kit.schema.json_schema import validate

REPO = Path(__file__).resolve().parents[2]
CHAIN_CASES = json.loads((REPO / "tests" / "formats" / "fallback-chain" / "cases.json").read_text(encoding="utf-8"))
RECORD_CASES = json.loads((REPO / "tests" / "formats" / "call-record" / "cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CHAIN_CASES["documents"], ids=lambda case: case["name"])
def test_the_reader_accepts_and_refuses_each_shared_document(case):
    if case["expect"] == "valid":
        assert validate(formats.load_schema(formats.FALLBACK_CHAIN_V1), case["document"]) == ()
        assert fc.check_fallback_chains(case["document"]) is case["document"]
    else:
        with pytest.raises(fc.FallbackChainError) as caught:
            fc.check_fallback_chains(case["document"])
        assert str(caught.value).startswith(caught.value.path)


@pytest.mark.parametrize("case", CHAIN_CASES["mappings"], ids=lambda case: case["name"])
def test_map_input_gives_each_shared_result_in_rule_order(case):
    rules_document = {"format": formats.FALLBACK_CHAIN_V1, "chains": {}, "models": {"m": {"hosts": {
        "a": {"endpoint": "e"}, "b": {"endpoint": "e", "from": "a", "input": case["rules"]}}}}}
    fc.check_fallback_chains(rules_document)
    assert json.dumps(fc.map_input(case["rules"], case["input"]), separators=(",", ":")) == case["json"]


def test_read_fallback_chains_reads_a_file_and_names_a_file_that_is_not_json(tmp_path):
    good = next(case for case in CHAIN_CASES["documents"] if case["name"] == "voice-chain")["document"]
    path = tmp_path / "chains.json"
    path.write_text(json.dumps(good), encoding="utf-8")
    assert fc.read_fallback_chains(path) == good
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(fc.FallbackChainError, match=r"^\$: not JSON"):
        fc.read_fallback_chains(path)


@pytest.mark.parametrize("case", RECORD_CASES["records"], ids=lambda case: case["name"])
def test_each_shared_call_record_against_the_schema(case):
    errors = validate(formats.load_schema(formats.CALL_RECORD_V1), case["record"])
    assert (errors == ()) is (case["expect"] == "valid"), errors


def test_the_module_names_have_docstrings():
    for item in (fc.FallbackChainError, fc.check_fallback_chains, fc.map_input, fc.read_fallback_chains):
        assert item.__doc__ and len(item.__doc__.strip()) > 40, item

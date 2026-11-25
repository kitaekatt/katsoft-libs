"""The paid call record format: the shared cases against the schema.

``tests/formats/paid-call/cases.json`` is the contract both readers run
(``@katsoft/llm-kit-send`` runs the same file in ``test/paid-call-format.test.mjs``).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_kit.schema import formats
from llm_kit.schema.json_schema import validate

REPO = Path(__file__).resolve().parents[2]
CASES = json.loads((REPO / "tests" / "formats" / "paid-call" / "cases.json").read_text(encoding="utf-8"))


def test_the_cases_name_their_format_and_schema():
    assert CASES["format"] == formats.PAID_CALL_V1 == "llm-kit.paid-call/v1"
    here = REPO / "tests" / "formats" / "paid-call"
    assert (here / CASES["schema"]).resolve().name == formats.SCHEMA_FILES[formats.PAID_CALL_V1]
    assert CASES["description"].isascii()


@pytest.mark.parametrize("case", CASES["records"], ids=lambda case: case["name"])
def test_each_shared_paid_call_record_against_the_schema(case):
    errors = validate(formats.load_schema(formats.PAID_CALL_V1), case["record"])
    assert (errors == ()) is (case["expect"] == "valid"), errors


def test_the_cases_hold_both_outcomes_for_each_phase():
    seen = {(case["record"].get("phase"), case["expect"]) for case in CASES["records"]}
    assert {("begin", "valid"), ("begin", "invalid"), ("end", "valid"), ("end", "invalid")} <= seen

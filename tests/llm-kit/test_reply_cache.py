"""The reply cache formats: the shared key and entry cases against the Python reader and the schemas.

``tests/formats/reply-key/cases.json``, ``tests/formats/reply-entry/cases.json``
and ``tests/formats/call-record/cases.v2.json`` are the contract both readers
run (``@katsoft/llm-kit-send`` runs the same files in its tests).
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from llm_kit.registry import reply_cache as rc
from llm_kit.schema import formats
from llm_kit.schema.json_schema import validate

REPO = Path(__file__).resolve().parents[2]
KEY_CASES = json.loads((REPO / "tests" / "formats" / "reply-key" / "cases.json").read_text(encoding="utf-8"))
ENTRY_CASES = json.loads((REPO / "tests" / "formats" / "reply-entry" / "cases.json").read_text(encoding="utf-8"))
RECORD_V2_CASES = json.loads((REPO / "tests" / "formats" / "call-record" / "cases.v2.json").read_text(encoding="utf-8"))


def _part(part: dict) -> str | bytes:
    return part["text"] if "text" in part else base64.b64decode(part["base64"])


def _body(body):
    if body is None:
        return None
    if "text" in body:
        return body["text"]
    if "base64" in body:
        return base64.b64decode(body["base64"])
    return [rc.FormPart(name=p["name"], data=p["text"].encode("utf-8")) if "text" in p
            else rc.FormPart(name=p["name"], data=base64.b64decode(p["base64"]), filename=p["filename"], type=p["type"])
            for p in body["form"]]


def _key_of(case: dict) -> str:
    request = case["request"]
    return rc.step_key(case["account"], request["method"], request["url"], request["headers"], _body(request["body"]),
                       case.get("scope", ()))


def test_the_cases_name_their_formats():
    assert KEY_CASES["format"] == rc.REPLY_KEY_FORMAT
    assert ENTRY_CASES["format"] == rc.REPLY_ENTRY_FORMAT == formats.REPLY_ENTRY_V1
    assert RECORD_V2_CASES["format"] == formats.CALL_RECORD_V2
    here = REPO / "tests" / "formats"
    assert (here / "reply-entry" / ENTRY_CASES["schema"]).resolve().name == formats.SCHEMA_FILES[formats.REPLY_ENTRY_V1]
    assert (here / "call-record" / RECORD_V2_CASES["schema"]).resolve().name == formats.SCHEMA_FILES[formats.CALL_RECORD_V2]


@pytest.mark.parametrize("case", KEY_CASES["parts"], ids=lambda case: case["name"])
def test_request_key_gives_each_shared_key(case):
    assert rc.request_key([_part(part) for part in case["parts"]]) == case["key"]


@pytest.mark.parametrize("case", KEY_CASES["requests"], ids=lambda case: case["name"])
def test_step_key_gives_each_shared_key(case):
    assert _key_of(case) == case["key"]


def test_the_same_and_different_groups_hold():
    keys = {case["name"]: case["key"] for case in KEY_CASES["requests"] + KEY_CASES["parts"]}
    for group in KEY_CASES["same"]:
        assert len({keys[name] for name in group}) == 1, group
    for first, second in KEY_CASES["different"]:
        assert keys[first] != keys[second], (first, second)


def test_a_secret_never_changes_a_key_and_a_bad_form_entry_is_refused():
    base = rc.step_key("a", "POST", "https://h.example/x", {"Content-Type": "application/json"}, "{}")
    for name in ("Authorization", "api-key", "x-api-key", "xi-api-key", "X-Feature"):
        assert rc.step_key("a", "POST", "https://h.example/x", {"Content-Type": "application/json", name: "SECRET"}, "{}") == base
    with pytest.raises(TypeError, match="FormPart"):
        rc.step_key("a", "POST", "https://h.example/x", {}, [("prompt", b"x")])


@pytest.mark.parametrize("case", ENTRY_CASES["entries"], ids=lambda case: case["name"])
def test_each_shared_entry_against_the_schema_and_the_reader(case):
    errors = validate(formats.load_schema(formats.REPLY_ENTRY_V1), case["entry"])
    assert (errors == ()) is (case["expect"] == "valid"), errors
    if case["expect"] == "valid":
        assert rc.read_entry(case["entry"]) == case["entry"]
    else:
        with pytest.raises(rc.ReplyEntryError) as caught:
            rc.read_entry(case["entry"])
        assert caught.value.path == case["path"]
        assert str(caught.value).startswith(case["path"])


@pytest.mark.parametrize("case", ENTRY_CASES["reader_rules"], ids=lambda case: case["name"])
def test_each_reader_rule_refuses_what_the_schema_accepts(case):
    assert validate(formats.load_schema(formats.REPLY_ENTRY_V1), case["entry"]) == ()
    with pytest.raises(rc.ReplyEntryError) as caught:
        rc.read_entry(case["entry"])
    assert caught.value.path == case["path"]


@pytest.mark.parametrize("case", RECORD_V2_CASES["records"], ids=lambda case: case["name"])
def test_each_shared_v2_call_record_against_the_schema(case):
    errors = validate(formats.load_schema(formats.CALL_RECORD_V2), case["record"])
    assert (errors == ()) is (case["expect"] == "valid"), errors


def test_every_public_name_has_a_docstring():
    for name in rc.__all__:
        item = getattr(rc, name)
        if callable(item):
            assert item.__doc__ and len(item.__doc__.strip()) > 40, name

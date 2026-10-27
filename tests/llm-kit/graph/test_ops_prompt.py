"""Strict prompt-template filling (llm_kit.graph.prompt)."""

from __future__ import annotations

import pytest

from llm_kit.graph.model import GraphError
from llm_kit.graph.prompt import PromptError, render_prompt, render_prompt_file

TEMPLATE = """\
A header for people that names {{header_only}}.
---PROMPT---
Write {{kind}} about {{topic}}.
Again: {{kind}}.
"""


def test_fills_every_use_of_a_slot_and_drops_the_header():
    out = render_prompt(TEMPLATE, {"kind": "a poem", "topic": "tea"})
    assert out == "Write a poem about tea.\nAgain: a poem.\n"


def test_a_file_without_the_marker_is_all_prompt():
    assert render_prompt("Hi {{who}}", {"who": "you"}) == "Hi you"


def test_suffix_is_appended_as_written():
    assert render_prompt("A{{x}}", {"x": "B"}, suffix="\n\nC") == "AB\n\nC"
    assert render_prompt("A", {}, suffix="") == "A"


def test_a_slot_without_a_value_is_an_error():
    with pytest.raises(PromptError, match=r"slots with no value: \['topic'\]"):
        render_prompt(TEMPLATE, {"kind": "x"})


def test_a_value_without_a_slot_is_an_error():
    with pytest.raises(PromptError, match=r"values with no slot: \['extra', 'header_only'\]"):
        render_prompt(TEMPLATE, {"kind": "x", "topic": "y", "extra": "z", "header_only": "w"})


def test_both_errors_are_reported_together():
    with pytest.raises(PromptError) as caught:
        render_prompt("{{a}}", {"b": "x"})
    assert "slots with no value: ['a']" in str(caught.value)
    assert "values with no slot: ['b']" in str(caught.value)


@pytest.mark.parametrize("value", [None, 3, ["x"], {"a": 1}, True])
def test_a_value_must_be_a_string(value):
    with pytest.raises(PromptError, match="slot 'a' must be a string"):
        render_prompt("{{a}}", {"a": value})


def test_the_suffix_must_be_a_string():
    with pytest.raises(PromptError, match="suffix must be a string"):
        render_prompt("A", {}, suffix=3)


def test_values_are_inserted_once_and_not_expanded_again():
    assert render_prompt("{{a}} {{b}}", {"a": "{{b}}", "b": "B"}) == "{{b}} B"


def test_text_that_only_looks_like_a_slot_is_kept():
    assert render_prompt("{{ spaced }} {{Upper}} {single}", {}) == "{{ spaced }} {{Upper}} {single}"


def test_two_marker_lines_are_refused():
    with pytest.raises(GraphError, match="may have one"):
        render_prompt("a\n---PROMPT---\nb\n---PROMPT---\nc", {})


def test_render_prompt_file_reads_utf8(tmp_path):
    path = tmp_path / "p.md"
    path.write_text("caf\u00e9 {{x}}", encoding="utf-8")
    assert render_prompt_file(path, {"x": "\u00fc"}) == "caf\u00e9 \u00fc"


def test_render_prompt_file_names_the_file_in_an_error(tmp_path):
    path = tmp_path / "p.md"
    path.write_text("{{x}}", encoding="utf-8")
    with pytest.raises(PromptError, match="p.md"):
        render_prompt_file(path, {})
    with pytest.raises(PromptError, match="cannot read the template"):
        render_prompt_file(tmp_path / "missing.md", {})

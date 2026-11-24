"""The browser script of a rendered graph page, read as text.

The script draws a catalog, including the labels a small model inferred. Those labels are
text a model wrote, so the script must never turn text into markup (security property S7).
The behaviour of the drawing itself is checked by eye on a labelled page; these tests pin
what can be read from the files: no markup parsing, the Labels control and its stored
choice, and that labels are set as text.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from llm_kit.graph.render import page as page_module

STATIC = Path(page_module.__file__).resolve().parent / "static"
FILES = ("browser.js", "browser.css", "page.html")

# Every way a script can parse or run text as markup or code.
BANNED = re.compile(
    r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new Function"
    r"|setTimeout\(\s*[\"'`]|setInterval\(\s*[\"'`]|javascript:|\.srcdoc|createContextualFragment|DOMParser"
)


def _text(name: str) -> str:
    return (STATIC / name).read_text(encoding="utf-8")


@pytest.mark.parametrize("name", FILES)
def test_browser_script_never_parses_markup(name):
    assert not BANNED.search(_text(name)), name


def test_banned_pattern_catches_each_way_in():
    for sample in ("a.innerHTML = x", "a.outerHTML=x", "a.insertAdjacentHTML('beforeend', x)", "document.write(x)",
                   "eval (x)", "new Function(x)", "setTimeout('x()', 1)", "new DOMParser()", "r.createContextualFragment(x)"):
        assert BANNED.search(sample), sample


def test_labels_are_set_as_text_not_markup():
    script = _text("browser.js")
    # every node the script makes from a label gets its words through the text helpers
    assert "textContent" in script
    assert script.count("createTextNode") >= 1
    assert "html:" not in script and '"html"' not in script


def test_the_labels_control_and_its_stored_choice():
    html, script = _text("page.html"), _text("browser.js")
    assert 'id="labels"' in html and 'aria-pressed="true"' in html
    assert '"llm-kit-graph-labels"' in script
    assert 'localStorage.getItem(LABELS_KEY) !== "off"' in script
    assert 'localStorage.setItem(LABELS_KEY, state.labels ? "on" : "off")' in script
    assert "--inferred" in _text("browser.css")


def test_the_list_has_an_unused_group_with_load_errors_shown_as_text():
    script = _text("browser.js")
    assert "CATALOG.unused" in script
    assert '"Unused (" +' in script
    # a file that failed to load is drawn with its error text, set as text, and is not a link into a graph
    assert 'text: item.error' in script
    assert "unusedFiles()" in script and "unusedErrors()" in script
    assert ".item.bad" in _text("browser.css")


def test_a_repeat_header_shows_its_max_until_and_carry_as_mono_text():
    script, css = _text("browser.js"), _text("browser.css")
    assert "function repeatLines(" in script
    body = script[script.index("function repeatLines("):]
    body = body[:body.index("\n  }\n")]
    assert "node.detail" in body
    for field in ("max", "until", "carry"):
        assert re.search(r"\bd\." + field + r"\b", body), field
    # the header grows to hold those lines, and they are drawn in a mono class of their own
    assert "repeatLines(node).length" in script
    assert '"class": "cmono"' in script
    assert ".compound .cmono" in css and "monospace" in css[css.index(".compound .cmono"):].splitlines()[0]


@pytest.mark.parametrize("name", FILES)
def test_the_page_files_stay_ascii_and_name_no_other_product(name):
    text = _text(name)
    assert text.isascii(), name
    assert "floorplan" not in text.lower(), name


def test_the_script_keeps_to_the_old_syntax_of_its_file():
    script = _text("browser.js")
    assert not re.search(r"\b(let|const)\s", script)
    assert "=>" not in script
    assert "`" not in script

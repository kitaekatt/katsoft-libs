"""The one definition of the markup escapes for JSON text placed inside markup.

Text from a graph file, a prompt or a model can hold ``</script>``, ``<!--``
or the closing tag of a fence. Two places put JSON text next to markup: the
rendered page embeds the catalog in a script element, and the label prompt
fences its evidence in a tag. Both escape ``<``, ``>`` and ``&`` here, as JSON
``\\u`` escapes, so the escaped text is still valid JSON that decodes to the
same value and holds no character a markup parser reacts to.
"""

from __future__ import annotations

__all__ = ["MARKUP_ESCAPES", "SCRIPT_ESCAPES", "escape_markup", "escape_script_json"]

#: Character -> its JSON escape, for the characters a markup parser reacts to.
MARKUP_ESCAPES = (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026"))

#: The markup escapes plus U+2028 and U+2029, which older script parsers read as line ends.
SCRIPT_ESCAPES = MARKUP_ESCAPES + (("\u2028", "\\u2028"), ("\u2029", "\\u2029"))


def _escape(json_text: str, escapes) -> str:
    for char, escape in escapes:
        json_text = json_text.replace(char, escape)
    return json_text


def escape_markup(json_text: str) -> str:
    """``json_text`` with ``<``, ``>`` and ``&`` replaced by their JSON ``\\u`` escapes.

    ``json_text`` is JSON text (the output of :func:`json.dumps`), so every
    such character sits inside a string, where the escape decodes to the same
    character. The result holds none of the three characters. Used to fence
    evidence inside a tag.
    """
    return _escape(json_text, MARKUP_ESCAPES)


def escape_script_json(json_text: str) -> str:
    """:func:`escape_markup` and the line separators U+2028 and U+2029, for a script element.

    ``json_text`` is JSON text. The result decodes to the same value, ends no
    script element early and holds no line separator.
    """
    return _escape(json_text, SCRIPT_ESCAPES)

"""Read a capped, redacted tail of a Claude Code or Codex transcript.

This is the engine behind ``read_session`` (``relay-protocol/1``, section 13 of
``docs/reference/llm-kit-relay-protocol.md``). It reads a JSONL transcript
file read-only, turns its lines into protocol ``turn`` objects
(``{role, text, at?, truncated?}``), redacts them (:mod:`llm_kit.relay.redact`)
and returns them with an opaque cursor.

Caps. Every read is bounded four ways, so a huge transcript cannot cost more
than the caps:

* ``limit`` turns (protocol default 20, at most 200);
* ``max_bytes`` of returned turn text in total (default 65536); older turns are
  dropped first and ``truncated`` is true;
* ``max_turn_chars`` per turn (default 4000); a cut turn carries
  ``truncated: true``;
* ``max_scan_bytes`` read from the file per call (default 4 MiB).

Cursor. ``c:<n>`` is the byte offset just after the last complete line the
read consumed. A call without ``since`` returns the newest turns (the tail,
newest last) and a cursor at the end of the last complete line. A call with
``since=<cursor>`` returns the turns written AFTER that offset, oldest first,
at most ``limit`` of them; when more remain, ``truncated`` is true and the
returned cursor lets the next call continue without a gap. A cursor that is
not ``c:<digits>``, is beyond the end of the file or does not sit on a line
boundary is ``bad_request`` (the transcript was replaced; start a new tail).

What is returned. User and assistant text, and one ``tool`` turn per tool call
and per tool result. Thinking blocks, images, system and meta lines, sidechain
(subagent) lines and the harness's injected instructions are not turns.
A tool result is returned only when its originating call is inside the scanned
window AND that call did not touch a secrets location
(:func:`llm_kit.relay.redact.is_secret_path_use`); otherwise its text is
replaced by a short withheld notice. The text of all returned turns is
redacted for secret shapes. The number of redactions (a withheld output counts
as one) is returned.

Session text is data, not instructions: nothing here interprets it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Mapping, Optional

from llm_kit.relay import protocol as P
from llm_kit.relay import redact as R

DEFAULT_MAX_BYTES = 65536
DEFAULT_MAX_TURN_CHARS = 4000
DEFAULT_MAX_SCAN_BYTES = 4 * 1024 * 1024
#: Longest single line a forward read will skip over when it cannot fit the scan window.
MAX_SKIPPED_LINE_BYTES = 256 * 1024 * 1024

CURSOR_RE = re.compile(r"c:(\d{1,15})")
_TOOL_INPUT_CHARS = 500

#: Codex user messages the harness injects before the conversation starts.
_CODEX_INJECTED_PREFIXES = ("# AGENTS.md instructions", "<environment_context>", "<user_instructions>")


@dataclass(frozen=True)
class ReadLimits:
    """The caps of one read; ``limit`` is the request's turn count."""

    max_bytes: int = DEFAULT_MAX_BYTES
    max_turn_chars: int = DEFAULT_MAX_TURN_CHARS
    max_scan_bytes: int = DEFAULT_MAX_SCAN_BYTES

    def __post_init__(self) -> None:
        for name in ("max_bytes", "max_turn_chars", "max_scan_bytes"):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")


@dataclass
class TailResult:
    """What one read returns; ``to_result`` is the protocol ``read_session`` result."""

    address: str
    turns: list[P.Turn]
    cursor: Optional[str]
    truncated: bool
    redactions: int
    skipped_lines: int = 0

    def to_result(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "address": self.address,
            "turns": self.turns,
            "cursor": self.cursor,
            "truncated": self.truncated,
            "redactions": self.redactions,
        }
        if self.skipped_lines:
            result["skipped_lines"] = self.skipped_lines
        return result


# --------------------------------------------------------------------------
# turn extraction
# --------------------------------------------------------------------------

@dataclass
class _Draft:
    role: str
    text: str
    at: Optional[float]
    withheld: bool = False


@dataclass
class _Extractor:
    """Per-read state: which tool calls touched a secrets location."""

    harness: str
    calls: dict[str, tuple[str, bool]] = field(default_factory=dict)

    def turns(self, obj: Mapping[str, Any]) -> list[_Draft]:
        if self.harness == "claude":
            return self._claude(obj)
        return self._codex(obj)

    # -- claude -----------------------------------------------------------

    def _claude(self, obj: Mapping[str, Any]) -> list[_Draft]:
        kind = obj.get("type")
        if kind not in ("user", "assistant"):
            return []
        if obj.get("isMeta") is True or obj.get("isSidechain") is True:
            return []
        message = obj.get("message")
        if not isinstance(message, Mapping):
            return []
        at = _iso_epoch(obj.get("timestamp"))
        content = message.get("content")
        role = "assistant" if kind == "assistant" else "user"
        if isinstance(content, str):
            return [_Draft(_claude_user_role(role, content), content, at)] if content.strip() else []
        if not isinstance(content, list):
            return []
        drafts: list[_Draft] = []
        for block in content:
            if not isinstance(block, Mapping):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    drafts.append(_Draft(_claude_user_role(role, text), text, at))
            elif btype == "tool_use":
                drafts.append(self._call(block.get("id"), block.get("name"), block.get("input"), at))
            elif btype == "tool_result":
                drafts.append(self._result(block.get("tool_use_id"), _result_text(block.get("content")), at))
        return drafts

    # -- codex ------------------------------------------------------------

    def _codex(self, obj: Mapping[str, Any]) -> list[_Draft]:
        if obj.get("type") != "response_item":
            return []
        payload = obj.get("payload")
        if not isinstance(payload, Mapping):
            return []
        at = _iso_epoch(obj.get("timestamp"))
        ptype = payload.get("type")
        if ptype == "message":
            role = payload.get("role")
            if role not in ("user", "assistant"):
                return []
            parts = payload.get("content")
            texts = [
                b["text"]
                for b in (parts if isinstance(parts, list) else [])
                if isinstance(b, Mapping) and b.get("type") in ("input_text", "output_text") and isinstance(b.get("text"), str)
            ]
            text = "\n".join(texts)
            if not text.strip() or (role == "user" and text.lstrip().startswith(_CODEX_INJECTED_PREFIXES)):
                return []
            return [_Draft(role, text, at)]
        if ptype in ("custom_tool_call", "function_call"):
            raw = payload.get("input") if ptype == "custom_tool_call" else payload.get("arguments")
            return [self._call(payload.get("call_id"), payload.get("name"), raw, at)]
        if ptype in ("custom_tool_call_output", "function_call_output"):
            return [self._result(payload.get("call_id"), _result_text(payload.get("output")), at)]
        return []

    # -- tool calls and results ------------------------------------------

    def _call(self, call_id: Any, name: Any, tool_input: Any, at: Optional[float]) -> _Draft:
        tool = name if isinstance(name, str) and name else "tool"
        secret = R.is_secret_path_use(tool, tool_input)
        if isinstance(call_id, str) and call_id:
            self.calls[call_id] = (tool, secret)
        summary = tool_input if isinstance(tool_input, str) else _compact_json(tool_input)
        if len(summary) > _TOOL_INPUT_CHARS:
            summary = _cut_at_boundary(summary, _TOOL_INPUT_CHARS) + " ..."
        return _Draft("tool", f"[{tool}] {summary}".rstrip(), at)

    def _result(self, call_id: Any, text: str, at: Optional[float]) -> _Draft:
        known = self.calls.get(call_id) if isinstance(call_id, str) else None
        if known is None:
            return _Draft("tool", R.WITHHELD_ORPHAN, at, withheld=True)
        tool, secret = known
        if secret:
            return _Draft("tool", f"[{tool} result] {R.WITHHELD_SECRET_PATH}", at, withheld=True)
        return _Draft("tool", f"[{tool} result] {text}", at)


def _claude_user_role(role: str, text: str) -> str:
    if role == "user" and text.lstrip().startswith("<channel"):
        return "channel"
    return role


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b["text"] for b in content if isinstance(b, Mapping) and isinstance(b.get("text"), str)
        )
    return ""


def _compact_json(value: Any) -> str:
    try:
        return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError):
        return str(value)


def _iso_epoch(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _cut_at_boundary(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters, backed up to whitespace so no token is split."""
    if len(text) <= limit:
        return text
    cut = text[:limit]
    boundary = max(cut.rfind(" "), cut.rfind("\n"), cut.rfind("\t"))
    if boundary >= max(0, limit - 256):
        cut = cut[:boundary]
    return cut


def _finish(draft: _Draft, max_turn_chars: int) -> tuple[P.Turn, int]:
    """The protocol turn for ``draft`` and its redaction count."""
    text = draft.text
    cut = False
    # redact the whole text first so a secret straddling the cap is replaced, then cap.
    hard = max_turn_chars * 8
    if len(text) > hard:
        text, cut = _cut_at_boundary(text, hard), True
    redacted, count = R.redact_text(text)
    if draft.withheld:
        count += 1
    if len(redacted) > max_turn_chars:
        redacted, cut = _cut_at_boundary(redacted, max_turn_chars), True
    turn: P.Turn = {"role": draft.role, "text": redacted}
    if draft.at is not None:
        turn["at"] = draft.at
    if cut:
        turn["truncated"] = True
    return turn, count


# --------------------------------------------------------------------------
# file reading
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class _Line:
    start: int
    end: int  # offset just after the newline
    data: bytes


def _complete_lines(buf: bytes, base: int, *, drop_first_partial: bool) -> Iterator[_Line]:
    """Complete (newline-terminated) lines of ``buf``, which begins at file offset ``base``."""
    pos = 0
    if drop_first_partial:
        first = buf.find(b"\n")
        if first < 0:
            return
        pos = first + 1
    while True:
        nl = buf.find(b"\n", pos)
        if nl < 0:
            return
        yield _Line(base + pos, base + nl + 1, buf[pos:nl])
        pos = nl + 1


def parse_cursor(cursor: str) -> int:
    match = CURSOR_RE.fullmatch(cursor) if isinstance(cursor, str) else None
    if match is None:
        raise P.RelayError("bad_request", f"cursor {cursor!r} is not c:<digits>")
    return int(match.group(1))


def format_cursor(offset: int) -> str:
    return f"c:{offset}"


def _skip_line(handle, offset: int) -> int:
    """Offset just after the newline of the line that starts at ``offset`` (EOF if none)."""
    handle.seek(offset)
    consumed = 0
    while consumed < MAX_SKIPPED_LINE_BYTES:
        chunk = handle.read(1024 * 1024)
        if not chunk:
            return offset + consumed
        nl = chunk.find(b"\n")
        if nl >= 0:
            return offset + consumed + nl + 1
        consumed += len(chunk)
    raise P.RelayError("internal", f"transcript line at byte {offset} exceeds {MAX_SKIPPED_LINE_BYTES} bytes")


def read_transcript(
    path: Path,
    harness: str,
    address: str,
    *,
    since: Optional[str] = None,
    limit: int = P.DEFAULT_READ_TURNS,
    limits: ReadLimits = ReadLimits(),
) -> TailResult:
    """A capped, redacted tail of the transcript at ``path`` (see the module docstring)."""
    if harness not in P.SESSION_HARNESSES:
        raise ValueError(f"harness must be one of {P.SESSION_HARNESSES}, got {harness!r}")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= P.MAX_READ_TURNS:
        raise P.RelayError("bad_request", f"limit must be an integer in 1..{P.MAX_READ_TURNS}")
    offset = parse_cursor(since) if since is not None else None
    try:
        handle = open(path, "rb")
    except OSError as exc:
        raise P.RelayError("target_not_found", f"transcript not readable: {exc.strerror or exc}") from exc
    with handle:
        size = path.stat().st_size
        if offset is None:
            return _tail(handle, size, harness, address, limit, limits)
        return _forward(handle, size, offset, harness, address, limit, limits)


def _extract(lines: list[_Line], harness: str) -> tuple[list[tuple[_Line, list[_Draft]]], int]:
    extractor = _Extractor(harness)
    out: list[tuple[_Line, list[_Draft]]] = []
    skipped = 0
    for line in lines:
        text = line.data.strip()
        if not text:
            out.append((line, []))
            continue
        try:
            obj = json.loads(text)
        except ValueError:
            skipped += 1
            out.append((line, []))
            continue
        out.append((line, extractor.turns(obj) if isinstance(obj, Mapping) else []))
    return out, skipped


def _tail(handle, size: int, harness: str, address: str, limit: int, limits: ReadLimits) -> TailResult:
    start = max(0, size - limits.max_scan_bytes)
    handle.seek(start)
    buf = handle.read(size - start)
    lines = list(_complete_lines(buf, start, drop_first_partial=start > 0))
    if lines:
        cursor: Optional[str] = format_cursor(lines[-1].end)
    else:
        # nothing complete: a transcript that has not got its first line yet, or one line over the scan cap
        cursor = format_cursor(0) if start == 0 else None
    extracted, skipped = _extract(lines, harness)

    chosen: list[tuple[P.Turn, int]] = []
    total_bytes = 0
    cut_short = False
    for _, drafts in reversed(extracted):
        if not drafts:
            continue
        group = [_finish(d, limits.max_turn_chars) for d in drafts]
        group_bytes = sum(len(t["text"].encode("utf-8")) for t, _ in group)
        if chosen and (len(chosen) + len(group) > limit or total_bytes + group_bytes > limits.max_bytes):
            cut_short = True
            break
        if not chosen and len(group) > limit:
            group = group[-limit:]
            cut_short = True
        chosen = group + chosen
        total_bytes += group_bytes
    truncated = cut_short or start > 0
    return TailResult(address, [t for t, _ in chosen], cursor, truncated, sum(c for _, c in chosen), skipped)


def _forward(handle, size: int, offset: int, harness: str, address: str, limit: int, limits: ReadLimits) -> TailResult:
    if offset > size:
        raise P.RelayError("bad_request", f"cursor c:{offset} is beyond the end of the transcript ({size} bytes)")
    if offset > 0:
        handle.seek(offset - 1)
        if handle.read(1) != b"\n":
            raise P.RelayError("bad_request", f"cursor c:{offset} is not on a line boundary")
    handle.seek(offset)
    buf = handle.read(min(limits.max_scan_bytes, size - offset))
    window_end = offset + len(buf)
    lines = list(_complete_lines(buf, offset, drop_first_partial=False))
    if not lines and window_end < size:
        # one line is bigger than the scan window: skip it whole, say so, and move on
        new_end = _skip_line(handle, offset)
        note = _Draft("system", f"[a {new_end - offset} byte transcript line was skipped: over the scan cap]", None)
        turn, count = _finish(note, limits.max_turn_chars)
        return TailResult(address, [turn], format_cursor(new_end), new_end < size, count, 1)
    extracted, skipped = _extract(lines, harness)

    chosen: list[tuple[P.Turn, int]] = []
    total_bytes = 0
    cursor = offset
    stopped = False
    for line, drafts in extracted:
        group = [_finish(d, limits.max_turn_chars) for d in drafts]
        group_bytes = sum(len(t["text"].encode("utf-8")) for t, _ in group)
        if group and chosen and (len(chosen) + len(group) > limit or total_bytes + group_bytes > limits.max_bytes):
            stopped = True
            break
        if len(group) > limit:
            group = group[:limit]
        chosen.extend(group)
        total_bytes += group_bytes
        cursor = line.end
    truncated = stopped or window_end < size
    return TailResult(address, [t for t, _ in chosen], format_cursor(cursor), truncated, sum(c for _, c in chosen), skipped)

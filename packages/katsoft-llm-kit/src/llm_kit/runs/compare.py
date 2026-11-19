"""Compare model selections on a set of cases; every attempt is a run record in a run store.

A comparison sends one prompt file, filled with the slots of each case, to each
of two or more candidates (resolved model selections). Each attempt is a
``llm-kit.run-record/v1`` record in one group (``group.kind`` ``compare``), so
the comparison is the records that share the group id.

The two steps:

1. :func:`plan_compare` checks everything and writes no record: the cases, the
   prompt (filled for every case), the candidates, the content class against
   the store's policy, a cost bound for every attempt that needs a call, the
   records the store already has, and the spend already recorded in the group.
   A dry run prints the plan and stops here.
2. :func:`run_compare` runs the plan in case order, each case on every
   candidate. For each attempt that needs a call it writes the started event
   before the call and the result event after it. It never sends an attempt
   twice and never retries; a failed attempt stays failed.

Reuse. Two attempts that have the same reuse key (``llm-kit.run-key/v1``: the
flow, the case input, the prompt and slot hashes, the resolved model and
effort, the whole request) asked the same question of the same model. A score
also depends on its scorer, so an attempt is done when the group already holds
an ``ok`` record with its key and the same scorer hash. When the store holds an
``ok`` record with the key but in another group or with another scorer, the
answer is reused with no call: a new record in this group copies the answer
(label ``compare.reused_from``, no usage and no cost) and the current scorer
scores it.

Budget (``max_usd``). The budget covers the whole group, across runs. The
spend of the group is the sum, over its records, of the recorded cost of each
result (its cost bound when the cost is unknown) and the cost bound of each
started event with no result (an attempt that may have been billed). Copies
count 0. Before each call, the attempt's cost bound is added; an attempt whose
bound would take the spend over ``max_usd`` is not started and the run stops.
The bound of an attempt is a worst case: the prompt's UTF-8 byte count plus
:data:`INPUT_OVERHEAD_TOKENS` input tokens (a token holds at least one byte)
and the candidate's output cap (else the price's ``context_window``) output
tokens, at the candidate's :class:`Price`. A candidate with no price, or with
neither a cap nor a context window, is refused at plan time. A harness adds
its own context to the prompt, so only a free price bounds a harness
candidate. The started event records the bound in the label
``compare.bound_usd``.

The model call itself is not in this module: :func:`run_compare` takes a
caller (``(Candidate, prompt) -> CallResult``) that makes exactly one call and
does not retry. ``llm-kit runs compare`` gives the caller that sends through
the project model file (``llm_kit.dispatch.invoke``). A scorer is optional: a
program that reads one ``llm-kit.run-import/v1`` line (the result record and
its blobs) and returns the same line with ``record.scores`` filled
(:class:`Scorer`, :func:`command_scorer`). Its name, version and hash are in
``provenance.scorer``.

This module is standard library only. The contract is
``docs/reference/llm-kit-runs.md``, "Comparisons".
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Union

from llm_kit._claude_env import _claude_child_env
from llm_kit.graph.model import GraphError
from llm_kit.graph.prompt import parse_prompt_file, render_prompt
from llm_kit.pipeline.artifacts.freshness.hashing import FULL_DIGEST_LENGTH, digest, stable_json
from llm_kit.schema.formats import COMPARE_CASE_V1, load_schema
from llm_kit.schema.json_schema import validate

from .record import CONTENT_CLASSES, blob_ref, finish_record, reuse_key, start_record
from .store import RUN_IMPORT_V1, RunRecordStore, RunStoreError, read_import_file

__all__ = [
    "ACTIONS",
    "CALL_STATUSES",
    "COMPARE_CASE_V1",
    "INPUT_OVERHEAD_TOKENS",
    "LABEL_BOUND",
    "LABEL_REUSED_FROM",
    "AttemptOutcome",
    "CallResult",
    "Candidate",
    "CompareCase",
    "CompareError",
    "ComparePlan",
    "CompareResult",
    "PlannedAttempt",
    "Price",
    "Scorer",
    "attempt_bound_usd",
    "command_scorer",
    "plan_compare",
    "read_cases",
    "run_compare",
]

#: Input tokens added to the prompt's byte count in a cost bound, for the message framing a transport adds.
INPUT_OVERHEAD_TOKENS = 64
#: The label of a started event that holds its cost bound in USD (``repr`` of a float).
LABEL_BOUND = "compare.bound_usd"
#: The label of a record that copies the answer of an earlier record (its id) with no call.
LABEL_REUSED_FROM = "compare.reused_from"
#: What a planned attempt does: ``done`` (the group has it), ``copy`` (reuse an answer), ``call``.
ACTIONS = ("done", "copy", "call")
#: The statuses of a :class:`CallResult`.
CALL_STATUSES = ("completed", "failed", "halted")

_CASE_SCHEMA = load_schema(COMPARE_CASE_V1)
_TOOL = {"name": "llm_kit.runs.compare", "version": None}


class CompareError(ValueError):
    """A comparison was refused, or stopped on an error; ``code`` names the reason.

    Codes: ``invalid`` (an argument, a case, the prompt or a scorer answer),
    ``policy`` (the store does not accept the content class), ``unpriced`` (an
    attempt has no cost bound), ``conflict`` (the group holds a record the
    budget cannot count), ``scorer-failed`` (the scorer failed; the attempt
    was recorded as ``invalid``). ``result`` is the partial
    :class:`CompareResult` when the error stopped a run, else None.
    """

    def __init__(self, code: str, message: str, *, result: Optional["CompareResult"] = None) -> None:
        super().__init__(message)
        self.code = code
        self.result = result


# -- inputs ---------------------------------------------------------------------


def _finite(value: Any, where: str, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise CompareError("invalid", f"{where} must be a finite number of at least {minimum}, not {value!r}")
    return float(value)


@dataclass(frozen=True)
class Price:
    """USD per one million tokens for one model, and its optional context window.

    The fields are those of a pricing table entry of
    ``llm_kit.pipeline.runtime.platform.estimate_cost``: ``input``,
    ``output``, ``cache_hit`` (default: the input rate) and ``context_window``
    (tokens; bounds the output of a call with no output cap). Raises
    :class:`CompareError` ``invalid`` for a negative, non-finite or non-number
    rate, or a context window that is not a positive integer.
    """

    input: float
    output: float
    cache_hit: Optional[float] = None
    context_window: Optional[int] = None

    def __post_init__(self) -> None:
        _finite(self.input, "price input")
        _finite(self.output, "price output")
        if self.cache_hit is not None:
            _finite(self.cache_hit, "price cache_hit")
        window = self.context_window
        if window is not None and (isinstance(window, bool) or not isinstance(window, int) or window < 1):
            raise CompareError("invalid", f"price context_window must be a positive integer, not {window!r}")

    @classmethod
    def from_entry(cls, entry: Mapping[str, Any]) -> "Price":
        """The price of one pricing table entry (a mapping with ``input`` and ``output``).

        Keys other than ``input``, ``output``, ``cache_hit``, ``context_window``
        and ``alias`` are refused. Raises :class:`CompareError` ``invalid``.
        """
        if not isinstance(entry, Mapping) or "input" not in entry or "output" not in entry:
            raise CompareError("invalid", f"a pricing entry is a mapping with input and output, not {entry!r}")
        unknown = sorted(set(entry) - {"input", "output", "cache_hit", "context_window", "alias"})
        if unknown:
            raise CompareError("invalid", f"a pricing entry has unknown keys {unknown}")
        return cls(entry["input"], entry["output"], entry.get("cache_hit"), entry.get("context_window"))

    @property
    def free(self) -> bool:
        """True when every rate is 0 (the owner declares the model costs nothing per call)."""
        return self.input == 0 and self.output == 0 and not self.cache_hit


@dataclass(frozen=True)
class Candidate:
    """One model selection to compare, resolved before any call.

    ``selection`` is the selection name (unique in a comparison); ``row`` the
    catalogue row it names; ``endpoint`` and ``model`` the endpoint and exact
    wire model; ``effort`` the effort sent (None: none); ``max_output_tokens``
    the output cap the call carries (None: no cap); ``extras`` every other
    request parameter the call carries; ``price`` its :class:`Price` (None:
    unknown, which refuses the plan); ``registry_sha256`` the hash of the
    model file it came from, or None; ``prompt_only`` False for a harness,
    which adds its own context to the prompt.
    """

    selection: str
    row: str
    model: str
    endpoint: Optional[str] = None
    effort: Optional[str] = None
    max_output_tokens: Optional[int] = None
    extras: Mapping[str, Any] = field(default_factory=dict)
    price: Optional[Price] = None
    registry_sha256: Optional[str] = None
    prompt_only: bool = True


@dataclass(frozen=True)
class CallResult:
    """What one model call returned, as a caller reports it to :func:`run_compare`.

    ``status`` is ``completed`` (``text`` is the answer), ``failed`` (no
    usable answer; ``error`` is ``{code, message}``, ``text`` a partial
    answer or None) or ``halted`` (a quota, credit or auth stop; the run
    stops after it is recorded). Token counts, ``reported_usd`` (an
    authoritative cost), ``estimated_usd`` (a figure that is not
    authoritative) and ``total_ms`` are None when unknown. Raises
    :class:`CompareError` ``invalid`` for an unknown status, a completed
    result without text or a failed one without an error.
    """

    status: str
    text: Optional[str] = None
    error: Optional[Mapping[str, Any]] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    cached_tokens: Optional[int] = None
    reasoning_tokens: Optional[int] = None
    reported_usd: Optional[float] = None
    estimated_usd: Optional[float] = None
    total_ms: Optional[float] = None

    def __post_init__(self) -> None:
        if self.status not in CALL_STATUSES:
            raise CompareError("invalid", f"call status {self.status!r} is not one of {CALL_STATUSES}")
        if self.status == "completed" and not isinstance(self.text, str):
            raise CompareError("invalid", "a completed call result needs its answer text")
        if self.status != "completed" and not (isinstance(self.error, Mapping) and self.error.get("code")):
            raise CompareError("invalid", f"a {self.status} call result needs an error {{code, message}}")


@dataclass(frozen=True)
class CompareCase:
    """One case: its ``id`` and the text of each prompt slot (``slots``)."""

    id: str
    slots: Mapping[str, str]


def _check_case(value: Any, where: str) -> CompareCase:
    problems = validate(_CASE_SCHEMA, value)
    if problems:
        raise CompareError("invalid", f"{where} is not a {COMPARE_CASE_V1} case: {list(problems)}")
    return CompareCase(value["id"], dict(value["slots"]))


def read_cases(source: Union[str, Path, bytes]) -> list[CompareCase]:
    """Read a comparison case file (JSON Lines, ``llm-kit.compare-case/v1``).

    ``source`` is a path or the file's bytes. Each non-empty line is one JSON
    object ``{id, slots}``; a final newline is allowed, an empty line elsewhere
    is not, and a CR before the LF is removed. Returns the cases in file order.
    Raises :class:`CompareError` ``invalid`` naming the line for bytes that are
    not UTF-8, a line that is not JSON or not a case, a repeated id, or a file
    with no case.
    """
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CompareError("invalid", f"the case file is not UTF-8: {exc}") from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    cases: list[CompareCase] = []
    seen: set[str] = set()
    for number, line in enumerate(lines, start=1):
        line = line[:-1] if line.endswith("\r") else line
        if not line.strip():
            raise CompareError("invalid", f"line {number} is empty")
        try:
            value = json.loads(line)
        except ValueError as exc:
            raise CompareError("invalid", f"line {number} is not JSON: {exc}") from None
        case = _check_case(value, f"line {number}")
        if case.id in seen:
            raise CompareError("invalid", f"line {number}: case id {case.id!r} is used twice")
        seen.add(case.id)
        cases.append(case)
    if not cases:
        raise CompareError("invalid", "the case file holds no case")
    return cases


# -- scorers --------------------------------------------------------------------


@dataclass(frozen=True)
class Scorer:
    """A scorer: its identity (``name``, ``version``, ``sha256``) and the function that scores.

    ``score(line) -> line`` takes one ``llm-kit.run-import/v1`` line as a dict
    (the result record, with ``scores`` empty, and the blobs of its case input
    and answer) and returns the same line with ``record.scores`` filled. A
    score note is a ``blob:`` reference whose text the returned line adds to
    ``blobs``. ``sha256`` is the hash of what the scorer is (its program and
    the files it reads); a changed hash makes earlier scores stale.
    """

    name: str
    sha256: str
    score: Callable[[dict], dict]
    version: Optional[str] = None

    def identity(self) -> dict:
        """``{name, version, sha256}``: the ``provenance.scorer`` of the records it scores."""
        return {"name": self.name, "version": self.version, "sha256": self.sha256}


def command_scorer(
    argv: Sequence[str],
    *,
    name: Optional[str] = None,
    version: Optional[str] = None,
    cwd: Union[str, Path, None] = None,
) -> Scorer:
    """A :class:`Scorer` that runs the program ``argv`` once for each answer.

    The program reads one ``llm-kit.run-import/v1`` line on stdin and writes
    the same line, with ``record.scores`` filled, on stdout. It runs in
    ``cwd`` (default: the current directory), with the process's environment
    minus ``CLAUDECODE`` (a scorer may start ``claude``; see
    ``llm_kit._claude_env``). A non-zero exit fails the score.
    ``sha256`` hashes the words of ``argv`` and the bytes of each word that
    names an existing file (relative to ``cwd``); it does not hash a program
    found on PATH or a file the program reads itself, so name those in
    ``version``. ``name`` defaults to the file name of the first word after
    the program that names a file (``python score.py`` is ``score.py``),
    else the program's file name. Raises :class:`CompareError`
    ``invalid`` for an empty ``argv``.
    """
    words = [str(word) for word in argv]
    if not words or not all(words):
        raise CompareError("invalid", "a scorer command needs at least one non-empty word")
    base = Path(cwd) if cwd is not None else Path.cwd()
    files: dict[str, str] = {}
    for word in words:
        path = Path(word) if Path(word).is_absolute() else base / word
        if path.is_file():
            files[word] = hashlib.sha256(path.read_bytes()).hexdigest()
    sha = digest(b"llm-kit.compare-scorer/v1", stable_json({"argv": words, "files": files}),
                 length=FULL_DIGEST_LENGTH)
    scripts = [word for word in words[1:] if word in files]
    label = name or Path(scripts[0] if scripts else words[0]).name

    def score(line: dict) -> dict:
        proc = subprocess.run(words, input=json.dumps(line, ensure_ascii=True).encode("ascii"),
                              capture_output=True, cwd=str(base), check=False, env=_claude_child_env())
        if proc.returncode != 0:
            tail = proc.stderr.decode("utf-8", "replace").strip()[-2000:]
            raise CompareError("scorer-failed", f"scorer {label} exited {proc.returncode}: {tail}")
        try:
            return json.loads(proc.stdout.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CompareError("scorer-failed", f"scorer {label} did not print one JSON line: {exc}") from None

    return Scorer(name=label, sha256=sha, score=score, version=version)


def _apply_scorer(scorer: Scorer, record: dict, blobs: Mapping[str, str]) -> tuple[list, dict[str, bytes]]:
    """``(scores, note blobs)`` of ``scorer`` for ``record``; :class:`CompareError` ``scorer-failed``."""
    line = {"format": RUN_IMPORT_V1, "record": record, "blobs": {ref: {"text": text} for ref, text in blobs.items()}}
    try:
        answer = scorer.score(copy.deepcopy(line))
    except CompareError as exc:
        raise CompareError("scorer-failed", str(exc)) from None
    except Exception as exc:  # noqa: BLE001 -- any failure of a scorer is a scorer failure, reported as one
        raise CompareError("scorer-failed", f"scorer {scorer.name} raised {type(exc).__name__}: {exc}") from None
    try:
        items = read_import_file((json.dumps(answer, ensure_ascii=True, allow_nan=False) + "\n").encode("ascii"))
    except (RunStoreError, TypeError, ValueError) as exc:
        raise CompareError("scorer-failed", f"scorer {scorer.name} returned no valid {RUN_IMPORT_V1} line: "
                           f"{exc}") from None
    if len(items) != 1:
        raise CompareError("scorer-failed", f"scorer {scorer.name} returned {len(items)} lines, not 1")
    scored, scored_blobs = items[0]
    changed = sorted(key for key in set(scored) | set(record) if key != "scores" and scored.get(key) != record.get(key))
    if changed:
        raise CompareError("scorer-failed", f"scorer {scorer.name} changed the record fields {changed}")
    notes = {ref: data for ref, data in scored_blobs.items() if ref not in blobs}
    named = {score["note"] for score in scored["scores"] if score["note"] is not None}
    if set(notes) != named - set(blobs):
        raise CompareError("scorer-failed", f"scorer {scorer.name}: the note blobs {sorted(notes)} are not the "
                           f"notes its scores name {sorted(named - set(blobs))}")
    return scored["scores"], notes


# -- planning -------------------------------------------------------------------


def attempt_bound_usd(candidate: Candidate, prompt: str) -> float:
    """The worst-case USD cost of one call of ``candidate`` with ``prompt`` (see the module docstring).

    0 for a free price. Raises :class:`CompareError` ``unpriced`` when
    ``candidate`` has no price, when it is a harness with a price that is not
    free, or when it has neither an output cap nor a price context window.
    """
    price = candidate.price
    if price is None:
        raise CompareError("unpriced", f"selection {candidate.selection!r} (model {candidate.model!r}) has no "
                           "price; give one in the pricing table (0 rates declare it free)")
    if price.free:
        return 0.0
    if not candidate.prompt_only:
        raise CompareError("unpriced", f"selection {candidate.selection!r} runs on a harness, which adds its own "
                           "context to the prompt, so its cost has no bound; only a free price (0 rates) is "
                           "accepted for a harness")
    output_tokens = candidate.max_output_tokens or price.context_window
    if output_tokens is None:
        raise CompareError("unpriced", f"selection {candidate.selection!r} has no output cap and its price has "
                           "no context_window, so its output has no bound; set an output cap")
    input_tokens = len(prompt.encode("utf-8")) + INPUT_OVERHEAD_TOKENS
    input_rate = max(price.input, price.cache_hit or 0.0)
    return (input_tokens * input_rate + output_tokens * price.output) / 1_000_000


@dataclass(frozen=True)
class PlannedAttempt:
    """One case on one candidate, as planned.

    ``action`` is one of :data:`ACTIONS`; ``record_id`` is the group's record
    (``done``) or the record whose answer is reused (``copy``), else None;
    ``bound_usd`` is the cost bound of a ``call`` (0 otherwise); ``key`` the
    reuse key; ``prompt`` the filled prompt; ``fields`` the keyword arguments
    of :func:`~llm_kit.runs.record.start_record` for its records.
    """

    case: CompareCase
    candidate: Candidate
    prompt: str
    key: str
    action: str
    record_id: Optional[str]
    bound_usd: float
    fields: Mapping[str, Any]


@dataclass(frozen=True)
class ComparePlan:
    """A checked comparison: what :func:`run_compare` will do. Make it with :func:`plan_compare`.

    ``group`` is ``{id, kind, label}``; ``spent_usd`` the spend already
    recorded in the group; ``attempts`` in run order (case order, then
    candidate order).
    """

    store: RunRecordStore
    group: Mapping[str, Any]
    flow: str
    content: str
    max_usd: float
    spent_usd: float
    attempts: tuple[PlannedAttempt, ...]
    scorer: Optional[Scorer] = None

    @property
    def bound_usd(self) -> float:
        """The sum of the cost bounds of the attempts that need a call."""
        return sum(attempt.bound_usd for attempt in self.attempts)

    def to_dict(self) -> dict:
        """The plan as JSON: the group, the budget, and each attempt with its action and bound."""
        return {
            "group": dict(self.group), "flow": self.flow, "content": self.content,
            "scorer": None if self.scorer is None else self.scorer.identity(),
            "max_usd": self.max_usd, "spent_usd": self.spent_usd, "bound_usd": self.bound_usd,
            "fits": self.spent_usd + self.bound_usd <= self.max_usd,
            "attempts": [{"case": a.case.id, "selection": a.candidate.selection, "model": a.candidate.model,
                          "effort": a.candidate.effort, "action": a.action, "record": a.record_id,
                          "bound_usd": a.bound_usd} for a in self.attempts],
        }


def _case_bytes(case: CompareCase) -> bytes:
    return json.dumps(dict(case.slots), sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _project_relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        raise CompareError("invalid", f"the prompt {path} is not under the project root {root}") from None


def _check_plan_args(store: RunRecordStore, flow: Any, content: Any, candidates: Sequence[Candidate],
                     cases: Sequence[CompareCase], max_usd: Any) -> float:
    if not isinstance(flow, str) or not flow.strip():
        raise CompareError("invalid", "flow must be non-empty text")
    if content not in CONTENT_CLASSES:
        raise CompareError("invalid", f"content must be one of {CONTENT_CLASSES}, not {content!r}")
    if content not in store.accepts:
        raise CompareError("policy", f"the store {store.root} accepts only {sorted(store.accepts)}, "
                           f"not {content!r}")
    names = [candidate.selection for candidate in candidates]
    if len(names) < 2 or len(set(names)) != len(names):
        raise CompareError("invalid", f"a comparison needs two or more distinct selections, not {names}")
    ids = [case.id for case in cases]
    if not ids or len(set(ids)) != len(ids):
        raise CompareError("invalid", "a comparison needs one or more cases with distinct ids")
    return _finite(max_usd, "max_usd")


def _default_group_id(flow: str, prompt_sha: str, cases: Sequence[CompareCase], names: Sequence[str]) -> str:
    payload = {"flow": flow, "prompt": prompt_sha, "selections": list(names),
               "cases": [[case.id, hashlib.sha256(_case_bytes(case)).hexdigest()] for case in cases]}
    return "compare-" + digest(b"llm-kit.compare-group/v1", stable_json(payload), length=16)


def _fields(case: CompareCase, candidate: Candidate, prompt: str, *, flow: str, group: Mapping, content: str,
            prompt_meta: Mapping, provenance: Mapping) -> dict:
    data = _case_bytes(case)
    case_sha = hashlib.sha256(data).hexdigest()
    return {
        "flow": flow, "group": dict(group), "content": content, "tool": dict(provenance["tool"]),
        "case": {"id": case.id, "input_sha256": case_sha, "input": blob_ref(data)},
        "prompt": {**prompt_meta, "slots_sha256": case_sha},
        "selection": {"name": candidate.selection, "model": candidate.row, "effort": candidate.effort},
        "resolved": {"row": candidate.row, "endpoint": candidate.endpoint, "model": candidate.model,
                     "effort": candidate.effort, "registry_sha256": candidate.registry_sha256},
        "request": {"max_output_tokens": candidate.max_output_tokens, "extras": dict(candidate.extras),
                    "sent_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest()},
        "provenance": {key: value for key, value in provenance.items() if key != "tool"},
    }


_PLACEHOLDER_ID = "19700101T000000000000Z-0000"


def _scorer_sha(record: Mapping) -> Optional[str]:
    scorer = record["provenance"]["scorer"]
    return None if scorer is None else scorer["sha256"]


def _charge(record: Mapping) -> float:
    """What one record of a group counts against its budget; :class:`CompareError` ``conflict``."""
    labels = record["labels"]
    if LABEL_REUSED_FROM in labels:
        return 0.0
    cost = record.get("cost")
    if cost is not None and cost["usd"] is not None:
        return float(cost["usd"])
    bound = labels.get(LABEL_BOUND)
    try:
        value = float(bound) if bound is not None else math.nan
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise CompareError("conflict", f"record {record['id']} of group {record['group']['id']} has no known cost "
                           f"and no valid {LABEL_BOUND} label, so the budget cannot count it; use another group")
    return value


def plan_compare(
    store: RunRecordStore,
    *,
    project_root: Union[str, Path],
    prompt: Union[str, Path],
    cases: Sequence[CompareCase],
    candidates: Sequence[Candidate],
    flow: str,
    content: str,
    max_usd: float,
    scorer: Optional[Scorer] = None,
    group_id: Optional[str] = None,
    label: Optional[str] = None,
    tool: Optional[Mapping[str, Any]] = None,
) -> ComparePlan:
    """Check a comparison and plan each attempt; write no record.

    ``prompt`` is a ``llm-kit.prompt-file/v1`` file under ``project_root``;
    each case fills its slots. ``candidates`` are the resolved selections (two
    or more, distinct names). ``content`` is the content class of every record
    and must be in the store's policy. ``max_usd`` is the budget of the group
    (see the module docstring). ``group_id`` defaults to an id derived from the
    flow, the prompt hash, the cases and the selection names, so a run of the
    same comparison again continues the same group. ``tool`` is
    ``provenance.tool`` (``{name, version}``).

    Returns a :class:`ComparePlan`. Raises :class:`CompareError`: ``invalid``
    (an argument, a case that does not fill the prompt, two selections that
    ask the same question), ``policy``, ``unpriced`` (an attempt that needs a
    call has no cost bound) or ``conflict``; and
    :class:`~llm_kit.runs.store.RunStoreError` when the store cannot be read.
    """
    budget = _check_plan_args(store, flow, content, candidates, cases, max_usd)
    root = Path(project_root)
    prompt_path = Path(prompt) if Path(prompt).is_absolute() else Path.cwd() / prompt
    relative = _project_relative(prompt_path, root)
    try:
        raw = prompt_path.read_bytes()
    except OSError as exc:
        raise CompareError("invalid", f"cannot read the prompt {relative}: {exc}") from None
    try:
        parsed = parse_prompt_file(raw, where=relative)
        text = raw.decode("utf-8")
    except (GraphError, UnicodeDecodeError) as exc:
        raise CompareError("invalid", f"the prompt {relative}: {exc}") from None
    group = {"id": group_id or _default_group_id(flow, parsed.sha256, cases, [c.selection for c in candidates]),
             "kind": "compare", "label": label}
    prompt_meta = {"id": Path(relative).name, "file": relative, "sha256": parsed.sha256}
    provenance = {"tool": {**_TOOL, **(tool or {})}, "git_sha": None, "git_dirty": None,
                  "files": {relative: hashlib.sha256(raw).hexdigest()},
                  "scorer": None if scorer is None else scorer.identity()}
    planned: list[tuple[CompareCase, Candidate, str, dict, str]] = []
    for case in cases:
        try:
            filled = render_prompt(text, case.slots, where=relative)
        except GraphError as exc:
            raise CompareError("invalid", f"case {case.id!r} does not fill the prompt: {exc}") from None
        seen: dict[str, str] = {}
        for candidate in candidates:
            fields = _fields(case, candidate, filled, flow=flow, group=group, content=content,
                             prompt_meta=prompt_meta, provenance=provenance)
            key = reuse_key(start_record(**fields, record_id=_PLACEHOLDER_ID))
            if key in seen:
                raise CompareError("invalid", f"selections {seen[key]!r} and {candidate.selection!r} ask the same "
                                   "question (same model, effort and request); compare distinct selections")
            seen[key] = candidate.selection
            planned.append((case, candidate, filled, fields, key))
    index = store.index()
    keys = {item[4] for item in planned}
    found: dict[str, list[dict]] = {}
    for row in index.query(flow=flow, status="ok"):
        if row["reuse_key"] in keys:
            found.setdefault(row["reuse_key"], []).append(store.get(row["id"]))
    spent = sum(_charge(store.get(row["id"])) for row in index.query(group=group["id"]))
    want = None if scorer is None else scorer.sha256
    attempts = []
    for case, candidate, filled, fields, key in planned:
        records = found.get(key, [])
        mine = [r for r in records if r["group"]["id"] == group["id"] and _scorer_sha(r) == want]
        if mine:
            action, record_id, bound = "done", mine[0]["id"], 0.0
        elif records:
            source = records[0]
            action, record_id, bound = "copy", source["labels"].get(LABEL_REUSED_FROM, source["id"]), 0.0
        else:
            action, record_id, bound = "call", None, attempt_bound_usd(candidate, filled)
        attempts.append(PlannedAttempt(case, candidate, filled, key, action, record_id, bound, fields))
    return ComparePlan(store, group, flow, content, budget, spent, tuple(attempts), scorer)


# -- running --------------------------------------------------------------------


@dataclass(frozen=True)
class AttemptOutcome:
    """What a run did for one planned attempt.

    ``action`` is the planned action, or ``not-run`` when the run stopped
    first; ``record`` is the record of the attempt (None when not run);
    ``text`` the answer text (None when there is none); ``charged_usd`` what
    the attempt added to the group's spend in this run.
    """

    case_id: str
    selection: str
    action: str
    record: Optional[Mapping[str, Any]]
    text: Optional[str]
    charged_usd: float = 0.0


@dataclass(frozen=True)
class CompareResult:
    """The result of :func:`run_compare`.

    ``spent_usd`` is the group's spend after the run; ``stopped`` is None when
    every attempt ran, ``budget`` when an attempt's bound would cross
    ``max_usd``, ``halted`` after a halted call, ``over-budget`` when a
    recorded cost took the spend over ``max_usd``, or ``scorer-failed`` (on
    the partial result of a :class:`CompareError`); ``message`` says why.
    """

    group: Mapping[str, Any]
    flow: str
    max_usd: float
    spent_usd: float
    stopped: Optional[str]
    message: Optional[str]
    outcomes: tuple[AttemptOutcome, ...]

    def to_dict(self) -> dict:
        """The result as JSON: totals per selection, then the answers side by side for each case."""
        selections: dict[str, dict] = {}
        cases: dict[str, dict] = {}
        for outcome in self.outcomes:
            record = outcome.record
            status = "not-run" if record is None else record["status"]
            totals = selections.setdefault(outcome.selection, {"statuses": {}, "charged_usd": 0.0, "scores": {}})
            totals["statuses"][status] = totals["statuses"].get(status, 0) + 1
            totals["charged_usd"] += outcome.charged_usd
            for score in (record or {}).get("scores", ()):
                value = score["value"]
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals["scores"].setdefault(score["name"], []).append(value)
            answers = cases.setdefault(outcome.case_id, {})
            answers[outcome.selection] = {
                "action": outcome.action, "record": None if record is None else record["id"], "status": status,
                "error": None if record is None else record.get("error"),
                "scores": [] if record is None else list(record.get("scores", ())), "text": outcome.text,
            }
        for totals in selections.values():
            totals["scores"] = {name: sum(values) / len(values) for name, values in sorted(totals["scores"].items())}
        return {
            "group": dict(self.group), "flow": self.flow, "max_usd": self.max_usd, "spent_usd": self.spent_usd,
            "stopped": self.stopped, "message": self.message, "selections": selections,
            "cases": [{"id": case_id, "answers": answers} for case_id, answers in cases.items()],
        }


def _usage(result: CallResult) -> Optional[dict]:
    usage = {"input_tokens": result.input_tokens, "output_tokens": result.output_tokens,
             "cached_tokens": result.cached_tokens, "reasoning_tokens": result.reasoning_tokens}
    return None if all(value is None for value in usage.values()) else usage


def _cost(candidate: Candidate, result: CallResult) -> dict:
    """The recorded cost of a call: reported, else the larger estimate, else unknown (usd null)."""
    if result.reported_usd is not None:
        return {"usd": _finite(result.reported_usd, "reported_usd"), "source": "reported"}
    price = candidate.price
    if price is not None and price.free:
        return {"usd": 0.0, "source": "estimated"}
    figures = []
    if result.estimated_usd is not None:
        figures.append(_finite(result.estimated_usd, "estimated_usd"))
    if price is not None and result.input_tokens is not None and result.output_tokens is not None:
        cached = min(result.cached_tokens or 0, result.input_tokens)
        cache_rate = price.input if price.cache_hit is None else price.cache_hit
        figures.append(((result.input_tokens - cached) * price.input + cached * cache_rate
                        + result.output_tokens * price.output) / 1_000_000)
    if figures:
        return {"usd": max(figures), "source": "estimated"}
    return {"usd": None, "source": "unknown"}


def _result_parts(result: CallResult) -> tuple[str, Optional[dict], Optional[dict], dict[str, str]]:
    """``(status, output, error, blobs)`` of the result event of a call."""
    blobs: dict[str, str] = {}
    output = None
    if result.text is not None:
        ref = blob_ref(result.text)
        blobs[ref] = result.text
        output = {"text": ref}
    if result.status == "completed" or result.error is None:  # CallResult requires an error otherwise
        return "ok", output, None, blobs
    error = {"code": str(result.error["code"]), "message": result.error.get("message")}
    return "failed", output, error, blobs


class _Run:
    """The state of one :func:`run_compare`: the store, the spend and the outcomes so far."""

    def __init__(self, plan: ComparePlan, call: Callable[[Candidate, str], CallResult]) -> None:
        self.plan = plan
        self.store = plan.store
        self.call = call
        self.spent = plan.spent_usd
        self.outcomes: list[AttemptOutcome] = []

    def result(self, stopped: Optional[str], message: Optional[str]) -> CompareResult:
        ran = len(self.outcomes)
        rest = [AttemptOutcome(a.case.id, a.candidate.selection, "not-run", None, None)
                for a in self.plan.attempts[ran:]]
        return CompareResult(self.plan.group, self.plan.flow, self.plan.max_usd, self.spent, stopped, message,
                             tuple(self.outcomes) + tuple(rest))

    def _case_blob(self, attempt: PlannedAttempt) -> dict[str, str]:
        return {attempt.fields["case"]["input"]: _case_bytes(attempt.case).decode("utf-8")}

    def _score(self, record: dict, blobs: Mapping[str, str]) -> tuple[list, dict[str, bytes]]:
        if self.plan.scorer is None:
            return [], {}
        return _apply_scorer(self.plan.scorer, record, blobs)

    def done(self, attempt: PlannedAttempt) -> None:
        record = self.store.get(str(attempt.record_id))
        self.outcomes.append(AttemptOutcome(attempt.case.id, attempt.candidate.selection, "done", record,
                                            self._text(record)))

    def _text(self, record: Mapping) -> Optional[str]:
        output = record.get("output")
        return None if output is None else self.store.get_blob(output["text"]).decode("utf-8")

    def copy(self, attempt: PlannedAttempt) -> None:
        source = self.store.get(str(attempt.record_id))
        text = self._text(source) or ""
        started = start_record(**attempt.fields, labels={LABEL_REUSED_FROM: source["id"]})
        blobs = {**self._case_blob(attempt), source["output"]["text"]: text}
        unscored = finish_record(started, status="ok", output={"text": source["output"]["text"],
                                                               "parsed": source["output"]["parsed"]},
                                 ended_at=started["started_at"])
        scores, notes = self._score(unscored, blobs)
        record = finish_record(started, status="ok", output=unscored["output"], scores=scores,
                               ended_at=started["started_at"])
        self.store.put(record, {**self._case_blob(attempt), **notes})
        self.outcomes.append(AttemptOutcome(attempt.case.id, attempt.candidate.selection, "copy", record, text))

    def send(self, attempt: PlannedAttempt) -> Optional[tuple[str, str]]:
        """Run one call attempt; return ``(stopped, message)`` when the run must stop after it."""
        candidate = attempt.candidate
        if self.spent + attempt.bound_usd > self.plan.max_usd:
            return "budget", (f"case {attempt.case.id!r} on {candidate.selection!r} could cost up to "
                              f"${attempt.bound_usd:.6f}; the group has spent ${self.spent:.6f} of "
                              f"${self.plan.max_usd:.6f}")
        started = start_record(**attempt.fields, labels={LABEL_BOUND: repr(attempt.bound_usd)})
        self.store.put(started, self._case_blob(attempt))
        try:
            answer = self.call(candidate, attempt.prompt)
            if not isinstance(answer, CallResult):
                raise CompareError("invalid", f"the caller returned {type(answer).__name__}, not a CallResult")
        except Exception as exc:
            failed = finish_record(started, status="failed", error={"code": "caller-error", "message": str(exc)})
            self.store.put(failed)
            self.spent += attempt.bound_usd
            self.outcomes.append(AttemptOutcome(attempt.case.id, candidate.selection, "call", failed, None,
                                                attempt.bound_usd))
            raise
        status, output, error, blobs = _result_parts(answer)
        cost = _cost(candidate, answer)
        common: dict[str, Any] = {"usage": _usage(answer), "cost": cost,
                  "timings": {"first_output_ms": None, "total_ms": answer.total_ms}}
        stop: Optional[tuple[str, str]] = None
        scores: list = []
        notes: dict[str, bytes] = {}
        if status == "ok":
            unscored = finish_record(started, status="ok", output=output, **common)
            try:
                scores, notes = self._score(unscored, {**self._case_blob(attempt), **blobs})
            except CompareError as exc:
                status, error = "invalid", {"code": "scorer-failed", "message": str(exc)}
                stop = ("scorer-failed", str(exc))
        record = finish_record(started, status=status, output=output, error=error, scores=scores, **common)
        self.store.put(record, {**blobs, **notes})
        charged = attempt.bound_usd if cost["usd"] is None else cost["usd"]
        self.spent += charged
        self.outcomes.append(AttemptOutcome(attempt.case.id, candidate.selection, "call", record, answer.text,
                                            charged))
        if stop is None and answer.status == "halted":
            halt = error or {}
            stop = ("halted", f"the call of {candidate.selection!r} halted: {halt.get('code')}: {halt.get('message')}")
        if stop is None and self.spent > self.plan.max_usd:
            stop = ("over-budget", (f"the recorded cost took the group's spend to ${self.spent:.6f}, over "
                                    f"${self.plan.max_usd:.6f}"))
        return stop


def run_compare(plan: ComparePlan, call: Callable[[Candidate, str], CallResult]) -> CompareResult:
    """Run ``plan``: reuse what it can, call the rest once each, and record every attempt.

    ``call(candidate, prompt) -> CallResult`` makes exactly one model call and
    never retries. For each ``call`` attempt the run writes the started event
    (with its cost bound) before the call and the result event after it; the
    scorer, when the plan has one, scores each ``ok`` answer before its result
    is written. ``copy`` attempts write one record that reuses an answer with
    no call; ``done`` attempts write nothing.

    Returns a :class:`CompareResult`; ``stopped`` says when the run stopped
    early (budget, a halted call, a cost over the budget). Raises
    :class:`CompareError` ``scorer-failed`` (with the partial ``result``)
    after it recorded the attempt as ``invalid``; an exception from ``call``
    is raised again after the attempt is recorded as ``failed``
    (``caller-error``, charged at its bound). Raises
    :class:`~llm_kit.runs.store.RunStoreError` when the store refuses a write.
    """
    run = _Run(plan, call)
    for attempt in plan.attempts:
        if attempt.action == "done":
            run.done(attempt)
            continue
        if attempt.action == "copy":
            try:
                run.copy(attempt)
            except CompareError as exc:
                raise CompareError(exc.code, str(exc), result=run.result(exc.code, str(exc))) from None
            continue
        stop = run.send(attempt)
        if stop is not None:
            stopped, message = stop
            if stopped == "scorer-failed":
                raise CompareError("scorer-failed", message, result=run.result(stopped, message))
            return run.result(stopped, message)
    return run.result(None, None)

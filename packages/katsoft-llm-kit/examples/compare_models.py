"""Compare two model selections on two cases, run it again, and see every answer reused.

Run it in a plain Python environment; it needs no AI harness, plugin, client
app, endpoint registry, network or extra:

    python -m venv .venv
    .venv/bin/python -m pip install katsoft-llm-kit
    .venv/bin/python compare_models.py

The script makes a project with one prompt file and a run store in a temporary
directory. It plans a comparison of two candidates with a price each (a dry
run: the cost bound, no call), runs it with a stand-in caller in place of a
model, runs it a second time (every attempt is done, nothing is called), and
shows that a budget smaller than one attempt's bound starts no call. A real
client gives a caller that sends one request (``llm-kit runs compare`` gives
the one of ``llm_kit.dispatch``). It exits non-zero with a message when a check
fails.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from llm_kit.runs import (
    CallResult,
    Candidate,
    CompareCase,
    Price,
    RunRecordStore,
    plan_compare,
    run_compare,
)

PROMPT = "A prompt file: this header is for people.\n---PROMPT---\nAnswer in one word: {{question}}"
CASES = [CompareCase("sum", {"question": "What is two plus two?"}),
         CompareCase("color", {"question": "What color is the sky on a clear day?"})]
CANDIDATES = [
    Candidate(selection="answer.small", row="small", model="example/small", max_output_tokens=20,
              price=Price(input=0.1, output=0.4)),
    Candidate(selection="answer.large", row="large", model="example/large", effort="high", max_output_tokens=20,
              price=Price(input=1.0, output=4.0)),
]


class StandIn:
    """A caller that answers from a table instead of a model, and counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, candidate: Candidate, prompt: str) -> CallResult:
        self.calls += 1
        answer = "Four." if "two plus two" in prompt else "Blue."
        return CallResult("completed", text=answer, input_tokens=len(prompt) // 4, output_tokens=2,
                          reported_usd=0.00001, total_ms=5.0)


def check(condition: bool, message: str) -> None:
    """Exit with ``message`` when ``condition`` is false."""
    if not condition:
        raise SystemExit(f"check failed: {message}")


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "project"
        (root / "prompts").mkdir(parents=True)
        prompt = root / "prompts" / "one-word.md"
        prompt.write_text(PROMPT, encoding="utf-8")
        store = RunRecordStore.init(Path(directory) / "runs", accepts=["synthetic"])
        arguments = {"project_root": root, "prompt": prompt, "cases": CASES, "candidates": CANDIDATES,
                     "flow": "example.one-word", "content": "synthetic", "max_usd": 0.01}

        plan = plan_compare(store, **arguments)
        print("dry run:", json.dumps({k: plan.to_dict()[k] for k in ("bound_usd", "fits")}))
        check([a.action for a in plan.attempts] == ["call"] * 4, "the first plan calls every attempt")

        caller = StandIn()
        result = run_compare(plan, caller)
        summary = result.to_dict()
        print("first run:", json.dumps({"spent_usd": summary["spent_usd"],
                                        "answers": {c["id"]: {s: a["text"] for s, a in c["answers"].items()}
                                                    for c in summary["cases"]}}))
        check(caller.calls == 4 and result.stopped is None, "four calls and no stop")

        again = run_compare(plan_compare(store, **arguments), caller)
        check(caller.calls == 4, "the second run reuses every answer")
        print("second run:", [o.action for o in again.outcomes])

        tight = plan_compare(store, **{**arguments, "group_id": "tight", "max_usd": 0.0,
                                       "cases": [CompareCase("new", {"question": "Is ice cold?"})]})
        stopped = run_compare(tight, caller)
        check(stopped.stopped == "budget" and caller.calls == 4, "a zero budget starts no paid call")
        print("zero budget:", stopped.stopped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

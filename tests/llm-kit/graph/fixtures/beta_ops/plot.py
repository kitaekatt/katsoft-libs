"""Plot ops: prompt parts, checks, accept, score and keep-best."""
from llm_kit.graph.ops import OpContext, op

from .helpers import PROMPT_PARTS, numbered

BETA = "../beta/contracts/"


@op("afterplay.beta.plot_prompt_parts", version="1",
    inputs={"brief": {"$ref": BETA + "brief.v1.schema.json"}, "current": {}, "findings": {},
            "guidance": {"type": "string"}},
    outputs={"type": "object", "properties": {key: {"type": "string"} for key in PROMPT_PARTS}})
def plot_prompt_parts(ctx: OpContext, brief, current, findings, guidance):
    return {key: "" for key in PROMPT_PARTS}


@op("afterplay.beta.plot_errors", version="1",
    inputs={"answer": {"$ref": BETA + "plot-answer.v2.schema.json"}},
    outputs={"type": "object", "required": ["ok", "errors", "numbered"],
             "properties": {"ok": {"type": "boolean"}, "errors": {"type": "array"}, "numbered": {"type": "string"}}})
def plot_errors(ctx: OpContext, answer):
    errors = []
    return {"ok": not errors, "errors": errors, "numbered": numbered(errors)}


@op("afterplay.beta.plot_accept", version="1", inputs={"answer": {}},
    outputs={"type": "object", "properties": {"plot": {"$ref": BETA + "plot.v2.schema.json"}}})
def plot_accept(ctx: OpContext, answer):
    return {"plot": answer}


@op("afterplay.beta.plot_score", version="1",
    inputs={"evaluation": {"$ref": BETA + "plot-evaluation.v1.schema.json"}},
    outputs={"type": "object", "properties": {"score": {"type": "integer"}}})
def plot_score(ctx: OpContext, evaluation):
    return {"score": 0}


@op("afterplay.beta.keep_best", version="1",
    inputs={"incumbent": {}, "baseline": {}, "rounds": {"type": "array"}},
    outputs={"type": "object", "properties": {
        "plot": {"$ref": BETA + "plot.v2.schema.json"},
        "evaluation": {"$ref": BETA + "plot-evaluation.v1.schema.json"},
        "round": {"type": "integer"}}})
def keep_best(ctx: OpContext, incumbent, baseline, rounds):
    return {"plot": incumbent, "evaluation": baseline, "round": 0}

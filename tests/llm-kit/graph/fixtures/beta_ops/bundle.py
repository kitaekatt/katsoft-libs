"""Pose image, bundle and the commit adapter."""
from llm_kit.graph.ops import OpContext, op

BETA = "../beta/contracts/"


@op("afterplay.beta.pose_image", version="1",
    inputs={"pose": {"type": "object", "properties": {"id": {"type": "string"}}}},
    outputs={"type": "object", "properties": {"image": {"type": "string"}}},
    paid=True, limit="model-image")
def pose_image(ctx: OpContext, pose):
    return {"image": pose["id"]}


@op("afterplay.beta.bundle", version="1",
    inputs={"plot": {"$ref": BETA + "plot.v2.schema.json"}, "images": {"type": "array"}},
    outputs={"$ref": BETA + "beta-deliverable.v1.schema.json"})
def bundle(ctx: OpContext, plot, images):
    return {"plot": plot, "images": images}


@op("afterplay.beta.encode", version="1", commit=True,
    inputs={"bundle": {"$ref": BETA + "beta-deliverable.v1.schema.json"}},
    outputs={"type": "object", "properties": {"preview_url": {"type": "string"}, "files_sha256": {"type": "string"}}})
def encode(ctx: OpContext, bundle):
    return {"preview_url": "", "files_sha256": ""}

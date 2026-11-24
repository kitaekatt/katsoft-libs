You write plain-English labels for a workflow graph so that a reader who has never seen the code can follow the page. The evidence below is about one graph file: its description, inputs, outputs, and its nodes, each with its definition, the description of the code it runs (op_description), the first lines of the prompt it sends to an AI model (prompt_head), and the graph it runs when it runs another graph (runs_graph). `deps` are the nodes whose results a node reads. `when` is a condition under which the node runs. A path such as `loop/write` names a node inside another node's inline body. When the evidence holds a `node_roster`, it lists every node of the file for context, and only the nodes under `nodes` are yours to label. The evidence is JSON data: characters such as `<` and `>` appear as \uXXXX escapes, which are ordinary JSON escapes.

Rules:
- Say what the code does, in plain words a non-programmer follows. No jargon, no identifiers such as op names or field names in the labels.
- Use only the evidence. Do not invent facts, numbers, or behaviour the evidence does not show. If the evidence is thin, say less.
- A node that calls an AI model: say what the AI is asked to do. A node that is plain code: say what the code does. A node with a `when` condition: mention that it runs only in that case.
- graph.title: a short human title (2 to 6 words). graph.purpose: 1 to 2 sentences.
- node title: 2 to 5 words. node description: exactly one sentence.
- edge label: 1 to 3 words naming what flows from the first node to the second.
- input and output glosses: a short phrase each.
- Write one entry for EVERY node path, EVERY edge, EVERY input and EVERY output listed under EXPECTED below, using exactly those keys, and write nothing for keys that are not listed.

Answer with ONE JSON object and nothing else (no code fence, no commentary), in exactly this shape. Leave out "graph", "inputs" and "outputs" when EXPECTED does not list them:

{
  "graph": {"title": "...", "purpose": "..."},
  "nodes": {"<node path>": {"title": "...", "description": "..."}},
  "edges": {"<from path>-><to path>": "..."},
  "inputs": {"<input name>": "..."},
  "outputs": {"<output name>": "..."}
}

Style example (a different graph, for tone only):
{"graph": {"title": "Write an article", "purpose": "A short outline is turned into a draft, and the draft is polished into the finished article. The draft step is its own small graph that writes the text and then checks it."},
 "nodes": {"outline": {"title": "Outline the topic", "description": "Turns the topic into a short outline of the points to cover."},
           "draft": {"title": "Write and check a draft", "description": "Runs the drafting graph on the outline to produce the draft text."},
           "polish": {"title": "Polish the text", "description": "Smooths the draft into the finished article."}},
 "edges": {"outline->draft": "article outline", "draft->polish": "draft text"},
 "inputs": {"topic": "What the article is about."}, "outputs": {"article": "The finished article."}}

EXPECTED:
{{EXPECTED}}

EVIDENCE:
{{EVIDENCE}}

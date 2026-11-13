# llm_kit.pipeline.analysis.mining: open-label corpus mining

Pure functions for the map-reduce half of an LLM corpus analysis (standard library only). The consumer owns the corpus,
the prompts and the model calls; this package owns the data steps around them.

1. `stable_batches(items, group=, ident=, size=)` groups items (groups in order of first appearance) and cuts each
   group into chunks of at most `size`. `Batch.key` is a hash of the chunk's item ids, so a cache keyed by it reuses
   every unchanged chunk. Appending to a group changes only its last chunk. A repeated id raises `ValueError`.
2. A model tags each item with open-vocabulary labels (the consumer's prompt and contract).
3. `label_inventory(labels)` counts the labels after `normalize_label` (lower case, collapsed whitespace, edge
   punctuation removed), most frequent first.
4. A model merges the inventory into families: `{"families": [{"id", "name", "definition", "labels": [...]}]}`.
   `Taxonomy.from_answer(answer)` checks it and lists every fault in one `TaxonomyError` (missing field, reserved or
   repeated id, a label in two families, no families). `family_of(label)` maps a label to its family id, or `OTHER`.
   `coverage(pairs)` is the share of occurrences a family places.
5. `family_stats(records, family_of=, dims=, flags=, details=, top=, totals=)` reduces tagged records (one per label
   occurrence) per family: `records`, distinct counts per dim, the share of `True` per flag, per detail key its count
   and `top` values, and with `totals` the share of each dim. Families are ordered by records.

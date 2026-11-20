# exp-02 Batched actions

**Question.** When a turn needs several edits, how do LLM steps, tokens, latency and pass rate change if the
model sends all edits in one response, or as one list in a single tool call, instead of one edit per step?

**Dataset.** `data/examples.json`, 300 single-turn examples; gold `ops` per example (1–4 edits).

**Setup.** Model `sarvam/deepseekv4.1-flash-1017`, one run per example per arm, arms interleaved per example.
All arms get prompt v25's `detected_places` (exact matcher).

| arm | prompt | tools and loop |
|---|---|---|
| A-v25 | `orchestrator_v25` | `set_route`, `add_stop`, `remove_stop`, `undo`; the turn ends when the model answers with no tool call |
| B-parallel | `prompts/orchestrator_v25_parallel.md` (rule 6: all edit calls in one response) | same tools; the turn ends after a step whose edits all succeeded (`variants.run_turn_end_on_edit`) |
| C-batch | `prompts/orchestrator_v25_batch.md` (one tool section instead of four) | `apply_edits(ops)`: checks every op on a copy, writes once (all or none), ends the turn (`variants.apply_edits`) |

Extra metrics: `edit ops = gold ops` (the successful edits, in order, equal the gold `ops` or `alt_ops`),
partial writes (on examples with 2+ gold ops: the final trip equals a strict prefix of the gold ops),
failed edit calls.

Script: `run.py`. Results: not run yet.

<!-- LLM:START -->
<!-- LLM:END -->

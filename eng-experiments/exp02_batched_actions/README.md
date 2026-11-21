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

Run after exp-01, alongside exp-03's pass-rate run (which also calls Sarvam DeepSeek); arms are interleaved per example, so that load falls on every arm.

Script: `run.py` (or `exp02.ipynb`).

<!-- LLM:START -->
Run `exp02-sarvam-deepseekv4.1-flash-1017-20261121-142447.json`: model `sarvam/deepseekv4.1-flash-1017`, 300 examples, commit df08b81 (dirty tree), 2026-11-21 14:49.

| arm | prompt | setup |
|---|---|---|
| A-v25 | orchestrator_v25 | small edit tools; turn ends on a step with no tool call |
| B-parallel | orchestrator_v25_parallel | small edit tools, all in one response; turn ends after a step whose edits all succeeded |
| C-batch | orchestrator_v25_batch | one atomic apply_edits(ops) tool; ends the turn |

### Per arm

| arm | turns | passed | pass rate | LLM steps / turn | steps histogram | resolve_place / turn | input tok / turn | cached tok / turn | output tok / turn | cost USD (all turns) | turn s p50 [95% CI] | p90 | p95 | p99 or max | LLM call ms p50 | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| A-v25 | 300 | 294 | 0.98 | 2.02 | {1: 48, 2: 204, 3: 45, 4: 1, 5: 2} | 0.13 | 7771.0 | 7325.0 | 160.0 | 0.05744 | 1.81 [1.73, 2.00] | 3.11 | 3.74 | 6.17 | 878.0 | 0 |
| B-parallel | 300 | 294 | 0.98 | 1.14 | {1: 260, 2: 39, 3: 1} | 0.13 | 4302.0 | 4068.0 | 141.0 | 0.04104 | 1.17 [1.07, 1.23] | 2.25 | 3.0 | 5.32 | 1065.0 | 0 |
| C-batch | 300 | 295 | 0.9833 | 1.13 | {1: 262, 2: 37, 4: 1} | 0.12 | 4226.0 | 4015.0 | 156.0 | 0.04273 | 1.20 [1.15, 1.28] | 2.33 | 3.11 | 4.38 | 1138.0 | 0 |

### Paired differences (same example, arm − A-v25)

| pair | metric | examples | mean diff | 95% CI | median diff | up | down | same |
|---|---|---|---|---|---|---|---|---|
| B-parallel − A-v25 | LLM steps | 300 | -0.88 | [-0.950, -0.813] | -1.0 | 2 | 238 | 60 |
| B-parallel − A-v25 | resolve_place calls | 300 | -0.007 | [-0.023, 0.007] | 0.0 | 2 | 4 | 294 |
| B-parallel − A-v25 | input tokens | 300 | -3468.927 | [-3743.820, -3204.665] | -3876.0 | 61 | 239 | 0 |
| B-parallel − A-v25 | turn seconds | 300 | -0.669 | [-0.784, -0.560] | -0.632 | 51 | 249 | 0 |
| B-parallel − A-v25 | pass (0/1) | 300 | 0.0 | [-0.013, 0.013] | 0.0 | 2 | 2 | 296 |
| C-batch − A-v25 | LLM steps | 300 | -0.883 | [-0.950, -0.813] | -1.0 | 1 | 237 | 62 |
| C-batch − A-v25 | resolve_place calls | 300 | -0.01 | [-0.027, 0.003] | 0.0 | 1 | 4 | 295 |
| C-batch − A-v25 | input tokens | 300 | -3544.35 | [-3826.546, -3262.548] | -3937.0 | 1 | 299 | 0 |
| C-batch − A-v25 | turn seconds | 300 | -0.614 | [-0.736, -0.493] | -0.518 | 62 | 238 | 0 |
| C-batch − A-v25 | pass (0/1) | 300 | 0.003 | [-0.007, 0.013] | 0.0 | 2 | 1 | 297 |

### By tag

| tag | examples | A-v25: passed / steps / s p50 | B-parallel: passed / steps / s p50 | C-batch: passed / steps / s p50 |
|---|---|---|---|---|
| compound | 80 | 79 / 2.3 / 2.37 | 78 / 1.06 / 1.26 | 79 / 1.05 / 1.34 |
| replace | 21 | 21 / 2.14 / 1.95 | 21 / 1.0 / 0.94 | 21 / 1.0 / 1.18 |
| move | 20 | 19 / 2.5 / 2.82 | 18 / 1.0 / 1.3 | 19 / 1.0 / 1.55 |
| add | 72 | 70 / 2.0 / 2.06 | 70 / 1.11 / 1.39 | 70 / 1.07 / 1.23 |
| remove | 34 | 34 / 2.12 / 1.73 | 34 / 1.09 / 1.18 | 34 / 1.03 / 1.17 |
| undo | 21 | 19 / 2.29 / 1.82 | 20 / 1.0 / 0.98 | 20 / 1.0 / 1.17 |
| clear | 20 | 20 / 2.1 / 1.46 | 20 / 1.1 / 0.75 | 20 / 1.1 / 0.98 |
| round-trip | 22 | 22 / 2.23 / 1.88 | 22 / 1.09 / 1.34 | 22 / 1.05 / 1.26 |
| new-trip | 88 | 88 / 1.97 / 1.76 | 87 / 1.24 / 1.12 | 87 / 1.26 / 1.29 |
| same-name | 30 | 30 / 1.77 / 1.55 | 30 / 1.17 / 1.08 | 30 / 1.17 / 0.99 |
| no-edit | 20 | 20 / 1.05 / 0.96 | 20 / 1.05 / 0.96 | 20 / 1.05 / 1.16 |
| hinglish | 150 | 148 / 2.03 / 1.96 | 150 / 1.17 / 1.14 | 150 / 1.18 / 1.24 |
| english | 150 | 146 / 2.0 / 1.76 | 144 / 1.11 / 1.18 | 145 / 1.09 / 1.17 |
| long | 22 | 22 / 2.09 / 2.14 | 21 / 1.14 / 1.39 | 21 / 1.14 / 1.46 |

### Edits

| arm | edit examples | edit ops = gold ops | examples with 2+ gold ops | partial writes (a strict prefix of the gold ops) | failed edit calls / edit calls | LLM steps with an edit call / turn |
|---|---|---|---|---|---|---|
| A-v25 | 237 | 219 | 70 | 0 | 6/334 | 1.11 |
| B-parallel | 237 | 223 | 70 | 1 | 3/330 | 1.0 |
| C-batch | 237 | 225 | 70 | 0 | 2/238 | 1.0 |

<!-- LLM:END -->

# eng-experiments

Controlled experiments on the agent's design: latency, tokens and LLM calls per turn, with accuracy measured on
the same examples. Results are metrics and numbers only.

| exp | question | dataset | LLM | cost | status |
|---|---|---|---|---|---|
| [exp-00](exp00_baseline/README.md) | Where do a turn's time and tokens go? | real app traces (`traces.db`) | none | free | done |
| [exp-01](exp01_preresolve/README.md) | Code resolves places before the LLM call: steps, tokens, latency, pass rate | 300 examples | Sarvam DeepSeek V4.1 Flash | step 1 free; step 2 $0.29 (1,500 turns) | done (2026-11-21) |
| [exp-02](exp02_batched_actions/README.md) | One edit per step vs all edits in one response vs one `apply_edits` list | 300 examples | Sarvam DeepSeek V4.1 Flash | $0.14 (900 turns) | done (2026-11-21) |
| [exp-03](exp03_providers/README.md) | Same request to 6 models: price, p50/p90/p95/p99, pass rate | 100 of the 300 | all 6 in `app/llm.py` | $0.64 (600 calls + 600 turns) | done; Groq full turns partial (daily token cap: 55 / 27 / 9 answered of 100) |
| exp-04 | Audio front end (STT model, silence trim, speed-up) | recordings of the 300 texts | none | – | later |

## Dataset: `data/examples.json`

300 independent single-turn examples (no sessions), written by hand in `data/source/*.txt` and built by
`data/simulate.py`. Each has `start` (trip, earlier trips for undo, a pending question), `text`, gold `ops`
(1–4 edits; `alt_ops` = another accepted outcome), gold `mentions` (place spans → ids) and `expect` (the checks of
`evals/run.py`), computed from the ops so the expected trip is right by construction.

```
.venv/bin/python eng-experiments/data/simulate.py      # source/*.txt -> examples.json
.venv/bin/python eng-experiments/data/validate.py      # ids, trips, fairness, coverage (exit 1 on errors)
```

`validate.py` requires every gold mention to be reachable by `resolve_place` (ok with the gold id, or ambiguous
with it among the matches) and every tag below to have ≥ 20 examples.

| tag | n | tag | n | tag | n |
|---|---|---|---|---|---|
| new-trip | 88 | add | 72 | remove | 34 |
| replace | 21 | move | 20 | compound | 80 |
| position | 23 | undo | 21 | clear | 20 |
| round-trip | 22 | typo | 31 | alias | 34 |
| tourist | 32 | same-name | 30 | region | 23 |
| answer | 20 | foreign | 20 | trap-word | 20 |
| no-edit | 20 | many-stops | 28 | long (> 120 chars) | 22 |
| hinglish | 150 | english | 150 | | |

Edits per example: 1 → 230, 2 → 58, 3 → 9, 4 → 3. Starting trip size: 0 to 8 stops.

## Ground rules

1. Numbers only: each README has the question, the setup and generated tables.
2. Paired design: every arm runs the same examples; tables show per-arm values and per-example differences.
3. One run per example per arm.
4. Arms interleaved per example, so provider load drift hits every arm.
5. Percentiles p50/p90/p95 with a bootstrap 95 % CI of the median; p99 only when n ≥ 300, else max.
6. Each result file stores the commit (and whether the tree was dirty), prompts, model, place-list size, date.
7. Experiments never edit `app/`. Variants are applied in-process by `common.py` (prompt file,
   `detected_places`, tool registry, loop, other attributes) and restored after each turn.
8. LLM calls: Sarvam DeepSeek V4.1 Flash for exp-01/02; exp-03 calls every provider.
9. Paid scripts print the plan and a cost estimate and stop unless given `--yes`. An experiment process has
   its own spend cap (`EXP_BUDGET_USD`, default $5) instead of the server's $2/day.
10. Experiment sessions are named `exp-…`, so exp-00 and the trace viewer can tell them from real traffic.

## Running an experiment

Each experiment folder has a notebook that runs it end to end: `exp00_baseline/exp00.ipynb`,
`exp01_preresolve/exp01.ipynb`, `exp02_batched_actions/exp02.ipynb`, `exp03_providers/exp03.ipynb` (kernel: the
project's `.venv`). Settings are in the first cells. Paid cells run only after their switch is set to True
(`RUN_PAID`, or `RUN_LATENCY` / `RUN_PASSRATE` in exp-03); with the switches off, the whole notebook runs free and
shows the plan, the cost estimate and the latest results in `results/`. Results cells also regenerate the
README tables. The same runs work from the command line (`run.py` / `bench.py`, `--yes` to call the LLM).

## Files

```
expNN_*/expNN.ipynb       the notebook that runs each experiment
common.py                 Variant + applied(), seed(), run_one(), run_examples(), stats, report tables
retry.py                  reruns rows a provider refused (429/timeout/5xx) or a Mac sleep slowed, in place
data/                     source/*.txt, simulate.py, validate.py, examples.json
exp00_baseline/           analyze.py
exp01_preresolve/         matchers.py, offline.py, run.py, prompts/orchestrator_v25_fuzzy.md
exp02_batched_actions/    variants.py (arm B loop, arm C apply_edits), run.py, prompts/
exp03_providers/          bench.py
tests/                    harness + exp-02 arms with a fake LLM (no paid calls)
```

Tests: `TRACE_PRINT=0 .venv/bin/python -m pytest -q eng-experiments/tests`.

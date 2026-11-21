# exp-03 Providers: same request, price and latency

**Question.** For an identical request, what does each model in `app/llm.py` cost, how long does one call take
(p50/p90/p95/p99), and how does it do on full turns?

**Dataset.** 100 of the 300 examples, picked round-robin over the tags (`results/sample_ids.json`; every tag
appears at least 5 times).

**Setup.**
- *One step-1 call per example per model*: the request the app would send for the first LLM step (prompt v25,
  exact `detected_places`, the example's seeded state, the tool registry), the same content for every model.
  Models called round-robin per example; no retries; 429s and timeouts counted separately and left out of the
  latency numbers; each Groq model paced 30 s apart (plan limit 8k tokens/min). Latency = wall time of
  `llm.step()` from this machine. Input tokens are as each provider counts them (tokenizers differ).
- *Full agent turn*: each model runs the 100 examples once (models side by side in threads, Groq turns 65 s
  apart), scored with `evals/run.py`'s `check()`.
- n = 100 calls per model, so p99 is reported as the max.

The one-call part ran alone. The full-turn part ran alongside exp-01/exp-02 (Sarvam DeepSeek), and its turns use the app's normal client (one retry): a Groq turn that hit the rate limit includes the SDK's wait in its seconds.

Groq plan (on_demand) also caps each model at 200,000 tokens per day (TPD); a full turn is ~8k tokens. The Groq
full-turn rows that ended on that cap are "refused by provider" in the table, so Groq's answered n is 55 / 27 / 9,
not 100. The Mac slept during the run (2026-11-21 03:28–13:49); 5 DeepSeek turns refused by Sarvam (429, while
exp-01 ran on the same model) were rerun with `../retry.py`.

Script: `bench.py` (or `exp03.ipynb`).

<!-- RESULTS:START -->
Files: `latency-20261121-022457.jsonl`, `passrate-20261121-031436.jsonl`. Commit df08b81.

### One step-1 call per example (same request for every model)

| model | calls | ok | 429 | timeout | other error | p50 ms | p50 95% CI | p90 ms | p95 ms | p99 or max ms | input tok p50 (provider) | cached share | output tok p50 | USD / call | valid tool names |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| openai/gpt-5.4-nano | 100 | 100 | 0 | 0 | 0 | 2625.0 | [2562, 2709] | 3132.0 | 3404.0 | 4858.0 | 2946.0 | 85% | 62.0 | 0.000227 | 100/100 |
| sarvam/gemma4 | 100 | 100 | 0 | 0 | 0 | 759.0 | [715, 855] | 1341.0 | 1533.0 | 2354.0 | 3430.0 | 84% | 153.0 | 0.00073 | 100/100 |
| sarvam/deepseekv4.1-flash-1017 | 100 | 100 | 0 | 0 | 0 | 934.0 | [872, 996] | 1602.0 | 2335.0 | 3125.0 | 3731.0 | 96% | 78.0 | 9.1e-05 | 100/100 |
| groq/openai/gpt-oss-120b | 100 | 100 | 0 | 0 | 0 | 406.0 | [375, 468] | 869.0 | 983.0 | 1474.0 | 3078.0 | 69% | 70.0 | 0.000347 | 100/100 |
| groq/openai/gpt-oss-20b | 100 | 98 | 0 | 0 | 2 | 496.0 | [416, 580] | 729.0 | 803.0 | 1502.0 | 3079.0 | 61% | 61.0 | 0.000183 | 96/98 |
| groq/qwen/qwen3.8-27b | 100 | 51 | 49 | 0 | 0 | 497.0 | [477, 506] | 607.0 | 690.0 | 738.0 | 3770.0 | 0% | 41.0 | 0.003208 | 51/51 |

### Full agent turn on the same examples

Refused by provider = the turn ended on a 429 / timeout / connection / 5xx error (not scored); rerun = provider-refused or slept-through turns run again with `retry.py` (counted under their new result). Model errors = other errors on answered turns (e.g. a 400 for a malformed tool call), scored as failures. Turn seconds include Groq's own queueing only when a call waited server-side; the pause between turns is not counted.

| model | turns | refused by provider | rerun | answered | passed | pass rate (of answered) | LLM steps / turn | turn s p50 | p90 | p99 or max | USD / turn | model errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| openai/gpt-5.4-nano | 100 | 0 | 0 | 100 | 96 | 0.96 | 2.07 | 4.86 | 7.28 | 9.3 | 0.000461 | 0 |
| sarvam/gemma4 | 100 | 0 | 0 | 100 | 99 | 0.99 | 1.99 | 1.0 | 1.69 | 5.24 | 0.001154 | 0 |
| sarvam/deepseekv4.1-flash-1017 | 100 | 0 | 5 | 100 | 98 | 0.98 | 2.0 | 2.09 | 3.42 | 6.34 | 0.000162 | 0 |
| groq/openai/gpt-oss-120b | 100 | 45 | 0 | 55 | 51 | 0.927 | 2.15 | 1.19 | 1.68 | 31.3 | 0.000785 | 0 |
| groq/openai/gpt-oss-20b | 100 | 73 | 0 | 27 | 24 | 0.889 | 2.78 | 1.51 | 16.89 | 27.16 | 0.000514 | 6 |
| groq/qwen/qwen3.8-27b | 100 | 91 | 0 | 9 | 7 | 0.778 | 2.11 | 8.25 | 42.03 | 42.98 | 0.007064 | 0 |

<!-- RESULTS:END -->

# exp-01 Pre-resolved places in the input

**Question.** If code finds and looks up the place names in the raw text before the LLM call, and puts the
results in the input, how do LLM steps, `resolve_place` calls, tokens, latency and pass rate change?

**Dataset.** `data/examples.json`, 300 single-turn examples (gold: `mentions`, `expect`).

## Step 1: matcher check (no LLM)

How well code alone finds the names and resolves them, against the gold `mentions`.

- `exact (v25)`: `places.find_exact()` (exact names and aliases of 4+ letters) then `tools.lookup()`, as
  `agent.detected_places()` does in prompt v25.
- `exact+fuzzy>=t`: the same, plus spelling matches on the words exact left over (1-3 word phrases, single words
  of 4+ letters, best `places.resolve()` score ≥ t), then `tools.lookup()`.
- Correct = overlaps a gold mention and is `ok` with the gold id, or `ambiguous` with every gold id among its
  matches. `wrong_ok` = `ok` with an id that is not the gold one. `examples_all_covered` = examples whose every
  gold mention is resolved by the matcher; `..._incl_trip` also counts mentions of stops already on the trip
  (their ids are in the input anyway).

Script: `offline.py`.

## Step 2: LLM run (paid)

Model `sarvam/deepseekv4.1-flash-1017`, 300 examples, one run each per arm (5 arms), arms interleaved per example.

| arm | prompt | what the LLM gets before its first call |
|---|---|---|
| 1 baseline | `orchestrator_v24` | raw text only; every name needs `resolve_place` |
| 2 pre-resolved exact | `orchestrator_v25` | `detected_places` from the exact matcher (ok → id, ambiguous → options) |
| 3-fuzzy85 / 3-fuzzy90 / 3-fuzzy95 | `prompts/orchestrator_v25_fuzzy.md` | `detected_places` from exact + fuzzy, one arm per threshold (85, 90, 95) |

Run alongside exp-03's pass-rate run (which also calls Sarvam DeepSeek); arms are interleaved per example, so that load falls on every arm. 28 turns (16 Sarvam 429s, 2 timeouts, 10 that overlapped a sleep of the Mac between 03:28 and 13:49) were rerun with `../retry.py` and replaced in place (`"retried": true` in the row).

Script: `run.py` (or `exp01.ipynb`).

<!-- OFFLINE:START -->
Commit df08b81 (dirty tree), 300 examples.

### All examples

| matcher | gold_mentions | detections | correct | precision | recall | false_positives | wrong_ok | examples_all_covered | examples_all_known_incl_trip |
|---|---|---|---|---|---|---|---|---|---|
| exact (v25) | 560 | 535 | 513 | 0.9589 | 0.9161 | 22 | 0 | 206/251 | 221/251 |
| exact+fuzzy>=85 | 560 | 584 | 541 | 0.9264 | 0.9661 | 41 | 2 | 232/251 | 233/251 |
| exact+fuzzy>=90 | 560 | 566 | 538 | 0.9505 | 0.9607 | 26 | 2 | 229/251 | 230/251 |
| exact+fuzzy>=95 | 560 | 545 | 522 | 0.9578 | 0.9321 | 23 | 0 | 215/251 | 223/251 |

### Tag: typo (31 examples)

| matcher | gold_mentions | detections | correct | precision | recall | false_positives | wrong_ok | examples_all_covered | examples_all_known_incl_trip |
|---|---|---|---|---|---|---|---|---|---|
| exact (v25) | 68 | 45 | 45 | 1.0 | 0.6618 | 0 | 0 | 9/30 | 19/30 |
| exact+fuzzy>=85 | 68 | 69 | 68 | 0.9855 | 1.0 | 1 | 0 | 30/30 | 30/30 |
| exact+fuzzy>=90 | 68 | 65 | 65 | 1.0 | 0.9559 | 0 | 0 | 27/30 | 27/30 |
| exact+fuzzy>=95 | 68 | 50 | 50 | 1.0 | 0.7353 | 0 | 0 | 14/30 | 20/30 |

### Tag: alias (34 examples)

| matcher | gold_mentions | detections | correct | precision | recall | false_positives | wrong_ok | examples_all_covered | examples_all_known_incl_trip |
|---|---|---|---|---|---|---|---|---|---|
| exact (v25) | 90 | 88 | 88 | 1.0 | 0.9778 | 0 | 0 | 32/34 | 32/34 |
| exact+fuzzy>=85 | 90 | 91 | 88 | 0.967 | 0.9778 | 2 | 1 | 32/34 | 32/34 |
| exact+fuzzy>=90 | 90 | 90 | 88 | 0.9778 | 0.9778 | 1 | 1 | 32/34 | 32/34 |
| exact+fuzzy>=95 | 90 | 88 | 88 | 1.0 | 0.9778 | 0 | 0 | 32/34 | 32/34 |

### Tag: trap-word (20 examples)

| matcher | gold_mentions | detections | correct | precision | recall | false_positives | wrong_ok | examples_all_covered | examples_all_known_incl_trip |
|---|---|---|---|---|---|---|---|---|---|
| exact (v25) | 31 | 49 | 30 | 0.6122 | 0.9677 | 19 | 0 | 15/16 | 16/16 |
| exact+fuzzy>=85 | 31 | 52 | 31 | 0.5962 | 1.0 | 21 | 0 | 16/16 | 16/16 |
| exact+fuzzy>=90 | 31 | 51 | 31 | 0.6078 | 1.0 | 20 | 0 | 16/16 | 16/16 |
| exact+fuzzy>=95 | 31 | 51 | 31 | 0.6078 | 1.0 | 20 | 0 | 16/16 | 16/16 |

### Tag: same-name (30 examples)

| matcher | gold_mentions | detections | correct | precision | recall | false_positives | wrong_ok | examples_all_covered | examples_all_known_incl_trip |
|---|---|---|---|---|---|---|---|---|---|
| exact (v25) | 43 | 44 | 42 | 0.9545 | 0.9767 | 2 | 0 | 23/24 | 23/24 |
| exact+fuzzy>=85 | 43 | 47 | 43 | 0.9149 | 1.0 | 4 | 0 | 24/24 | 24/24 |
| exact+fuzzy>=90 | 43 | 45 | 43 | 0.9556 | 1.0 | 2 | 0 | 24/24 | 24/24 |
| exact+fuzzy>=95 | 43 | 44 | 42 | 0.9545 | 0.9767 | 2 | 0 | 23/24 | 23/24 |

### Tag: region (23 examples)

| matcher | gold_mentions | detections | correct | precision | recall | false_positives | wrong_ok | examples_all_covered | examples_all_known_incl_trip |
|---|---|---|---|---|---|---|---|---|---|
| exact (v25) | 39 | 25 | 25 | 1.0 | 0.641 | 0 | 0 | 9/23 | 9/23 |
| exact+fuzzy>=85 | 39 | 25 | 25 | 1.0 | 0.641 | 0 | 0 | 9/23 | 9/23 |
| exact+fuzzy>=90 | 39 | 25 | 25 | 1.0 | 0.641 | 0 | 0 | 9/23 | 9/23 |
| exact+fuzzy>=95 | 39 | 25 | 25 | 1.0 | 0.641 | 0 | 0 | 9/23 | 9/23 |

### Tag: tourist (32 examples)

| matcher | gold_mentions | detections | correct | precision | recall | false_positives | wrong_ok | examples_all_covered | examples_all_known_incl_trip |
|---|---|---|---|---|---|---|---|---|---|
| exact (v25) | 101 | 98 | 98 | 1.0 | 0.9703 | 0 | 0 | 29/32 | 30/32 |
| exact+fuzzy>=85 | 101 | 100 | 100 | 1.0 | 0.9901 | 0 | 0 | 31/32 | 31/32 |
| exact+fuzzy>=90 | 101 | 100 | 100 | 1.0 | 0.9901 | 0 | 0 | 31/32 | 31/32 |
| exact+fuzzy>=95 | 101 | 99 | 99 | 1.0 | 0.9802 | 0 | 0 | 30/32 | 31/32 |

### Tag: answer (20 examples)

| matcher | gold_mentions | detections | correct | precision | recall | false_positives | wrong_ok | examples_all_covered | examples_all_known_incl_trip |
|---|---|---|---|---|---|---|---|---|---|
| exact (v25) | 16 | 16 | 15 | 0.9375 | 0.9375 | 1 | 0 | 13/14 | 13/14 |
| exact+fuzzy>=85 | 16 | 18 | 16 | 0.8889 | 1.0 | 2 | 0 | 14/14 | 14/14 |
| exact+fuzzy>=90 | 16 | 17 | 16 | 0.9412 | 1.0 | 1 | 0 | 14/14 | 14/14 |
| exact+fuzzy>=95 | 16 | 16 | 15 | 0.9375 | 0.9375 | 1 | 0 | 13/14 | 13/14 |

### Tag: hinglish (150 examples)

| matcher | gold_mentions | detections | correct | precision | recall | false_positives | wrong_ok | examples_all_covered | examples_all_known_incl_trip |
|---|---|---|---|---|---|---|---|---|---|
| exact (v25) | 269 | 256 | 244 | 0.9531 | 0.9071 | 12 | 0 | 102/125 | 108/125 |
| exact+fuzzy>=85 | 269 | 286 | 259 | 0.9056 | 0.9628 | 26 | 1 | 115/125 | 116/125 |
| exact+fuzzy>=90 | 269 | 273 | 256 | 0.9377 | 0.9517 | 16 | 1 | 112/125 | 113/125 |
| exact+fuzzy>=95 | 269 | 259 | 246 | 0.9498 | 0.9145 | 13 | 0 | 104/125 | 108/125 |

### Tag: english (150 examples)

| matcher | gold_mentions | detections | correct | precision | recall | false_positives | wrong_ok | examples_all_covered | examples_all_known_incl_trip |
|---|---|---|---|---|---|---|---|---|---|
| exact (v25) | 291 | 279 | 269 | 0.9642 | 0.9244 | 10 | 0 | 104/126 | 113/126 |
| exact+fuzzy>=85 | 291 | 298 | 282 | 0.9463 | 0.9691 | 15 | 1 | 117/126 | 117/126 |
| exact+fuzzy>=90 | 291 | 293 | 282 | 0.9625 | 0.9691 | 10 | 1 | 117/126 | 117/126 |
| exact+fuzzy>=95 | 291 | 286 | 276 | 0.965 | 0.9485 | 10 | 0 | 111/126 | 115/126 |

### False positives and wrong `ok` results (text -> id)

- **exact (v25)**: false positives: sambhar->sambhar(ok) ×3, khana->khanna(ok) ×3, puri->puri(ok) ×2, Zero->ziro(ok) ×2, lava->lava(ok) ×2, Anand->anand(ok) ×2, Bihar->bihar-sharif(ok) ×2, Sagar->sagar(ok) ×2, anand->anand(ok) ×1, Uri->uri(ok) ×1, tala->tala(ok) ×1, Islamabad->anantnag(ok) ×1. Wrong ok: none.
- **exact+fuzzy>=85**: false positives: sambhar->sambhar(ok) ×3, khana->khanna(ok) ×3, puri->puri(ok) ×2, camp->chamba-himachal-pradesh(ok) ×2, karna->karnal(ok) ×2, Zero->ziro(ok) ×2, daalo->aalo(ok) ×2, lava->lava(ok) ×2, bheed->beed(ok) ×2, Anand->anand(ok) ×2, Bihar->bihar-sharif(ok) ×2, Tripura->agartala(ambiguous) ×2, Sagar->sagar(ok) ×2, anand->anand(ok) ×1, gaya->gaya(ok) ×1, Ganga->guna(ok) ×1, baar->basar(ok) ×1, chal->chail(ok) ×1, bana->patna(ambiguous) ×1, kitna->machilipatnam(ok) ×1, best->bet-dwarka(ok) ×1, Uri->uri(ok) ×1, tala->tala(ok) ×1, wahi->wai(ok) ×1, forget Madhya Pradesh->indore(ok) ×1, Islamabad->anantnag(ok) ×1. Wrong ok: e003: to Goa->old-goa (gold panaji); e284: ko Goa->old-goa (gold panaji).
- **exact+fuzzy>=90**: false positives: sambhar->sambhar(ok) ×3, khana->khanna(ok) ×3, puri->puri(ok) ×2, karna->karnal(ok) ×2, Zero->ziro(ok) ×2, lava->lava(ok) ×2, Anand->anand(ok) ×2, Bihar->bihar-sharif(ok) ×2, Sagar->sagar(ok) ×2, anand->anand(ok) ×1, gaya->gaya(ok) ×1, kitna->machilipatnam(ok) ×1, Uri->uri(ok) ×1, tala->tala(ok) ×1, Islamabad->anantnag(ok) ×1. Wrong ok: e003: to Goa->old-goa (gold panaji); e284: ko Goa->old-goa (gold panaji).
- **exact+fuzzy>=95**: false positives: sambhar->sambhar(ok) ×3, khana->khanna(ok) ×3, puri->puri(ok) ×2, Zero->ziro(ok) ×2, lava->lava(ok) ×2, Anand->anand(ok) ×2, Bihar->bihar-sharif(ok) ×2, Sagar->sagar(ok) ×2, anand->anand(ok) ×1, gaya->gaya(ok) ×1, Uri->uri(ok) ×1, tala->tala(ok) ×1, Islamabad->anantnag(ok) ×1. Wrong ok: none.
<!-- OFFLINE:END -->

<!-- LLM:START -->
Run `exp01-sarvam-deepseekv4.1-flash-1017-20261121-031436.json`: model `sarvam/deepseekv4.1-flash-1017`, 300 examples, commit df08b81 (dirty tree), 2026-11-21 14:24, place list 1063.

| arm | prompt | input |
|---|---|---|
| 1-v24 | orchestrator_v24 | no detected_places; resolve_place for every name |
| 2-exact | orchestrator_v25 | v25: exact names/aliases |
| 3-fuzzy85 | orchestrator_v25_fuzzy | exact + spelling matches >= 85 |
| 3-fuzzy90 | orchestrator_v25_fuzzy | exact + spelling matches >= 90 |
| 3-fuzzy95 | orchestrator_v25_fuzzy | exact + spelling matches >= 95 |

### Per arm

| arm | turns | passed | pass rate | LLM steps / turn | steps histogram | resolve_place / turn | input tok / turn | cached tok / turn | output tok / turn | cost USD (all turns) | turn s p50 [95% CI] | p90 | p95 | p99 or max | LLM call ms p50 | errors |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1-v24 | 300 | 295 | 0.9833 | 2.6 | {1: 28, 2: 111, 3: 121, 4: 35, 5: 4, 6: 1} | 0.64 | 9393.0 | 8791.0 | 177.0 | 0.06951 | 2.11 [1.96, 2.30] | 3.92 | 4.76 | 6.55 | 775.0 | 0 |
| 2-exact | 300 | 295 | 0.9833 | 2.03 | {1: 49, 2: 200, 3: 47, 4: 2, 5: 2} | 0.13 | 7812.0 | 7353.0 | 156.0 | 0.05746 | 1.68 [1.60, 1.81] | 3.27 | 4.0 | 5.87 | 832.0 | 0 |
| 3-fuzzy85 | 300 | 295 | 0.9833 | 1.98 | {1: 46, 2: 217, 3: 34, 4: 2, 5: 1} | 0.1 | 7652.0 | 7174.0 | 152.0 | 0.05742 | 1.64 [1.57, 1.77] | 3.19 | 3.6 | 5.48 | 812.0 | 0 |
| 3-fuzzy90 | 300 | 295 | 0.9833 | 1.97 | {1: 51, 2: 212, 3: 34, 4: 2, 5: 1} | 0.1 | 7564.0 | 7167.0 | 147.0 | 0.05271 | 1.67 [1.58, 1.78] | 3.07 | 3.62 | 5.27 | 821.0 | 0 |
| 3-fuzzy95 | 300 | 295 | 0.9833 | 1.99 | {1: 52, 2: 206, 3: 37, 4: 3, 5: 2} | 0.12 | 7654.0 | 7300.0 | 150.0 | 0.05122 | 1.69 [1.57, 1.82] | 3.0 | 3.62 | 4.84 | 812.0 | 0 |

### Paired differences (same example, arm − 1-v24)

| pair | metric | examples | mean diff | 95% CI | median diff | up | down | same |
|---|---|---|---|---|---|---|---|---|
| 2-exact − 1-v24 | LLM steps | 300 | -0.57 | [-0.657, -0.483] | -1.0 | 9 | 152 | 139 |
| 2-exact − 1-v24 | resolve_place calls | 300 | -0.51 | [-0.570, -0.450] | 0.0 | 0 | 148 | 152 |
| 2-exact − 1-v24 | input tokens | 300 | -1580.92 | [-1905.468, -1255.516] | -2919.5 | 148 | 152 | 0 |
| 2-exact − 1-v24 | turn seconds | 300 | -0.539 | [-0.797, -0.339] | -0.362 | 91 | 209 | 0 |
| 2-exact − 1-v24 | pass (0/1) | 300 | 0.0 | [-0.010, 0.010] | 0.0 | 1 | 1 | 298 |
| 3-fuzzy85 − 1-v24 | LLM steps | 300 | -0.613 | [-0.700, -0.530] | -1.0 | 7 | 158 | 135 |
| 3-fuzzy85 − 1-v24 | resolve_place calls | 300 | -0.543 | [-0.603, -0.487] | -1.0 | 0 | 157 | 143 |
| 3-fuzzy85 − 1-v24 | input tokens | 300 | -1741.393 | [-2064.617, -1418.739] | -3007.0 | 142 | 158 | 0 |
| 3-fuzzy85 − 1-v24 | turn seconds | 300 | -0.567 | [-0.835, -0.372] | -0.307 | 98 | 202 | 0 |
| 3-fuzzy85 − 1-v24 | pass (0/1) | 300 | 0.0 | [-0.010, 0.010] | 0.0 | 1 | 1 | 298 |
| 3-fuzzy90 − 1-v24 | LLM steps | 300 | -0.63 | [-0.713, -0.550] | -1.0 | 8 | 162 | 130 |
| 3-fuzzy90 − 1-v24 | resolve_place calls | 300 | -0.537 | [-0.597, -0.480] | -1.0 | 0 | 155 | 145 |
| 3-fuzzy90 − 1-v24 | input tokens | 300 | -1828.967 | [-2136.151, -1527.511] | -3034.5 | 138 | 162 | 0 |
| 3-fuzzy90 − 1-v24 | turn seconds | 300 | -0.613 | [-0.878, -0.421] | -0.386 | 88 | 212 | 0 |
| 3-fuzzy90 − 1-v24 | pass (0/1) | 300 | 0.0 | [-0.010, 0.010] | 0.0 | 1 | 1 | 298 |
| 3-fuzzy95 − 1-v24 | LLM steps | 300 | -0.607 | [-0.690, -0.527] | -1.0 | 8 | 155 | 137 |
| 3-fuzzy95 − 1-v24 | resolve_place calls | 300 | -0.523 | [-0.583, -0.467] | -1.0 | 0 | 152 | 148 |
| 3-fuzzy95 − 1-v24 | input tokens | 300 | -1739.443 | [-2048.964, -1436.819] | -2980.5 | 145 | 155 | 0 |
| 3-fuzzy95 − 1-v24 | turn seconds | 300 | -0.514 | [-0.813, -0.178] | -0.388 | 91 | 209 | 0 |
| 3-fuzzy95 − 1-v24 | pass (0/1) | 300 | 0.0 | [-0.010, 0.010] | 0.0 | 1 | 1 | 298 |

### By tag

| tag | examples | 1-v24: passed / steps / s p50 | 2-exact: passed / steps / s p50 | 3-fuzzy85: passed / steps / s p50 | 3-fuzzy90: passed / steps / s p50 | 3-fuzzy95: passed / steps / s p50 |
|---|---|---|---|---|---|---|
| typo | 31 | 31 / 3.03 / 2.81 | 31 / 2.48 / 1.8 | 31 / 2.13 / 1.63 | 31 / 2.23 / 1.62 | 31 / 2.39 / 1.87 |
| alias | 34 | 34 / 3.21 / 2.77 | 34 / 2.12 / 1.66 | 34 / 2.09 / 1.81 | 34 / 2.06 / 1.95 | 34 / 2.12 / 1.91 |
| same-name | 30 | 30 / 2.47 / 2.3 | 30 / 1.77 / 1.62 | 30 / 1.77 / 1.49 | 30 / 1.77 / 1.71 | 30 / 1.77 / 1.6 |
| region | 23 | 19 / 2.22 / 2.01 | 19 / 2.0 / 2.07 | 20 / 2.09 / 2.05 | 20 / 2.09 / 2.13 | 20 / 2.09 / 2.17 |
| answer | 20 | 19 / 2.6 / 2.34 | 19 / 2.4 / 2.08 | 19 / 2.4 / 2.3 | 20 / 2.5 / 2.42 | 20 / 2.5 / 2.15 |
| trap-word | 20 | 20 / 2.45 / 2.07 | 20 / 1.85 / 1.82 | 20 / 1.85 / 1.5 | 20 / 1.85 / 1.57 | 20 / 1.8 / 1.54 |
| foreign | 20 | 20 / 1.75 / 1.81 | 20 / 1.3 / 1.42 | 20 / 1.35 / 1.41 | 20 / 1.2 / 1.07 | 20 / 1.2 / 1.46 |
| compound | 80 | 80 / 3.26 / 2.85 | 79 / 2.42 / 2.33 | 79 / 2.35 / 2.08 | 79 / 2.31 / 2.29 | 79 / 2.34 / 2.28 |
| undo | 21 | 20 / 2.14 / 1.56 | 21 / 2.24 / 1.62 | 20 / 2.05 / 1.4 | 20 / 2.0 / 1.41 | 20 / 2.05 / 1.54 |
| no-edit | 20 | 20 / 1.05 / 0.84 | 20 / 1.0 / 0.87 | 20 / 1.05 / 0.89 | 20 / 1.05 / 0.85 | 20 / 1.0 / 0.89 |
| new-trip | 88 | 87 / 2.61 / 2.39 | 87 / 1.92 / 1.79 | 88 / 1.84 / 1.68 | 87 / 1.82 / 1.73 | 87 / 1.89 / 1.78 |
| hinglish | 150 | 149 / 2.61 / 2.02 | 149 / 2.05 / 1.69 | 149 / 2.0 / 1.67 | 150 / 2.01 / 1.62 | 150 / 2.05 / 1.65 |
| english | 150 | 146 / 2.59 / 2.22 | 146 / 2.0 / 1.64 | 146 / 1.97 / 1.63 | 145 / 1.93 / 1.73 | 145 / 1.93 / 1.72 |
| long | 22 | 21 / 2.82 / 3.44 | 21 / 2.14 / 2.16 | 22 / 2.14 / 1.99 | 21 / 2.0 / 2.01 | 21 / 1.95 / 1.85 |

### Wrong hints that became stops

None.

### Examples whose gold is a question (same-name, region)

| arm | examples | asked | LLM steps when it asked (mean) | steps histogram |
|---|---|---|---|---|
| 1-v24 | 26 | 23 | 2.0 | {2: 23} |
| 2-exact | 26 | 23 | 1.48 | {1: 12, 2: 11} |
| 3-fuzzy85 | 26 | 24 | 1.54 | {1: 12, 2: 11, 3: 1} |
| 3-fuzzy90 | 26 | 23 | 1.48 | {1: 12, 2: 11} |
| 3-fuzzy95 | 26 | 23 | 1.48 | {1: 12, 2: 11} |

<!-- LLM:END -->

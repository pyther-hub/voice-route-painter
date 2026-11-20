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

Model `sarvam/deepseekv4.1-flash-1017`, 300 examples, one run each per arm, arms interleaved per example.

| arm | prompt | what the LLM gets before its first call |
|---|---|---|
| 1 baseline | `orchestrator_v24` | raw text only; every name needs `resolve_place` |
| 2 pre-resolved exact | `orchestrator_v25` | `detected_places` from the exact matcher (ok → id, ambiguous → options) |
| 3 pre-resolved exact + fuzzy | `prompts/orchestrator_v25_fuzzy.md` | `detected_places` from exact + fuzzy at the threshold chosen from step 1 |

Script: `run.py`. Results: not run yet.

<!-- OFFLINE:START -->
Commit 256a261 (dirty tree), 300 examples.

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

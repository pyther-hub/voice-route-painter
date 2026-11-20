"""Check data/examples.json before any run (free, no LLM).

    .venv/bin/python eng-experiments/data/simulate.py && .venv/bin/python eng-experiments/data/validate.py

Errors (exit 1):
- an id (trip, history, ops, mentions, question options) is not in data/places.json
- a trip before or after the turn breaks the app's rules (state.validate_stops)
- a mention's gold id can't be reached by resolve_place on its text (the example would be unfair to every arm):
  lookup must return status ok with the gold id, or ambiguous with every gold id among its matches
- the same text twice
Then prints the coverage: examples per tag (each REQUIRED tag needs MIN_PER_TAG) and per length.
"""
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from app import places, state, tools  # noqa: E402

EXAMPLES = Path(__file__).parent / "examples.json"
MIN_PER_TAG = 20
REQUIRED = ["new-trip", "add", "remove", "replace", "move", "compound", "position", "undo", "clear", "round-trip",
            "typo", "alias", "tourist", "same-name", "region", "answer", "foreign", "trap-word", "no-edit",
            "many-stops", "long", "hinglish", "english"]


def ids_of(ex: dict) -> set[str]:
    s = ex["start"]
    found = set(s["stops"]) | {i for h in s["history"] for i in h}
    if s["pending_question"]:
        found |= set(s["pending_question"]["options"])
    for op in ex["ops"] + ex["alt_ops"]:
        kind, *args = op.split()
        found |= {a for a in args if a not in ("after", "before", "end") and not (kind == "undo")}
    found |= {i for m in ex["mentions"] for i in m["ids"]}
    return found


def check(ex: dict) -> list[str]:
    errs = []
    if bad := sorted(i for i in ids_of(ex) if places.get(i) is None):
        return [f"unknown ids {bad}"]
    exp = ex["expect"]
    trips = [ex["start"]["stops"], *ex["start"]["history"]]
    for alt in exp.get("any_of", [exp]):
        if "stops" in alt:
            trips.append(alt["stops"])
    for t in trips:
        try:
            if t:
                state.validate_stops(t)
        except ValueError as e:
            errs.append(f"trip {t}: {e}")
    for m in ex["mentions"]:
        r = tools.lookup(m["text"])
        got = [x["id"] for x in r["matches"]]
        fair = ((r["status"] == "ok" and got[0] in m["ids"]) or
                (r["status"] == "ambiguous" and all(i in got for i in m["ids"])))
        if not fair:
            errs.append(f"mention {m['text']!r} -> {r['status']} {got}, gold {m['ids']}")
    return errs


def main() -> None:
    examples = json.loads(EXAMPLES.read_text())
    failed = 0
    for ex in examples:
        for e in check(ex):
            failed += 1
            print(f"ERROR {ex['id']} {ex['text'][:60]!r}: {e}")
    texts = Counter(ex["text"] for ex in examples)
    for t, n in texts.items():
        if n > 1:
            failed += 1
            print(f"ERROR text used {n}x: {t!r}")

    tags = Counter(t for ex in examples for t in ex["tags"])
    tags.update(ex["length"] for ex in examples if ex["length"] == "long")
    print(f"\n{len(examples)} examples. Length: {dict(Counter(ex['length'] for ex in examples))}")
    print("Ops per example:", dict(sorted(Counter(len(ex['ops']) for ex in examples).items())))
    print("Start trip size:", dict(sorted(Counter(len(ex['start']['stops']) for ex in examples).items())))
    print("\nTag counts:")
    for t in REQUIRED:
        flag = "" if tags[t] >= MIN_PER_TAG else f"   < {MIN_PER_TAG}"
        print(f"  {t:12} {tags[t]:4}{flag}")
    extra = sorted(set(tags) - set(REQUIRED) - {"long"})
    if extra:
        print("  other tags:", {t: tags[t] for t in extra})
    if failed:
        sys.exit(f"\n{failed} error(s)")
    print("\nOK")


if __name__ == "__main__":
    main()

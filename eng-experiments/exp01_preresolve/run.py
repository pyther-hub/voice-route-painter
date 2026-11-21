"""exp-01, LLM run (paid): the same 300 examples through three arms, one run each, interleaved per example.

    .venv/bin/python eng-experiments/exp01_preresolve/run.py --threshold 90            # dry run: plan + cost
    .venv/bin/python eng-experiments/exp01_preresolve/run.py --threshold 90 --first 3 --yes
    .venv/bin/python eng-experiments/exp01_preresolve/run.py --threshold 90 --yes
    .venv/bin/python eng-experiments/exp01_preresolve/run.py --report results/<file>.json   # tables again

Arms: 1 = prompt v24, no detected_places (every name needs resolve_place); 2 = prompt v25 with the exact
matcher (today's agent.detected_places); 3 = v25 wording for spelling matches + exact+fuzzy matcher at
--threshold (from offline.py). Model: Sarvam DeepSeek V4.1 Flash. Tables go to README.md (LLM markers).
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import common  # noqa: E402
import matchers  # noqa: E402
from app import places  # noqa: E402

USD_PER_TURN = 0.0002          # DeepSeek V4.1 Flash on Sarvam, from the 2026-11-20 benchmark
TAGS = ("typo", "alias", "same-name", "region", "answer", "trap-word", "foreign", "compound", "undo", "no-edit",
        "new-trip", "hinglish", "english", "long")


def arms(thresholds) -> list[common.Variant]:
    """Arms 1 and 2, then one arm 3 per fuzzy threshold."""
    thresholds = [thresholds] if isinstance(thresholds, int) else list(thresholds)
    return [
        common.Variant("1-v24", prompt_file=common.prompt("orchestrator_v24"), detected=common.no_detection,
                       notes="no detected_places; resolve_place for every name"),
        common.Variant("2-exact", prompt_file=common.prompt("orchestrator_v25"),
                       detected=matchers.as_detected(matchers.exact), notes="v25: exact names/aliases"),
    ] + [common.Variant(f"3-fuzzy{th}", prompt_file=HERE / "prompts" / "orchestrator_v25_fuzzy.md",
                        detected=matchers.as_detected(lambda t, th=th: matchers.exact_fuzzy(t, th)),
                        notes=f"exact + spelling matches >= {th}") for th in thresholds]


def extra_tables(rows: list[dict], examples: dict) -> str:
    """Hints that were not gold mentions and ended up as new stops; steps until ask_user on same-name turns."""
    used = []
    for r in rows:
        ex = examples[r["example"]]
        gold = {i for m in ex["mentions"] for i in m["ids"]}
        if r["arm"].startswith("1-"):
            continue
        matcher = matchers.exact if r["arm"].startswith("2-") else \
            (lambda t, th=int(r["arm"].split("fuzzy")[1]): matchers.exact_fuzzy(t, th))
        wrong = {d["matches"][0]["id"] for d in matcher(ex["text"]) if d["status"] == "ok"} - gold
        added = set(r["after"]["stops"]) - set(ex["start"]["stops"])
        if wrong & added:
            used.append([r["arm"], r["example"], ", ".join(sorted(wrong & added)), ex["text"][:70]])
    arms_ = list(dict.fromkeys(r["arm"] for r in rows))
    asks = []
    for a in arms_:
        sub = [r for r in rows if r["arm"] == a and "ask" in " ".join(examples[r["example"]]["ops"])]
        asked = [r for r in sub if "ask_user" in r["tool_names"]]
        steps = [r["steps"] for r in asked]
        asks.append([a, len(sub), len(asked), common._f(sum(steps) / len(steps)) if steps else None,
                     {s: steps.count(s) for s in sorted(set(steps))}])
    return ("### Wrong hints that became stops\n\n" +
            (common.md_table(used, ["arm", "example", "id", "text"]) if used else "None.") +
            "\n\n### Examples whose gold is a question (same-name, region)\n\n" +
            common.md_table(asks, ["arm", "examples", "asked", "LLM steps when it asked (mean)",
                                   "steps histogram"]) + "\n")


def report(path: Path) -> None:
    data = json.loads(path.read_text())
    rows, meta = data["rows"], data["meta"]
    examples = {e["id"]: e for e in common.load_examples()}
    arm_names = [a["name"] for a in meta["arms"]]
    head = (f"Run `{path.name}`: model `{meta['model']}`, {meta['examples']} examples, commit {meta['commit']}"
            f"{' (dirty tree)' if meta['dirty'] else ''}, {meta['date']}, place list {meta['places']}.\n\n" +
            common.md_table([[a["name"], a["prompt"], a["notes"]] for a in meta["arms"]], ["arm", "prompt", "input"]))
    body = head + "\n\n" + common.report_md(rows, arm_names, TAGS) + "\n" + extra_tables(rows, examples)
    common.write_section(HERE / "README.md", "LLM", body)
    print(body)
    return body


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--threshold", type=int, nargs="+", help="fuzzy score(s) for arm 3 (85 90 95; see offline.py)")
    p.add_argument("--yes", action="store_true", help="really call the LLM (costs money)")
    p.add_argument("--first", type=int)
    p.add_argument("--only", nargs="*")
    p.add_argument("--model", default=common.DEFAULT_MODEL)
    p.add_argument("--report", type=Path, help="regenerate the README tables from a results JSON")
    a = p.parse_args()
    if a.report:
        return report(a.report if a.report.is_absolute() else HERE / a.report)
    if not a.threshold:
        sys.exit("--threshold is required (arm 3's fuzzy score)")
    examples = common.load_examples(only=a.only, first=a.first)
    variants = arms(a.threshold)
    common.confirm_paid(len(examples) * len(variants), USD_PER_TURN, a.yes)
    places.load()
    common.run_examples("exp01", variants, examples, a.model, HERE / "results")
    latest = max((HERE / "results").glob("exp01-*.json"), key=lambda f: f.stat().st_mtime)
    report(latest)


if __name__ == "__main__":
    main()

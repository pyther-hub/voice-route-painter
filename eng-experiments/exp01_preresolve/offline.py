"""exp-01, matcher check (free, no LLM): how well code alone finds and resolves the place names in the 300 texts.

    .venv/bin/python eng-experiments/exp01_preresolve/offline.py

Gold = each example's `mentions` (text span -> place id; "a|b" = the right result is an ambiguous list holding
all of them). A detection counts as correct when it overlaps a gold mention and either says ok with a gold id,
or says ambiguous with every gold id among its matches (the model can then pick or ask).

Writes results/offline.json and the matcher tables in README.md (between the OFFLINE markers).
"""
import json
import re
import sys
from collections import Counter
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import common  # noqa: E402
import matchers  # noqa: E402

THRESHOLDS = (85, 90, 95)
TAGS = ("typo", "alias", "trap-word", "same-name", "region", "tourist", "answer", "hinglish", "english")


def overlaps(a, b) -> bool:
    return a["start"] < b["end"] and b["start"] < a["end"]


def correct(d: dict, gold: dict) -> bool:
    ids = [m["id"] for m in d["matches"]]
    if d["status"] == "ok":
        return ids[0] in gold["ids"]
    return all(g in ids for g in gold["ids"])


def score_example(ex: dict, detections: list[dict]) -> dict:
    """Gold is counted once per name: "Pune ... wapas Pune" is one name to resolve, and a detection of either
    copy covers it (the matchers, like v25, list each name once)."""
    groups = {}
    for m in ex["mentions"]:
        groups.setdefault((matchers.places.norm(m["text"]), tuple(m["ids"])), []).append(m)
    gold = [{"ids": k[1], "spans": v} for k, v in groups.items()]
    on_trip = set(ex["start"]["stops"])
    hits, fps, wrong_ok = set(), [], []
    for d in detections:
        g = next((i for i, m in enumerate(gold) if any(overlaps(d, s) for s in m["spans"])), None)
        if g is None:
            fps.append(f"{d['text']}->{d['matches'][0]['id']}({d['status']})")
        elif correct(d, gold[g]):
            hits.add(g)
        elif d["status"] == "ok":
            wrong_ok.append(f"{d['text']}->{d['matches'][0]['id']} (gold {'|'.join(gold[g]['ids'])})")
    covered = [i in hits for i in range(len(gold))]
    known = [c or (len(m["ids"]) == 1 and m["ids"][0] in on_trip) for c, m in zip(covered, gold)]
    return {"gold": len(gold), "detected": len(detections), "correct": len(hits), "fp": fps, "wrong_ok": wrong_ok,
            "all_covered": bool(gold) and all(covered), "all_known": bool(gold) and all(known)}


def table(name: str, scored: list[tuple[dict, dict]]) -> dict:
    s = [r for _, r in scored]
    with_gold = [r for r in s if r["gold"]]
    det, cor = sum(r["detected"] for r in s), sum(r["correct"] for r in s)
    gold = sum(r["gold"] for r in s)
    return {"matcher": name, "examples": len(s), "gold_mentions": gold, "detections": det, "correct": cor,
            "precision": round(cor / det, 4) if det else None, "recall": round(cor / gold, 4) if gold else None,
            "false_positives": sum(len(r["fp"]) for r in s), "wrong_ok": sum(len(r["wrong_ok"]) for r in s),
            "examples_all_covered": f"{sum(r['all_covered'] for r in with_gold)}/{len(with_gold)}",
            "examples_all_known_incl_trip": f"{sum(r['all_known'] for r in with_gold)}/{len(with_gold)}"}


def main():
    examples = common.load_examples()
    runs = {"exact (v25)": matchers.exact}
    for t in THRESHOLDS:
        runs[f"exact+fuzzy>={t}"] = lambda text, t=t: matchers.exact_fuzzy(text, t)
    res, fp_lists = {"overall": [], "by_tag": {}}, {}
    for name, fn in runs.items():
        scored = [(ex, score_example(ex, fn(ex["text"]))) for ex in examples]
        res["overall"].append(table(name, scored))
        for tag in TAGS:
            sub = [(e, r) for e, r in scored if tag in e["tags"]]
            res["by_tag"].setdefault(tag, []).append(table(name, sub))
        fp_lists[name] = {"false_positives": Counter(x for _, r in scored for x in r["fp"]).most_common(),
                          "wrong_ok": [f"{e['id']}: {x}" for e, r in scored for x in r["wrong_ok"]]}
    res["lists"] = fp_lists
    res["meta"] = {**common.git_meta(), "examples": len(examples)}
    (HERE / "results").mkdir(exist_ok=True)
    (HERE / "results" / "offline.json").write_text(json.dumps(res, indent=1, ensure_ascii=False))

    cols = ["matcher", "gold_mentions", "detections", "correct", "precision", "recall", "false_positives",
            "wrong_ok", "examples_all_covered", "examples_all_known_incl_trip"]
    md = lambda rows: "\n".join(["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)] +
                                ["| " + " | ".join(str(r[c]) for c in cols) + " |" for r in rows])
    parts = [f"Commit {res['meta']['commit']}{' (dirty tree)' if res['meta']['dirty'] else ''}, "
             f"{len(examples)} examples.\n", "### All examples\n", md(res["overall"])]
    for tag, rows in res["by_tag"].items():
        parts += [f"\n### Tag: {tag} ({rows[0]['examples']} examples)\n", md(rows)]
    parts.append("\n### False positives and wrong `ok` results (text -> id)\n")
    for name, L in fp_lists.items():
        fp = ", ".join(f"{x} ×{n}" for x, n in L["false_positives"]) or "none"
        wo = "; ".join(L["wrong_ok"]) or "none"
        parts.append(f"- **{name}**: false positives: {fp}. Wrong ok: {wo}.")
    block = "<!-- OFFLINE:START -->\n" + "\n".join(parts) + "\n<!-- OFFLINE:END -->"
    readme = HERE / "README.md"
    text = readme.read_text() if readme.exists() else ""
    if "<!-- OFFLINE:START -->" in text:
        text = re.sub(r"<!-- OFFLINE:START -->.*<!-- OFFLINE:END -->", lambda m: block, text, flags=re.S)
    else:
        text += "\n" + block + "\n"
    readme.write_text(text)
    print(md(res["overall"]))


if __name__ == "__main__":
    main()

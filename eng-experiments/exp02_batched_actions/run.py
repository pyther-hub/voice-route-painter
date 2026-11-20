"""exp-02, LLM run (paid): how the edit tools are shaped vs LLM steps per turn. One run per example per arm.

    .venv/bin/python eng-experiments/exp02_batched_actions/run.py                 # dry run: plan + cost
    .venv/bin/python eng-experiments/exp02_batched_actions/run.py --first 3 --yes
    .venv/bin/python eng-experiments/exp02_batched_actions/run.py --yes
    .venv/bin/python eng-experiments/exp02_batched_actions/run.py --report results/<file>.json

Arms (all on prompt v25 with its exact detected_places, model Sarvam DeepSeek V4.1 Flash):
  A-v25       small edit tools; the turn ends when the model answers with no tool call (today's app)
  B-parallel  same tools; prompt asks for all edit calls in one response; the turn ends after a step whose
              edits all succeeded
  C-batch     one apply_edits(ops) tool instead of set_route/add_stop/remove_stop/undo; atomic; ends the turn
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent / "data"))

import common  # noqa: E402
import simulate  # noqa: E402
import variants  # noqa: E402
from app import places, tools  # noqa: E402

USD_PER_TURN = 0.0002
TAGS = ("compound", "replace", "move", "add", "remove", "undo", "clear", "round-trip", "new-trip", "same-name",
        "no-edit", "hinglish", "english", "long")


def arms() -> list[common.Variant]:
    v25 = common.prompt("orchestrator_v25")
    return [
        common.Variant("A-v25", prompt_file=v25, notes="small edit tools; turn ends on a step with no tool call"),
        common.Variant("B-parallel", prompt_file=HERE / "prompts" / "orchestrator_v25_parallel.md",
                       run_turn=variants.run_turn_end_on_edit,
                       notes="small edit tools, all in one response; turn ends after a step whose edits all succeeded"),
        common.Variant("C-batch", prompt_file=HERE / "prompts" / "orchestrator_v25_batch.md",
                       tools=variants.BATCH_TOOLS, patches=[(tools, "_validate", variants.validate)],
                       notes="one atomic apply_edits(ops) tool; ends the turn"),
    ]


# ---------------------------------------------------------------- op-level scoring

def _ops_of_call(name: str, a: dict) -> list[str]:
    if name == "set_route":
        return [" ".join(["set", *a.get("stops", [])])]
    if name == "add_stop" or (name == "op" and a.get("op") == "add"):
        where = "end" if a.get("position") == "end" else f"after {a['after']}" if a.get("after") \
            else f"before {a.get('before')}"
        return [f"add {a.get('place_id')} {where}"]
    if name == "remove_stop" or (name == "op" and a.get("op") == "remove"):
        return [f"rm {a.get('place_id')}"]
    if name == "undo" or (name == "op" and a.get("op") == "undo"):
        return [f"undo {a.get('count') or 1}"]
    if name == "op" and a.get("op") == "set_route":
        return [" ".join(["set", *(a.get("stops") or [])])]
    if name == "apply_edits":
        return [x for o in a.get("ops", []) for x in _ops_of_call("op", o)]
    return []


def edit_ops(row: dict) -> list[str]:
    """The edits the turn really made: successful edit calls, in order, as simulate.py ops."""
    calls = [c for step in row["calls"] for c in step]
    out = []
    for c, t in zip(calls, row["tools"]):
        name = c.split("(", 1)[0]
        if not t["ok"]:
            continue
        try:
            args = json.loads(c[len(name) + 1:-1])
        except ValueError:
            continue
        out += _ops_of_call(name, args)
    return out


def op_tables(rows: list[dict], examples: dict) -> str:
    out = []
    for arm in dict.fromkeys(r["arm"] for r in rows):
        rs = [r for r in rows if r["arm"] == arm]
        scored = [r for r in rs if not {o.split()[0] for o in examples[r["example"]]["ops"]} & {"ask", "none"}]
        agree = sum(edit_ops(r) in ([examples[r["example"]]["ops"]] + [examples[r["example"]]["alt_ops"]])
                    for r in scored)
        multi = [r for r in scored if len(examples[r["example"]]["ops"]) >= 2]
        partial = 0
        for r in multi:
            ex = examples[r["example"]]
            ops = simulate.parse_ops("; ".join(ex["ops"]))
            full, _ = simulate.apply_ops(ex["start"]["stops"], ex["start"]["history"], ops)
            prefixes = [simulate.apply_ops(ex["start"]["stops"], ex["start"]["history"], ops[:k])[0]
                        for k in range(1, len(ops))]
            if r["after"]["stops"] != full and r["after"]["stops"] in prefixes:
                partial += 1
        edit_names = {"set_route", "add_stop", "remove_stop", "undo", "apply_edits"}
        failed = sum(1 for r in rs for t in r["tools"] if t["tool"] in edit_names and not t["ok"])
        total = sum(1 for r in rs for t in r["tools"] if t["tool"] in edit_names)
        edit_steps = [sum(any(c.split("(", 1)[0] in edit_names for c in step) for step in r["calls"])
                      for r in scored]
        out.append([arm, len(scored), agree, len(multi), partial, f"{failed}/{total}",
                    common._f(sum(edit_steps) / len(edit_steps)) if edit_steps else None])
    return "### Edits\n\n" + common.md_table(
        out, ["arm", "edit examples", "edit ops = gold ops", "examples with 2+ gold ops",
              "partial writes (a strict prefix of the gold ops)", "failed edit calls / edit calls",
              "LLM steps with an edit call / turn"]) + "\n"


def report(path: Path) -> None:
    data = json.loads(path.read_text())
    rows, meta = data["rows"], data["meta"]
    examples = {e["id"]: e for e in common.load_examples()}
    names = [a["name"] for a in meta["arms"]]
    head = (f"Run `{path.name}`: model `{meta['model']}`, {meta['examples']} examples, commit {meta['commit']}"
            f"{' (dirty tree)' if meta['dirty'] else ''}, {meta['date']}.\n\n" +
            common.md_table([[a["name"], a["prompt"], a["notes"]] for a in meta["arms"]], ["arm", "prompt", "setup"]))
    body = head + "\n\n" + common.report_md(rows, names, TAGS) + "\n" + op_tables(rows, examples)
    common.write_section(HERE / "README.md", "LLM", body)
    print(body)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--yes", action="store_true", help="really call the LLM (costs money)")
    p.add_argument("--first", type=int)
    p.add_argument("--only", nargs="*")
    p.add_argument("--model", default=common.DEFAULT_MODEL)
    p.add_argument("--report", type=Path)
    a = p.parse_args()
    if a.report:
        return report(a.report if a.report.is_absolute() else HERE / a.report)
    examples = common.load_examples(only=a.only, first=a.first)
    vs = arms()
    common.confirm_paid(len(examples) * len(vs), USD_PER_TURN, a.yes)
    places.load()
    common.run_examples("exp02", vs, examples, a.model, HERE / "results")
    report(max((HERE / "results").glob("exp02-*.json"), key=lambda f: f.stat().st_mtime))


if __name__ == "__main__":
    main()

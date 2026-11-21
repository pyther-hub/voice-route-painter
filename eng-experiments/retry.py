"""Rerun only the rows of a finished run that the provider broke, and replace them in place (paid).

    .venv/bin/python eng-experiments/retry.py exp01_preresolve/results/<file>.turns.jsonl            # dry run
    .venv/bin/python eng-experiments/retry.py exp01_preresolve/results/<file>.turns.jsonl --sleep --yes
    .venv/bin/python eng-experiments/retry.py exp03_providers/results/passrate-<time>.jsonl --skip groq --yes

A row is rerun when its error is the provider's (429, timeout, connection, 5xx) or, with --sleep, when its turn
overlapped a sleep of this Mac (`pmset -g log`), so its seconds include the sleep. Errors that are the model's
(a 400 for a malformed tool call) stay. The new row replaces the old one at the same place and carries
"retried": true and the old error. For exp-01/02 the matching .json is rewritten too.
"""
import argparse
import datetime as dt
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import common  # noqa: E402
from app import llm  # noqa: E402

INFRA = ("RateLimitError", "APITimeoutError", "APIConnectionError", "InternalServerError")


def load_module(rel: str, name: str):
    import importlib.util
    path = HERE / rel
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def sleeps() -> list[tuple[float, float]]:
    """(start, end) epoch seconds of each sleep in this Mac's power log."""
    out, start = [], None
    for line in subprocess.run(["pmset", "-g", "log"], capture_output=True, text=True).stdout.splitlines():
        p = line.split()
        if len(p) > 3 and p[3] in ("Sleep", "Wake", "DarkWake"):
            try:
                t = dt.datetime.strptime(" ".join(p[:3]), "%Y-%m-%d %H:%M:%S %z").timestamp()
            except ValueError:
                continue
            if p[3] == "Sleep":
                start = t
            elif start is not None:
                out.append((start, t))
                start = None
    return out


def slept(trace_id: str | None, windows) -> bool:
    if not trace_id:
        return False
    db = sqlite3.connect(common.ROOT / "traces.db")
    s = db.execute("select started_at, duration_ms from spans where trace_id=? and parent is null",
                   (trace_id,)).fetchone()
    if not s or s[0] is None:
        return False
    a, b = s[0], s[0] + (s[1] or 0) / 1000
    return any(a < e and b > st for st, e in windows)


def variants_for(path: Path) -> dict:
    if path.name.startswith("exp01"):
        return {v.name: v for v in load_module("exp01_preresolve/run.py", "exp01_run").arms([85, 90, 95])}
    if path.name.startswith("exp02"):
        return {v.name: v for v in load_module("exp02_batched_actions/run.py", "exp02_run").arms()}
    return {"v25": common.Variant("v25")}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("file", type=Path, help="a .turns.jsonl (exp01/exp02) or passrate-*.jsonl (exp03)")
    p.add_argument("--sleep", action="store_true", help="also rerun turns that overlapped a sleep of this Mac")
    p.add_argument("--skip", nargs="*", default=[], help="providers to leave alone (e.g. groq)")
    p.add_argument("--yes", action="store_true")
    a = p.parse_args()
    path = a.file if a.file.is_absolute() else HERE / a.file
    rows = [json.loads(line) for line in path.open()]
    windows = sleeps() if a.sleep else []
    todo = [i for i, r in enumerate(rows)
            if llm.MODELS[r.get("model", common.DEFAULT_MODEL)]["provider"] not in a.skip
            and ((r.get("error") or "").startswith(INFRA) or (a.sleep and slept(r.get("trace_id"), windows)))]
    print(f"{len(todo)} of {len(rows)} rows to rerun")
    if not todo:
        return
    common.confirm_paid(len(todo), 0.001, a.yes)
    examples = {e["id"]: e for e in common.load_examples()}
    variants = variants_for(path)
    for n, i in enumerate(todo, 1):
        old = rows[i]
        model = old.get("model", common.DEFAULT_MODEL)
        r = common.run_one(variants[old["arm"]], examples[old["example"]], model)
        if "model" in old:
            r["model"] = model
        r["retried"] = True
        r["first_error"] = old.get("error") or "overlapped a sleep"
        rows[i] = r
        print(f"[{n}/{len(todo)}] {old['example']} {old['arm']} {model.split('/')[-1]} "
              f"{'PASS' if r['pass'] else 'FAIL'} {r['seconds']:.2f}s {(r.get('error') or '')[:60]}", flush=True)
        path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in rows))
    full = path.with_name(path.name.replace(".turns.jsonl", ".json"))
    if full != path and full.exists():
        data = json.loads(full.read_text())
        data["rows"] = rows
        data["meta"]["retried"] = len(todo)
        full.write_text(json.dumps(data, indent=1, ensure_ascii=False))
    print("saved", path)


if __name__ == "__main__":
    main()

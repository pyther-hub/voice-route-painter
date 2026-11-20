"""Shared harness for the experiments: run the real agent on data/examples.json under different variants.

    from common import Variant, load_examples, run_examples, summarize
    rows = run_examples([Variant("v24", prompt_file=...), Variant("v25")], examples, model="sarvam/...")

- A Variant is applied in-process (monkeypatching app.agent / app.tools for the length of one turn), so the
  experiments never edit app/.
- Every example runs in a fresh session seeded from its `start` (earlier trips for undo, the trip, a pending
  question), exactly like a user arriving at that point; the turn is scored with evals/run.py's check().
- Arms are interleaved per example (A, B, C on e001, then on e002, ...), so provider load drift hits every arm.
- Each turn is appended to results/<name>.turns.jsonl as it finishes; the final JSON holds meta + rows.
"""
import contextlib
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import numpy as np

HERE = Path(__file__).parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))

import os  # noqa: E402

from app import agent, guard, places, state, tools, trace  # noqa: E402
from evals.run import check, snapshot  # noqa: E402
from app.llm import cost_usd  # noqa: E402

EXAMPLES = HERE / "data" / "examples.json"
DEFAULT_MODEL = "sarvam/deepseekv4.1-flash-1017"      # owner's choice for exp-01/02 (Sarvam only)
SESSION_PREFIX = "exp-"                                # exp-00 leaves these sessions out of "real" traffic

# The server's spend cap (DAILY_BUDGET_USD, default $2) is per process and would turn late turns of a run into
# "spend cap" replies. An experiment process gets its own explicit cap instead: above every planned run, so it
# only stops a runaway. Turns that hit it are marked with error "spend cap" in the rows.
guard.DAILY_BUDGET_USD = float(os.getenv("EXP_BUDGET_USD", "5"))


# ---------------------------------------------------------------- variants

@dataclass
class Variant:
    """One arm. Anything left as None keeps what app/ does today."""
    name: str
    prompt_file: Path | None = None                    # a prompt .md (app/prompts/... or the experiment's own)
    detected: Callable[[str, str], list] | None = None  # replaces agent.detected_places(session_id, text)
    tools: dict | None = None                          # replaces tools.TOOLS (the registry the LLM sees)
    run_turn: Callable[[str, str, str], str] | None = None   # replaces agent.run_turn (a loop change)
    patches: list = field(default_factory=list)        # more (module, attribute, value) to set for the turn
    notes: str = ""


@contextlib.contextmanager
def applied(v: Variant):
    saved = (agent.PROMPT_VERSION, agent.SYSTEM_PROMPT, agent.detected_places, tools.TOOLS)
    saved_patches = [(mod, attr, getattr(mod, attr)) for mod, attr, _ in v.patches]
    try:
        for mod, attr, value in v.patches:
            setattr(mod, attr, value)
        if v.prompt_file:
            agent.PROMPT_VERSION = Path(v.prompt_file).stem
            agent.SYSTEM_PROMPT = Path(v.prompt_file).read_text()
        if v.detected:
            agent.detected_places = v.detected
        if v.tools is not None:
            tools.TOOLS = v.tools
        yield v.run_turn or agent.run_turn
    finally:
        agent.PROMPT_VERSION, agent.SYSTEM_PROMPT, agent.detected_places, tools.TOOLS = saved
        for mod, attr, value in saved_patches:
            setattr(mod, attr, value)


def no_detection(session_id: str, text: str) -> list:
    return []


def prompt(version: str) -> Path:
    return ROOT / "app" / "prompts" / f"{version}.md"


# ---------------------------------------------------------------- examples

def load_examples(only: list[str] | None = None, first: int | None = None, tag: str | None = None) -> list[dict]:
    ex = json.loads(EXAMPLES.read_text())
    if only:
        ex = [e for e in ex if e["id"] in only]
    if tag:
        ex = [e for e in ex if tag in e["tags"] or e["length"] == tag]
    return ex[:first] if first else ex


def option_label(place_id: str) -> str:
    p = places.get(place_id)
    return f"{p['name']}, {p['state']}"


def seed(session_id: str, ex: dict, model: str) -> None:
    """A fresh session at the example's starting point. Writes go through update_state(), so the undo history
    is [empty, *history] like a real session that built those trips one after another."""
    state.drop_session(session_id)
    sess = state.session(session_id)
    sess.model = model
    start = ex["start"]
    for trip in start["history"]:
        state.update_state(session_id, trip)
    if start["stops"] or start["history"]:
        state.update_state(session_id, start["stops"])
    if pq := start["pending_question"]:
        sess.pending_question = {"question": pq["question"],
                                 "options": [{"label": option_label(i), "id": i} for i in pq["options"]]}
    state.set_display(session_id, question=None, suggestions=[])      # as main.py does on new input


def turn_metrics(trace_id: str | None, model: str) -> dict:
    """From the turn's trace: LLM steps and their durations, token split, cost, tool calls."""
    data = trace.read_trace(trace_id) if trace_id else None
    if not data:
        return {"steps": None, "llm_ms": [], "usage": None, "cost_usd": None, "calls": [], "tools": [],
                "error": "no trace"}
    spans = data["spans"]
    root = next((s for s in spans if s["parent"] is None), {})
    llm = [s for s in spans if s["kind"] == "llm"]
    usage = {"input": 0, "cached": 0, "output": 0}
    calls = []
    for s in llm:
        out = s["output"] if isinstance(s["output"], dict) else {}
        for k in usage:
            usage[k] += (out.get("usage") or {}).get(k, 0)
        calls.append(out.get("calls") or [])
    tool_spans = [{"tool": s["name"], "ms": s["duration_ms"],
                   "ok": (s["output"] or {}).get("ok") if isinstance(s["output"], dict) else None}
                  for s in spans if s["kind"] == "tool"]
    return {"steps": len(llm), "llm_ms": [s["duration_ms"] for s in llm], "usage": usage,
            "cost_usd": round(cost_usd(model, usage), 7), "calls": calls, "tools": tool_spans,
            "error": root.get("error"), "trace_id": trace_id}


def run_one(v: Variant, ex: dict, model: str) -> dict:
    sid = f"{SESSION_PREFIX}{v.name}-{ex['id']}-{int(time.time() * 1000)}"
    seed(sid, ex, model)
    before = snapshot(sid)
    with applied(v) as run_turn:
        t0 = time.perf_counter()
        reply = run_turn(sid, ex["text"], ex["kind"])
        seconds = time.perf_counter() - t0
    after = snapshot(sid)
    fails = check(ex["expect"], after, before)
    m = turn_metrics(state.ui(sid)["trace_id"], model)
    state.drop_session(sid)
    names = [t["tool"] for t in m["tools"]]
    return {"example": ex["id"], "arm": v.name, "tags": ex["tags"], "length": ex["length"], "text": ex["text"],
            "pass": not fails, "fails": fails, "reply": reply, "after": after, "seconds": round(seconds, 3),
            "resolve_calls": names.count("resolve_place"), "tool_names": names, **m}


# ---------------------------------------------------------------- running

def git_meta() -> dict:
    def git(*a):
        try:
            return subprocess.run(["git", *a], cwd=ROOT, capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return None
    return {"commit": git("rev-parse", "--short", "HEAD"), "dirty": bool(git("status", "--porcelain"))}


def run_examples(name: str, variants: list[Variant], examples: list[dict], model: str = DEFAULT_MODEL,
                 out_dir: Path | None = None) -> list[dict]:
    out_dir = out_dir or HERE / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = out_dir / f"{name}-{model.replace('/', '-')}-{stamp}"
    log = base.with_suffix(".turns.jsonl")
    rows = []
    for n, ex in enumerate(examples, 1):
        for v in variants:                                     # interleaved: every arm on this example
            r = run_one(v, ex, model)
            rows.append(r)
            with log.open("a") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"[{n}/{len(examples)}] {ex['id']} {v.name:10} {'PASS' if r['pass'] else 'FAIL'} "
                  f"steps={r['steps']} resolve={r['resolve_calls']} {r['seconds']:.2f}s  {ex['text'][:50]!r}",
                  flush=True)
    meta = {"experiment": name, "model": model, "date": time.strftime("%Y-%m-%d %H:%M"), **git_meta(),
            "examples": len(examples), "places": len(places.load()),
            "arms": [{"name": v.name, "prompt": Path(v.prompt_file).stem if v.prompt_file else agent.PROMPT_VERSION,
                      "notes": v.notes} for v in variants]}
    base.with_suffix(".json").write_text(json.dumps({"meta": meta, "rows": rows}, indent=1, ensure_ascii=False))
    print(f"saved {base.with_suffix('.json')}")
    return rows


def confirm_paid(turns: int, usd_per_turn: float, yes: bool) -> None:
    """Paid runs need --yes. Without it: print the plan and the cost estimate, then stop."""
    print(f"Plan: {turns} agent turns, about ${turns * usd_per_turn:.2f} (at ~${usd_per_turn}/turn).")
    if not yes:
        sys.exit("Dry run only. Add --yes to really call the LLM (costs money).")


# ---------------------------------------------------------------- statistics

def pct(values, p: float) -> float | None:
    v = [x for x in values if x is not None]
    return float(np.percentile(v, p)) if v else None


def bootstrap_ci(values, stat=np.median, n: int = 2000, seed: int = 0) -> tuple[float, float] | None:
    """95 % interval of `stat` by resampling with replacement."""
    v = np.array([x for x in values if x is not None], dtype=float)
    if len(v) < 2:
        return None
    rng = np.random.default_rng(seed)
    stats = [stat(rng.choice(v, len(v))) for _ in range(n)]
    return float(np.percentile(stats, 2.5)), float(np.percentile(stats, 97.5))


def percentiles(values) -> dict:
    """p50/p90/p95 (+ 95 % CI of the median) and p99 only when there are >= 300 values, else max."""
    v = [x for x in values if x is not None]
    out = {"n": len(v), "p50": pct(v, 50), "p50_ci": bootstrap_ci(v), "p90": pct(v, 90), "p95": pct(v, 95)}
    out["p99" if len(v) >= 300 else "max"] = pct(v, 99) if len(v) >= 300 else (max(v) if v else None)
    return out


def paired_diff(rows: list[dict], key: str, arm_a: str, arm_b: str) -> dict:
    """Per-example b - a for one metric: mean, median, 95 % CI of the mean, and how many go up/down/stay."""
    by = {}
    for r in rows:
        by.setdefault(r["example"], {})[r["arm"]] = r.get(key)
    d = [x[arm_b] - x[arm_a] for x in by.values()
         if x.get(arm_a) is not None and x.get(arm_b) is not None]
    if not d:
        return {"n": 0}
    return {"n": len(d), "mean": float(np.mean(d)), "median": float(np.median(d)),
            "mean_ci": bootstrap_ci(d, np.mean), "up": sum(x > 0 for x in d), "down": sum(x < 0 for x in d),
            "same": sum(x == 0 for x in d)}


def md_table(rows: list[list], head: list[str]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    return "\n".join(out + ["| " + " | ".join("–" if c is None else str(c) for c in r) + " |" for r in rows])


def _f(x, nd=2):
    return None if x is None else round(x, nd)


def report_md(rows: list[dict], arms: list[str], tags: tuple = ()) -> str:
    """Numbers-only tables for an LLM run: per arm, paired differences against the first arm, and per tag."""
    s = summarize(rows, arms)
    head = ["arm", "turns", "passed", "pass rate", "LLM steps / turn", "steps histogram", "resolve_place / turn",
            "input tok / turn", "cached tok / turn", "output tok / turn", "cost USD (all turns)",
            "turn s p50 [95% CI]", "p90", "p95", "p99 or max", "LLM call ms p50", "errors"]
    def ts(v):
        t = v["turn_seconds"]
        ci = t["p50_ci"]
        return [f"{t['p50']:.2f} [{ci[0]:.2f}, {ci[1]:.2f}]" if ci else _f(t["p50"]), _f(t["p90"]), _f(t["p95"]),
                _f(t.get("p99", t.get("max")))]
    main = md_table([[a, v["turns"], v["passed"], v["pass_rate"], _f(v["steps_mean"]), v["steps_hist"],
                      _f(v["resolve_calls_mean"]), _f(v["input_tokens_mean"], 0), _f(v["cached_tokens_mean"], 0),
                      _f(v["output_tokens_mean"], 0), v["cost_usd_total"], *ts(v), _f(v["llm_call_ms"]["p50"], 0),
                      v["errors"]] for a, v in s.items()], head)
    for r in rows:
        r["pass_int"] = int(r["pass"])
        r["input_tokens"] = (r["usage"] or {}).get("input")
    diffs = []
    for a in arms[1:]:
        for key, label in (("steps", "LLM steps"), ("resolve_calls", "resolve_place calls"),
                           ("input_tokens", "input tokens"), ("seconds", "turn seconds"), ("pass_int", "pass (0/1)")):
            d = paired_diff(rows, key, arms[0], a)
            if d["n"]:
                ci = d["mean_ci"]
                diffs.append([f"{a} − {arms[0]}", label, d["n"], _f(d["mean"], 3),
                              f"[{ci[0]:.3f}, {ci[1]:.3f}]" if ci else None, _f(d["median"], 3),
                              d["up"], d["down"], d["same"]])
    paired = md_table(diffs, ["pair", "metric", "examples", "mean diff", "95% CI", "median diff", "up", "down",
                              "same"])
    by_tag = []
    for tag in tags:
        sub = [r for r in rows if tag in r["tags"] or r["length"] == tag]
        if not sub:
            continue
        st = summarize(sub, arms)
        by_tag.append([tag, st[arms[0]]["turns"]] +
                      [f"{st[a]['passed']} / {_f(st[a]['steps_mean'])} / {_f(st[a]['turn_seconds']['p50'])}"
                       for a in arms])
    tag_table = md_table(by_tag, ["tag", "examples"] + [f"{a}: passed / steps / s p50" for a in arms])
    return (f"### Per arm\n\n{main}\n\n### Paired differences (same example, arm − {arms[0]})\n\n{paired}\n\n"
            f"### By tag\n\n{tag_table}\n")


def write_section(readme: Path, marker: str, body: str) -> None:
    """Replace the text between <!-- MARKER:START --> and <!-- MARKER:END --> (appended if missing)."""
    import re
    block = f"<!-- {marker}:START -->\n{body}\n<!-- {marker}:END -->"
    text = readme.read_text() if readme.exists() else ""
    if f"<!-- {marker}:START -->" in text:
        text = re.sub(rf"<!-- {marker}:START -->.*<!-- {marker}:END -->", lambda m: block, text, flags=re.S)
    else:
        text += "\n" + block + "\n"
    readme.write_text(text)


def summarize(rows: list[dict], arms: list[str]) -> dict:
    """Numbers per arm: pass rate, steps, resolve calls, tokens, cost, latency percentiles."""
    out = {}
    for a in arms:
        rs = [r for r in rows if r["arm"] == a]
        tok = lambda k: [(r["usage"] or {}).get(k) for r in rs]
        out[a] = {"turns": len(rs), "passed": sum(r["pass"] for r in rs),
                  "pass_rate": round(sum(r["pass"] for r in rs) / len(rs), 4) if rs else None,
                  "steps_mean": float(np.mean([r["steps"] for r in rs if r["steps"] is not None])) if rs else None,
                  "steps_hist": {s: sum(r["steps"] == s for r in rs) for s in sorted({r["steps"] for r in rs})},
                  "resolve_calls_mean": float(np.mean([r["resolve_calls"] for r in rs])) if rs else None,
                  "input_tokens_mean": float(np.mean([x for x in tok("input") if x is not None] or [0])),
                  "cached_tokens_mean": float(np.mean([x for x in tok("cached") if x is not None] or [0])),
                  "output_tokens_mean": float(np.mean([x for x in tok("output") if x is not None] or [0])),
                  "cost_usd_total": round(sum(r["cost_usd"] or 0 for r in rs), 5),
                  "turn_seconds": percentiles([r["seconds"] for r in rs]),
                  "llm_call_ms": percentiles([ms for r in rs for ms in r["llm_ms"]]),
                  "errors": sum(bool(r["error"]) for r in rs)}
    return out

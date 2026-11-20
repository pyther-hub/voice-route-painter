"""exp-03 (paid): the same request to every model in app/llm.py: price and latency (p50/p90/p95/p99).

    .venv/bin/python eng-experiments/exp03_providers/bench.py --part latency            # dry run: plan + cost
    .venv/bin/python eng-experiments/exp03_providers/bench.py --part latency --first 2 --yes
    .venv/bin/python eng-experiments/exp03_providers/bench.py --part latency --yes
    .venv/bin/python eng-experiments/exp03_providers/bench.py --part passrate --yes
    .venv/bin/python eng-experiments/exp03_providers/bench.py --report

Sample: 100 of the 300 examples, picked round-robin over the dataset's tags (fixed: results/sample_ids.json).

latency  = one LLM call per example per model: the step-1 request the app would send (prompt v25, exact
           detected_places, the example's seeded state, the tool registry), identical content for every model.
           Models are called round-robin per example. No retries (max_retries=0); 429s and timeouts are counted
           separately and left out of the latency numbers. Each Groq model is paced (its plan: 8k tokens/min).
passrate = the full agent turn on the same 100 examples, once per model, models run side by side (threads),
           Groq turns paced. Scored with evals/run.py's check().
"""
import argparse
import json
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import common  # noqa: E402
from app import agent, llm, places, state, tools  # noqa: E402
from openai import OpenAI  # noqa: E402

RESULTS = HERE / "results"
SAMPLE = RESULTS / "sample_ids.json"
N_SAMPLE = 100
GROQ_GAP_S = {"latency": 30, "passrate": 65}      # seconds between two calls/turns to one Groq model
# Rough cost of one step-1 call / one full turn per model (USD), from the 2026-11-20 benchmark; for the plan only.
USD_CALL = {"openai/gpt-5.4-nano": 0.0004, "sarvam/gemma4": 0.0013, "sarvam/deepseekv4.1-flash-1017": 0.0001,
            "groq/openai/gpt-oss-120b": 0.0005, "groq/openai/gpt-oss-20b": 0.0003, "groq/qwen/qwen3.8-27b": 0.003}
USD_TURN = {"openai/gpt-5.4-nano": 0.0007, "sarvam/gemma4": 0.0018, "sarvam/deepseekv4.1-flash-1017": 0.0002,
            "groq/openai/gpt-oss-120b": 0.0008, "groq/openai/gpt-oss-20b": 0.0004, "groq/qwen/qwen3.8-27b": 0.008}
TAGS = ("new-trip", "add", "remove", "replace", "move", "compound", "position", "undo", "clear", "round-trip",
        "typo", "alias", "tourist", "same-name", "region", "answer", "foreign", "trap-word", "no-edit", "many-stops")


def sample() -> list[dict]:
    """100 examples: round-robin over the tags, each time the first example with that tag not yet taken."""
    examples = common.load_examples()
    if SAMPLE.exists():
        ids = json.loads(SAMPLE.read_text())
        return [e for e in examples if e["id"] in set(ids)]
    chosen, i = [], 0
    while len(chosen) < N_SAMPLE:
        tag = TAGS[i % len(TAGS)]
        nxt = next((e for e in examples if tag in e["tags"] and e not in chosen), None) or \
            next(e for e in examples if e not in chosen)
        chosen.append(nxt)
        i += 1
    RESULTS.mkdir(exist_ok=True)
    SAMPLE.write_text(json.dumps(sorted(e["id"] for e in chosen)))
    return sorted(chosen, key=lambda e: e["id"])


def no_retry_clients() -> None:
    """One attempt per call: a retry would hide a 429 or add its wait to the latency."""
    llm._openai = llm._openai.with_options(max_retries=0)
    for name, p in llm.PROVIDERS.items():
        if p["url"]:
            import os
            llm._clients[name] = OpenAI(base_url=p["url"], api_key=os.environ.get(p["key"]), timeout=llm.TIMEOUT_S,
                                        max_retries=0)


def request_for(ex: dict) -> list[dict]:
    sid = f"{common.SESSION_PREFIX}exp03-req-{ex['id']}"
    common.seed(sid, ex, llm.DEFAULT)
    items = agent._context_items(sid, ex["text"], ex["kind"])
    state.drop_session(sid)
    return items


def one_call(model: str, items: list[dict]) -> dict:
    m = llm.MODELS[model]
    t0 = time.perf_counter()
    try:
        r = llm.step(model, agent.SYSTEM_PROMPT, items, tools.openai_tools(), m["effort"], m["max_out"])
    except Exception as e:
        status = getattr(e, "status_code", None)
        kind = "429" if status == 429 else "timeout" if "Timeout" in type(e).__name__ else f"error {status or ''}"
        return {"status": kind.strip(), "error": f"{type(e).__name__}: {e}"[:300],
                "ms": round((time.perf_counter() - t0) * 1000, 1)}
    names = [c.name for c in r.calls]
    return {"status": "ok", "ms": round((time.perf_counter() - t0) * 1000, 1), "usage": r.usage,
            "cost_usd": round(llm.cost_usd(model, r.usage), 7), "calls": names,
            "valid_tool_names": all(n in tools.TOOLS for n in names), "text": r.text[:200]}


def run_latency(examples: list[dict], models: list[str]) -> Path:
    no_retry_clients()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log = RESULTS / f"latency-{stamp}.jsonl"
    last_groq: dict[str, float] = {}
    for n, ex in enumerate(examples, 1):
        items = request_for(ex)
        for model in models:                                   # round-robin: every model on this example
            if llm.MODELS[model]["provider"] == "groq":
                wait = GROQ_GAP_S["latency"] - (time.time() - last_groq.get(model, 0))
                if wait > 0:
                    time.sleep(wait)
                last_groq[model] = time.time()
            row = {"example": ex["id"], "model": model, "tags": ex["tags"], **one_call(model, items)}
            with log.open("a") as f:
                f.write(json.dumps(row, ensure_ascii=False) + "\n")
            print(f"[{n}/{len(examples)}] {ex['id']} {model:34} {row['status']:8} {row['ms']:8.0f} ms "
                  f"{row.get('calls', '')}", flush=True)
    return log


def run_passrate(examples: list[dict], models: list[str]) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    log = RESULTS / f"passrate-{stamp}.jsonl"
    lock = threading.Lock()
    v25 = common.Variant("v25")              # no patches, so threads don't touch each other's globals

    def worker(model: str) -> None:
        for n, ex in enumerate(examples, 1):
            if llm.MODELS[model]["provider"] == "groq" and n > 1:
                time.sleep(GROQ_GAP_S["passrate"])
            r = common.run_one(v25, ex, model)
            r["model"] = model
            with lock, log.open("a") as f:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
            print(f"[{model} {n}/{len(examples)}] {ex['id']} {'PASS' if r['pass'] else 'FAIL'} "
                  f"steps={r['steps']} {r['seconds']:.2f}s", flush=True)

    threads = [threading.Thread(target=worker, args=(m,)) for m in models]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return log


def report() -> None:
    lat_files = sorted(RESULTS.glob("latency-*.jsonl"))
    pass_files = sorted(RESULTS.glob("passrate-*.jsonl"))
    lat = [json.loads(l) for f in lat_files for l in f.read_text().splitlines()]
    pas = [json.loads(l) for f in pass_files for l in f.read_text().splitlines()]
    models = list(dict.fromkeys([r["model"] for r in lat] + [r["model"] for r in pas]))
    rows = []
    for m in models:
        rs = [r for r in lat if r["model"] == m]
        ok = [r for r in rs if r["status"] == "ok"]
        p = common.percentiles([r["ms"] for r in ok])
        ci = p["p50_ci"]
        u = lambda k: common.pct([r["usage"][k] for r in ok], 50)
        cached = sum(r["usage"]["cached"] for r in ok) / max(1, sum(r["usage"]["input"] for r in ok))
        rows.append([m, len(rs), len(ok), sum(r["status"] == "429" for r in rs),
                     sum(r["status"] == "timeout" for r in rs), sum(r["status"].startswith("error") for r in rs),
                     common._f(p["p50"], 0), f"[{ci[0]:.0f}, {ci[1]:.0f}]" if ci else None, common._f(p["p90"], 0),
                     common._f(p["p95"], 0), common._f(p.get("p99", p.get("max")), 0),
                     common._f(u("input"), 0), f"{cached:.0%}", common._f(u("output"), 0),
                     common._f(sum(r["cost_usd"] for r in ok) / len(ok), 6) if ok else None,
                     f"{sum(r['valid_tool_names'] for r in ok)}/{len(ok)}"])
    lat_md = common.md_table(rows, ["model", "calls", "ok", "429", "timeout", "other error", "p50 ms",
                                    "p50 95% CI", "p90 ms", "p95 ms", "p99 or max ms", "input tok p50 (provider)",
                                    "cached share", "output tok p50", "USD / call", "valid tool names"])
    prow = []
    for m in models:
        rs = [r for r in pas if r["model"] == m]
        if not rs:
            continue
        t = common.percentiles([r["seconds"] for r in rs])
        prow.append([m, len(rs), sum(r["pass"] for r in rs), round(sum(r["pass"] for r in rs) / len(rs), 3),
                     common._f(sum(r["steps"] or 0 for r in rs) / len(rs)), common._f(t["p50"]), common._f(t["p90"]),
                     common._f(t.get("p99", t.get("max"))),
                     common._f(sum(r["cost_usd"] or 0 for r in rs) / len(rs), 6), sum(bool(r["error"]) for r in rs)])
    pas_md = common.md_table(prow, ["model", "turns", "passed", "pass rate", "LLM steps / turn", "turn s p50",
                                    "p90", "p99 or max", "USD / turn", "errors"]) if prow else "Not run yet."
    files = ", ".join(f"`{f.name}`" for f in lat_files + pass_files)
    body = (f"Files: {files or 'none'}. Commit {common.git_meta()['commit']}.\n\n### One step-1 call per example "
            f"(same request for every model)\n\n{lat_md}\n\n### Full agent turn on the same examples\n\n"
            f"Turn seconds include Groq's own queueing only when a call waited server-side; the pause between "
            f"turns is not counted.\n\n{pas_md}\n")
    common.write_section(HERE / "README.md", "RESULTS", body)
    print(body)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--part", choices=["latency", "passrate"])
    p.add_argument("--yes", action="store_true", help="really call the LLMs (costs money)")
    p.add_argument("--first", type=int)
    p.add_argument("--models", nargs="*", help="default: every model whose key is set")
    p.add_argument("--report", action="store_true")
    a = p.parse_args()
    if a.report:
        return report()
    if not a.part:
        sys.exit("--part latency|passrate, or --report")
    examples = sample()[:a.first] if a.first else sample()
    models = a.models or [m["id"] for m in llm.available()]
    table = USD_CALL if a.part == "latency" else USD_TURN
    usd = sum(table[m] for m in models) * len(examples)
    groq = sum(llm.MODELS[m]["provider"] == "groq" for m in models)
    mins = len(examples) * GROQ_GAP_S[a.part] / 60 if groq else 0
    print(f"Plan: {a.part}, {len(examples)} examples x {len(models)} models = {len(examples) * len(models)} "
          f"{'calls' if a.part == 'latency' else 'turns'}, about ${usd:.2f}; Groq pacing makes it ~{mins:.0f} min.\n"
          f"Models: {models}")
    if not a.yes:
        sys.exit("Dry run only. Add --yes to really call the LLMs (costs money).")
    places.load()
    log = run_latency(examples, models) if a.part == "latency" else run_passrate(examples, models)
    print(f"saved {log}")
    report()


if __name__ == "__main__":
    main()

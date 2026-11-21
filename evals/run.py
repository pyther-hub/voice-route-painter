"""Eval runner: plays every session in evals/sessions.json through the real agent and scores each turn.

    python -m evals.run --yes                 # all 50 sessions (calls the LLM API: costs money)
    python -m evals.run --yes --only s06 s37  # a few sessions
    python -m evals.run --yes --tag undo      # sessions with this tag
    python -m evals.run --yes --first 5       # the first 5 sessions (cost check before a full run)
    python -m evals.run --yes --model sarvam/gemma4   # another model from app/llm.py (default: LLM_MODEL or DeepSeek)
    python -m evals.run --yes --pause 65      # wait between turns (a provider's per-minute token limit, e.g. Groq's)

Every turn is printed and appended to evals/results/<prompt>-<time>.turns.jsonl as it finishes (reply, tool
calls, stops, question/suggestion cards, token split, cost in USD, failed checks), so a stopped run keeps its log.

Without --yes it only prints what it would run. Results go to evals/results/<prompt>-<model>-<time>.json, and a
summary is printed: pass rate overall, by input length (short < 40 chars, medium < 120, long), and by tag,
plus LLM steps, tokens and seconds per turn (read from the traces).

Each turn's `expect` is a set of checks on the trip after the turn (all must hold):
    stops      exact list of place ids               order     these ids appear in this order (others may be between)
    first/last the start / the end                   has       these ids are in the trip
    not_has    these ids are not in the trip         max_len   at most this many stops
    round_trip the trip ends where it starts         asks      a question card is showing
    unchanged  the stops are as before               any_of    a list of check sets; at least one must hold
"""
import argparse
import json
import sys
import time
from pathlib import Path

from app import state

HERE = Path(__file__).parent
SESSIONS = HERE / "sessions.json"
CHECKS = {"stops", "order", "first", "last", "has", "not_has", "max_len", "round_trip", "asks",
          "unchanged", "any_of"}


def length_bucket(text: str) -> str:
    return "short" if len(text) < 40 else "medium" if len(text) < 120 else "long"


def _subsequence(want: list[str], ids: list[str]) -> bool:
    it = iter(ids)
    return all(any(x == w for x in it) for w in want)


def check(expect: dict, after: dict, before: dict) -> list[str]:
    """Failed checks, as short messages ([] = pass). `after`/`before` = {stops, asks}."""
    ids, fails = after["stops"], []
    for key, want in expect.items():
        ok = {
            "stops": lambda: ids == want,
            "order": lambda: _subsequence(want, ids),
            "first": lambda: bool(ids) and ids[0] == want,
            "last": lambda: bool(ids) and ids[-1] == want,
            "has": lambda: all(w in ids for w in want),
            "not_has": lambda: not any(w in ids for w in want),
            "max_len": lambda: len(ids) <= want,
            "round_trip": lambda: (len(ids) >= 3 and ids[0] == ids[-1]) == want,
            "asks": lambda: after["asks"] == want,
            "unchanged": lambda: (ids == before["stops"]) == want,
            "any_of": lambda: any(not check(alt, after, before) for alt in want),
        }[key]()
        if not ok:
            fails.append(f"{key}: want {want!r}, got stops={ids} "
                         f"asks={after['asks']}")
    return fails


def snapshot(session_id: str) -> dict:
    t, u = state.trip(session_id), state.ui(session_id)
    return {"stops": [x["id"] for x in t["stops"]],
            "asks": u["question"] is not None}


from app.llm import cost_usd   # per-model prices, the same the server's spend cap uses


def turn_stats(trace_id: str | None, model: str) -> dict:
    """From the turn's trace: LLM steps, token split, cost, every tool call and any error."""
    from app import trace
    data = trace.read_trace(trace_id) if trace_id else None
    if not data:
        return {"steps": None, "tokens": None, "usage": None, "cost_usd": None, "calls": [], "error": None}
    root = next((sp for sp in data["spans"] if sp["parent"] is None), {})
    llm = [sp for sp in data["spans"] if sp["kind"] == "llm" and sp["name"] != "frontier"]   # the cheap agent's steps
    from app.llm import FRONTIER
    frontier = {"input": 0, "cached": 0, "output": 0}            # the planner's calls, priced at its own rate
    for sp in data["spans"]:
        if sp["kind"] == "llm" and sp["name"] == "frontier" and isinstance(sp["output"], dict):
            for k in frontier:
                frontier[k] += (sp["output"].get("usage") or {}).get(k, 0)
    usage = {"input": 0, "cached": 0, "output": 0}
    calls = []
    for sp in llm:
        out = sp["output"] if isinstance(sp["output"], dict) else {}
        for k in usage:
            usage[k] += (out.get("usage") or {}).get(k, 0)
        calls += out.get("calls") or []
    tools = [{"tool": sp["name"], "ok": (sp["output"] or {}).get("ok") if isinstance(sp["output"], dict) else None,
              "error": sp["error"] or ((sp["output"] or {}).get("error") if isinstance(sp["output"], dict) else None)}
             for sp in data["spans"] if sp["kind"] == "tool"]
    return {"steps": len(llm), "tokens": (root.get("output") or {}).get("tokens"), "usage": usage,
            "cost_usd": round(cost_usd(model, usage) + cost_usd(FRONTIER, frontier), 6), "frontier_usage": frontier, "calls": calls, "tool_results": tools,
            "error": root.get("error"), "trace_id": trace_id}


def _name(model: str, stamp: str) -> str:
    from app import agent
    return f"{agent.PROMPT_VERSION}-{model.replace('/', '-')}-{stamp}"


def run(sessions: list[dict], model: str, pause: float = 0) -> dict:
    from app import agent                     # imported here: building the client needs the API key
    rows = []
    stamp = time.strftime('%Y%m%d-%H%M%S')
    log = HERE / "results" / f"{_name(model, stamp)}.turns.jsonl"   # one line per turn, written as it runs
    log.parent.mkdir(exist_ok=True)
    for sess in sessions:
        sid = f"eval-{sess['id']}-{int(time.time())}"
        state.session(sid).model = model
        for n, t in enumerate(sess["turns"], 1):
            if pause and rows:
                time.sleep(pause)                     # not counted in the turn's seconds
            before = snapshot(sid)
            state.set_display(sid, question=None, suggestions=[])      # as main.py does on new input
            t0 = time.time()
            reply = agent.run_turn(sid, t["say"])
            seconds = round(time.time() - t0, 2)
            after = snapshot(sid)
            fails = check(t["expect"], after, before)
            u = state.ui(sid)
            rows.append({"session": sess["id"], "turn": n, "say": t["say"], "length": length_bucket(t["say"]),
                         "tags": sess["tags"], "expect": t["expect"], "reply": reply, "pass": not fails,
                         "fails": fails, "after": after, "question": u["question"],
                         "suggestions": [x["name"] for x in u["suggestions"]],
                         "seconds": seconds, **turn_stats(u["trace_id"], model)})
            with log.open("a") as f:
                f.write(json.dumps(rows[-1], ensure_ascii=False) + "\n")
            r = rows[-1]
            u = r["usage"] or {}
            print(f"{'PASS' if not fails else 'FAIL'} {sess['id']}.{n} [{r['length']}] {t['say'][:70]!r}\n"
                  f"      reply: {reply[:110]!r}\n"
                  f"      calls: {[c[:90] for c in r['calls']]}\n"
                  f"      stops: {after['stops']}  steps={r['steps']} in={u.get('input')} cached={u.get('cached')} "
                  f"out={u.get('output')} ${r['cost_usd']} {seconds}s"
                  + ("" if not fails else "".join(f"\n      FAIL {x}" for x in fails)), flush=True)
        state.drop_session(sid)
    return summarize(rows, stamp, model)


def _rate(rows: list[dict]) -> str:
    return f"{sum(r['pass'] for r in rows)}/{len(rows)}" if rows else "-"


def _avg(rows: list[dict], key: str):
    vals = [r[key] for r in rows if r.get(key) is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def _pct(rows: list[dict], key: str, p: float):
    """Nearest-rank percentile (with few turns, p99 is the slowest or near it)."""
    vals = sorted(r[key] for r in rows if r.get(key) is not None)
    return vals[max(0, -(-len(vals) * p // 100) - 1)] if vals else None


def summarize(rows: list[dict], stamp: str, model: str) -> dict:
    from app import agent
    sessions = {}
    for r in rows:
        sessions.setdefault(r["session"], []).append(r["pass"])
    usage = {k: sum((r["usage"] or {}).get(k, 0) for r in rows) for k in ("input", "cached", "output")}
    cost = sum(r["cost_usd"] or 0 for r in rows)
    all_turns = sum(len(s["turns"]) for s in json.loads(SESSIONS.read_text()))
    summary = {
        "prompt_version": agent.PROMPT_VERSION, "model": model,
        "turns_passed": _rate(rows),
        "sessions_passed": f"{sum(all(v) for v in sessions.values())}/{len(sessions)}",
        "by_length": {b: _rate([r for r in rows if r["length"] == b]) for b in ("short", "medium", "long")},
        "by_tag": {tag: _rate([r for r in rows if tag in r["tags"]])
                   for tag in sorted({t for r in rows for t in r["tags"]})},
        "per_turn": {k: _avg(rows, k) for k in ("steps", "tokens", "seconds")},
        "seconds_p50_p95_p99": [_pct(rows, "seconds", p) for p in (50, 95, 99)],
        "seconds_max": max((r["seconds"] for r in rows), default=None),
        "tokens": usage,
        "cost_usd": round(cost, 4),
        "cost_per_turn_usd": round(cost / len(rows), 5) if rows else None,
        "projected_all_turns_usd": round(cost / len(rows) * all_turns, 3) if rows else None,
        "errors": [f"{r['session']}.{r['turn']}: {r['error']}" for r in rows if r.get("error")],
    }
    out = HERE / "results" / f"{_name(model, stamp)}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "turns": rows}, indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1), f"\nsaved {out}")
    return summary


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--yes", action="store_true", help="really run (calls the LLM API, costs money)")
    p.add_argument("--only", nargs="*", help="session ids")
    p.add_argument("--tag", help="only sessions with this tag")
    p.add_argument("--first", type=int, help="only the first N sessions")
    p.add_argument("--pause", type=float, default=0, help="seconds to wait between turns (rate-limited providers)")
    p.add_argument("--model", help="an id from app/llm.py MODELS (default: LLM_MODEL or DeepSeek)")
    a = p.parse_args()
    sessions = json.loads(SESSIONS.read_text())
    if a.only:
        sessions = [s for s in sessions if s["id"] in a.only]
    if a.tag:
        sessions = [s for s in sessions if a.tag in s["tags"]]
    if a.first:
        sessions = sessions[:a.first]
    turns = sum(len(s["turns"]) for s in sessions)
    if not a.yes:
        print(f"Would run {len(sessions)} sessions, {turns} turns (~{turns * 2} LLM calls, ~{turns * 4}k tokens). "
              f"Add --yes to run.")
        sys.exit(0)
    from app import llm
    model = a.model or llm.DEFAULT
    if model not in llm.MODELS:
        sys.exit(f"Unknown model {model!r}; one of {sorted(llm.MODELS)}")
    run(sessions, model, a.pause)


if __name__ == "__main__":
    main()

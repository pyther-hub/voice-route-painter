"""Cheap-model-only vs escalated (plan_with_frontier) on 10 open-ended requests: evals/frontier.json.

    python -m evals.frontier                  # prints the plan only
    python -m evals.frontier --yes            # runs both arms on all 10 cases (paid: ~20 cheap turns + ~10 frontier calls)
    python -m evals.frontier --yes --only f01 f07 --model sarvam/gemma4

Arms (fresh session per case and arm, one turn each):
    cheap      the previous prompt (orchestrator_v25) and the tool set without plan_with_frontier / accept_proposal:
               what the cheap agent does on its own.
    escalated  the current prompt and all tools: the cheap agent may hand the request to llm.FRONTIER.

Checks, by code (no judge model):
    itinerary  usable = a plan of min..max stops (the proposal, or the trip the cheap agent built) with at least
               80% of its stops in the expected states; in_region = that share.
    compare    usable = escalated: a comparison with a recommendation and both routes; cheap: a reply that names
               one of the case's keywords (it at least took a side).
    all        escalated = the frontier was called; mutated = the trip changed (the escalated arm must not);
               spoken line <= 160 chars; language of the reply matches the user's (simple Hinglish word check).
Results: evals/results/frontier-<model>-<time>.json, and a side-by-side table.
"""
import argparse
import json
import re
import statistics
import sys
import time
from pathlib import Path

from app import llm, places, state, tools, trace

HERE = Path(__file__).parent
CASES = HERE / "frontier.json"
CHEAP_PROMPT = "orchestrator_v25"
FRONTIER_TOOLS = ("plan_with_frontier", "accept_proposal")
HINGLISH = {"hai", "hain", "se", "ka", "ki", "ke", "mein", "karo", "kar", "wala", "aur", "nahi", "ho", "ye", "yeh",
            "raha", "rahe", "chahiye", "bhi", "din", "jaana", "jagah", "accha", "behtar", "kaunsa"}


def is_hinglish(text: str) -> bool:
    words = re.findall(r"[a-z]+", (text or "").lower())
    return sum(w in HINGLISH for w in words) >= 2


def _usage(spans: list[dict], frontier: bool) -> dict:
    total = {"input": 0, "cached": 0, "output": 0}
    for sp in spans:
        if sp["kind"] == "llm" and (sp["name"] == "frontier") == frontier and isinstance(sp["output"], dict):
            for k in total:
                total[k] += (sp["output"].get("usage") or {}).get(k, 0)
    return total


def _plan_stops(proposal: dict | None, trip_ids: list[str]) -> list[dict]:
    if proposal and proposal["kind"] == "itinerary":
        return proposal["stops"]
    return [places.get(i) for i in trip_ids]


def score(case: dict, arm: str, reply: str, before: list[str], after: list[str], proposal: dict | None,
          frontier_called: bool) -> dict:
    r = {"escalated": frontier_called, "mutated": after != before,
         "lang_ok": is_hinglish(reply) == (case["lang"] == "hinglish")}
    spoken = (proposal or {}).get("short_answer") or (proposal or {}).get("tagline") or reply
    r["spoken_ok"] = len(spoken or "") <= 160
    if case["mode"] == "itinerary":
        stops = _plan_stops(proposal, after)
        inside = sum(1 for p in stops if p and p["state"] in case["states"])
        r["n_stops"] = len(stops)
        r["in_region"] = round(inside / len(stops), 2) if stops else 0.0
        r["usable"] = case["min_stops"] <= len(stops) <= case["max_stops"] and r["in_region"] >= 0.8
    else:
        if arm == "escalated":
            r["usable"] = bool(proposal and proposal["kind"] == "compare" and proposal.get("recommended")
                               and all(len(o["stops"]) >= 2 for o in proposal["options"]))
        else:
            r["usable"] = any(k in (reply or "").lower() for k in case["keywords"])
    return r


def run_case(case: dict, arm: str, model: str) -> dict:
    from app import agent
    saved_prompt, saved_tools = agent.SYSTEM_PROMPT, dict(tools.TOOLS)
    if arm == "cheap":                       # what the agent did before the planner existed
        agent.SYSTEM_PROMPT = (Path(agent.__file__).parent / "prompts" / f"{CHEAP_PROMPT}.md").read_text()
        for name in FRONTIER_TOOLS:
            tools.TOOLS.pop(name, None)
    sid = f"evalf-{case['id']}-{arm}-{int(time.time())}"
    state.session(sid).model = model
    try:
        before = state.stop_ids(sid)
        t0 = time.time()
        reply = agent.run_turn(sid, case["say"])
        seconds = round(time.time() - t0, 2)
    finally:
        agent.SYSTEM_PROMPT = saved_prompt
        tools.TOOLS.clear()
        tools.TOOLS.update(saved_tools)
    after, ui = state.stop_ids(sid), state.ui(sid)
    data = trace.read_trace(ui["trace_id"]) if ui["trace_id"] else {"spans": []}
    spans = data["spans"] if data else []
    cheap_u, front_u = _usage(spans, False), _usage(spans, True)
    frontier_called = any(sp["kind"] == "llm" and sp["name"] == "frontier" for sp in spans)
    row = {"case": case["id"], "mode": case["mode"], "arm": arm, "say": case["say"], "reply": reply,
           "stops_after": after, "proposal": ui["proposal"], "seconds": seconds,
           "steps": sum(1 for sp in spans if sp["kind"] == "llm" and sp["name"] != "frontier"),
           "tools": [sp["name"] for sp in spans if sp["kind"] == "tool"],
           "usage_cheap": cheap_u, "usage_frontier": front_u,
           "cost_usd": round(llm.cost_usd(model, {**cheap_u, "total": 0})
                             + llm.cost_usd(llm.FRONTIER, {**front_u, "total": 0}), 6),
           "trace_id": ui["trace_id"],
           **score(case, arm, reply, before, after, ui["proposal"], frontier_called)}
    state.drop_session(sid)
    return row


def summarize(rows: list[dict]) -> dict:
    out = {}
    for arm in ("cheap", "escalated"):
        rs = [r for r in rows if r["arm"] == arm]
        if not rs:
            continue
        out[arm] = {
            "usable": f"{sum(r['usable'] for r in rs)}/{len(rs)}",
            "usable_itinerary": f"{sum(r['usable'] for r in rs if r['mode'] == 'itinerary')}/"
                                f"{sum(r['mode'] == 'itinerary' for r in rs)}",
            "usable_compare": f"{sum(r['usable'] for r in rs if r['mode'] == 'compare')}/"
                              f"{sum(r['mode'] == 'compare' for r in rs)}",
            "escalated": sum(r["escalated"] for r in rs),
            "mutated_trip": sum(r["mutated"] for r in rs),
            "in_region_avg": round(statistics.mean(r["in_region"] for r in rs if "in_region" in r), 2)
            if any("in_region" in r for r in rs) else None,
            "lang_ok": f"{sum(r['lang_ok'] for r in rs)}/{len(rs)}",
            "spoken_ok": f"{sum(r['spoken_ok'] for r in rs)}/{len(rs)}",
            "seconds_p50": statistics.median(r["seconds"] for r in rs),
            "seconds_max": max(r["seconds"] for r in rs),
            "cost_usd": round(sum(r["cost_usd"] for r in rs), 4),
        }
    return out


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--yes", action="store_true", help="really run (calls the LLM APIs, costs money)")
    p.add_argument("--only", nargs="*", help="case ids")
    p.add_argument("--arm", choices=["cheap", "escalated"], help="only one arm")
    p.add_argument("--model", help="cheap model, an id from app/llm.py MODELS (default: LLM_MODEL or nano)")
    a = p.parse_args()
    cases = json.loads(CASES.read_text())
    if a.only:
        cases = [c for c in cases if c["id"] in a.only]
    arms = [a.arm] if a.arm else ["cheap", "escalated"]
    model = a.model or llm.DEFAULT
    if not a.yes:
        print(f"Would run {len(cases)} cases × {arms} with cheap model {model} and frontier {llm.FRONTIER} "
              f"(~{len(cases) * len(arms) * 2} cheap LLM calls + up to {len(cases)} frontier calls). Add --yes to run.")
        sys.exit(0)
    places.load()
    rows = []
    for c in cases:
        for arm in arms:
            r = run_case(c, arm, model)
            rows.append(r)
            print(f"{'OK  ' if r['usable'] else 'FAIL'} {c['id']} {arm:9} esc={r['escalated']!s:5} "
                  f"mut={r['mutated']!s:5} {r['seconds']:5.1f}s ${r['cost_usd']:.4f}  {r['reply'][:90]!r}", flush=True)
    summary = summarize(rows)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = HERE / "results" / f"frontier-{model.replace('/', '-')}-{stamp}.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"model": model, "frontier": llm.FRONTIER, "summary": summary, "rows": rows},
                              indent=1, ensure_ascii=False))
    print(json.dumps(summary, indent=1), f"\nsaved {out}")


if __name__ == "__main__":
    main()

"""exp-00: where a turn's time and tokens go, from traces.db (free, no LLM calls).

    .venv/bin/python eng-experiments/exp00_baseline/analyze.py

Reads real app sessions only (session id = 12 hex characters, as main.py makes them; tests, evals, CLI and
experiment runs are left out) with the current tool set (prompt v20 or later). Writes results/exp00.json and
regenerates README.md (numbers only).
"""
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))

import common  # noqa: E402  (puts the repo root on sys.path)
import tiktoken  # noqa: E402
from app import agent, state, tools  # noqa: E402

DB = common.ROOT / "traces.db"
ENC = tiktoken.get_encoding("o200k_base")
EDIT_TOOLS = {"set_route", "add_stop", "remove_stop", "undo"}
REAL_SESSION = re.compile(r"^[0-9a-f]{12}$")


def tok(x) -> int:
    return len(ENC.encode(x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)))


def load():
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    rows = db.execute("select trace_id, span_id, parent, kind, name, input, output, duration_ms, error, started_at "
                      "from spans").fetchall()
    cols = ["trace_id", "span_id", "parent", "kind", "name", "input", "output", "ms", "error", "started_at"]
    spans = []
    for r in rows:
        s = dict(zip(cols, r))
        for k in ("input", "output"):
            try:
                s[k] = json.loads(s[k]) if s[k] else None
            except (TypeError, ValueError):
                pass
        spans.append(s)
    return spans


def version_no(v: str | None) -> int:
    m = re.search(r"_v(\d+)$", v or "")
    return int(m.group(1)) if m else 0


def turns_of(spans):
    by_trace = defaultdict(list)
    for s in spans:
        by_trace[s["trace_id"]].append(s)
    turns = []
    for tid, ss in by_trace.items():
        root = next((s for s in ss if s["parent"] is None), None)
        if not root or root["kind"] != "agent" or root["name"] != "turn" or not isinstance(root["input"], dict):
            continue
        inp = root["input"]
        if not REAL_SESSION.match(inp.get("session") or "") or version_no(inp.get("prompt_version")) < 20:
            continue
        llm = sorted([s for s in ss if s["kind"] == "llm" and s["name"].startswith("step")],
                     key=lambda s: s["started_at"])
        if not llm or root["ms"] < 100:
            continue
        tool = [s for s in ss if s["kind"] == "tool"]
        steps = []
        for s in llm:
            out = s["output"] if isinstance(s["output"], dict) else {}
            names = [c.split("(")[0] for c in out.get("calls") or []]
            steps.append({"ms": s["ms"], "calls": names, "usage": out.get("usage") or {}})
        model = inp.get("model") or "gpt-5.4-nano"
        model = model if "/" in model else f"openai/{model}"      # older traces logged the bare OpenAI name
        turns.append({"trace": tid, "session": inp["session"], "model": model,
                      "prompt": inp.get("prompt_version"), "text": inp.get("text"), "started": root["started_at"],
                      "reply": (root["output"] or {}).get("reply") if isinstance(root["output"], dict) else None,
                      "ms": root["ms"], "llm_ms": sum(s["ms"] for s in llm), "tool_ms": sum(s["ms"] for s in tool),
                      "tool_ms_by": Counter({t["name"]: 0 for t in tool}) + Counter(), "steps": steps,
                      "tool_names": [t["name"] for t in tool]})
        for t in tool:
            turns[-1]["tool_ms_by"][t["name"]] += t["ms"]
    turns.sort(key=lambda t: t["started"])
    return turns


def r1(x):
    return None if x is None else round(x, 1)


def latency_table(turns):
    out = {}
    for model in sorted({t["model"] for t in turns}):
        ts = [t for t in turns if t["model"] == model]
        tot = sum(t["ms"] for t in ts)
        p = common.percentiles([t["ms"] / 1000 for t in ts])
        step_ms = common.percentiles([s["ms"] for t in ts for s in t["steps"]])
        out[model] = {"turns": len(ts), "steps_mean": r1(sum(len(t["steps"]) for t in ts) / len(ts)),
                      "steps_hist": dict(sorted(Counter(len(t["steps"]) for t in ts).items())),
                      "turn_s": p, "llm_step_ms": step_ms,
                      "share_llm": round(sum(t["llm_ms"] for t in ts) / tot, 3),
                      "share_tools": round(sum(t["tool_ms"] for t in ts) / tot, 3),
                      "share_other": round(1 - (sum(t["llm_ms"] + t["tool_ms"] for t in ts)) / tot, 3)}
    return out


def first_step_table(turns):
    out = {}
    for model in sorted({t["model"] for t in turns}):
        c, n = Counter(), 0
        for t in turns:
            if t["model"] != model:
                continue
            n += 1
            calls = set(t["steps"][0]["calls"])
            kind = ("resolve_place" if "resolve_place" in calls else "edit" if calls & EDIT_TOOLS
                    else next(iter(sorted(calls)), "no tool call (text)"))
            c[kind] += 1
        out[model] = {"turns": n, **{k: round(v / n, 3) for k, v in c.most_common()}}
    return out


def edits_per_step(turns):
    out = {}
    for model in sorted({t["model"] for t in turns}):
        hist, steps_with_edits, turns_multi = Counter(), 0, Counter()
        for t in turns:
            if t["model"] != model:
                continue
            n_edit_steps = 0
            for s in t["steps"]:
                k = sum(c in EDIT_TOOLS for c in s["calls"])
                if k:
                    hist[k] += 1
                    n_edit_steps += 1
            if n_edit_steps:
                turns_multi[n_edit_steps] += 1
        out[model] = {"edit_calls_per_edit_step": dict(sorted(hist.items())),
                      "edit_steps_per_edit_turn": dict(sorted(turns_multi.items()))}
    return out


def tool_time(turns):
    by = defaultdict(list)
    for t in turns:
        for name, ms in t["tool_ms_by"].items():
            by[name].append(ms)
    return {k: {"calls": len(v), "p50_ms": r1(common.pct(v, 50)), "p95_ms": r1(common.pct(v, 95))}
            for k, v in sorted(by.items(), key=lambda kv: -len(kv[1]))}


def stt_and_chips(spans):
    stt = [s for s in spans if s["kind"] == "stt" and s["ms"] > 50 and isinstance(s["output"], dict)
           and s["output"].get("transcript")]
    chips = [s for s in spans if s["kind"] == "llm" and s["name"] == "followups" and s["ms"] > 100]
    return {"stt": {**common.percentiles([s["ms"] for s in stt]),
                    "bytes_p50": common.pct([(s["input"] or {}).get("bytes") for s in stt], 50)},
            "followup_chips_llm": common.percentiles([s["ms"] for s in chips])}


def tokens_table(turns):
    """What one LLM step sends, in o200k tokens (OpenAI's tokenizer; other providers count differently)."""
    system = tok(agent.SYSTEM_PROMPT)
    schemas = tok(tools.openai_tools())
    turn_json = []
    for ex in common.load_examples():
        sid = f"{common.SESSION_PREFIX}exp00-tok"
        common.seed(sid, ex, "openai/gpt-5.4-nano")
        turn_json.append(tok(agent._context_items(sid, ex["text"], ex["kind"])[-1]["content"]))
        state.drop_session(sid)
    by_session, history = defaultdict(list), []
    for t in turns:
        prev = by_session[t["session"]][-state.MAX_TURNS:]
        history.append(sum(tok(u) + tok(r or "") for u, r in prev))
        by_session[t["session"]].append((t["text"] or "", t["reply"]))
    reported = defaultdict(lambda: {"input": [], "cached": []})
    for t in turns:
        s = t["steps"][0]["usage"]
        if s.get("input"):
            reported[t["model"]]["input"].append(s["input"])
            reported[t["model"]]["cached"].append(s.get("cached") or 0)
    return {"o200k": {"system_prompt_" + agent.PROMPT_VERSION: system, "tool_schemas": schemas,
                      "turn_input_json_p50_300_examples": common.pct(turn_json, 50),
                      "turn_input_json_p95_300_examples": common.pct(turn_json, 95),
                      "history_last3_turns_p50_real": common.pct(history, 50),
                      "history_last3_turns_p95_real": common.pct(history, 95)},
            "provider_reported_step1": {m: {"input_tokens_p50": common.pct(v["input"], 50),
                                            "cached_share": round(sum(v["cached"]) / max(1, sum(v["input"])), 3)}
                                        for m, v in sorted(reported.items())}}


def md_table(rows: list[list], head: list[str]) -> str:
    out = ["| " + " | ".join(head) + " |", "|" + "---|" * len(head)]
    out += ["| " + " | ".join("" if c is None else str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def fmt(x, nd=2):
    return "–" if x is None else f"{x:.{nd}f}" if isinstance(x, float) else str(x)


def write_readme(res: dict) -> None:
    L = res["latency"]
    lat = md_table([[m, v["turns"], v["steps_mean"], v["steps_hist"], fmt(v["turn_s"]["p50"]), fmt(v["turn_s"]["p90"]),
                     fmt(v["turn_s"]["p95"]), fmt(v["turn_s"].get("p99", v["turn_s"].get("max"))),
                     fmt(v["llm_step_ms"]["p50"], 0), f"{v['share_llm']:.0%}", f"{v['share_tools']:.0%}",
                     f"{v['share_other']:.0%}"] for m, v in L.items()],
                   ["model", "turns", "LLM steps / turn", "steps histogram", "turn p50 s", "p90 s", "p95 s",
                    "p99 or max s", "LLM step p50 ms", "LLM share", "tools share", "other share"])
    F = res["first_step"]
    kinds = sorted({k for v in F.values() for k in v if k != "turns"})
    first = md_table([[m, v["turns"]] + [f"{v.get(k, 0):.0%}" for k in kinds] for m, v in F.items()],
                     ["model", "turns"] + kinds)
    E = res["edits_per_step"]
    edits = md_table([[m, v["edit_calls_per_edit_step"], v["edit_steps_per_edit_turn"]] for m, v in E.items()],
                     ["model", "edit calls in one step: {calls: steps}", "steps with edits in one turn: {steps: turns}"])
    T = res["tool_time"]
    tt = md_table([[k, v["calls"], v["p50_ms"], v["p95_ms"]] for k, v in T.items()],
                  ["tool", "calls", "p50 ms", "p95 ms"])
    S = res["stt_chips"]
    stt = md_table([["Sarvam STT (saaras:v4)", S["stt"]["n"], fmt(S["stt"]["p50"], 0), fmt(S["stt"]["p90"], 0),
                     fmt(S["stt"]["p95"], 0), fmt(S["stt"].get("p99", S["stt"].get("max")), 0),
                     fmt(S["stt"]["bytes_p50"], 0)],
                    ["Follow-up chips LLM call (after the reply)", S["followup_chips_llm"]["n"],
                     fmt(S["followup_chips_llm"]["p50"], 0), fmt(S["followup_chips_llm"]["p90"], 0),
                     fmt(S["followup_chips_llm"]["p95"], 0),
                     fmt(S["followup_chips_llm"].get("p99", S["followup_chips_llm"].get("max")), 0), ""]],
                   ["call", "n", "p50 ms", "p90 ms", "p95 ms", "p99 or max ms", "audio bytes p50"])
    K = res["tokens"]
    tk = md_table([[k, fmt(v, 0)] for k, v in K["o200k"].items()], ["part of one LLM step's input", "o200k tokens"])
    rep = md_table([[m, fmt(v["input_tokens_p50"], 0), f"{v['cached_share']:.0%}"]
                    for m, v in K["provider_reported_step1"].items()],
                   ["model", "step-1 input tokens p50 (provider count)", "cached share"])
    meta = res["meta"]
    (HERE / "README.md").write_text(f"""# exp-00 Where a turn's time and tokens go

**Question.** Of a turn's latency and input tokens, how much is STT, each LLM step, tools, and the follow-up
chips call; how many LLM steps does a turn take, and what does the first step do?

**Setup.** `traces.db`, real app sessions only (12-hex session ids; tests, evals, CLI and experiment runs left
out), prompt v20 or later (the current tool set), turns ≥ 100 ms. No LLM calls. Generated by
`analyze.py` on {meta['date']} at commit {meta['commit']}{' (dirty tree)' if meta['dirty'] else ''}.
Token counts with tiktoken `o200k_base`. Percentiles: p99 only with n ≥ 300, else max.
Turns: {meta['turns']} from {meta['sessions']} sessions, prompts {meta['prompts']}.

## Turn latency by model

"other" = turn time outside LLM and tool spans (building the input, writing state, routing outside tools).

{lat}

## What the first LLM step does (share of turns)

{first}

## Edit calls per step

{edits}

## Tool time

{tt}

## STT and the follow-up chips call

STT runs before the turn (separate trace); chips run after the reply is shown.

{stt}

## Input tokens of one LLM step

{tk}

Provider-reported input tokens of step 1 (includes the parts above plus message framing):

{rep}
""")


def main():
    spans = load()
    turns = turns_of(spans)
    res = {"meta": {"date": __import__("time").strftime("%Y-%m-%d %H:%M"), **common.git_meta(),
                    "turns": len(turns), "sessions": len({t["session"] for t in turns}),
                    "prompts": sorted({t["prompt"] for t in turns}, key=version_no)},
           "latency": latency_table(turns), "first_step": first_step_table(turns),
           "edits_per_step": edits_per_step(turns), "tool_time": tool_time(turns),
           "stt_chips": stt_and_chips(spans), "tokens": tokens_table(turns)}
    out = HERE / "results"
    out.mkdir(exist_ok=True)
    (out / "exp00.json").write_text(json.dumps(res, indent=1, default=str))
    write_readme(res)
    print(json.dumps(res, indent=1, default=str)[:4000])


if __name__ == "__main__":
    main()

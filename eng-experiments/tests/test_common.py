"""The experiment harness, with a fake LLM (no paid calls): seeding, variant patching, scoring, stats."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common  # noqa: E402
from app import agent, llm, state  # noqa: E402

def load_module(rel: str, name: str):
    """Each experiment has a run.py: load it under its own name (a plain `import run` would clash)."""
    import importlib.util
    path = Path(__file__).resolve().parents[1] / rel
    sys.path.insert(0, str(path.parent))
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


NANO = "openai/gpt-5.4-nano"     # the fake replaces the OpenAI client, so the harness is tested on that path


def _call(name, args, n):
    return SimpleNamespace(type="function_call", name=name, arguments=json.dumps(args), call_id=f"c{n}",
                           model_dump=lambda **kw: {"type": "function_call", "name": name,
                                                    "arguments": json.dumps(args), "call_id": f"c{n}"})


def _fake_llm(monkeypatch, steps):
    sent, it = [], iter(steps)

    def create(**kw):
        sent.append({"instructions": kw["instructions"], "input": list(kw["input"])})
        usage = SimpleNamespace(total_tokens=10, input_tokens=8, output_tokens=2, input_tokens_details=None)
        return SimpleNamespace(output=next(it), output_text="", usage=usage)
    monkeypatch.setattr(llm._openai.responses, "create", create)
    return sent


EX = {"id": "t001", "tags": ["add"], "length": "short", "kind": "text", "text": "Jaipur ke baad Ajmer jodo",
      "start": {"stops": ["delhi", "jaipur"], "history": [["delhi", "agra"]],
                "pending_question": {"question": "Kaunsa?", "options": ["udaipur", "udaipur-tripura"]}},
      "ops": ["add ajmer after jaipur"], "alt_ops": [], "mentions": [],
      "expect": {"stops": ["delhi", "jaipur", "ajmer"], "asks": False}}


def test_seed_builds_history_trip_and_question():
    common.seed("seed1", EX, NANO)
    s = state.session("seed1")
    assert state.stop_ids("seed1") == ["delhi", "jaipur"]
    assert [h["stops"] for h in s.history] == [[], ["delhi", "agra"]]
    assert s.pending_question["options"][0] == {"label": "Udaipur, Rajasthan", "id": "udaipur"}
    assert s.model == NANO


def test_variant_is_restored_after_the_turn():
    before = (agent.PROMPT_VERSION, agent.detected_places)
    v = common.Variant("v24", prompt_file=common.prompt("orchestrator_v24"), detected=common.no_detection)
    with common.applied(v):
        assert agent.PROMPT_VERSION == "orchestrator_v24" and agent.detected_places is common.no_detection
    assert (agent.PROMPT_VERSION, agent.detected_places) == before


def test_run_one_scores_and_counts(monkeypatch):
    sent = _fake_llm(monkeypatch, [
        [_call("resolve_place", {"queries": ["Ajmer"]}, 1)],
        [_call("add_stop", {"place_id": "ajmer", "after": "jaipur", "before": None, "position": None}, 2)],
        [],
    ])
    v = common.Variant("v24", prompt_file=common.prompt("orchestrator_v24"), detected=common.no_detection)
    r = common.run_one(v, EX, NANO)
    assert r["pass"] and r["steps"] == 3 and r["resolve_calls"] == 1 and r["after"]["stops"][-1] == "ajmer"
    assert r["usage"] == {"input": 24, "cached": 0, "output": 6}
    first = json.loads(sent[0]["input"][-1]["content"])
    assert "detected_places" not in first and "detected_places" not in sent[0]["instructions"]


def test_v25_arm_gets_detected_places(monkeypatch):
    sent = _fake_llm(monkeypatch, [
        [_call("add_stop", {"place_id": "ajmer", "after": "jaipur", "before": None, "position": None}, 1)],
        [],
    ])
    r = common.run_one(common.Variant("v25"), EX, NANO)
    first = json.loads(sent[0]["input"][-1]["content"])
    assert r["pass"] and r["steps"] == 2 and r["resolve_calls"] == 0
    assert first["detected_places"][0]["matches"][0]["id"] == "ajmer"      # Jaipur is on the trip: left out


def test_stats():
    rows = [{"example": f"e{i}", "arm": a, "steps": s} for i, (x, y) in enumerate([(3, 2), (2, 2), (2, 1)])
            for a, s in (("a", x), ("b", y))]
    d = common.paired_diff(rows, "steps", "a", "b")
    assert d["n"] == 3 and d["down"] == 2 and d["same"] == 1 and abs(d["mean"] + 2 / 3) < 1e-9
    p = common.percentiles(list(range(1, 101)))
    assert p["n"] == 100 and "max" in p and "p99" not in p and p["max"] == 100
    assert "p99" in common.percentiles(list(range(300)))


def test_report_tables_build_from_rows(monkeypatch):
    exp01 = load_module("exp01_preresolve/run.py", "exp01_run")
    examples = {e["id"]: e for e in common.load_examples(first=4)}
    rows = []
    for i, ex in enumerate(examples.values()):
        for arm, steps in (("1-v24", 3), ("2-exact", 2), ("3-fuzzy90", 2)):
            rows.append({"example": ex["id"], "arm": arm, "tags": ex["tags"], "length": ex["length"],
                         "pass": i % 2 == 0, "steps": steps, "resolve_calls": 1 if arm == "1-v24" else 0,
                         "usage": {"input": 3000 * steps, "cached": 2000, "output": 50}, "cost_usd": 0.0002,
                         "seconds": 1.0 * steps, "llm_ms": [900.0] * steps, "error": None,
                         "tool_names": ["set_route"], "after": {"stops": ex["expect"].get("stops", []), "asks": False}})
    md = common.report_md(rows, ["1-v24", "2-exact", "3-fuzzy90"], ("new-trip",))
    assert "| 2-exact − 1-v24 | LLM steps | 4 | -1.0 |" in md and "### By tag" in md
    assert "### Wrong hints" in exp01.extra_tables(rows, examples)

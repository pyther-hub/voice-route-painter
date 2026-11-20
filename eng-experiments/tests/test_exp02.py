"""exp-02 arms with a fake LLM (no paid calls)."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE / "exp02_batched_actions"))

import common  # noqa: E402
from app import state  # noqa: E402
from test_common import NANO, _call, _fake_llm, load_module  # noqa: E402

exp02 = load_module("exp02_batched_actions/run.py", "exp02_run")
import variants  # noqa: E402

A, B, C = exp02.arms()
EX = {"id": "t2", "tags": ["replace"], "length": "short", "kind": "text", "text": "Agra ki jagah Mathura",
      "start": {"stops": ["delhi", "agra", "jaipur"], "history": [], "pending_question": None},
      "ops": ["rm agra", "add mathura after delhi"], "alt_ops": [], "mentions": [],
      "expect": {"stops": ["delhi", "mathura", "jaipur"], "asks": False}}


def _op(op, **kw):
    return {"op": op, "stops": None, "place_id": None, "after": None, "before": None, "position": None,
            "count": None, **kw}


def test_arm_a_needs_a_step_to_stop(monkeypatch):
    _fake_llm(monkeypatch, [[_call("remove_stop", {"place_id": "agra"}, 1),
                             _call("add_stop", {"place_id": "mathura", "after": "delhi", "before": None,
                                                "position": None}, 2)], []])
    r = common.run_one(A, EX, NANO)
    assert r["pass"] and r["steps"] == 2


def test_arm_b_ends_after_the_edit_step(monkeypatch):
    sent = _fake_llm(monkeypatch, [[_call("remove_stop", {"place_id": "agra"}, 1),
                                    _call("add_stop", {"place_id": "mathura", "after": "delhi", "before": None,
                                                       "position": None}, 2)]])
    r = common.run_one(B, EX, NANO)
    assert r["pass"] and r["steps"] == 1 and len(sent) == 1
    assert "ONE response" in sent[0]["instructions"]
    assert exp02.edit_ops(r) == ["rm agra", "add mathura after delhi"]


def test_arm_b_failed_edit_gets_another_step(monkeypatch):
    _fake_llm(monkeypatch, [[_call("add_stop", {"place_id": "mathura", "after": "nowhere", "before": None,
                                                "position": None}, 1)],
                            [_call("remove_stop", {"place_id": "agra"}, 2),
                             _call("add_stop", {"place_id": "mathura", "after": "delhi", "before": None,
                                                "position": None}, 3)]])
    r = common.run_one(B, EX, NANO)
    assert r["pass"] and r["steps"] == 2


def test_arm_c_one_call_atomic(monkeypatch):
    sent = _fake_llm(monkeypatch, [[_call("apply_edits", {"ops": [_op("remove", place_id="agra"),
                                                                   _op("add", place_id="mathura", after="delhi")]}, 1)]])
    r = common.run_one(C, EX, NANO)
    assert r["pass"] and r["steps"] == 1 and r["reply"] == "Removed Agra. Added Mathura after Delhi."
    assert exp02.edit_ops(r) == ["rm agra", "add mathura after delhi"]


def test_arm_c_bad_op_changes_nothing():
    common.seed("batch1", EX, NANO)
    with common.applied(C):
        r = variants.apply_edits("batch1", [_op("remove", place_id="agra"), _op("add", place_id="mathura",
                                                                                 after="agra")])
    assert not r["ok"] and "op 2" in r["error"] and state.stop_ids("batch1") == ["delhi", "agra", "jaipur"]


def test_arm_c_undo_then_add_and_history():
    ex = {**EX, "start": {"stops": ["delhi", "agra"], "history": [["delhi", "jaipur"]], "pending_question": None}}
    common.seed("batch2", ex, NANO)
    r = variants.apply_edits("batch2", [_op("undo", count=1), _op("add", place_id="ajmer", position="end")])
    assert r["ok"] and state.stop_ids("batch2") == ["delhi", "jaipur", "ajmer"]
    state.undo("batch2")
    assert state.stop_ids("batch2") == ["delhi", "jaipur"]


def test_validator_is_restored_after_arm_c():
    from app import tools
    before = tools._validate
    with common.applied(C):
        assert tools._validate is variants.validate and "apply_edits" in tools.TOOLS
    assert tools._validate is before and "apply_edits" not in tools.TOOLS

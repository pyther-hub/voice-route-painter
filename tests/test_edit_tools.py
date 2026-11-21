"""Prompt v20 tools: resolve_place statuses, small edit tools, and the agent loop rules (fake LLM, no paid calls)."""
import json
from types import SimpleNamespace

from app import agent, llm, state, tools


# ---------------------------------------------------------------- resolve_place statuses

def _status(q):
    r = tools.lookup(q)
    return r["status"], [m["id"] for m in r["matches"]]


def test_resolve_statuses():
    assert _status("Bombay") == ("ok", ["mumbai"])                 # alias
    assert _status("Banglore") == ("ok", ["bengaluru"])            # typo, clear winner
    assert _status("Dubai") == ("not_found", [])                   # abroad = not in the list
    assert _status("Pokhara") == ("not_found", [])                 # abroad, though it spells close to Pokaran
    assert _status("Xyzabc") == ("not_found", [])
    status, ids = _status("Himachal")                              # a state, not one place
    assert status == "ambiguous" and "shimla" in ids and 2 <= len(ids) <= 4
    status, ids = _status("Rampur")                                # several close spelling guesses
    assert status == "ambiguous" and len(ids) >= 2
    assert _status("Goa") == ("ok", ["panaji"])                    # an exact alias beats the state name


def test_resolve_with_state_label():
    assert _status("Aurangabad, Maharashtra") == ("ok", ["aurangabad"])
    assert _status("Shimla (Himachal Pradesh)") == ("ok", ["shimla"])
    assert _status("Aurangabad, Bihar") == ("ok", ["aurangabad-bihar"])
    status, ids = _status("Aurangabad")                            # two towns of that name: the agent must ask
    assert status == "ambiguous" and set(ids) == {"aurangabad", "aurangabad-bihar"}
    status, ids = _status("Bilaspur")
    assert status == "ambiguous" and set(ids) == {"bilaspur", "bilaspur-himachal-pradesh"}
    assert tools.lookup("Pune")["matches"] == [{"id": "pune", "name": "Pune, Maharashtra"}]


# ---------------------------------------------------------------- edit tools

def _trip(sid, ids):
    state.drop_session(sid)
    state.update_state(sid, ids)
    return sid


def test_add_stop_after_before_end():
    sid = _trip("add1", ["mumbai", "bengaluru"])
    r = tools.add_stop(sid, "panaji", "mumbai", None, None)
    assert r["ok"] and r["edit"] and r["line"] == "Added Panaji after Mumbai."
    assert state.stop_ids(sid) == ["mumbai", "panaji", "bengaluru"] and state.session(sid).last_mentioned == "panaji"
    tools.add_stop(sid, "pune", None, "panaji", None)
    tools.add_stop(sid, "mysuru", None, None, "end")
    assert state.stop_ids(sid) == ["mumbai", "pune", "panaji", "bengaluru", "mysuru"]


def test_add_stop_errors():
    sid = _trip("add2", ["mumbai", "panaji"])
    bad = [("pune", "mumbai", "panaji", None),        # two anchors
           ("pune", None, None, None),             # no anchor
           ("pune", None, None, "start"),          # unknown position
           ("pune", "delhi", None, None),          # anchor not on the route
           ("panaji", "mumbai", None, None),       # already on the route
           ("atlantis", "mumbai", None, None)]     # unknown id
    for args in bad:
        r = tools.add_stop(sid, *args)
        assert not r["ok"] and r["hint"], args
    assert state.stop_ids(sid) == ["mumbai", "panaji"]
    assert not tools.add_stop(_trip("add3", []), "pune", None, None, "end")["ok"]     # empty route: use set_route


def test_remove_stop_by_id():
    sid = _trip("rmid", ["delhi", "agra", "jaipur"])
    assert tools.remove_stop(sid, "agra")["line"] == "Removed Agra."
    assert state.stop_ids(sid) == ["delhi", "jaipur"]
    assert not tools.remove_stop(sid, "agra")["ok"]                # not on the route
    assert not tools.remove_stop(sid, "delhi")["ok"]               # would leave one stop
    loop = _trip("rmloop", ["jammu", "delhi", "kolkata", "jammu"])
    assert tools.remove_stop(loop, "jammu")["ok"] and state.stop_ids(loop) == ["delhi", "kolkata"]


def test_set_route_clear_and_undo_count():
    sid = _trip("undo1", ["delhi", "agra"])
    state.update_state(sid, ["delhi", "agra", "jaipur"])
    assert tools.set_route(sid, [])["line"] == "Cleared the trip."
    assert not tools.undo(sid, 5)["ok"]                            # history is shorter
    assert tools.undo(sid, 2)["line"] == "Undid 2 changes."
    assert state.stop_ids(sid) == ["delhi", "agra"]
    assert not tools.undo(sid, 0)["ok"]


# ---------------------------------------------------------------- agent loop (fake LLM)

def _call(name, args, n):
    return SimpleNamespace(type="function_call", name=name, arguments=json.dumps(args), call_id=f"c{n}",
                           model_dump=lambda **kw: {"type": "function_call", "name": name,
                                                    "arguments": json.dumps(args), "call_id": f"c{n}"})


def _fake_llm(monkeypatch, steps):
    """Each LLM step returns the next list of tool calls; records what the model was sent."""
    sent, it = [], iter(steps)

    def create(**kw):
        sent.append(list(kw["input"]))      # a copy: the loop keeps appending
        calls = next(it)
        usage = SimpleNamespace(total_tokens=10, input_tokens=8, output_tokens=2, input_tokens_details=None)
        return SimpleNamespace(output=calls, output_text="", usage=usage)
    monkeypatch.setattr(llm._openai.responses, "create", create)
    return sent


def test_replace_in_one_step_is_one_undo_step_and_code_writes_the_line(monkeypatch):
    sid = _trip("loop-replace", ["mumbai", "panaji", "bengaluru"])
    sent = _fake_llm(monkeypatch, [
        [_call("resolve_place", {"queries": ["Kochi"]}, 1)],
        [_call("remove_stop", {"place_id": "panaji"}, 2),
         _call("add_stop", {"place_id": "kochi", "after": None, "before": "bengaluru", "position": None}, 3)],
        [],                                                         # done: no tool call
    ])
    reply = agent.run_turn(sid, "Panaji ki jagah Kochi kar do")
    assert reply == "Removed Panaji. Added Kochi before Bengaluru."
    assert state.stop_ids(sid) == ["mumbai", "kochi", "bengaluru"]
    assert len(sent) == 3
    turn = json.loads(sent[0][-1]["content"])                       # the JSON input the prompt describes
    assert turn["user_input"] == {"type": "text", "value": "Panaji ki jagah Kochi kar do"}
    assert [s["id"] for s in turn["state"]["stops"]] == ["mumbai", "panaji", "bengaluru"]
    state.undo(sid)                                                 # both edits come back in one undo
    assert state.stop_ids(sid) == ["mumbai", "panaji", "bengaluru"]


def test_edits_over_several_steps_all_run(monkeypatch):
    """Session 96841b92a489: "Banaras ki jagah Patna" removed Varanasi and the turn ended before the add."""
    sid = _trip("loop-steps", ["delhi", "agra", "varanasi"])
    sent = _fake_llm(monkeypatch, [
        [_call("resolve_place", {"queries": ["Patna"]}, 1)],
        [_call("remove_stop", {"place_id": "varanasi"}, 2)],
        [_call("add_stop", {"place_id": "patna", "after": None, "before": None, "position": "end"}, 3)],
        [],
    ])
    assert agent.run_turn(sid, "Banaras ki jagah Patna kar do") == "Removed Varanasi. Added Patna at the end."
    assert state.stop_ids(sid) == ["delhi", "agra", "patna"] and len(sent) == 4
    state.undo(sid)                                                 # the whole turn is one undo step
    assert state.stop_ids(sid) == ["delhi", "agra", "varanasi"]


def test_failed_call_in_the_step_lets_the_agent_fix_it(monkeypatch):
    sid = _trip("loop-fix", ["delhi", "agra"])
    sent = _fake_llm(monkeypatch, [
        [_call("add_stop", {"place_id": "jaipur", "after": "nowhere", "before": None, "position": None}, 1)],
        [_call("add_stop", {"place_id": "jaipur", "after": "agra", "before": None, "position": None}, 2)],
        [],
    ])
    assert agent.run_turn(sid, "Agra ke baad Jaipur") == "Added Jaipur after Agra."
    assert len(sent) == 3 and state.stop_ids(sid) == ["delhi", "agra", "jaipur"]


def test_question_then_choice(monkeypatch):
    sid = _trip("loop-ask", ["delhi", "agra"])
    _fake_llm(monkeypatch, [[_call("ask_user", {"question": "Himachal me kahan?", "options": ["Shimla", "Manali"]}, 1)]])
    assert agent.run_turn(sid, "Himachal wala bhi daalo") == "Himachal me kahan?"
    assert state.session(sid).pending_question["options"][0] == {"label": "Shimla", "id": "shimla"}

    sent = _fake_llm(monkeypatch, [[_call("add_stop", {"place_id": "shimla", "after": None, "before": None,
                                                       "position": "end"}, 2)], []])
    agent.run_turn(sid, "Shimla", "choice")
    turn = json.loads(sent[0][-1]["content"])
    assert turn["user_input"] == {"type": "choice", "value": "Shimla"}
    assert turn["state"]["pending_question"]["question"] == "Himachal me kahan?"
    assert state.session(sid).pending_question is None              # answered
    assert state.stop_ids(sid)[-1] == "shimla" and state.session(sid).last_mentioned == "shimla"


def test_reply_changes_nothing_and_last_mentioned_follows_the_text(monkeypatch):
    sid = _trip("loop-reply", ["delhi", "agra", "jaipur"])
    _fake_llm(monkeypatch, [[_call("reply", {"message": "Sirf India ke stops add ho sakte hain."}, 1)]])
    assert agent.run_turn(sid, "Agra ke baad Dubai daalo") == "Sirf India ke stops add ho sakte hain."
    assert state.stop_ids(sid) == ["delhi", "agra", "jaipur"]
    assert state.session(sid).last_mentioned == "agra"              # the stop the user named


# ---------------------------------------------------------------- detected_places (prompt v25)

def test_detected_places_in_the_input(monkeypatch):
    sid = _trip("detect1", ["delhi", "agra"])
    found = agent.detected_places(sid, "Agra ke baad Pune aur Bilaspur daalo, Dubai bhi")
    by_text = {f["text"]: f for f in found}
    assert "Agra" not in by_text                                   # already a stop: the model has its id
    assert by_text["Pune"]["status"] == "ok" and by_text["Pune"]["matches"][0]["id"] == "pune"
    assert by_text["Bilaspur"]["status"] == "ambiguous" and len(by_text["Bilaspur"]["matches"]) >= 2
    assert "Dubai" not in by_text                                  # abroad: nothing to hint
    assert agent.detected_places(sid, "Pune, Pune") == [f for f in found if f["text"] == "Pune"]   # once per name

    sent = _fake_llm(monkeypatch, [[]])
    agent.run_turn(sid, "Pune daalo")
    turn = json.loads(sent[0][-1]["content"])
    assert turn["detected_places"][0]["matches"] == [{"id": "pune", "name": "Pune, Maharashtra"}]

    sent = _fake_llm(monkeypatch, [[]])
    agent.run_turn(sid, "Pune", "choice")                           # a click carries its ids already
    assert "detected_places" not in json.loads(sent[0][-1]["content"])
    sent = _fake_llm(monkeypatch, [[]])
    agent.run_turn(sid, "kuch bhi nahi")
    assert "detected_places" not in json.loads(sent[0][-1]["content"])

import json

from app import state, tools, trace


def _ask(sid, question, options):
    return tools.run_tool(sid, "ask_user", json.dumps({"question": question, "options": options}))


def test_ask_user_sets_question_card_and_pending_question():
    r = _ask("ask1", "Himachal me kahan?", ["Shimla", "Manali", "Not a place"])
    assert r["ok"] and r["ends_turn"] and r["reply"] == "Himachal me kahan?"
    assert "Shimla; Manali; Not a place" in r["memory"]          # the agent sees the options next turn
    q = state.ui("ask1")["question"]
    assert q["text"] == "Himachal me kahan?" and q["options"] == ["Shimla", "Manali", "Not a place"]
    assert [x and x["name"] for x in q["spots"]] == ["Shimla", "Manali", None]
    pending = state.session("ask1").pending_question
    assert pending["options"] == [{"label": "Shimla", "id": "shimla"}, {"label": "Manali", "id": "manali"},
                                  {"label": "Not a place", "id": None}]


def test_ask_user_option_with_state_names_one_place():
    _ask("ask5", "Kaunsa?", ["Aurangabad, Maharashtra", "Coimbatore (Tamil Nadu)"])
    assert [o["id"] for o in state.session("ask5").pending_question["options"]] == ["aurangabad", "coimbatore"]


def test_ask_user_rejects_bad_options_and_keeps_state():
    for options in [[], ["only one"], ["a"] * 2, [str(i) for i in range(5)], ["x" * 41, "y"], ["", "b"]]:
        r = _ask("ask3", "Pick one", options)
        assert not r["ok"] and r["hint"], options
    assert state.ui("ask3")["question"] is None and state.session("ask3").pending_question is None


def test_read_trace(tmp_path, monkeypatch):
    monkeypatch.setattr(trace, "DB_PATH", tmp_path / "t.db")
    ids = []
    for text in ["Delhi to Agra", "add Jaipur"]:
        with trace.span("agent", "turn", input={"text": text, "session": "sess1"}) as root:
            with trace.span("tool", "resolve_place", input='{"name": "Agra"}') as sp:
                sp["output"] = {"ok": True}
            root["output"] = {"reply": f"done: {text}"}
            ids.append(root["trace_id"])
    with trace.span("agent", "turn", input={"text": "other", "session": "sess2"}):
        pass

    data = trace.read_trace(ids[0])
    assert [s["name"] for s in data["spans"]] == ["turn", "resolve_place"]
    assert data["spans"][1]["parent"] == data["spans"][0]["span_id"]
    assert data["spans"][1]["output"] == {"ok": True}                   # JSON parsed back
    assert [t["trace_id"] for t in data["turns"]] == ids[::-1]          # same session only, newest first
    assert data["turns"][0]["reply"] == "done: add Jaipur"
    assert trace.read_trace("000000000000") is None
    assert trace.read_trace("../etc") is None


def test_long_drive_card_add_is_inserted_and_not_offered_again():
    state.session("pick3").current_text = "Varanasi se Mumbai via Jaipur"
    tools.set_route("pick3", ["varanasi", "jaipur", "mumbai"])
    sugg = state.ui("pick3")["suggestions"]
    assert 1 <= len(sugg) <= tools.MAX_SUGGESTIONS
    reply = tools.add_suggestion("pick3", 0)
    ids = state.stop_ids("pick3")
    assert reply and ids[0] == "varanasi" and ids[-1] == "mumbai" and len(ids) == 4 and sugg[0]["id"] in ids
    left = state.ui("pick3")["suggestions"]
    # the leg it went into is broken: its cards are gone; any new leg over 12 h gets fresh cards
    assert sugg[0]["id"] not in [x["id"] for x in left]
    assert all(tuple(x["between"]) in set(zip(ids, ids[1:])) for x in left)
    assert tools.add_suggestion("pick3", 99) is None and tools.add_suggestion("pick3", True) is None


def test_skip_removes_the_card_and_it_never_comes_back():
    tools.set_route("skip1", ["jaipur", "kochi"])
    sugg = state.ui("skip1")["suggestions"]
    tools.dismiss_suggestion("skip1", 0)
    left = state.ui("skip1")["suggestions"]
    assert len(left) == len(sugg) - 1 and sugg[0]["id"] in state.session("skip1").dismissed
    tools.dismiss_suggestion("skip1", 99)                      # a bad index does nothing
    assert state.ui("skip1")["suggestions"] == left


def test_remove_at_without_agent():
    state.update_state("rm1", ["delhi", "agra", "jaipur"])
    assert tools.remove_at("rm1", 1) == "Removed Agra."
    assert state.stop_ids("rm1") == ["delhi", "jaipur"] and state.ui("rm1")["message"] == "Removed Agra."
    assert tools.remove_at("rm1", 0) is None                  # would leave one stop
    assert tools.remove_at("rm1", 5) is None and tools.remove_at("rm1", "agra") is None
    assert state.undo("rm1")["trip"]["stops"][1]["id"] == "agra"        # undo still works


def test_followups_set_chips_and_drop_stale(monkeypatch):
    from types import SimpleNamespace
    from app import agent
    sid = "follow1"
    tools.set_route(sid, ["delhi", "jaipur"])
    fake = SimpleNamespace(output_text="Add Agra\n- Make it a round trip\n", output=[], usage=SimpleNamespace(total_tokens=50))
    monkeypatch.setattr(agent.llm._openai.responses, "create", lambda **kw: fake)
    agent.followups(sid, "Delhi se Jaipur", 0)
    assert state.ui(sid)["followups"] == ["Add Agra", "Make it a round trip"] and state.ui(sid)["tokens"] == 50
    state.session(sid).input_no = 1                                   # a newer message arrived: late chips are dropped
    state.set_display(sid, followups=[])
    agent.followups(sid, "x", 0)
    assert state.ui(sid)["followups"] == []


def test_round_trip():
    s = state.update_state("loop1", ["jammu", "delhi", "kolkata", "jammu"])["trip"]
    assert len(s["legs"]) == 3 and s["stops"][-1]["id"] == "jammu"
    for bad in (["jammu", "jammu"], ["jammu", "delhi", "jammu", "delhi"], ["delhi", "jammu", "jammu", "agra"]):
        try:
            state.update_state("loop1", bad)
            assert False, bad
        except ValueError:
            pass
    assert tools.remove_at("loop1", 3) == "Removed Jammu."    # stop going back: the last Jammu only
    assert state.stop_ids("loop1") == ["jammu", "delhi", "kolkata"]
    state.update_state("loop2", ["jammu", "delhi", "jammu"])
    assert tools.remove_at("loop2", 1) is None                # would leave Jammu → Jammu
    # the agent can close a loop by adding the start at the end
    assert tools.add_stop("loop1", "jammu", None, None, "end")["ok"]
    assert state.stop_ids("loop1") == ["jammu", "delhi", "kolkata", "jammu"]

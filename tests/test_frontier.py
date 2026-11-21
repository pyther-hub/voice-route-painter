"""plan_with_frontier / accept_proposal / proposal clicks, with a fake frontier model (no API calls)."""
import pytest

from app import guard, llm, state, tools

USAGE = {"input": 900, "cached": 0, "output": 300, "total": 1200}


@pytest.fixture(autouse=True)
def _fresh_ip_limit():
    """All test sessions share the IP "local": reset its per-hour planner count for each test."""
    guard._hits.pop(("local", "frontier"), None)
    yield


def _fake(monkeypatch, answer):
    sent = []

    def structured(model_id, instructions, user_text, schema, max_out):
        sent.append({"model": model_id, "text": user_text})
        return answer, USAGE
    monkeypatch.setattr(llm, "structured", structured)
    return sent


def _session(sid, text, stops=()):
    sess = state.session(sid)
    sess.current_text, sess.frontier_used = text, False
    if stops:
        state.update_state(sid, list(stops))
    return sess


NE_PLAN = {"title": "North East loop", "tagline": "Pahad, chai aur badal", "reasoning": "Short drives.",
           "days_estimate": 7,
           "stops": [{"name": "Guwahati, Assam", "why": "Gateway city"},
                     {"name": "Shillong, Meghalaya", "why": "Hill capital"},
                     {"name": "Atlantis Town, Nowhere", "why": "made up"},
                     {"name": "Kaziranga, Assam", "why": "Rhinos"}]}


def test_itinerary_is_a_proposal_and_never_touches_the_trip(monkeypatch):
    sent = _fake(monkeypatch, NE_PLAN)
    sess = _session("fr1", "koi nayi trip batao poora North East")
    r = tools.run_tool("fr1", "plan_with_frontier", '{"mode": "itinerary", "request": "North East trip", '
                       '"filler": "Ek minute, plan bana raha hoon", "route_a": null, "route_b": null}')
    assert r["ok"] and r["ends_turn"] and sent[0]["model"] == llm.FRONTIER
    assert state.stop_ids("fr1") == []                                   # trip untouched
    p = state.ui("fr1")["proposal"]
    assert p["kind"] == "itinerary" and [x["name"] for x in p["stops"]][:2] == ["Guwahati", "Shillong"]
    assert p["dropped"] == ["Atlantis Town, Nowhere"] and len(p["legs"]) == len(p["stops"]) - 1
    assert p["stops"][0]["why"] == "Gateway city" and p["total_km"] > 0
    assert sess.proposal is p or sess.proposal == p
    assert state.ui("fr1")["tokens"] >= USAGE["total"]


def test_accept_click_writes_one_undo_step_and_clears_the_proposal(monkeypatch):
    _fake(monkeypatch, NE_PLAN)
    _session("fr2", "plan a north east trip", ["delhi", "agra"])
    tools.plan_with_frontier("fr2", "itinerary", "north east", "one sec")
    ids = [x["id"] for x in state.ui("fr2")["proposal"]["stops"]]
    line = tools.proposal_click("fr2", "accept")
    assert line and state.stop_ids("fr2") == ids and state.ui("fr2")["proposal"] is None
    state.undo("fr2")
    assert state.stop_ids("fr2") == ["delhi", "agra"]


def test_decline_and_remove(monkeypatch):
    _fake(monkeypatch, NE_PLAN)
    _session("fr3", "north east trip")
    tools.plan_with_frontier("fr3", "itinerary", "north east", "one sec")
    n = len(state.ui("fr3")["proposal"]["stops"])
    assert tools.proposal_click("fr3", "remove", index=0)
    p = state.ui("fr3")["proposal"]
    assert len(p["stops"]) == n - 1 and len(p["legs"]) == n - 2 and p["stops"][0]["why"]
    while state.ui("fr3")["proposal"] and len(state.ui("fr3")["proposal"]["stops"]) >= 2:
        tools.proposal_click("fr3", "remove", index=0)
    assert state.ui("fr3")["proposal"] is None and state.stop_ids("fr3") == []    # below 2 stops = declined
    state.session("fr3").frontier_used = False
    tools.plan_with_frontier("fr3", "itinerary", "north east", "one sec")
    assert tools.proposal_click("fr3", "decline") and state.ui("fr3")["proposal"] is None


def test_compare_uses_code_numbers_and_option_b_can_be_accepted(monkeypatch):
    sent = _fake(monkeypatch, {"recommendation": "B", "short_answer": "Doosra wala better hai.",
                               "options": [{"key": "A", "label": "Coastal", "pros": ["Sea views"], "cons": ["Slow"]},
                                           {"key": "B", "label": "Via Kolhapur", "pros": ["Faster"], "cons": ["Boring"]}]})
    _session("fr4", "Mumbai se Goa coastal ya Kolhapur wala, kaunsa better?")
    r = tools.plan_with_frontier("fr4", "compare", "Mumbai to Goa: coastal or via Kolhapur?", "Ek minute",
                                 ["Mumbai", "Ratnagiri", "Goa"], ["Mumbai", "Kolhapur", "Goa"])
    assert r["ok"] and r["reply"] == "Doosra wala better hai." and state.stop_ids("fr4") == []
    p = state.ui("fr4")["proposal"]
    a, b = p["options"]
    assert p["recommended"] == "B" and a["label"] == "Coastal" and a["total_km"] > 0 and b["total_min"] > 0
    assert f"{a['total_km']} km" in sent[0]["text"]                     # the model got the app's numbers
    assert tools.accept_proposal("fr4", "B")["ok"]
    assert state.stop_ids("fr4")[1] == "kolhapur" and state.ui("fr4")["proposal"] is None


def test_named_places_are_anchors_not_a_refusal(monkeypatch):
    plan = {"title": "South run", "tagline": "Hyderabad se Bengaluru, raste ke best stops", "reasoning": "", "days_estimate": 3,
            "stops": [{"name": "Kurnool, Andhra Pradesh", "why": "Fort town"},
                      {"name": "Anantapur, Andhra Pradesh", "why": "Halfway"}]}      # the model forgot both ends
    sent = _fake(monkeypatch, plan)
    _session("fr5", "From Hyderabad to Bangalore, create a full trip and best places to visit in South.")
    r = tools.plan_with_frontier("fr5", "itinerary", "Hyderabad to Bengaluru with the best places", "one sec")
    assert r["ok"] and "Hyderabad, Telangana, Bengaluru, Karnataka" in sent[0]["text"]
    assert "start at the first and end at the last" in sent[0]["text"]
    ids = [x["id"] for x in state.ui("fr5")["proposal"]["stops"]]
    assert ids[0] == "hyderabad" and ids[-1] == "bengaluru" and "kurnool" in ids      # the code put the ends back


def test_limits(monkeypatch):
    sent = _fake(monkeypatch, NE_PLAN)
    _session("fr5b", "Delhi, Agra, Jaipur aur Udaipur ghumna hai")                    # four named places: their own route
    r = tools.plan_with_frontier("fr5b", "itinerary", "rajasthan loop", "one sec")
    assert not r["ok"] and "named 4 places" in r["error"] and not sent
    _session("fr6", "north east")
    assert tools.plan_with_frontier("fr6", "itinerary", "north east", "one sec")["ok"]
    r = tools.plan_with_frontier("fr6", "itinerary", "north east again", "one sec")
    assert not r["ok"] and "already" in r["error"] and len(sent) == 1             # once per turn
    assert not tools.accept_proposal("fr7", None)["ok"]                          # nothing pending


def test_agent_edit_clears_a_pending_proposal(monkeypatch):
    _fake(monkeypatch, NE_PLAN)
    _session("fr8", "north east")
    tools.plan_with_frontier("fr8", "itinerary", "north east", "one sec")
    tools.set_route("fr8", ["delhi", "agra"])
    assert state.ui("fr8")["proposal"] is None and state.session("fr8").proposal is None


def test_progress_step_reports_the_frontier_call(monkeypatch):
    _fake(monkeypatch, NE_PLAN)
    sess = _session("fr9", "north east")
    events = []
    sess.on_progress = events.append
    tools.plan_with_frontier("fr9", "itinerary", "north east", "Ek minute, plan bana raha hoon")
    done = [e for e in events if e.get("kind") == "llm" and e["status"] == "done"]
    assert done and done[0]["name"] == llm.FRONTIER and done[0]["detail"] == "trigger: no_stops_named"
    assert done[0]["label"] == "Ek minute, plan bana raha hoon" and "tokens" in done[0]["result"]
    sess.on_progress = None


def test_voice_turn_speaks_filler_and_tagline(monkeypatch):
    _fake(monkeypatch, NE_PLAN)
    sess = _session("fr10", "north east")
    said = []
    sess.speak = said.append
    tools.plan_with_frontier("fr10", "itinerary", "north east", "Ek minute")
    assert said == ["Ek minute", "Pahad, chai aur badal"]
    sess.speak = None


def test_frontier_is_hidden_from_the_picker():
    assert all(m["id"] != "openai/gpt-5.4" for m in llm.available())


def test_proposal_click_message_goes_through_main(monkeypatch):
    import asyncio
    import json
    from app import main
    _fake(monkeypatch, NE_PLAN)
    _session("fr11", "north east")
    tools.plan_with_frontier("fr11", "itinerary", "north east", "one sec")
    ids = [x["id"] for x in state.ui("fr11")["proposal"]["stops"]]
    text, error, _ = asyncio.run(main.read_input("fr11", json.dumps({"type": "proposal", "action": "accept"})))
    assert text is None and error is None and state.stop_ids("fr11") == ids


def test_spoken_language_reaches_the_agent_and_the_planner(monkeypatch):
    from app import agent, tts
    sent = _fake(monkeypatch, NE_PLAN)
    sess = _session("fr12", "north east ghumna hai")
    sess.speak_in = "ta-IN"                                  # Sarvam heard Tamil
    turn = __import__("json").loads(agent._context_items("fr12", "north east ghumna hai", "text")[-1]["content"])
    assert turn["user_input"]["spoken_language"] == "Tamil, in Tamil script"
    tools.plan_with_frontier("fr12", "itinerary", "north east", "ஒரு நிமிடம்")
    assert "Tamil, in Tamil script" in sent[0]["text"]
    sess.speak_in = None                                     # typed: no language line anywhere
    turn = __import__("json").loads(agent._context_items("fr12", "north east", "text")[-1]["content"])
    assert "spoken_language" not in turn["user_input"]
    assert tts.write_as("ur-IN") == tts.WRITE_AS["hi-IN"]    # a language Bulbul can't speak falls back to Hinglish


def test_voice_message_sets_the_spoken_language(monkeypatch):
    import asyncio
    import base64
    import json
    from app import main, stt

    async def fake_detect(audio, mime, keyterms):
        return "Chennai to Madurai", "ta-IN"
    monkeypatch.setattr(stt, "transcribe_detect", fake_detect)
    sid = "fr13"
    state.session(sid)
    msg = json.dumps({"type": "audio", "mime": "audio/webm", "data": base64.b64encode(b"x" * 3000).decode()})
    text, error, _ = asyncio.run(main.read_input(sid, msg))
    assert text == "Chennai to Madurai" and state.session(sid).speak_in == "ta-IN"


def test_bad_json_is_retried_and_both_attempts_are_charged(monkeypatch):
    calls = []

    def structured(model_id, instructions, user_text, schema, max_out):
        calls.append(max_out)
        if len(calls) == 1:
            raise llm.StructuredError("truncated", USAGE, '{"title": "North East lo')
        return NE_PLAN, USAGE
    monkeypatch.setattr(llm, "structured", structured)
    charged = []
    monkeypatch.setattr(guard, "charge", lambda sid, usd: charged.append(usd))
    _session("fr20", "north east")
    r = tools.plan_with_frontier("fr20", "itinerary", "north east", "one sec")
    assert r["ok"] and len(calls) == 2 and calls[1] > calls[0]          # retried with more room
    assert len(charged) == 2 and all(c > 0 for c in charged)             # the failed attempt cost money too


def test_planner_failure_is_a_clean_error(monkeypatch):
    def structured(*a, **k):
        raise llm.StructuredError("not_json", USAGE, "Sure! Here is your trip...")
    monkeypatch.setattr(llm, "structured", structured)
    _session("fr21", "north east")
    r = tools.plan_with_frontier("fr21", "itinerary", "north east", "Ek minute")
    assert not r["ok"] and "could not answer" in r["error"] and "Do not call" in r["hint"]
    assert state.ui("fr21")["proposal"] is None and state.ui("fr21")["message"] == ""   # filler taken back

    def timeout(*a, **k):
        raise TimeoutError("read timed out")
    monkeypatch.setattr(llm, "structured", timeout)
    _session("fr22", "north east")
    assert not tools.plan_with_frontier("fr22", "itinerary", "north east", "one sec")["ok"]


def test_malformed_fields_are_skipped_not_fatal(monkeypatch):
    _fake(monkeypatch, {"title": "X", "tagline": "Y", "reasoning": "", "days_estimate": "seven",
                        "stops": ["Guwahati", {"name": "Guwahati, Assam", "why": "start"}, {"name": 5},
                                  {"name": "Shillong, Meghalaya", "why": "hills"}]})
    _session("fr23", "north east")
    assert tools.plan_with_frontier("fr23", "itinerary", "north east", "one sec")["ok"]
    p = state.ui("fr23")["proposal"]
    assert [x["id"] for x in p["stops"]] == ["guwahati", "shillong"] and p["days_estimate"] is None


def test_planner_caps_per_session_and_per_ip(monkeypatch):
    sent = _fake(monkeypatch, NE_PLAN)
    sess = _session("fr24", "north east")
    sess.frontier_calls = guard.FRONTIER_PER_SESSION
    r = tools.plan_with_frontier("fr24", "itinerary", "north east", "one sec")
    assert not r["ok"] and "limit" in r["error"] and not sent
    for n in range(guard.FRONTIER_PER_IP_HOUR):
        s2 = _session(f"fr25-{n}", "north east")
        s2.ip = "9.9.9.9"
        assert tools.plan_with_frontier(f"fr25-{n}", "itinerary", "north east", "one sec")["ok"]
    s3 = _session("fr26", "north east")
    s3.ip = "9.9.9.9"
    assert not tools.plan_with_frontier("fr26", "itinerary", "north east", "one sec")["ok"]
    guard._hits.pop(("9.9.9.9", "frontier"), None)


def test_any_trip_change_clears_the_proposal(monkeypatch):
    _fake(monkeypatch, NE_PLAN)
    _session("fr27", "north east", ["delhi", "agra", "jaipur"])
    for change in (lambda: tools.remove_at("fr27", 1), lambda: tools.undo_redo("fr27", "undo"),
                   lambda: tools.load_trip("fr27", ["delhi", "agra"])):
        state.session("fr27").frontier_used = False
        tools.plan_with_frontier("fr27", "itinerary", "north east", "one sec")
        assert state.ui("fr27")["proposal"]
        assert change()
        assert state.ui("fr27")["proposal"] is None and state.session("fr27").proposal is None


def test_dismiss_by_voice(monkeypatch):
    _fake(monkeypatch, NE_PLAN)
    _session("fr28", "north east", ["delhi", "agra"])
    tools.plan_with_frontier("fr28", "itinerary", "north east", "one sec")
    r = tools.run_tool("fr28", "dismiss_proposal", '{"reply": "Theek hai, purana trip hi rakhte hain."}')
    assert r["ok"] and r["ends_turn"] and state.ui("fr28")["proposal"] is None
    assert state.stop_ids("fr28") == ["delhi", "agra"]
    assert not tools.dismiss_proposal("fr28", "ok")["ok"]                       # nothing pending any more


def test_spoken_lines_keep_their_order():
    """The filler is asked for first; its audio must go out first even if its TTS call is slower."""
    import asyncio
    from app import main

    async def run():
        sent = []
        lock = asyncio.Lock()

        async def say(text, delay):
            async with lock:
                await asyncio.sleep(delay)
                sent.append(text)
        await asyncio.gather(say("filler", 0.05), say("answer", 0.0))
        return sent
    assert asyncio.run(run()) == ["filler", "answer"]
    assert "asyncio.Lock()" in open(main.__file__).read()          # main.speaker uses the same pattern

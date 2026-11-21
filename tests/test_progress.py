import json

from app import state, tools


def _collect(sid):
    events = []
    state.session(sid).on_progress = events.append
    return events


def test_tool_emits_running_then_done_with_label_and_pin():
    sid = "prog1"
    events = _collect(sid)
    result = tools.run_tool(sid, "resolve_place", json.dumps({"queries": ["Agra"]}))
    assert result["ok"]
    assert [e["status"] for e in events] == ["running", "done"]
    assert events[0]["id"] == events[1]["id"]
    assert events[0]["label"] == "Looking up Agra"
    assert events[0]["pin"] is None and events[1]["pin"]["name"] == "Agra"
    assert all(e["type"] == "progress" and e["event"] == "step" for e in events)


def test_resolve_place_takes_many_names_in_one_call():
    r = tools.run_tool("multi1", "resolve_place", json.dumps({"queries": ["Agra", "Xyzabc", "Jaipur, Rajasthan"]}))
    assert r["ok"] and [x["query"] for x in r["results"]] == ["Agra", "Xyzabc", "Jaipur, Rajasthan"]
    assert [x["status"] for x in r["results"]] == ["ok", "not_found", "ok"]
    assert r["results"][0]["matches"] == [{"id": "agra", "name": "Agra, Uttar Pradesh"}]
    assert not tools.run_tool("multi1", "resolve_place", json.dumps({"queries": []}))["ok"]


def test_failed_tool_emits_error():
    sid = "prog2"
    events = _collect(sid)
    result = tools.run_tool(sid, "remove_stop", json.dumps({"place_id": "agra"}))     # not on the (empty) route
    assert not result["ok"]
    assert [e["status"] for e in events] == ["running", "error"]
    assert events[1]["pin"] is None


def test_set_route_label():
    sid = "prog3"
    events = _collect(sid)
    tools.run_tool(sid, "set_route", json.dumps({"stops": ["delhi", "agra"]}))
    assert events[0]["label"] == "Drawing the route" and events[-1]["status"] == "done"


def test_no_listener_and_broken_listener_do_not_break_tools():
    tools.run_tool("prog4", "resolve_place", json.dumps({"queries": ["Agra"]}))                    # no listener: fine

    def boom(event):
        raise RuntimeError("socket closed")
    state.session("prog5").on_progress = boom
    assert tools.run_tool("prog5", "resolve_place", json.dumps({"queries": ["Agra"]}))["ok"]       # listener raises: tool still works

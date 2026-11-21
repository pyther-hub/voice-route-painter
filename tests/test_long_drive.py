"""Road estimates, stops on the way, and the once-per-trip offer to split a day-long drive."""
import re

from fastapi.testclient import TestClient

from app import geo, places, state, tools


def test_road_distance_follows_roads():
    s = state.update_state("road1", ["jaipur", "kochi"])["trip"]
    a, b = places.get("jaipur"), places.get("kochi")
    straight = state.haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
    leg = s["legs"][0]
    assert straight < leg["km"] < 1.5 * straight and 2200 < leg["km"] < 2600     # Google: ~2,400 km
    assert leg["car_min"] == round(leg["km"] / state.CAR_KMH * 60)
    path = leg["path"]                                     # the road's shape, starting and ending at the stops
    assert 10 < len(path) <= geo.PATH_MAX_POINTS + 1
    assert path[0] == [round(a["lat"], 4), round(a["lon"], 4)] and path[-1] == [round(b["lat"], 4), round(b["lon"], 4)]


def test_island_falls_back_to_straight_line():
    s = state.update_state("road2", ["chennai", "port-blair"])["trip"]
    a, b = places.get("chennai"), places.get("port-blair")
    assert "path" not in s["legs"][0]
    assert s["legs"][0]["km"] == round(state.haversine_km(a["lat"], a["lon"], b["lat"], b["lon"]) * geo.ROAD_FACTOR)


def test_suggest_is_on_the_way_in_order_and_prefers_big_towns():
    way = geo.suggest("delhi", "mumbai", 4)
    ids = [p["id"] for p in way]
    assert 3 <= len(way) <= 4 and "delhi" not in ids and "mumbai" not in ids
    assert [p["km_from_start"] for p in way] == sorted(p["km_from_start"] for p in way)
    assert all(p["detour_km"] <= 60 for p in way)
    assert "mathura" in [p["id"] for p in geo.suggest("delhi", "agra", 2)]   # the town on the road, not a village next to it


def test_long_legs_show_suggestions_once_without_llm():
    sess = state.session("long1")
    sess.current_text = "trip from jaipur to kochi"
    r = tools.set_route("long1", ["jaipur", "kochi"])
    assert r["ok"] and r["edit"] and r["line"] == "Jaipur → Kochi"
    sugg = state.ui("long1")["suggestions"]                           # cards on the right, not a question
    assert state.ui("long1")["question"] is None
    assert 3 <= len(sugg) <= tools.MAX_PER_LEG and re.search(r"~4\d h\)", sugg[0]["group"])   # ~2,400 km
    assert all(x["between"] == ["jaipur", "kochi"] and "Jaipur → Kochi" in x["group"] for x in sugg)
    assert all(places.resolve(x["name"])[0]["score"] == 100 for x in sugg)   # every one is in our list

    # The same trip again (or after a click): the leg was offered already, no new cards.
    state.set_display("long1", suggestions=[])
    tools.set_route("long1", ["jaipur", "kochi"])
    assert state.ui("long1")["suggestions"] == []

    # Every long leg gets its own cards, under its own header.
    state.session("long3").current_text = "jaipur kochi kolkata"
    tools.set_route("long3", ["jaipur", "kochi", "kolkata"])
    groups = {x["group"] for x in state.ui("long3")["suggestions"]}
    assert len(groups) == 2 and len(state.ui("long3")["suggestions"]) <= tools.MAX_SUGGESTIONS


def test_static_files_are_revalidated():
    from app.main import app
    r = TestClient(app).get("/app.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_set_route_takes_stops_the_user_never_named():
    # The 2-unnamed-stops guardrail was removed (owner's choice, 2026-11-21): planned trips name many towns.
    sess = state.session("guard1")
    sess.current_text = "plan a trip from delhi to mumbai"
    r = tools.set_route("guard1", ["delhi", "agra", "jaipur", "ahmedabad", "mumbai"])
    assert r["ok"] and state.stop_ids("guard1") == ["delhi", "agra", "jaipur", "ahmedabad", "mumbai"]


def test_named_places_count_with_misspellings():
    assert places.mentioned("varanasi", "Banaras jaana hai")          # alias
    assert places.mentioned("jaipur", "jaipr se kochi")               # small typo
    assert not places.mentioned("pune", "punjab trip")


def test_add_keeps_cards_of_other_long_legs_and_refreshes_the_broken_one():
    sid = "keep1"
    state.session(sid).current_text = "jaipur kochi kolkata"
    tools.set_route(sid, ["jaipur", "kochi", "kolkata"])            # two long legs, each with cards
    cards = state.ui(sid)["suggestions"]
    first = next(c for c in cards if c["between"] == ["jaipur", "kochi"])
    other = [c for c in cards if c["between"] == ["kochi", "kolkata"]]
    assert other
    tools.add_suggestion(sid, cards.index(first))
    ids = state.stop_ids(sid)
    after = state.ui(sid)["suggestions"]
    assert first["id"] in ids and ids.index(first["id"]) == 1
    assert [c["id"] for c in other] == [c["id"] for c in after if c["between"] == ["kochi", "kolkata"]]   # kept
    assert not any(c["between"] == ["jaipur", "kochi"] for c in after)                  # that leg is gone
    pairs = set(zip(ids, ids[1:]))
    assert all(tuple(c["between"]) in pairs for c in after)
    assert len({c["id"] for c in after}) == len(after)

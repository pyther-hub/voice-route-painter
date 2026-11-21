"""suggest_enroute_places / suggest_nearby_places: places from the list, shown as suggestion cards (no LLM)."""
import json

from app import geo, places, state, tools


def test_enroute_is_on_the_road_in_travel_order():
    way = geo.enroute("delhi", "mumbai", 8)
    ids = [p["id"] for p in way]
    assert 1 <= len(way) <= 8 and "delhi" not in ids and "mumbai" not in ids
    assert "mathura" in [p["id"] for p in geo.enroute("delhi", "agra", 8)]
    assert [p["km_from_start"] for p in way] == sorted(p["km_from_start"] for p in way)
    limit = geo.max_detour_km(geo.leg(places.get("delhi"), places.get("mumbai"))[0])
    assert all(p["detour_km"] <= limit + 1 for p in way)


def test_nearby_is_within_radius_nearest_first():
    near = geo.nearby("udaipur", 100, 6)
    assert near and all(p["km_away"] <= 100 for p in near) and "udaipur" not in [p["id"] for p in near]
    assert [p["km_away"] for p in near] == sorted(p["km_away"] for p in near)


def test_enroute_tool_leaves_out_trip_stops_and_skipped_places():
    sid = "sug-enroute"
    state.update_state(sid, ["chennai", "vijayawada", "kolkata"])
    state.session(sid).dismissed.add("bhubaneswar")
    r = tools.run_tool(sid, "suggest_enroute_places",
                       json.dumps({"from_id": "chennai", "to_id": "kolkata", "range_km": 60}))
    ids = [p["id"] for p in r["places"]]
    assert r["ok"] and r["ends_turn"] and ids and "vijayawada" not in ids and "bhubaneswar" not in ids
    assert "hyderabad" not in ids                                  # far off this road
    cards = state.ui(sid)["suggestions"]                           # shown as cards, not in the reply text
    assert [c["id"] for c in cards] == ids                         # chennai → kolkata = both legs of the trip
    assert {tuple(c["between"]) for c in cards} <= {("chennai", "vijayawada"), ("vijayawada", "kolkata")}
    assert "60 km" in cards[0]["group"] and not any(p["name"] in r["reply"] for p in r["places"])
    assert state.stop_ids(sid) == ["chennai", "vijayawada", "kolkata"]          # changes nothing


def test_suggest_tools_reject_bad_ids():
    for name, args in [("suggest_enroute_places", {"from_id": "atlantis", "to_id": "agra"}),
                       ("suggest_enroute_places", {"from_id": "agra", "to_id": "agra"}),
                       ("suggest_enroute_places", {"from_id": "agra", "to_id": None, "range_km": None}),
                       ("suggest_enroute_places", {"from_id": None, "to_id": None}),     # no route yet
                       ("suggest_nearby_places", {"place_id": "atlantis"})]:
        r = tools.run_tool("sug-bad", name, json.dumps(args))
        assert not r["ok"] and r["hint"], (name, args)


def test_nearby_tool_range_default_and_clamp():
    r = tools.run_tool("sug-near", "suggest_nearby_places", json.dumps({"place_id": "udaipur", "range_km": None}))
    cards = state.ui("sug-near")["suggestions"]
    assert r["ok"] and len(cards) <= tools.NEARBY_LIMIT
    assert all(int(c["note"].split()[0]) <= tools.SUGGEST_DEFAULT_KM for c in cards)   # default 30 km
    assert tools._range(5000, 5, tools.NEARBY_MAX_KM) == tools.NEARBY_MAX_KM and tools._range(None, 5, 300) == 30
    wide = tools.run_tool("sug-near2", "suggest_nearby_places", json.dumps({"place_id": "visakhapatnam", "range_km": 200}))
    assert wide["ok"] and wide["places"] and "200 km" in state.ui("sug-near2")["suggestions"][0]["group"]


def test_nothing_found_keeps_the_turn_open():
    r = tools.run_tool("sug-none", "suggest_nearby_places", json.dumps({"place_id": "port-blair", "range_km": 5}))
    if places.get("port-blair") is None:
        return
    assert r["ok"] and r["places"] == [] and not r.get("ends_turn") and "range_km" in r["hint"]


def test_add_nearby_card_goes_next_to_its_place():
    sid = "sug-add-near"
    state.update_state(sid, ["shillong", "visakhapatnam"])
    tools.suggest_nearby_places(sid, "visakhapatnam", 200)
    cards = state.ui(sid)["suggestions"]
    first = cards[0]["id"]
    tools.add_suggestion(sid, 0)
    ids = state.stop_ids(sid)                                      # right before or after it, whichever adds
    assert ids[0] == "shillong" and abs(ids.index(first) - ids.index("visakhapatnam")) == 1   # fewer km
    state.update_state(sid, ["delhi", "agra"])                     # Agra is the end: a nearby place goes after it
    tools.suggest_nearby_places(sid, "agra", 150)
    d = places.get("delhi")
    far = max(state.ui(sid)["suggestions"],                        # the one farthest from Delhi (e.g. Gwalior)
              key=lambda c: geo.haversine_km(d["lat"], d["lon"], c["lat"], c["lon"]))
    tools.add_suggestion(sid, state.ui(sid)["suggestions"].index(far))
    assert state.stop_ids(sid)[:2] == ["delhi", "agra"] and state.stop_ids(sid)[-1] == far["id"]
    assert first not in [c["id"] for c in state.ui(sid)["suggestions"]]


def test_add_enroute_card_goes_between_its_two_stops():
    sid = "sug-add-way"
    state.update_state(sid, ["delhi", "agra", "lucknow"])
    tools.suggest_enroute_places(sid, "agra", "lucknow", 60)
    card = state.ui(sid)["suggestions"][0]
    tools.add_suggestion(sid, 0)
    assert state.stop_ids(sid) == ["delhi", "agra", card["id"], "lucknow"]


def test_whole_trip_gets_places_on_every_leg():
    sid = "sug-legs"
    state.update_state(sid, ["delhi", "jaipur", "kota", "gandhinagar", "patna"])
    r = tools.run_tool(sid, "suggest_enroute_places", json.dumps({"from_id": None, "to_id": None, "range_km": None}))
    cards = state.ui(sid)["suggestions"]
    assert r["ok"] and r["ends_turn"] and "4 legs" in r["reply"]
    legs = [tuple(c["between"]) for c in cards]
    assert set(legs) == {("delhi", "jaipur"), ("jaipur", "kota"), ("kota", "gandhinagar"), ("gandhinagar", "patna")}
    assert legs == sorted(legs, key=[("delhi", "jaipur"), ("jaipur", "kota"), ("kota", "gandhinagar"),
                                     ("gandhinagar", "patna")].index)              # in trip order, one group per leg
    assert len({c["group"] for c in cards}) == 4 and "Gandhinagar → Patna" in cards[-1]["group"]
    assert legs.count(("gandhinagar", "patna")) > legs.count(("jaipur", "kota"))    # longer leg, more places
    assert len({c["id"] for c in cards}) == len(cards)                              # each place once
    assert state.stop_ids(sid) == ["delhi", "jaipur", "kota", "gandhinagar", "patna"]


def test_stretch_covers_only_its_legs_and_add_goes_into_the_right_leg():
    sid = "sug-stretch"
    state.update_state(sid, ["delhi", "jaipur", "kota", "gandhinagar", "patna"])
    tools.suggest_enroute_places(sid, "kota", "patna", None)
    cards = state.ui(sid)["suggestions"]
    assert {tuple(c["between"]) for c in cards} == {("kota", "gandhinagar"), ("gandhinagar", "patna")}
    k = next(i for i, c in enumerate(cards) if c["between"] == ["gandhinagar", "patna"])
    new = cards[k]["id"]
    tools.add_suggestion(sid, k)
    assert state.stop_ids(sid) == ["delhi", "jaipur", "kota", "gandhinagar", new, "patna"]


def test_per_leg_counts_fit_the_cap():
    assert tools._per_leg_count(100) == 1 and tools._per_leg_count(500) == 2 and tools._per_leg_count(3000) == 4
    assert sum(tools._fit([4, 4, 4, 4])) == tools.MAX_SUGGESTIONS
    assert tools._fit([4, 1, 4, 4]) == [3, 1, 4, 4]
    assert tools._fit([1] * 15) == [1] * 12 + [0] * 3
    ids = ["delhi", "jaipur", "udaipur", "ahmedabad", "mumbai", "pune", "panaji", "mangaluru", "kochi", "madurai",
           "chennai", "bengaluru", "hyderabad", "nagpur"]
    state.update_state("sug-cap", ids)
    tools.suggest_enroute_places("sug-cap", None, None, 60)
    assert len(state.ui("sug-cap")["suggestions"]) <= tools.MAX_SUGGESTIONS

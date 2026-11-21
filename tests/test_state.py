import uuid

import pytest

from app import state


def sid():
    return uuid.uuid4().hex


def test_haversine_known_distance():
    # Delhi -> Mumbai is ~1150 km in a straight line
    assert 1130 < state.haversine_km(28.61, 77.21, 19.08, 72.88) < 1170
    assert state.haversine_km(26.85, 80.95, 26.85, 80.95) == 0


def test_update_state_shape_matches_what_the_ui_reads():
    full = state.update_state(sid(), ["lucknow", "agra", "jaipur"], "ok")
    assert set(full) == {"trip", "ui"} and set(state.empty_state()) == {"trip", "ui"}
    s = full["trip"]
    assert set(s["stops"][0]) == {"id", "name", "state", "lat", "lon"}
    assert set(s["legs"][0]) == {"km", "car_min", "train_min", "path"}
    assert len(s["legs"]) == 2 and s["total_km"] == sum(l["km"] for l in s["legs"])
    assert s["version"] == 1 and full["ui"]["message"] == "ok"


def test_validation_rejects_bad_stops_and_keeps_state():
    i = sid()
    state.update_state(i, ["delhi", "agra"])
    for bad in (["delhi"], ["delhi", "delhi"], ["delhi", "atlantis"]):
        with pytest.raises(ValueError):
            state.update_state(i, bad)
    assert state.stop_ids(i) == ["delhi", "agra"] and state.trip(i)["version"] == 1


def test_empty_trip_allowed():
    i = sid()
    state.update_state(i, ["delhi", "agra"])
    assert state.update_state(i, [])["trip"]["stops"] == []


def test_undo_and_listener():
    i = sid()
    pushed = []
    state.session(i).listener = pushed.append
    state.update_state(i, ["delhi", "agra"])
    state.update_state(i, ["delhi", "agra", "jaipur"])
    s = state.undo(i)["trip"]
    assert [x["id"] for x in s["stops"]] == ["delhi", "agra"]
    assert s["version"] == 3 and len(pushed) == 3
    state.undo(i)
    assert state.stop_ids(i) == []
    with pytest.raises(ValueError):
        state.undo(i)


def test_state_shape_is_checked():
    import pydantic
    i = sid()
    state.update_state(i, ["lucknow", "agra"])
    state.check_state(state.get_state(i))                 # a real state matches the schema
    with pytest.raises(pydantic.ValidationError):         # unknown key (a typo) is refused
        state.check_state({**state.get_state(i), "ui": {**state.ui(i), "stopps": []}})
    with pytest.raises(pydantic.ValidationError):         # wrong type is refused
        state.set_display(i, tokens="many")
    assert state.ui(i)["tokens"] == 0              # and nothing was saved

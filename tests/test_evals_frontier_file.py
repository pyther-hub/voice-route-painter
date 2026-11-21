"""evals/frontier.json is well formed, and the scoring works (no LLM calls)."""
import json
from pathlib import Path

from app import places
from evals import frontier

CASES = json.loads((Path(__file__).parent.parent / "evals" / "frontier.json").read_text())
STATES = {p["state"] for p in places.load()}


def test_ten_cases_with_valid_fields():
    assert len(CASES) == 10 and len({c["id"] for c in CASES}) == 10
    assert sum(c["mode"] == "itinerary" for c in CASES) == 6 and sum(c["mode"] == "compare" for c in CASES) == 4
    for c in CASES:
        assert c["lang"] in ("hinglish", "english") and c["say"]
        if c["mode"] == "itinerary":
            assert set(c["states"]) <= STATES and 1 < c["min_stops"] <= c["max_stops"]
        else:
            assert len(c["routes"]) == 2 and all(places.get(i) for r in c["routes"] for i in r) and c["keywords"]


def test_scoring():
    it = next(c for c in CASES if c["id"] == "f02")
    jaipur, udaipur, jodhpur = (places.get(i) for i in ("jaipur", "udaipur", "jodhpur"))
    proposal = {"kind": "itinerary", "stops": [jaipur, udaipur, jodhpur], "tagline": "Raja-maharaja wala trip"}
    r = frontier.score(it, "escalated", "Rajasthan ka trip ready hai, dekh lo", [], [], proposal, True)
    assert r["usable"] and r["in_region"] == 1.0 and not r["mutated"] and r["escalated"] and r["lang_ok"]
    r = frontier.score(it, "cheap", "Which cities?", [], [], None, False)
    assert not r["usable"] and not r["lang_ok"]
    cmp_case = next(c for c in CASES if c["id"] == "f08")
    assert frontier.score(cmp_case, "cheap", "Via Shimla is more scenic.", [], [], None, False)["usable"]

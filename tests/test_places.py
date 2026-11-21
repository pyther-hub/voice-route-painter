from app import places
from app.places import norm, resolve


def test_norm_strips_accents_and_punctuation():
    assert norm("  Rājasthān! ") == "rajasthan"


def test_exact_name_and_alias():
    assert resolve("agra")[0]["id"] == "agra"
    assert resolve("Bombay")[0]["id"] == "mumbai"      # old name, from GeoNames aliases
    assert resolve("Gurgaon")[0]["id"] == "gurugram"


def test_fuzzy_typo():
    top = resolve("Lucknau")[0]
    assert top["id"] == "lucknow" and top["score"] < 100


def test_nothing_close_returns_empty():
    assert resolve("Xyzabc") == []
    assert resolve("") == []


def test_tourist_alias_from_csv():
    assert resolve("Kaziranga")[0]["id"] == "kohora"
    assert resolve("Havelock")[0]["id"] == "swaraj-dweep"   # manual coordinates


def test_place_not_in_list_returns_empty():
    assert resolve("Doodhpathri") == []      # small meadow village, not in data/places.json


def test_state_hint_breaks_ties():
    assert resolve("Goa", "Goa")[0]["id"] == "panaji"
    assert resolve("Ladakh", "Ladakh")[0]["id"] == "leh"


def test_at_most_three_candidates_best_first():
    cands = resolve("Banglore")
    assert 1 <= len(cands) <= 3
    assert cands[0]["id"] == "bengaluru"
    assert [c["score"] for c in cands] == sorted([c["score"] for c in cands], reverse=True)


def test_find_spans():
    text = "Luknow se Jaypur jana hai, Agra hata do"
    spans = {f["id"]: text[f["start"]:f["end"]] for f in places.find_spans(text, ["lucknow", "jaipur", "agra", "kochi"])}
    assert spans == {"lucknow": "Luknow", "jaipur": "Jaypur", "agra": "Agra"}


def test_find_exact():
    text = "Lucknow se Jaipur jaana hai, beech mein Mount Abu, Agra hata do"
    got = [text[f["start"]:f["end"]] for f in places.find_exact(text)]
    assert got == ["Lucknow", "Jaipur", "Mount Abu", "Agra"]
    assert places.find_exact("add one stop in between, do it") == []

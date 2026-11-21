"""Fixes from the first full eval run (2026-11-16): lenient list limits (s13, s47)."""
from app import tools


def test_resolve_place_looks_up_the_first_eight():
    names = ["Delhi", "Agra", "Jaipur", "Udaipur", "Jodhpur", "Pushkar", "Ajmer", "Bikaner", "Jaisalmer"]
    r = tools.resolve_place("fix-resolve", names)
    assert r["ok"] and len(r["results"]) == 8 and "Jaisalmer" in r["note"]

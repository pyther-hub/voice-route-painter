"""The eval set is well-formed (no LLM calls: this never runs the agent)."""
import json
from collections import Counter

from app import places
from app.main import MAX_TEXT_LEN
from evals.run import CHECKS, SESSIONS, check, length_bucket

SESS = json.loads(SESSIONS.read_text())


def _ids(expect):
    for k, v in expect.items():
        if k == "any_of":
            for alt in v:
                yield from _ids(alt)
        elif k in ("stops", "order", "has"):
            yield from v
        elif k in ("first", "last"):
            yield v


def _keys(expect):
    for k, v in expect.items():
        yield k
        if k == "any_of":
            for alt in v:
                yield from _keys(alt)


def test_sessions_valid_ids_and_checks():
    assert len(SESS) == 52 and len({s["id"] for s in SESS}) == 52
    for s in SESS:
        for t in s["turns"]:
            assert 0 < len(t["say"]) <= MAX_TEXT_LEN, t["say"]
            assert set(_keys(t["expect"])) <= CHECKS, t["expect"]
            for i in _ids(t["expect"]):
                assert places.get(i) or i == "kathmandu", (s["id"], i)   # kathmandu: only in not_has


def test_input_lengths_vary():
    c = Counter(length_bucket(t["say"]) for s in SESS for t in s["turns"])
    assert c["short"] >= 20 and c["medium"] >= 10 and c["long"] >= 6, c


def test_check_logic():
    before = {"stops": [], "asks": False}
    after = {"stops": ["delhi", "agra", "jaipur", "delhi"], "asks": False}
    assert check({"round_trip": True, "order": ["delhi", "jaipur"], "max_len": 4}, after, before) == []
    assert check({"order": ["jaipur", "agra"]}, after, before)
    assert check({"any_of": [{"asks": True}, {"first": "delhi"}]}, after, before) == []
    assert check({"unchanged": True}, after, before)

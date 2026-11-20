"""Code-only place matchers for exp-01: find place names in the raw text and look them up, no LLM.

    exact(text)          -> what v25's agent.detected_places() finds (exact names/aliases, then tools.lookup)
    exact_fuzzy(text, t) -> exact, plus spelling matches (score >= t) on the words exact left over

Each returns [{text, start, end, status: ok|ambiguous, matches: [{id, name}]}], the shape resolve_place returns
per name. `as_detected(session_id, text)` wraps one for common.Variant(detected=...): it drops names whose
single match is already on the trip, as v25 does.
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import common  # noqa: E402,F401  (repo root on sys.path)
from app import places, state, tools  # noqa: E402

MIN_FUZZY_LEN = 4       # a single short word ("ko", "se", "aur") is never spelling-matched


def exact(text: str) -> list[dict]:
    out, seen = [], set()
    for f in places.find_exact(text):
        words = text[f["start"]:f["end"]]
        key = places.norm(words)
        if key in seen:
            continue
        seen.add(key)
        r = tools.lookup(words)
        if r["status"] != "not_found":
            out.append({"text": words, "start": f["start"], "end": f["end"], "status": r["status"],
                        "matches": r["matches"]})
    return out


def exact_fuzzy(text: str, threshold: int) -> list[dict]:
    found = exact(text)
    taken = [(f["start"], f["end"]) for f in places.find_exact(text)]     # every copy of an exact name
    words = [(m.start(), m.end(), m.group()) for m in re.finditer(r"\w+", text)]
    free = [not any(s <= w[0] < e for s, e in taken) for w in words]
    seen = {places.norm(d["text"]) for d in found}
    i = 0
    while i < len(words):
        hit = None
        for k in (3, 2, 1):                                  # longest first, like find_exact
            if i + k > len(words) or not all(free[i:i + k]):
                continue
            phrase = text[words[i][0]:words[i + k - 1][1]]
            if k == 1 and len(phrase) < MIN_FUZZY_LEN:
                continue
            cands = places.resolve(phrase)
            if cands and cands[0]["score"] >= threshold and places.norm(phrase) not in seen:
                r = tools.lookup(phrase)
                if r["status"] != "not_found":
                    hit = (k, {"text": phrase, "start": words[i][0], "end": words[i + k - 1][1],
                               "status": r["status"], "matches": r["matches"]})
                    break
        if hit:
            k, d = hit
            found.append(d)
            seen.add(places.norm(d["text"]))
            i += k
        else:
            i += 1
    return sorted(found, key=lambda d: d["start"])


def as_detected(matcher):
    """A detected_places(session_id, text) replacement built on a matcher, in the shape v25 sends."""
    def detected(session_id: str, text: str) -> list[dict]:
        try:
            on_trip = set(state.stop_ids(session_id))
            return [{"text": d["text"], "status": d["status"], "matches": d["matches"]} for d in matcher(text)
                    if not (d["status"] == "ok" and d["matches"][0]["id"] in on_trip)]
        except Exception:
            return []
    return detected

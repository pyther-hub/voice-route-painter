"""Tools the agent can call, and run_tool(): the one gateway every tool call goes through.

Each tool = name + description + JSON schema (strict) + a plain function(session_id, **args) -> dict.
Results are small dicts. Failures are {"ok": False, "error": ..., "hint": what to do next},
so the model can read them and fix its call.

Edit tools (set_route, add_stop, remove_stop, undo) change the trip only through state.update_state() /
state.undo() and return {"ok": True, "edit": True, "line": ...}: the agent keeps going until it stops calling
tools, then the turn's headline is these lines (written here, in code). ask_user and reply end the turn with their text.
"""
import json
import re

from app import geo, guard, llm, places, state, tts
from app.trace import span

MAX_LOOKUPS = 8           # names per resolve_place call (more are skipped, with a note)
MAX_NAME_LEN = 80
MAX_QUESTION_LEN = 200
MAX_REPLY_LEN = 300
MIN_OPTIONS, MAX_OPTIONS = 2, 4
MAX_OPTION_LEN = 40
OK_SCORE, OK_LEAD = 85, 5   # a spelling guess is "ok" if it scores this much and leads the next one by this much
ENROUTE_LIMIT = 8         # places suggest_enroute_places shows
NEARBY_LIMIT = 6          # places suggest_nearby_places shows
SUGGEST_DEFAULT_KM = 30   # range_km when the agent gives none: nearby radius / en-route km off the road
NEARBY_MAX_KM, ENROUTE_MAX_KM = 300, 100   # larger range_km values are clamped to these


# ---------------------------------------------------------------- resolve_place

def _split_state(query: str) -> tuple[str, str | None]:
    """'Aurangabad, Bihar' or 'Aurangabad (Bihar)' -> ('Aurangabad', 'Bihar'); else (query, None).
    Option labels look like this, so a clicked option resolves to exactly one place."""
    m = re.fullmatch(r"\s*(.*?)\s*\(([^()]*)\)\s*", query) or re.fullmatch(r"\s*(.*?)\s*,\s*([^,]+?)\s*", query)
    return (m.group(1), m.group(2)) if m else (query.strip(), None)


def _in_state(candidate: dict, hint: str) -> bool:
    st, h = places.norm(candidate["state"]), places.norm(hint)
    return bool(h) and (st.startswith(h) or h in st.split())


def lookup(query: str) -> dict:
    """One name -> {query, status: ok | ambiguous | not_found, matches: [{id, name}]}."""
    name, hint = _split_state(query)
    cands = places.resolve(name, hint)
    if hint:
        cands = [c for c in cands if _in_state(c, hint)]
    exact = [c for c in cands if c["score"] == 100]
    if exact:                                                   # exact name/alias: ok if only one place has it
        found, status = exact, "ok" if len(exact) == 1 else "ambiguous"
    elif not hint and (region := places.in_state(name)):        # "Himachal": a state, not one place
        found, status = region, "ambiguous"
    elif cands:                                                 # spelling guesses: ok only if one clearly wins
        clear = cands[0]["score"] >= OK_SCORE and (len(cands) == 1 or cands[0]["score"] - cands[1]["score"] >= OK_LEAD)
        found, status = (cands[:1], "ok") if clear else (cands, "ambiguous")
    else:
        found, status = [], "not_found"
    return {"query": query, "status": status,
            "matches": [{"id": c["id"], "name": f"{c['name']}, {c['state']}"} for c in found]}


def resolve_place(session_id: str, queries: list[str]) -> dict:
    """Look up several names in one call (one LLM step instead of one per place). More than MAX_LOOKUPS: the first
    ones are looked up and the result says so (rejecting the call cost a whole retry step: eval s13, s47)."""
    note = None
    if len(queries) > MAX_LOOKUPS:
        note = f"only the first {MAX_LOOKUPS} names were looked up: {', '.join(queries[MAX_LOOKUPS:])} skipped"
        queries = queries[:MAX_LOOKUPS]
    if not queries or any(not q.strip() or len(q) > MAX_NAME_LEN for q in queries):
        return {"ok": False, "error": f"need 1-{MAX_LOOKUPS} short place names",
                "hint": "Call again with just the place names, e.g. ['Agra', 'Jaipur']."}
    return {"ok": True, "results": [lookup(q) for q in queries], **({"note": note} if note else {})}


# ---------------------------------------------------------------- place suggestions (cards the user picks from)

def _unknown(*ids: str) -> dict | None:
    bad = [i for i in ids if places.get(i) is None]
    if bad:
        return {"ok": False, "error": f"unknown place ids: {bad}",
                "hint": "Use ids from resolve_place or the current stops."}
    return None


def _not_in_trip(session_id: str, found: list[dict]) -> list[dict]:
    """Leave out stops already on the trip and places the user skipped."""
    skip = set(state.stop_ids(session_id)) | state.session(session_id).dismissed
    return [x for x in found if x["id"] not in skip]


def _range(range_km, low: int, high: int) -> int:
    """The agent's range_km, or the default; out-of-range values are clamped (trim, don't reject)."""
    if not isinstance(range_km, (int, float)) or isinstance(range_km, bool):
        return SUGGEST_DEFAULT_KM
    return int(min(high, max(low, range_km)))


def _card(place_id: str, note: str, group: str, **extra) -> dict:
    """One suggestion card: the place (name + where it is), a note, the header of its search, and where
    "✓ Add" puts it (`between` two stops / `near` one place)."""
    p = places.get(place_id)
    return {**_spot(p), "id": place_id, "state": p["state"], "note": note, "group": group, **extra}


def _show_cards(session_id: str, cards: list[dict], line: str, more: str = "") -> dict:
    """Put the places on screen as suggestion cards (+ ★ pins) and end the turn with a line written in code,
    so the list is shown as cards, not as text in the headline. The agent's memory keeps the names."""
    state.set_display(session_id, suggestions=cards)
    names = ", ".join(c["name"] for c in cards)
    return {"ok": True, "ends_turn": True, "reply": line, "memory": f"{line} Shown as cards: {names}.{more}",
            "places": [{k: c[k] for k in ("id", "name", "state")} for c in cards]}


# Suggestions for several legs (the agent's search over the trip, and the long-drive cards): each leg gets places
# by its length, all of them together at most MAX_SUGGESTIONS cards.
PER_LEG_KM = 250          # one place per this many km of road
MAX_PER_LEG = 4
MAX_SUGGESTIONS = 12


def _per_leg_count(km: float) -> int:
    return max(1, min(MAX_PER_LEG, round(km / PER_LEG_KM)))


def _fit(counts: list[int], cap: int = MAX_SUGGESTIONS) -> list[int]:
    """Per-leg counts that add up to at most `cap`: the largest goes down by 1 until they fit (each leg keeps 1).
    More legs than `cap`: the first `cap` legs get one each."""
    if len(counts) > cap:
        return [1] * cap + [0] * (len(counts) - cap)
    counts = list(counts)
    while sum(counts) > cap:
        counts[counts.index(max(counts))] -= 1
    return counts


def _leg_picks(session_id: str, legs: list[tuple[str, str, float]], max_off_km=None,
               also_skip=()) -> list[tuple[str, str, list]]:
    """For each leg (a, b, km): places spread along its road (geo.suggest), how many by the leg's length.
    Trip stops, skipped places and places already picked for an earlier leg are left out."""
    skip = set(state.stop_ids(session_id)) | state.session(session_id).dismissed | set(also_skip)
    out = []
    for (a, b, _), n in zip(legs, _fit([_per_leg_count(km) for _, _, km in legs])):
        if n:
            found = geo.suggest(a, b, n, max_off_km, skip)
            skip |= {x["id"] for x in found}
            out.append((a, b, found))
    return out


def _trip_legs(session_id: str, from_id: str | None, to_id: str | None) -> list[tuple[str, str, float]] | None:
    """The trip's legs (a, b, km) from from_id to to_id (both None = the whole trip), or None if those two are not
    stops on the trip with from_id first (then it is a road off the trip)."""
    ids, legs = state.stop_ids(session_id), state.trip(session_id)["legs"]
    if from_id is None:
        i, j = 0, len(ids) - 1
    else:
        if from_id not in ids:
            return None
        i = ids.index(from_id)
        j = next((k for k in range(i + 1, len(ids)) if ids[k] == to_id), None)
        if j is None:
            return None
    return [(ids[k], ids[k + 1], legs[k]["km"]) for k in range(i, j)]


def suggest_enroute_places(session_id: str, from_id: str | None = None, to_id: str | None = None,
                           range_km=None) -> dict:
    """Places from the list at most range_km off the road, shown as cards; ends the turn; changes nothing.
    Both ids None = every leg of the trip; two stops of the trip = the legs between them (several legs: places per
    leg by its length, one header per leg); anything else (one leg, or a road off the trip) = the best
    ENROUTE_LIMIT places on that road, as before."""
    if (from_id is None) != (to_id is None):
        return {"ok": False, "error": "pass both from_id and to_id, or neither",
                "hint": "Both null = every leg of the route; two place ids = that stretch."}
    if from_id is None and len(state.stop_ids(session_id)) < 2:
        return {"ok": False, "error": "there is no route yet",
                "hint": "Pass from_id and to_id, or ask the user where the trip goes."}
    if from_id is not None:
        if problem := _unknown(from_id, to_id):
            return problem
        if from_id == to_id:
            return {"ok": False, "error": "from_id and to_id are the same place",
                    "hint": "Pass two different places (or both null for the whole route); for places around one "
                            "place use suggest_nearby_places."}
    km = _range(range_km, 5, ENROUTE_MAX_KM)
    legs = _trip_legs(session_id, from_id, to_id)
    if legs is None or len(legs) == 1:                           # one road: its best places
        a_id, b_id = (from_id, to_id) if legs is None else legs[0][:2]
        found = _not_in_trip(session_id, geo.enroute(a_id, b_id, ENROUTE_LIMIT, km))
        picks = [(a_id, b_id, found)]
    else:
        picks = _leg_picks(session_id, legs, km)
    name = lambda i: places.get(i)["name"]
    cards = [_card(x["id"], f"{x['km_from_start']} km from {name(a)}; {x['detour_km']} km off the road",
                   f"On the way {name(a)} → {name(b)} (≤{km} km off the road)", between=[a, b])
             for a, b, found in picks for x in found]
    roads = "; ".join(f"{name(a)} → {name(b)}" for a, b, _ in picks)
    if not cards:
        return {"ok": True, "places": [], "range_km": km,
                "note": f"no place in the list within {km} km of the road ({roads})",
                "hint": f"Try a larger range_km (up to {ENROUTE_MAX_KM}), or tell the user with reply."}
    if len(picks) == 1:
        a, b = picks[0][:2]
        line = f"{len(cards)} places on the way from {name(a)} to {name(b)}, within {km} km of the road."
    else:
        line = f"{len(cards)} places on the way, on {len(picks)} legs, within {km} km of the road."
    empty = [f"{name(a)} → {name(b)}" for a, b, found in picks if not found]
    return _show_cards(session_id, cards, line, f" Nothing found on: {', '.join(empty)}." if empty else "")


def suggest_nearby_places(session_id: str, place_id: str, range_km=None) -> dict:
    """Places from the list within range_km (straight line) of one place, nearest first.
    Shown as cards; ends the turn. Changes nothing on the trip."""
    if problem := _unknown(place_id):
        return problem
    km = _range(range_km, 5, NEARBY_MAX_KM)
    found = _not_in_trip(session_id, geo.nearby(place_id, km, NEARBY_LIMIT))
    name = places.get(place_id)["name"]
    if not found:
        return {"ok": True, "places": [], "range_km": km, "note": f"no other place in the list within {km} km of {name}",
                "hint": f"Try a larger range_km (up to {NEARBY_MAX_KM}), or tell the user with reply."}
    cards = [_card(x["id"], f"{x['km_away']} km from {name}", f"Within {km} km of {name}", near=place_id)
             for x in found]
    return _show_cards(session_id, cards, f"{len(found)} places within {km} km of {name}.")


# ---------------------------------------------------------------- edit tools

def _write(session_id: str, ids: list[str], line: str) -> dict:
    """The one way the agent's edit tools change the trip. All edits of one agent turn make one undo step:
    only the first write of the turn saves history (Session.turn_wrote; outside a turn it is False)."""
    sess = state.session(session_id)
    state.update_state(session_id, ids, line, save_history=not sess.turn_wrote)   # raises ValueError if invalid
    sess.turn_wrote = True
    _offer_long_legs(session_id)          # (a pending proposal was cleared by update_state: the trip changed)
    return {"ok": True, "edit": True, "line": line, "stops": [s["name"] for s in state.trip(session_id)["stops"]]}


def _name(place_id: str) -> str:
    p = places.get(place_id)
    return p["name"] if p else place_id


def _bad_ids(e: ValueError) -> dict:
    return {"ok": False, "error": str(e),
            "hint": "Use ids from resolve_place or the current stops, and keep at least 2 stops (or clear with set_route([]))."}


def set_route(session_id: str, stops: list[str]) -> dict:
    """Replace the whole route (new trip, full rewrite, or [] to clear)."""
    line = " → ".join(_name(i) for i in stops) if stops else "Cleared the trip."
    try:
        result = _write(session_id, stops, line)
    except ValueError as e:
        return _bad_ids(e)
    if stops:
        state.session(session_id).last_mentioned = stops[-1]
    return result


def add_stop(session_id: str, place_id: str, after: str | None, before: str | None, position: str | None) -> dict:
    """Insert one stop next to a stop already on the route, or at the end."""
    ids = state.stop_ids(session_id)
    if sum(x is not None for x in (after, before, position)) != 1 or position not in (None, "end"):
        return {"ok": False, "error": "pass exactly one of after, before or position ('end')",
                "hint": "Name a stop on the route as `after` or `before`, or use position 'end'."}
    if not ids:
        return {"ok": False, "error": "the route is empty", "hint": "Start a new trip with set_route."}
    anchor = after or before
    if anchor is not None and anchor not in ids:
        return {"ok": False, "error": f"'{anchor}' is not on the route",
                "hint": "Use the id of a stop in state.stops as the anchor."}
    if position == "end":
        k, where = len(ids), "at the end"
    elif after is not None:
        k, where = ids.index(after) + 1, f"after {_name(after)}"
    else:
        k, where = ids.index(before), f"before {_name(before)}"
    try:
        result = _write(session_id, ids[:k] + [place_id] + ids[k:], f"Added {_name(place_id)} {where}.")
    except ValueError as e:
        return _bad_ids(e)
    state.session(session_id).last_mentioned = place_id
    return result


def remove_stop(session_id: str, place_id: str) -> dict:
    """Remove one stop (in a round trip, removing the start removes both ends)."""
    ids = state.stop_ids(session_id)
    if place_id not in ids:
        return {"ok": False, "error": f"'{place_id}' is not on the route",
                "hint": "Tell the user with reply; the current stops are in state.stops."}
    try:
        result = _write(session_id, [i for i in ids if i != place_id], f"Removed {_name(place_id)}.")
    except ValueError as e:
        return {"ok": False, "error": str(e),
                "hint": "A route needs at least 2 stops. To clear it, use set_route([])."}
    state.session(session_id).last_mentioned = place_id
    return result


def undo(session_id: str, count: int) -> dict:
    """Go back `count` changes in the history."""
    sess = state.session(session_id)
    if isinstance(count, bool) or not 1 <= count <= state.MAX_HISTORY:
        return {"ok": False, "error": f"count must be 1-{state.MAX_HISTORY}", "hint": "Use count 1 for one step back."}
    if len(sess.history) < count:
        return {"ok": False, "error": f"only {len(sess.history)} change(s) to undo",
                "hint": "Tell the user with reply how far back it can go."}
    line = "Undone." if count == 1 else f"Undid {count} changes."
    for _ in range(count):
        state.undo(session_id, line)
    sess.turn_wrote = False         # an edit after this, in the same turn, saves its own undo step
    _offer_long_legs(session_id)
    return {"ok": True, "edit": True, "line": line, "stops": [s["name"] for s in state.trip(session_id)["stops"]]}


# ---------------------------------------------------------------- ending a turn without an edit

def ask_user(session_id: str, question: str, options: list[str]) -> dict:
    """Question card with option buttons. A click comes back next turn as user_input.type = "choice"."""
    question, options = question.strip(), [o.strip() for o in options]
    if not question or len(question) > MAX_QUESTION_LEN:
        return {"ok": False, "error": f"question must be 1-{MAX_QUESTION_LEN} characters", "hint": "Ask one short question."}
    if (not MIN_OPTIONS <= len(options) <= MAX_OPTIONS or len(set(options)) != len(options)
            or any(not o or len(o) > MAX_OPTION_LEN for o in options)):
        return {"ok": False,
                "error": f"options must be {MIN_OPTIONS}-{MAX_OPTIONS} different labels of 1-{MAX_OPTION_LEN} characters",
                "hint": "Keep the best few choices, with short labels such as 'Aurangabad, Bihar'."}
    found = [_option_place(o) for o in options]
    state.session(session_id).pending_question = {
        "question": question, "options": [{"label": o, "id": p["id"] if p else None} for o, p in zip(options, found)]}
    # The UI shows a card with one button per option, and the options that are places as pins on the map.
    state.set_display(session_id, question={"text": question, "options": options, "spots": [_spot(p) for p in found]})
    return {"ok": True, "ends_turn": True, "reply": question,
            "memory": f"{question} (options: {'; '.join(options)})"}


def reply(session_id: str, message: str) -> dict:
    message = message.strip()
    if not message or len(message) > MAX_REPLY_LEN:
        return {"ok": False, "error": f"message must be 1-{MAX_REPLY_LEN} characters", "hint": "One or two short sentences."}
    return {"ok": True, "ends_turn": True, "reply": message}


def _spot(place: dict | None) -> dict | None:
    return {"name": place["name"], "lat": place["lat"], "lon": place["lon"]} if place else None


def _option_place(label: str) -> dict | None:
    """The place an option names, if it names exactly one: 'Mysuru' or 'Aurangabad, Bihar'."""
    r = lookup(label)
    return places.get(r["matches"][0]["id"]) if r["status"] == "ok" else None


# ---------------------------------------------------------------- long drives: code-made suggestion cards

def _offer_long_legs(session_id: str, keep: list | None = None) -> None:
    """Offer towns on the way for every leg of more than a day's drive (12 h), as suggestion cards the user accepts
    or skips: places per leg by its length, at most MAX_SUGGESTIONS in all, one header per leg. Built in code: no
    LLM call, and every card is a place in our list. Each leg is offered once (also after the user accepts or
    skips), so cards don't keep coming back. Runs after every trip change. `keep` = cards that stay on screen
    (they still fit the trip): new cards are added to them, in trip order, and their places are not offered twice."""
    sess, s = state.session(session_id), state.trip(session_id)
    stops, legs = s["stops"], s["legs"]
    todo = [(stops[i]["id"], stops[i + 1]["id"], leg["km"]) for i, leg in enumerate(legs)
            if leg["car_min"] > state.DAY_DRIVE_MIN and (stops[i]["id"], stops[i + 1]["id"]) not in sess.offered_legs]
    if not todo:
        return
    sess.offered_legs.update((a, b) for a, b, _ in todo)
    hours = {(a, b): round(leg["car_min"] / 60) for a, b, leg in zip((x["id"] for x in stops),
                                                                      (x["id"] for x in stops[1:]), legs)}
    cards = []
    for a, b, found in _leg_picks(session_id, todo, also_skip={c["id"] for c in keep or []}):
        an, bn, h = places.get(a)["name"], places.get(b)["name"], hours[(a, b)]
        cards += [_card(c["id"], f"{c['km_from_start']} km from {an}; {c['detour_km']} km off the road",
                        f"Long drive {an} → {bn} (~{h} h)", between=[a, b]) for c in found]
    if cards:
        order = {(stops[i]["id"], stops[i + 1]["id"]): i for i in range(len(stops) - 1)}
        merged = (keep or []) + cards
        merged.sort(key=lambda c: order.get(tuple(c.get("between") or ()), len(stops)))     # stable: trip order
        state.set_display(session_id, suggestions=merged)


def _insert_on_the_way(ids: list[str], new_id: str) -> list[str]:
    """Put a place between two existing stops where it adds the fewest km (never before the start or after the end)."""
    if new_id in ids or len(ids) < 2:
        return ids
    p, best = places.get(new_id), None
    for k in range(1, len(ids)):
        a, b = places.get(ids[k - 1]), places.get(ids[k])
        extra = geo.leg(a, p)[0] + geo.leg(p, b)[0] - geo.leg(a, b)[0]
        if best is None or extra < best[0]:
            best = (extra, k)
    return ids[:best[1]] + [new_id] + ids[best[1]:]


# ---------------------------------------------------------------- clicks applied with no LLM call
# Each returns the reply line, or None if it can't be done. main.py traces them and shows the reply.

def undo_redo(session_id: str, which: str) -> str | None:
    """The Undo / Redo buttons."""
    try:
        if which == "undo":
            state.undo(session_id, "Undone.")
        else:
            state.redo(session_id, "Redone.")
    except ValueError:
        return None
    return "Undone." if which == "undo" else "Redone."


def load_trip(session_id: str, place_ids) -> str | None:
    """A shared link (#trip=a,b,c) was opened: put that trip on the map."""
    if not isinstance(place_ids, list):
        return None
    try:
        state.update_state(session_id, place_ids, "Opened a shared trip.")
    except (ValueError, TypeError):
        return None
    _offer_long_legs(session_id)
    return "Opened a shared trip."


def remove_at(session_id: str, index) -> str | None:
    """The user clicked ✕ on a stop (by position: a round trip has its start twice). Returns None if it can't be
    done (bad index, or the trip would be left with one stop or as A → A)."""
    ids = state.stop_ids(session_id)
    if not isinstance(index, int) or isinstance(index, bool) or not 0 <= index < len(ids) or len(ids) <= 2:
        return None
    name = state.trip(session_id)["stops"][index]["name"]
    try:
        state.update_state(session_id, ids[:index] + ids[index + 1:], f"Removed {name}.")
    except ValueError:
        return None
    _offer_long_legs(session_id)          # the two legs around it became one: offer a stop if that is a long drive
    return f"Removed {name}."


def _insert_for_card(ids: list[str], card: dict) -> list[str]:
    """Where "✓ Add" puts a card's place: between the two stops its en-route search was for (if they are
    neighbours on the trip), next to the place its nearby search was around (right before or right after it,
    whichever adds fewer km; after the last stop is allowed), else where it adds the fewest km."""
    new_id = card["id"]
    if new_id in ids:
        return ids
    a_b = card.get("between")
    if a_b:
        for k in range(1, len(ids)):
            if [ids[k - 1], ids[k]] == list(a_b):
                return ids[:k] + [new_id] + ids[k:]
    near = card.get("near")
    if near and not ids:
        return [near, new_id]                                    # no trip yet: from that place to this one
    if near in ids:
        p, best = places.get(new_id), None
        for k, i in enumerate(ids):
            if i != near:
                continue
            for pos in (k, k + 1):                               # before it / after it
                prev = places.get(ids[pos - 1]) if pos > 0 else None
                nxt = places.get(ids[pos]) if pos < len(ids) else None
                if prev is None:                                 # never in front of the start
                    continue
                extra = geo.leg(prev, p)[0] + (geo.leg(p, nxt)[0] - geo.leg(prev, nxt)[0] if nxt else 0)
                if best is None or extra < best[0]:
                    best = (extra, pos)
        if best:
            return ids[:best[1]] + [new_id] + ids[best[1]:]
    return _insert_on_the_way(ids, new_id)


def add_suggestion(session_id: str, pick) -> str | None:
    """"✓ Add" on a suggestion card: insert that place (see _insert_for_card). The leg it went into is broken, so
    its cards go; cards for the other legs (and nearby cards, which belong to no leg) stay. Each new leg that is
    still a long drive then gets fresh cards. Cleared cards are not "skipped": they can be suggested again."""
    sugg = state.ui(session_id)["suggestions"]
    if not isinstance(pick, int) or isinstance(pick, bool) or not 0 <= pick < len(sugg):
        return None
    card, ids = sugg[pick], state.stop_ids(session_id)
    ids = _insert_for_card(ids, card)
    if card["id"] not in ids:
        return None
    pairs = set(zip(ids, ids[1:]))
    keep = [x for i, x in enumerate(sugg) if i != pick and x["id"] not in ids
            and (not x.get("between") or tuple(x["between"]) in pairs)]
    state.set_display(session_id, suggestions=keep)             # before the trip push: the browser sees both at once
    try:
        line = state.update_state(session_id, ids)["ui"]["message"]     # "A → B → C"
    except ValueError:
        state.set_display(session_id, suggestions=sugg)
        return None
    _offer_long_legs(session_id, keep)
    return line


def dismiss_suggestion(session_id: str, pick) -> None:
    """The user skipped a suggestion card: remove it and never suggest that place again. Display only."""
    sugg = state.ui(session_id)["suggestions"]
    if not isinstance(pick, int) or isinstance(pick, bool) or not 0 <= pick < len(sugg):
        return
    state.session(session_id).dismissed.add(sugg[pick]["id"])
    state.set_display(session_id, suggestions=[x for i, x in enumerate(sugg) if i != pick])


# ---------------------------------------------------------------- registry

# ---------------------------------------------------------------- the frontier planner (proposals, never edits)
# The cheap agent escalates two kinds of request to a bigger model (llm.FRONTIER): planning a whole trip when the
# user named no stops, and comparing two routes. The answer is a proposal (ui.proposal): a ghost route + card.
# The trip changes only when the user accepts it (a click, or the agent's accept_proposal).

MAX_FILLER_LEN = 80
FRONTIER_MAX_OUT = 3000
FRONTIER_ATTEMPTS = 2     # one retry when the answer is cut off or not JSON (both calls are charged)
MIN_PLAN_STOPS, MAX_PLAN_STOPS = 3, 8

_ITINERARY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "tagline", "stops", "reasoning", "days_estimate"],
    "properties": {
        "title": {"type": "string"}, "tagline": {"type": "string"},
        "stops": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "why"],
            "properties": {"name": {"type": "string"}, "why": {"type": "string"}}}},
        "reasoning": {"type": "string"}, "days_estimate": {"type": "integer"},
    },
}
_COMPARE_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["recommendation", "options", "short_answer"],
    "properties": {
        "recommendation": {"type": "string", "enum": ["A", "B"]},
        "options": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["key", "label", "pros", "cons"],
            "properties": {"key": {"type": "string", "enum": ["A", "B"]}, "label": {"type": "string"},
                           "pros": {"type": "array", "items": {"type": "string"}},
                           "cons": {"type": "array", "items": {"type": "string"}}}}},
        "short_answer": {"type": "string"},
    },
}
ITINERARY_PROMPT = (
    "You plan road trips across India for a voice trip-planning app. From the user's request (English or "
    f"Hinglish) and their current trip, propose ONE road trip of {MIN_PLAN_STOPS}-{MAX_PLAN_STOPS} stops in "
    "travel order. Every stop is a town or city in India written as 'Town, State' (not a single temple, fort, "
    "lake or park: use the town it is in). Road travel only. If the user named places, they are anchors: keep all of "
    "them, start at the first and end at the last, and fill the middle with the best stops on or near the way. "
    "If a previous proposal is given and the request "
    "changes it (fewer days, more hill stations, cheaper), revise that proposal. Each stop's `why`: at most 12 "
    "words. `title`: at most 40 characters. `tagline`: at most 80 characters. `reasoning`: at most 2 short "
    "sentences. `days_estimate`: whole days for the trip. Write why, tagline and reasoning in the user's "
    "language (English, or Hinglish in Latin script).")
COMPARE_PROMPT = (
    "You compare two road routes in India for a voice trip-planning app. You get each route's stops plus the road "
    "km and driving hours the app computed: use those numbers, never others, and do not invent distances. Give "
    "each option a `label` of at most 20 characters (e.g. 'Coastal', 'Via Kolhapur'), 2-3 `pros` and 2-3 `cons` "
    "of at most 10 words each, `recommendation` A or B, and a `short_answer` of at most 160 characters that a "
    "voice reads aloud, in the user's language (Hinglish in Latin script if they wrote Hinglish).")


def _route_info(ids: list[str]) -> dict:
    """Stops + road legs for a list of place ids (same numbers the trip would get). Never writes anything."""
    found = [places.get(i) for i in ids]
    legs = []
    for a, b in zip(found, found[1:]):
        km, path = geo.leg(a, b)
        leg = {"km": km, "car_min": round(km / state.CAR_KMH * 60)}
        if path:
            leg["path"] = path
        legs.append(leg)
    return {"stops": [{"id": p["id"], "name": p["name"], "state": p["state"], "lat": p["lat"], "lon": p["lon"]}
                      for p in found],
            "legs": legs, "total_km": round(sum(l["km"] for l in legs)),
            "total_min": sum(l["car_min"] for l in legs)}


def _resolve_names(names: list) -> tuple[list[str], list[str]]:
    """Place names (as the planner or the agent wrote them) -> (ids in order, names not found). An ambiguous
    name takes its best match (a 'Town, State' label usually isn't ambiguous). Repeats dropped."""
    ids, missing = [], []
    for n in names or []:
        r = lookup(str(n)) if str(n).strip() else {"status": "not_found"}
        if r["status"] == "not_found":
            missing.append(str(n))
        elif (pid := r["matches"][0]["id"]) not in ids:
            ids.append(pid)
    return ids, missing


def _proposal_line(p: dict) -> str:
    """The proposal as the agent's memory sees it (no ids, short)."""
    if p["kind"] == "itinerary":
        return f"Proposed trip '{p['title']}': " + " → ".join(x["name"] for x in p["stops"])
    return "Compared " + "; ".join(f"{o['key']} {o['label']}: " + " → ".join(x["name"] for x in o["stops"])
                                   for o in p["options"]) + f" (recommended {p['recommended']})"


def pending_proposal(session_id: str) -> dict | None:
    """What the agent's input shows about a pending proposal (names only)."""
    p = state.session(session_id).proposal
    if not p:
        return None
    if p["kind"] == "itinerary":
        return {"kind": "itinerary", "title": p["title"], "stops": [x["name"] for x in p["stops"]]}
    return {"kind": "compare", "recommended": p["recommended"],
            "options": [{"key": o["key"], "label": o["label"], "stops": [x["name"] for x in o["stops"]]}
                        for o in p["options"]]}


MAX_ANCHORS = 3     # a message naming more places than this has already defined its route: no planner needed


def _named_places(text: str) -> list[str]:
    """Place ids named exactly in the user's text, in the order they were said (no repeats)."""
    try:
        ids: list[str] = []
        for f in sorted(places.find_exact(text), key=lambda f: f["start"]):
            if f["id"] not in ids:
                ids.append(f["id"])
        return ids
    except Exception:
        return []


def _with_anchors(ids: list[str], anchors: list[str]) -> list[str]:
    """The plan must keep the places the user named: the first one starts it, the last one ends it (when the user
    named two or more), and any other named place that the planner left out goes where it adds the fewest km."""
    ids = list(ids)
    if len(anchors) >= 2:
        if anchors[0] not in ids:
            ids.insert(0, anchors[0])
        if anchors[-1] not in ids:
            ids.append(anchors[-1])
    for a in anchors:
        if a not in ids:
            ids = _insert_on_the_way(ids, a) if len(ids) >= 2 else ids + [a]
    return ids


def _strs(value, n: int, length: int) -> list[str]:
    """A list of short strings from whatever the model sent (a string or a number where a list was expected = [])."""
    return [str(x)[:length] for x in value if isinstance(x, (str, int, float))][:n] if isinstance(value, list) else []


def _ask_frontier(session_id: str, model: str, instructions: str, ask: str, schema: dict,
                  sp: dict) -> tuple[dict | None, dict]:
    """The planner call, with one retry if the answer was cut off (more room) or wasn't JSON. Every attempt is
    charged to the spend cap, including failed ones (the tokens were spent). API errors (timeout, 429, 5xx) are not
    retried here: the SDK already retried once. Returns (answer or None, usage summed over attempts)."""
    total = {"input": 0, "cached": 0, "output": 0, "total": 0}
    attempts, max_out, note = [], FRONTIER_MAX_OUT, ""
    for _ in range(FRONTIER_ATTEMPTS):
        try:
            obj, usage = llm.structured(model, instructions, ask + note, schema, max_out)
        except llm.StructuredError as e:
            usage, obj = e.usage, None
            attempts.append({"failed": e.reason, "raw": e.raw[:500], "max_out": max_out})
        except Exception as e:                      # network / API: give up cleanly, the turn goes on
            attempts.append({"failed": f"{type(e).__name__}: {str(e)[:200]}"})
            break
        guard.charge(session_id, guard.llm_cost(usage, model))
        for k in total:
            total[k] += usage.get(k, 0)
        if obj is not None:
            attempts.append({"ok": True, "max_out": max_out})
            sp["output"] = {"answer": obj, "usage": total, "tokens": total["total"], "model": model,
                            "attempts": attempts}
            return obj, total
        if attempts[-1]["failed"] == "refused":
            break
        if attempts[-1]["failed"] == "truncated":
            max_out = int(max_out * 1.5)            # reasoning used up the room: give it more
        else:
            note = "\n(Your last answer was not valid JSON. Answer with the JSON object only.)"
    sp["output"] = {"answer": None, "usage": total, "tokens": total["total"], "model": model, "attempts": attempts}
    sp["error"] = "planner failed: " + "; ".join(a.get("failed", "") for a in attempts)
    return None, total


def plan_with_frontier(session_id: str, mode: str, request: str, filler: str,
                       route_a: list | None = None, route_b: list | None = None) -> dict:
    """Ask the frontier model for an itinerary or a comparison. Shows a proposal; never changes the trip."""
    sess = state.session(session_id)
    request, filler = request.strip(), filler.strip()[:MAX_FILLER_LEN]
    if sess.frontier_used:
        return {"ok": False, "error": "the planner was already asked this turn",
                "hint": "Answer the user with what you have (reply)."}
    if mode not in ("itinerary", "compare"):
        return {"ok": False, "error": "mode must be 'itinerary' or 'compare'", "hint": "Fix mode and call again."}
    if not request:
        return {"ok": False, "error": "request is empty", "hint": "Pass the user's ask in one line."}
    anchors = _named_places(sess.current_text) if mode == "itinerary" else []
    if len(anchors) > MAX_ANCHORS:        # up to 3 named places are anchors for the plan; more = their own route
        return {"ok": False, "error": f"the user named {len(anchors)} places themselves",
                "hint": "Build the route from their places with set_route / add_stop instead."}
    if mode == "compare":
        a_ids, a_missing = _resolve_names(route_a) if route_a else (state.stop_ids(session_id), [])
        b_ids, b_missing = _resolve_names(route_b)
        if a_missing or b_missing:
            return {"ok": False, "error": f"not in the place list: {a_missing + b_missing}",
                    "hint": "Use towns from the list (resolve_place), or ask the user."}
        if len(a_ids) < 2 or len(b_ids) < 2:
            return {"ok": False, "error": "each route needs at least 2 places (route_a null = the current trip)",
                    "hint": "Pass both routes as place-name lists in travel order."}
    if (blocked := guard.over_budget(session_id)):
        return {"ok": False, "error": "spend cap reached", "hint": f"Tell the user: {blocked}"}
    if (blocked := guard.frontier_blocked(sess.ip, sess.frontier_calls)):
        return {"ok": False, "error": "planner limit reached",
                "hint": f"Tell the user (in their language): {blocked}"}

    sess.frontier_used = True
    sess.frontier_calls += 1
    trigger = "comparison" if mode == "compare" else "no_stops_named"
    state.set_display(session_id, message=filler)
    state.speak(session_id, filler)                        # voice turns: "ek minute, plan bana raha hoon"
    model = llm.FRONTIER
    label = filler or f"Planning with {llm.MODELS[model]['label']}"
    with span("llm", "frontier", input={"mode": mode, "trigger": trigger, "model": model, "request": request,
                                        "session": session_id}) as sp, \
            state.progress_step(session_id, label, kind="llm", name=model, detail=f"trigger: {trigger}") as ui:
        trip_now = " → ".join(x["name"] for x in state.trip(session_id)["stops"]) or "(empty)"
        # A voice turn: every text field the user sees or hears is in the language they spoke (detected by STT).
        lang = (f"\nThe user spoke {tts.write_as(sess.speak_in)}: write every text field in that language."
                if sess.speak_in else "")
        if mode == "itinerary":
            prev = f"\nPrevious proposal: {_proposal_line(sess.proposal)}" if sess.proposal else ""
            named = ", ".join(f"{_name(i)}, {places.get(i)['state']}" for i in anchors)
            keep = (f"\nThe user named these places, in this order: {named}. Keep every one of them in the plan; "
                    "start at the first and end at the last unless the request says otherwise." if anchors else "")
            ask = f"User: {request}\nCurrent trip: {trip_now}{keep}{prev}{lang}"
            obj, usage = _ask_frontier(session_id, model, ITINERARY_PROMPT, ask, _ITINERARY_SCHEMA, sp)
        else:
            info = {"A": _route_info(a_ids), "B": _route_info(b_ids)}
            ask = f"User: {request}\n" + "\n".join(
                f"Route {k}: {' → '.join(x['name'] for x in v['stops'])} | {v['total_km']} km by road, "
                f"~{v['total_min'] / 60:.1f} h driving" for k, v in info.items()) + lang
            obj, usage = _ask_frontier(session_id, model, COMPARE_PROMPT, ask, _COMPARE_SCHEMA, sp)
        state.set_display(session_id, tokens=state.ui(session_id)["tokens"] + usage["total"])
        if obj is None:
            ui["status"], ui["result"] = "error", sp["error"][:90]
            state.set_display(session_id, message="")           # the filler promised a plan that isn't coming
            return {"ok": False, "error": "the planner could not answer",
                    "hint": "Apologise in one short sentence in the user's language and offer to build the trip "
                            "from a few towns they name. Do not call plan_with_frontier again this turn."}

        if mode == "itinerary":
            raw_stops = [x for x in (obj.get("stops") if isinstance(obj.get("stops"), list) else [])
                         if isinstance(x, dict) and isinstance(x.get("name"), str)]   # skip malformed stops
            ids, dropped = _resolve_names([x["name"] for x in raw_stops][:MAX_PLAN_STOPS])
            ids = _with_anchors(ids, anchors)                  # the user's own places are never dropped
            if len(ids) < 2:
                ui["status"], ui["result"] = "error", f"only {len(ids)} known places"
                return {"ok": False, "error": f"the planner's places aren't in the list: {dropped}",
                        "hint": "Tell the user with reply, or ask them for a region or a few towns."}
            why = {}
            for x in raw_stops:
                r = lookup(x["name"])
                if r["status"] != "not_found":
                    why.setdefault(r["matches"][0]["id"], str(x.get("why", ""))[:100])
            info = _route_info(ids)
            for st in info["stops"]:
                st["why"] = why.get(st["id"], "")
            proposal = {"kind": "itinerary", "title": str(obj.get("title", ""))[:60],
                        "tagline": str(obj.get("tagline", ""))[:120], "reasoning": str(obj.get("reasoning", ""))[:300],
                        "days_estimate": obj.get("days_estimate") if isinstance(obj.get("days_estimate"), int) else None,
                        "dropped": dropped, **info}
            ui["result"] = f"{len(ids)} stops · {usage['total']} tokens"
            reply_text = f"{proposal['title']}: " + " → ".join(x["name"] for x in info["stops"])
        else:
            opts = {o.get("key"): o for o in (obj.get("options") if isinstance(obj.get("options"), list) else [])
                    if isinstance(o, dict)}
            rec = obj.get("recommendation") if obj.get("recommendation") in ("A", "B") else None
            proposal = {"kind": "compare", "recommended": rec,
                        "short_answer": str(obj.get("short_answer", ""))[:200],
                        "options": [{"key": k, "label": str(opts.get(k, {}).get("label") or f"Route {k}")[:30],
                                     "pros": _strs(opts.get(k, {}).get("pros"), 3, 80),
                                     "cons": _strs(opts.get(k, {}).get("cons"), 3, 80),
                                     **info[k]} for k in ("A", "B")]}
            ui["result"] = f"recommends {rec or '?'} · {usage['total']} tokens"
            reply_text = proposal["short_answer"] or f"Route {rec} is better."
    sess.proposal = proposal
    state.set_display(session_id, proposal=proposal)
    # Voice turns hear one short line: the comparison's answer, or the plan's tagline (the card shows the rest).
    state.speak(session_id, proposal.get("short_answer") or proposal.get("tagline") or proposal.get("title", ""))
    return {"ok": True, "ends_turn": True, "reply": reply_text, "memory": _proposal_line(proposal)}


def _proposal_ids(p: dict, option: str | None) -> list[str] | None:
    if p["kind"] == "itinerary":
        return [x["id"] for x in p["stops"]]
    o = next((o for o in p["options"] if o["key"] == (option or p.get("recommended"))), None)
    return [x["id"] for x in o["stops"]] if o else None


def accept_proposal(session_id: str, option: str | None) -> dict:
    """The user accepted the pending proposal (or picked option A / B of a comparison): it becomes the trip."""
    p = state.session(session_id).proposal
    if not p:
        return {"ok": False, "error": "no proposal is pending", "hint": "Handle the request normally."}
    ids = _proposal_ids(p, option)
    if not ids:
        return {"ok": False, "error": "pick option 'A' or 'B'", "hint": "Ask the user which one with ask_user."}
    title = p.get("title") or next(o["label"] for o in p["options"] if [x["id"] for x in o["stops"]] == ids)
    try:
        return _write(session_id, ids, f"{title}: " + " → ".join(_name(i) for i in ids))
    except ValueError as e:
        return _bad_ids(e)


def dismiss_proposal(session_id: str, reply: str) -> dict:
    """The user turned the pending proposal down by voice or text ("nahi, rehne do"): take it off the screen."""
    sess = state.session(session_id)
    reply = reply.strip()
    if not sess.proposal:
        return {"ok": False, "error": "no proposal is pending", "hint": "Answer with reply instead."}
    if not reply or len(reply) > MAX_REPLY_LEN:
        return {"ok": False, "error": f"reply must be 1-{MAX_REPLY_LEN} characters", "hint": "One short sentence."}
    sess.proposal = None
    state.set_display(session_id, proposal=None)
    return {"ok": True, "ends_turn": True, "reply": reply}


def proposal_click(session_id: str, action, option=None, index=None) -> str | None:
    """Buttons on the proposal card (no LLM): accept / decline / remove a proposed stop. Returns the headline."""
    sess = state.session(session_id)
    p = sess.proposal
    if not p:
        return None
    if action == "accept":
        r = accept_proposal(session_id, option if option in ("A", "B") else None)
        return r.get("line") if r.get("ok") else None
    if action == "decline":
        sess.proposal = None
        state.set_display(session_id, proposal=None)
        return "Kept your current trip."
    if action == "remove" and p["kind"] == "itinerary" and isinstance(index, int) and 0 <= index < len(p["stops"]):
        keep = [x for i, x in enumerate(p["stops"]) if i != index]
        if len(keep) < 2:
            return proposal_click(session_id, "decline")
        why = {x["id"]: x.get("why", "") for x in keep}
        info = _route_info([x["id"] for x in keep])
        for st in info["stops"]:
            st["why"] = why.get(st["id"], "")
        sess.proposal = {**p, **info}
        state.set_display(session_id, proposal=sess.proposal)
        return f"Removed {p['stops'][index]['name']} from the plan."
    return None


def _schema(properties: dict) -> dict:
    """Strict-mode schema: every property required, nothing extra (optional = may be null)."""
    return {"type": "object", "properties": properties, "required": list(properties),
            "additionalProperties": False}


_ANCHOR = {"type": ["string", "null"]}

TOOLS = {
    "resolve_place": {
        "fn": resolve_place,
        "description": "Turn place names (any spelling, aliases, colloquial names) into place ids. Pass every name "
                       f"from the request in ONE call (up to {MAX_LOOKUPS}). Returns one result per query: "
                       "{query, status: ok | ambiguous | not_found, matches: [{id, name}]}.",
        "parameters": _schema({
            "queries": {"type": "array", "items": {"type": "string"},
                        "description": "Place names as the user said them, e.g. ['Bombay', 'Banglore', 'Goa']."},
        }),
    },
    "suggest_enroute_places": {
        "fn": suggest_enroute_places,
        "description": "Show the user places from the known list that lie close to the road, as cards they can add "
                       "with one click (in travel order). Both ids null = every leg of the route; two stops of the "
                       "route = the legs between them (several legs: places per leg by its length, up to "
                       f"{MAX_SUGGESTIONS} in all); two places where the route doesn't go = up to {ENROUTE_LIMIT} "
                       "places on that road. Stops already on the trip are left out. Changes nothing on the trip "
                       "and ends your turn. If nothing is found you get an empty list and can try a larger range_km.",
        "parameters": _schema({
            "from_id": {"type": ["string", "null"],
                        "description": "Place id where the stretch of road starts, or null for the whole route."},
            "to_id": {"type": ["string", "null"],
                      "description": "Place id where it ends, or null for the whole route."},
            "range_km": {"type": ["integer", "null"],
                         "description": f"How far off the road a place may be, in km (5-{ENROUTE_MAX_KM}). "
                                        f"The distance the user gave, or null for {SUGGEST_DEFAULT_KM}."},
        }),
    },
    "suggest_nearby_places": {
        "fn": suggest_nearby_places,
        "description": f"Show the user up to {NEARBY_LIMIT} places from the known list around one place (straight "
                       "line), as cards they can add with one click, nearest first. Stops already on the trip are "
                       "left out. Changes nothing on the trip and ends your turn. If nothing is found you get an "
                       "empty list and can try a larger range_km.",
        "parameters": _schema({
            "place_id": {"type": "string", "description": "Id of the place to look around."},
            "range_km": {"type": ["integer", "null"],
                         "description": f"Search radius in km (5-{NEARBY_MAX_KM}). The distance the user gave, "
                                        f"or null for {SUGGEST_DEFAULT_KM}."},
        }),
    },
    "set_route": {
        "fn": set_route,
        "description": "Replace the whole route with these place ids in travel order (2 or more, or [] to clear). For a "
                       "new trip or a full rewrite, not for single adds, removes or reorders.",
        "parameters": _schema({
            "stops": {"type": "array", "items": {"type": "string"}, "description": "Ordered place ids."},
        }),
    },
    "add_stop": {
        "fn": add_stop,
        "description": "Add one stop. Give exactly one of `after` or `before` (the id of a stop on the route), or "
                       "position 'end'; the others null. To replace or move a stop, call remove_stop and then add_stop "
                       "(both in one step is fine: calls run in order).",
        "parameters": _schema({
            "place_id": {"type": "string", "description": "Id of the place to add."},
            "after": {**_ANCHOR, "description": "Id of the stop it goes right after, or null."},
            "before": {**_ANCHOR, "description": "Id of the stop it goes right before, or null."},
            "position": {"type": ["string", "null"], "enum": ["end", None],
                         "description": "'end' to add it as the last stop, or null."},
        }),
    },
    "remove_stop": {
        "fn": remove_stop,
        "description": "Remove one stop that is on the route. To replace or move a stop, call add_stop "
                       "after it (in the same step or the next).",
        "parameters": _schema({"place_id": {"type": "string", "description": "Id of the stop to remove."}}),
    },
    "undo": {
        "fn": undo,
        "description": "Restore an earlier route from history (also reverses a clear).",
        "parameters": _schema({"count": {"type": "integer", "description": "How many changes to go back (usually 1)."}}),
    },
    "ask_user": {
        "fn": ask_user,
        "description": f"Show a question card with {MIN_OPTIONS}-{MAX_OPTIONS} clickable options and end the turn. "
                       "Only when you cannot proceed safely (an ambiguous place, or something you cannot infer).",
        "parameters": _schema({
            "question": {"type": "string", "description": "One short question in the user's language."},
            "options": {"type": "array", "items": {"type": "string"},
                        "description": f"{MIN_OPTIONS}-{MAX_OPTIONS} short labels, e.g. 'Aurangabad, Bihar'."},
        }),
    },
    "plan_with_frontier": {
        "fn": plan_with_frontier,
        "description": "Ask a stronger planning model, which shows the user a proposal card (it does not change the "
                       "route; the user accepts it). mode 'itinerary': plan a whole trip when the user names no "
                       "stops (a region, a theme, 'koi nayi trip'), or revise a pending itinerary proposal. mode "
                       "'compare': which of two routes is better. Ends your turn; at most once per turn.",
        "parameters": _schema({
            "mode": {"type": "string", "enum": ["itinerary", "compare"]},
            "request": {"type": "string", "description": "The user's ask in one line, with any constraints "
                                                         "(days, region, budget, likes)."},
            "filler": {"type": "string", "description": "A short line shown and spoken while the planner works, "
                                                        "in the user's language, e.g. 'Ek minute, plan bana raha hoon'."},
            "route_a": {"type": ["array", "null"], "items": {"type": "string"},
                        "description": "compare: first route as place names in travel order, or null = the current "
                                       "trip. itinerary: null."},
            "route_b": {"type": ["array", "null"], "items": {"type": "string"},
                        "description": "compare: second route as place names in travel order. itinerary: null."},
        }),
    },
    "accept_proposal": {
        "fn": accept_proposal,
        "description": "Make the pending proposal the route, when the user accepts it or picks one of its options "
                       "('haan yahi', 'pehla wala', 'option B').",
        "parameters": _schema({
            "option": {"type": ["string", "null"], "enum": ["A", "B", None],
                       "description": "'A' or 'B' for a comparison; null for an itinerary."},
        }),
    },
    "dismiss_proposal": {
        "fn": dismiss_proposal,
        "description": "Take the pending proposal off the screen when the user turns it down ('nahi, rehne do', "
                       "'no thanks', 'current wala hi theek hai'). The route stays as it is. Ends your turn.",
        "parameters": _schema({
            "reply": {"type": "string", "description": "One short sentence in the user's language."},
        }),
    },
    "reply": {
        "fn": reply,
        "description": "Send a plain message and change nothing (refusals, errors to explain, answers). Ends the turn.",
        "parameters": _schema({"message": {"type": "string", "description": "One or two short sentences."}}),
    },
}


def openai_tools() -> list[dict]:
    """The registry in the OpenAI Responses API format."""
    return [{"type": "function", "name": name, "description": t["description"],
             "parameters": t["parameters"], "strict": True} for name, t in TOOLS.items()]


# ---------------------------------------------------------------- gateway

_JSON_TYPES = {"string": str, "array": list, "null": type(None), "object": dict, "boolean": bool, "integer": int}


def _validate(schema: dict, args) -> str | None:
    """Tiny validator for our flat schemas. Returns an error message or None."""
    if not isinstance(args, dict):
        return "arguments must be a JSON object"
    props = schema["properties"]
    extra = set(args) - set(props)
    missing = set(props) - set(args)
    if extra or missing:
        return f"expected exactly the arguments {sorted(props)}; missing {sorted(missing)}, unexpected {sorted(extra)}"
    for key, spec in props.items():
        types = spec["type"] if isinstance(spec["type"], list) else [spec["type"]]
        if not isinstance(args[key], tuple(_JSON_TYPES[t] for t in types)):
            return f"'{key}' must be {' or '.join(types)}"
        if spec["type"] == "array" and not all(isinstance(v, str) for v in args[key]):
            return f"'{key}' must be a list of strings"
    return None


def _step_label(name: str, args) -> str:
    """Short text for the UI while a tool runs. Built in code from the tool name, never by the model."""
    queries = args.get("queries") if name == "resolve_place" and isinstance(args, dict) else None
    place = ", ".join(str(n).strip() for n in queries)[:40] if isinstance(queries, list) else ""
    return {"resolve_place": f"Looking up {place}" if place else "Looking up a place",
            "suggest_enroute_places": "Finding places on the way", "suggest_nearby_places": "Finding places nearby",
            "set_route": "Drawing the route", "add_stop": "Adding a stop", "remove_stop": "Removing a stop",
            "undo": "Going back", "ask_user": "Asking you", "reply": "Writing a reply",
            "plan_with_frontier": "Asking the planner", "accept_proposal": "Using the plan",
            "dismiss_proposal": "Dropping the plan"}.get(name, name)


def _short(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


def _result_line(name: str, result: dict) -> str:
    """One line about a tool's result for the activity timeline. Built in code, never by the model."""
    if not result.get("ok"):
        return _short(result.get("error") or "failed", 90)
    if name == "resolve_place":
        return _short(", ".join(f"{r.get('query')}: {r.get('status')}" for r in result.get("results", [])), 90)
    if result.get("line"):
        return _short(result["line"], 90)
    if "reply" in result:
        return _short(result["reply"], 90)
    for key in ("places", "suggestions", "cards"):
        if isinstance(result.get(key), list):
            return f"{len(result[key])} places"
    return "ok"


def run_tool(session_id: str, name: str, raw_args: str) -> dict:
    """Validate -> run -> trace. Never raises: every failure becomes a structured error result."""
    try:
        args, problem = json.loads(raw_args or "{}"), None
    except json.JSONDecodeError as e:
        args, problem = None, f"arguments are not valid JSON ({e})"
    with span("tool", name, input=raw_args) as sp, \
            state.progress_step(session_id, _step_label(name, args), kind="tool", name=name,
                                detail=_short(raw_args or "", 110)) as ui:
        tool = TOOLS.get(name)
        if tool is None:
            result = {"ok": False, "error": f"unknown tool '{name}'", "hint": f"Use one of {sorted(TOOLS)}."}
        elif problem or (problem := _validate(tool["parameters"], args)):
            result = {"ok": False, "error": problem, "hint": "Fix the arguments and call again."}
        else:
            try:
                result = tool["fn"](session_id, **args)
            except Exception as e:   # a bug in a tool must not kill the turn
                result = {"ok": False, "error": f"internal error: {type(e).__name__}",
                          "hint": "Tell the user something went wrong and to try again."}
                sp["error"] = repr(e)
        sp["output"] = result
        ui["result"] = _result_line(name, result)
        if not result.get("ok"):
            ui["status"] = "error"
        elif name == "resolve_place":
            # Show the first name's best guess as a faint pin on the map while the model is still deciding.
            first = next((r for r in result["results"] if r["matches"]), None)
            if first:
                p = places.get(first["matches"][0]["id"])
                ui["pin"] = {"name": p["name"], "lat": p["lat"], "lon": p["lon"]}
        return result

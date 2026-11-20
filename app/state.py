"""Per-session trip state, held in memory.

The state dict is what app.js renders: {"trip": {...}, "ui": {...}} (shape: the State model below; stops carry `id` so the agent can refer
to them; legs may carry `path`, the road's shape).

Write paths:
- update_state(): the ONLY function that changes `trip` (stops/legs). Bumps version,
  keeps undo/redo history, and pushes the full state to whoever listens (the WebSocket in main.py).
- set_display(): changes only `ui` fields (heard, status, message, question, trace_id, tokens, heard_marks). No version bump.
- emit_progress(): live "what the agent is doing" events for the UI. Not trip state, never breaks a turn.
"""
import itertools
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field

from app import geo, llm, places

# TODO(db): trip state lives in memory only; a restart forgets every trip (no DB in V0).

# Road km comes from geo.leg() (a small graph of India's major roads). Car time is an estimate:
# long drives average ~55-60 km/h door to door (checked by hand against Google Maps).
CAR_KMH = 55
DAY_DRIVE_MIN = 12 * 60   # a leg longer than this is more than one long day of driving
MAX_HISTORY = 20      # undo depth
MAX_TURNS = 3         # past (user, reply) pairs the agent sees
DISPLAY_FIELDS = {"heard", "status", "message", "question", "trace_id", "tokens", "heard_marks", "suggestions", "followups",
                  "model", "models"}   # what set_display may change


@dataclass
class Session:
    state: dict
    history: list[dict] = field(default_factory=list)        # previous trips {stops}, for undo
    future: list[dict] = field(default_factory=list)         # trips undone, for redo (cleared by any new change)
    turns: list[tuple[str, str]] = field(default_factory=list)  # recent (user text, reply)
    listener: Callable[[dict], None] | None = None           # called with the full state on change
    on_progress: Callable[[dict], None] | None = None        # called with progress events (not trip state)
    current_text: str = ""                                   # the message of the turn now running
    pending_question: dict | None = None                     # {question, options: [{label, id}]} from ask_user
    last_mentioned: str | None = None                        # place id the user referred to most recently
    turn_wrote: bool = False                                 # this agent turn already saved an undo snapshot
    offered_legs: set[tuple[str, str]] = field(default_factory=set)   # (from_id, to_id) long legs already offered stops
    dismissed: set[str] = field(default_factory=set)         # place ids the user skipped (never suggested again)
    input_no: int = 0        # counts the user's messages; a late follow-up list checks it so it never shows up stale
    model: str = field(default_factory=lambda: llm.DEFAULT)  # llm.MODELS id the agent uses (the UI's switch)


_sessions: dict[str, Session] = {}


# ---- The state's shape. This is what app.js receives and renders. The models only check the shape
# (update_state() and set_display() validate before they save); the state itself stays a plain dict:
# {"trip": {...}, "ui": {...}}. `trip` changes only in update_state(); `ui` (DISPLAY_FIELDS) in set_display().

class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")     # nested items may carry extra keys (km, kind, ...)


class Spot(_Loose):                      # a point on the map
    name: str
    lat: float
    lon: float


class Stop(_Loose):
    id: str
    name: str
    state: str
    lat: float
    lon: float


class Leg(_Loose):
    km: float
    car_min: int
    train_min: int | None = None
    path: list[list[float]] | None = None    # the road's shape; absent = draw a straight line


class Question(_Loose):                  # the agent is waiting for an answer
    text: str
    options: list[str]
    spots: list[Spot | None]             # where options[i] is on the map, or None


class Suggestion(_Loose):                # a card in the side panel: a place the user can add with one click
    id: str
    name: str
    state: str
    note: str
    group: str = "On the way (long drive)"   # header the UI shows above cards of one search
    near: str | None = None              # nearby search: "✓ Add" puts it next to this place
    between: list[str] | None = None     # en-route search: "✓ Add" puts it between these two (if adjacent)
    lat: float | None = None
    lon: float | None = None


class HeardMark(_Loose):                 # a place found in `heard`: UI shows it bold + a ring on the map
    start: int
    end: int
    name: str
    lat: float
    lon: float


class Trip(BaseModel):
    """The trip itself. Changed only by update_state(); undo and redo restore it."""
    model_config = ConfigDict(extra="forbid")   # an unknown key is a bug

    version: int = 0                     # +1 on every trip change
    stops: list[Stop] = []
    legs: list[Leg] = []
    total_km: float = 0
    can_undo: bool = False
    can_redo: bool = False


class ModelChoice(BaseModel):
    id: str                              # "sarvam/gemma4"
    provider: str                        # "openai" | "sarvam"
    provider_label: str                  # "Sarvam"
    label: str                           # "Gemma 4 31B"
    note: str                            # price, shown as a tooltip


class UI(BaseModel):
    """What the screen shows right now. Changed by set_display() (and `message` by update_state()).
    Cleared on new input, never part of undo."""
    model_config = ConfigDict(extra="forbid")

    status: str = "idle"
    heard: str = ""
    message: str = ""
    question: Question | None = None
    suggestions: list[Suggestion] = []
    followups: list[str] = []            # things the user could say next (made by agent.followups)
    heard_marks: list[HeardMark] = []
    tokens: int = 0                      # LLM tokens used by this session so far (all turns)
    trace_id: str | None = None          # trace of the last finished turn (the UI's "Trace" link)
    session_id: str = ""                 # shown in the UI (copyable) so a test case can be referred to
    model: str = ""                      # llm.MODELS id this session's agent uses
    models: list[ModelChoice] = []       # what the switch + picker offer (only models whose API key is set)


class State(BaseModel):
    """The full message the browser gets: {"trip": {...}, "ui": {...}}."""
    model_config = ConfigDict(extra="forbid")

    trip: Trip = Field(default_factory=Trip)
    ui: UI = Field(default_factory=UI)


def check_state(state: dict) -> dict:
    """Raise pydantic.ValidationError if `state` doesn't match State; return it unchanged."""
    State.model_validate(state)
    return state


def empty_state() -> dict:
    return State().model_dump()


def session(session_id: str) -> Session:
    if session_id not in _sessions:
        _sessions[session_id] = Session(state=empty_state())
    return _sessions[session_id]


def get_state(session_id: str) -> dict:
    return session(session_id).state


def trip(session_id: str) -> dict:
    return session(session_id).state["trip"]


def ui(session_id: str) -> dict:
    return session(session_id).state["ui"]


def stop_ids(session_id: str) -> list[str]:
    return [s["id"] for s in trip(session_id)["stops"]]


haversine_km = geo.haversine_km   # straight-line km; lives in geo.py with the rest of the distance code


def is_round_trip(ids: list[str]) -> bool:
    """A round trip ends where it starts (A, B, ..., A) and has at least one other stop."""
    return len(ids) >= 3 and ids[0] == ids[-1]


def validate_stops(stop_ids_: list[str]) -> list[dict]:
    """Return the place dicts for the ids, or raise ValueError saying what to fix."""
    if not isinstance(stop_ids_, list) or not all(isinstance(i, str) for i in stop_ids_):
        raise ValueError("stops must be a list of place id strings")
    if len(stop_ids_) == 1:
        raise ValueError("a trip needs at least 2 stops (or 0 to clear it)")
    loop = is_round_trip(stop_ids_)
    inner = stop_ids_[:-1] if loop else stop_ids_       # a round trip repeats its start as the last stop, nothing else
    dupes = sorted({i for i in inner if inner.count(i) > 1})
    if dupes:
        raise ValueError(f"duplicate stops: {dupes}; each place can appear once (only a round trip may end "
                         f"at its start, with at least one stop in between)")
    unknown = [i for i in stop_ids_ if places.get(i) is None]
    if unknown:
        raise ValueError(f"unknown place ids: {unknown}; use ids returned by resolve_place")
    return [places.get(i) for i in stop_ids_]


def _snapshot(s: Session) -> dict:
    return {"stops": [x["id"] for x in s.state["trip"]["stops"]]}


def update_state(session_id: str, stops: list[str], reply: str | None = None, save_history: bool = True) -> dict:
    """Replace the trip with `stops` (place ids). Validates, computes legs, bumps version,
    saves the previous trip for undo, notifies the listener, returns the full state."""
    s = session(session_id)
    found = validate_stops(stops)          # raises before anything changes

    legs = []
    for a, b in zip(found, found[1:]):
        km, path = geo.leg(a, b)          # path: the road's shape for the map, or None (straight line)
        leg = {"km": km, "car_min": round(km / CAR_KMH * 60), "train_min": None}
        if path:
            leg["path"] = path
        legs.append(leg)

    if save_history:
        s.history = (s.history + [_snapshot(s)])[-MAX_HISTORY:]
        s.future = []

    new = {"trip": {**s.state["trip"],
                    "version": s.state["trip"]["version"] + 1,
                    "stops": [{"id": p["id"], "name": p["name"], "state": p["state"],
                               "lat": p["lat"], "lon": p["lon"]} for p in found],
                    "legs": legs,
                    "total_km": sum(l["km"] for l in legs),
                    "can_undo": bool(s.history), "can_redo": bool(s.future)},
           "ui": {**s.state["ui"],
                  "message": reply if reply is not None else " → ".join(p["name"] for p in found)}}
    s.state = check_state(new)
    _notify(s)
    return new


def undo(session_id: str, reply: str | None = None) -> dict:
    """Go back to the previous trip (redo can bring it back). Writes through update_state()."""
    s = session(session_id)
    if not s.history:
        raise ValueError("nothing to undo")
    previous, now = s.history.pop(), _snapshot(s)
    s.future.append(now)
    return update_state(session_id, previous["stops"], reply, save_history=False)


def redo(session_id: str, reply: str | None = None) -> dict:
    """Bring back the trip the last undo took away. Writes through update_state()."""
    s = session(session_id)
    if not s.future:
        raise ValueError("nothing to redo")
    nxt = s.future.pop()
    s.history = (s.history + [_snapshot(s)])[-MAX_HISTORY:]
    return update_state(session_id, nxt["stops"], reply, save_history=False)


def set_display(session_id: str, **fields) -> dict:
    """Change the display fields only (not the trip). Notifies the listener."""
    bad = set(fields) - DISPLAY_FIELDS
    if bad:
        raise ValueError(f"set_display cannot change {bad}")
    s = session(session_id)
    s.state = check_state({**s.state, "ui": {**s.state["ui"], **fields}})
    _notify(s)
    return s.state


def emit_progress(session_id: str, event: dict) -> None:
    """Tell the listener what the agent is doing right now. Fire and forget: errors are swallowed."""
    s = session(session_id)
    if s.on_progress:
        try:
            s.on_progress({"type": "progress", **event})
        except Exception as e:
            print(f"[state] progress listener failed: {e!r}")


_step_ids = itertools.count(1)


@contextmanager
def progress_step(session_id: str, label: str):
    """Show one step in the UI: 'running' now, 'done' (or 'error') when the block ends.
    The block may set info["label"], info["pin"] ({name, lat, lon}) or info["status"] before it ends."""
    info = {"id": f"s{next(_step_ids)}", "label": label, "status": "done", "pin": None}
    emit_progress(session_id, {"event": "step", **info, "status": "running"})
    try:
        yield info
    except Exception:
        info["status"] = "error"
        raise
    finally:
        emit_progress(session_id, {"event": "step", **info})


def drop_session(session_id: str) -> None:
    """Forget a session (called when its WebSocket closes)."""
    _sessions.pop(session_id, None)


def add_turn(session_id: str, user_text: str, reply: str) -> None:
    s = session(session_id)
    s.turns = (s.turns + [(user_text, reply)])[-MAX_TURNS:]


def _notify(s: Session) -> None:
    if s.listener:
        try:
            s.listener(s.state)
        except Exception as e:   # a dead socket must not break the turn
            print(f"[state] listener failed: {e!r}")

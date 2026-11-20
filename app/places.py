"""Known places and name resolution.

Two halves:
- build(): ONE-TIME script. Reads raw_data/top_cities.csv (big cities + famous tourist places)
  and the GeoNames dump, writes data/places.json. Run with:  python -m app.places build
- resolve(): RUNTIME. Loads only data/places.json (small). Names not in it return [].
  (A runtime fallback to the full GeoNames dump was removed: it needed ~375 MB of RAM.)

Scores (0-100) returned by resolve():
  100       exact name/alias
  75-99     fuzzy (typo) match
"""
import csv
import json
import re
import sys
import unicodedata
from pathlib import Path

from rapidfuzz import fuzz, process

ROOT = Path(__file__).resolve().parent.parent
PLACES_PATH = ROOT / "data" / "places.json"
GEONAMES_PATH = ROOT / "raw_data" / "IN" / "IN.txt"
CITIES_CSV = ROOT / "raw_data" / "top_cities.csv"

FUZZY_CUTOFF = 75  # rapidfuzz ratio (0-100); below this a name is "not close"

# Places abroad that people plan trips to. With ~1,000 Indian towns in the list, their spellings come close to some
# small town ("Pokhara" -> Pokaran, "Doha" -> Dahod, "Dubai" -> Dhubri), so they are never typo-matched: resolve()
# returns [] unless the name is an exact name/alias in the list.
FOREIGN = {
    "kathmandu", "pokhara", "lumbini", "chitwan", "nagarkot", "biratnagar", "janakpur", "nepal",
    "thimphu", "paro", "punakha", "phuentsholing", "bhutan",
    "colombo", "kandy", "galle", "jaffna", "ella", "sri lanka", "srilanka",
    "dhaka", "chittagong", "sylhet", "coxs bazar", "bangladesh",
    "karachi", "lahore", "rawalpindi", "peshawar", "multan", "pakistan",
    "male", "maldives", "myanmar", "yangon", "mandalay", "tibet", "lhasa", "china", "beijing",
    "dubai", "abu dhabi", "sharjah", "doha", "qatar", "muscat", "oman", "riyadh", "jeddah", "mecca", "makkah",
    "medina", "kuwait", "bahrain", "tehran", "kabul",
    "singapore", "bangkok", "phuket", "pattaya", "thailand", "bali", "indonesia", "kuala lumpur", "malaysia",
    "hanoi", "vietnam", "london", "paris", "new york", "tokyo", "sydney", "toronto", "berlin", "rome",
}
MAX_CANDIDATES = 3

# GeoNames feature codes we accept, most important first. Used to rank same-name places
# (e.g. three places called Kasol): capital > admin seat > town/village > tourist spot.
FEATURE_RANK = {"PPLC": 0, "PPLA": 1, "PPLA2": 2, "PPLA3": 3, "PPLA4": 4, "PPL": 5, "PPLX": 6,
                "PPLL": 7, "PPLS": 7}
TOURISM_CODES = {"FT", "RUIN", "MNMT", "PAL", "HSTS", "RSRT", "TMPL",   # forts, ruins, palaces…
                 "ANS", "RLG",                                          # ancient / religious sites
                 "PASS", "VAL", "BCH", "PRK", "LK"}                    # passes, valleys, lakes…

# GeoNames still files some places under the state they belonged to before a split
# (e.g. Bhimtal is coded Uttar Pradesh, not Uttarakhand). When building, a place may also come
# from its paired state.
STATE_SPLITS = [("Uttarakhand", "Uttar Pradesh"), ("Jharkhand", "Bihar"),
                ("Chhattisgarh", "Madhya Pradesh"), ("Telangana", "Andhra Pradesh"),
                ("Ladakh", "Jammu and Kashmir")]


def norm(text: str) -> str:
    """Lowercase, strip accents (Rājasthān -> rajasthan), keep only letters/digits/single spaces."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def rank_key(place: dict) -> tuple:
    """Sort key for same-name places: better feature code first, then bigger population."""
    return (FEATURE_RANK.get(place["feature_code"], 8), -place["population"])


def _good_alias(alt: str) -> bool:
    """Keep Latin-script alternate names people would type; drop codes and non-place labels."""
    if not alt or len(alt) < 3 or not re.fullmatch(r"[A-Za-z .'\-]+", alt):
        return False
    if alt.isupper() and len(alt) <= 4:          # airport/station codes like BOM, IXJ
        return False
    return not re.search(r"airport|station|district|junction|cantonment", alt, re.I)


def _rough(lat: str, lon: str) -> bool:
    """Coordinates with 2 decimals or fewer (17.56, 73.4) come from GeoNames' old, imprecise imports, and are
    often a different place: that row put Munsiyari on a glacier 60 km away and Mahabaleshwar 45 km off."""
    return max(len(s.partition(".")[2]) for s in (lat, lon)) <= 2


def build_key(row: dict) -> tuple:
    """Which GeoNames row to take when several share a name in one state: precise coordinates first, then the
    same order as rank_key (feature code, then population)."""
    return (row["rough"], *rank_key(row))


def _km(a: tuple, b: tuple) -> float:
    """Great-circle km between two (lat, lon) points (build only; geo.py has its own)."""
    import math
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


def _clean_state(admin1_ascii: str) -> str:
    """'State of Rajasthan' -> 'Rajasthan', 'National Capital Territory of Delhi' -> 'Delhi'."""
    return re.sub(r"^(State of|Union Territory of|National Capital Territory of) ", "", admin1_ascii)


# ---------------------------------------------------------------- GeoNames reader (build only)

def read_geonames(keep_alts: bool = False) -> tuple[dict[str, list[dict]], dict[str, str]]:
    """One pass over IN.txt. Returns
       index:  normalised name/alias -> [place dicts]   (populated places + tourist spots)
       states: admin1 code -> clean state name
    Column layout is documented in raw_data/IN/readme.txt (see geonames.py)."""
    csv.field_size_limit(10**7)
    index: dict[str, list[dict]] = {}
    states: dict[str, str] = {}
    with open(GEONAMES_PATH, encoding="utf-8") as f:
        for r in csv.reader(f, delimiter="\t", quoting=csv.QUOTE_NONE):
            fclass, fcode = r[6], r[7]
            if fcode == "ADM1":
                states[r[10]] = _clean_state(r[2])
                continue
            if not (fcode in FEATURE_RANK or fcode in TOURISM_CODES):
                continue
            place = {"gid": int(r[0]), "name": r[2] or r[1], "admin1": r[10],   # ASCII name: "Rampur"
                     "lat": round(float(r[4]), 4), "lon": round(float(r[5]), 4),
                     "feature_code": fcode, "population": int(r[14] or 0),
                     "rough": _rough(r[4], r[5])}
            alts = [a for a in r[3].split(",") if _good_alias(a)]
            if keep_alts:
                place["alts"] = alts
            for key in {norm(r[1]), norm(r[2]), *map(norm, alts)}:
                if key:
                    index.setdefault(key, []).append(place)
    return index, states


# ---------------------------------------------------------------- runtime

_places: list[dict] | None = None          # places from data/places.json
_by_id: dict[str, dict] = {}               # id -> place
_alias_index: dict[str, list[int]] = {}    # normalised alias -> indexes into _places
_alias_list: list[str] = []


def load() -> list[dict]:
    global _places, _alias_list
    if _places is None:
        _places = json.loads(PLACES_PATH.read_text())
        for i, p in enumerate(_places):
            _by_id[p["id"]] = p
            for a in {norm(p["name"]), *p["aliases"]}:
                _alias_index.setdefault(a, []).append(i)
        _alias_list = list(_alias_index)
    return _places


def get(place_id: str) -> dict | None:
    load()
    return _by_id.get(place_id)


def resolve(name: str, state_hint: str | None = None) -> list[dict]:
    """Return up to 3 candidates [{id, name, state, score}], best first, or [] if nothing is close.

    1. exact match on a name/alias -> only those (score 100)
    2. otherwise fuzzy matches (score = similarity, 75-99)
    Ties are broken by: matches state_hint, then feature code, then population.
    """
    places = load()
    q = norm(name)
    if not q:
        return []

    best: dict[str, tuple[dict, int]] = {}   # place id -> (place, best score)

    def add(place: dict, score: int) -> None:
        if score > best.get(place["id"], (None, -1))[1]:
            best[place["id"]] = (place, score)

    for i in _alias_index.get(q, []):
        add(places[i], 100)
    if not best and q in FOREIGN:
        return []
    if not best:
        for alias, score, _ in process.extract(q, _alias_list, scorer=fuzz.ratio,
                                               score_cutoff=FUZZY_CUTOFF, limit=20):
            for i in _alias_index[alias]:
                add(places[i], min(99, round(score)))

    hint = norm(state_hint or "")

    def in_hinted_state(p: dict) -> bool:
        # "himachal" or "himachal pradesh" (or a small typo) -- but not "madhya pradesh"
        st = norm(p["state"])
        return bool(hint) and (st.startswith(hint) or fuzz.ratio(hint, st) >= 85)

    order = sorted(best.values(), key=lambda ps: (-ps[1], not in_hinted_state(ps[0]), rank_key(ps[0])))
    return [{"id": p["id"], "name": p["name"], "state": p["state"], "score": s}
            for p, s in order[:MAX_CANDIDATES]]


def in_state(name: str, limit: int = 4) -> list[dict]:
    """If `name` is an Indian state or region ("Himachal", "Kerala", "Bengal"), its best places (same order as
    resolve's tie-break: feature code, then population), as [{id, name, state}]. Else []."""
    load()
    q = norm(name)
    if len(q) < 4:
        return []
    states ={p["state"] for p in _by_id.values()}
    hits = [s for s in states if norm(s) == q or norm(s).startswith(q + " ") or norm(s).endswith(" " + q)]
    if len(hits) != 1:            # "Pradesh" names several states: not a region the user meant
        return []
    found = sorted((p for p in _by_id.values() if p["state"] == hits[0]), key=rank_key)[:limit]
    return [{"id": p["id"], "name": p["name"], "state": p["state"]} for p in found]


def mentioned(place_id: str, text: str) -> bool:
    """Is this place named anywhere in `text`, by any of its names (small misspellings allowed)?"""
    p, words = _by_id.get(place_id), norm(text).split()
    if p is None:
        return False
    for name in {norm(p["name"])} | set(p.get("aliases", [])):
        k = len(name.split())          # compare whole words: "pune" must not match "punjab"
        for i in range(len(words) - k + 1):
            if fuzz.ratio(name, " ".join(words[i:i + k])) >= 85:
                return True
    return False


_name_index: dict[str, str] = {}      # normalized name or alias -> place id (the biggest place wins)
# Town names that are also everyday words ("chala gaya", "sabzi mandi"): not highlighted the moment they are heard.
# The highlight is display-only; if the town ends up in the trip, the end-of-turn marks (find_spans) still find it.
EVERYDAY_WORDS = {"gaya", "mandi", "mon"}


def find_exact(text: str) -> list[dict]:
    """Every place named in `text` by its exact name or alias (no typo matching), as [{"id","start","end"}].
    Needs no LLM, so the UI can highlight places the moment the words are heard. A rare exact-name
    collision with an ordinary word is possible, so aliases must be 4+ letters."""
    load()
    if not _name_index:
        for p in sorted(_by_id.values(), key=lambda p: p.get("population") or 0):   # biggest written last
            for name in {norm(p["name"])} | {a for a in p.get("aliases", []) if len(a) >= 4}:
                if name not in EVERYDAY_WORDS:
                    _name_index[name] = p["id"]
    words = [(m.start(), m.end(), norm(m.group())) for m in re.finditer(r"\w+", text)]
    found, i = [], 0
    while i < len(words):
        for k in (3, 2, 1):                             # longest name first: "mount abu" before "abu"
            pid = _name_index.get(" ".join(w[2] for w in words[i:i + k])) if i + k <= len(words) else None
            if pid:
                found.append({"id": pid, "start": words[i][0], "end": words[i + k - 1][1]})
                i += k
                break
        else:
            i += 1
    return found


def find_spans(text: str, place_ids: list[str]) -> list[dict]:
    """Where each of these places is named in `text`: [{"id", "start", "end"}] as character offsets,
    one (the best) per place. Same fuzzy rule as mentioned(); callers pass only a few likely ids
    (the trip's stops), so a loose match can't pick up some unrelated place."""
    load()
    words = [(m.start(), m.end(), norm(m.group())) for m in re.finditer(r"\w+", text)]
    found = []
    for pid in dict.fromkeys(place_ids):
        p = _by_id.get(pid)
        if p is None:
            continue
        best = None                                   # (score, start, end)
        for name in {norm(p["name"])} | set(p.get("aliases", [])):
            k = len(name.split())
            for i in range(len(words) - k + 1):
                score = fuzz.ratio(name, " ".join(w[2] for w in words[i:i + k]))
                if score >= 85 and (best is None or score > best[0]):
                    best = (score, words[i][0], words[i + k - 1][1])
        if best:
            found.append({"id": pid, "start": best[1], "end": best[2]})
    return found


# ---------------------------------------------------------------- one-time build

HINT_KM = 30   # a CSV row with lat/lon takes the GeoNames row nearest to that point, if one is this close


def _find_in_geonames(index, state_code, keys: list[str], state: str, near: tuple | None = None) -> dict | None:
    """The GeoNames row for any of `keys`.
    With `near` (lat, lon from the CSV): the matching row closest to that point, within HINT_KM, in any state
    (picks the right one of several same-name villages). Else: the best row (build_key) in `state`, or, failing
    that, its split pair."""
    if near:
        rows = [(_km(near, (g["lat"], g["lon"])), g) for k in keys for g in index.get(norm(k), [])]
        rows = [(d, g) for d, g in rows if d <= HINT_KM]
        return min(rows, key=lambda dg: (dg[0], build_key(dg[1])))[1] if rows else None
    pair = {a: b for a, b in STATE_SPLITS} | {b: a for a, b in STATE_SPLITS}
    for st in (state, pair.get(state)):
        code = state_code.get(norm(st or ""))
        rows = [g for k in keys for g in index.get(norm(k), []) if code and g["admin1"] == code]
        if rows:
            return min(rows, key=build_key)   # same-name places in the state: precise, then best rank
    return None


def build() -> None:
    """CSV columns: city, state, lat, lon, aliases (';'-separated). lat/lon, when filled, say where the place is:
    the GeoNames row nearest to that point is used (so the right one of several same-name places is taken), or,
    if GeoNames has none within HINT_KM, the CSV point itself. Aliases are names people say AND extra GeoNames
    search keys. Rows whose name repeats an earlier row's (another state) get the state added to their id."""
    index, states = read_geonames(keep_alts=True)
    state_code = {norm(name): code for code, name in states.items()}

    with open(CITIES_CSV, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    places, missing, manual = [], [], []
    for row in rows:
        city, state = row["city"].strip(), row["state"].strip()
        extra = [a.strip() for a in (row.get("aliases") or "").split(";") if a.strip()]
        near = (float(row["lat"]), float(row["lon"])) if row.get("lat") else None
        g = _find_in_geonames(index, state_code, [city, *extra], state, near)
        if g:
            place = {"lat": g["lat"], "lon": g["lon"], "population": g["population"],
                     "feature_code": g["feature_code"], "gid": g["gid"], "source": "geonames",
                     "aliases": {norm(g["name"]), *map(norm, g["alts"])}}
        elif row.get("lat"):
            manual.append(city)
            place = {"lat": float(row["lat"]), "lon": float(row["lon"]), "population": 0,
                     "feature_code": "PPL", "gid": None, "source": "manual", "aliases": set()}
        else:
            missing.append(f"{city} ({state})")
            continue
        aliases = sorted((place.pop("aliases") | {norm(city), *map(norm, extra)}) - {""})
        places.append({"id": norm(city).replace(" ", "-"), "name": city, "state": state,
                       **place, "aliases": aliases, "_csv_aliases": {norm(a) for a in extra}})

    # Same city name twice in the CSV (different states): the first row keeps the plain id ("bilaspur"), later
    # ones get the state ("bilaspur-himachal-pradesh"). So adding a second Bilaspur never changes the first one's
    # id, which old traces, evals and shared links use.
    seen: set[str] = set()
    for p in places:
        if p["id"] in seen:
            p["id"] += "-" + norm(p["state"]).replace(" ", "-")
        seen.add(p["id"])

    # An alias equal to ANOTHER place's own name, or to an alias the CSV gives another place, is
    # noise (e.g. a suburb listed as 'Delhi'; GeoNames lists 'Goa' for Mormugao, the CSV says Panaji).
    own: dict[str, set] = {}
    for p in places:
        for a in {norm(p["name"]), *p.pop("_csv_aliases")}:
            own.setdefault(a, set()).add(p["id"])
    for p in places:
        p["aliases"] = [a for a in p["aliases"] if p["id"] in own.get(a, {p["id"]})]

    PLACES_PATH.parent.mkdir(exist_ok=True)
    PLACES_PATH.write_text(json.dumps(places, indent=1, ensure_ascii=False))
    print(f"wrote {len(places)} places to {PLACES_PATH}")
    print(f"manual coordinates from the CSV ({len(manual)}): {manual}")
    print(f"NOT FOUND, skipped ({len(missing)}): {missing}")


if __name__ == "__main__":
    if sys.argv[1:] == ["build"]:
        build()
    else:
        for n in sys.argv[1:]:
            print(n, "->", resolve(n))

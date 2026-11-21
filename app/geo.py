"""Road routing and stop suggestions on a graph of India's roads.

Data: OpenStreetMap (© OpenStreetMap contributors, ODbL), India extract from Geofabrik: motorways, trunk,
primary and secondary roads (and their link roads). Junctions are graph nodes; each edge keeps the road's
shape between two junctions, so a route line follows the real road. Distances come out close to Google's
(roads smaller than secondary are missing, and a town is joined to the nearest road with a straight piece).

Runtime (no extra dependencies):
    leg(a, b)                  -> (road km, path [[lat, lon], ...] or None)   used by state.update_state()
    enroute(a, b, limit)       -> places close to the road from a to b, best first, in travel order
                                  (the agent's suggest_enroute_places tool)
    suggest(a, b, n)           -> up to n of those, spread along the drive     the long-drive suggestion cards
    nearby(place, km, limit)   -> places within km (straight line) of one place  (suggest_nearby_places)

One-time build (needs `pip install osmium shapely`, which are NOT runtime dependencies):
    python -m app.geo build
    in:  raw_data/osm/india-latest.osm.pbf (download.geofabrik.de/asia/india-latest.osm.pbf), static/india.geojson
    out: data/roads.json (the graph), data/roads.bin (the same, packed) and the map layers static/roads-core.geojson (backbone highways, coarse),
         static/roads-overview.geojson (main highways), static/roads.geojson (all national highways),
         static/roads-main.geojson (primary), static/roads-detail.geojson (secondary)
    python -m app.geo pack     data/roads.json -> data/roads.bin (the packed copy the server loads; `build` does it too)
    python -m app.geo layers   rebuilds only the map layers, from the cached roads (+ highway numbers, cached
                               in raw_data/osm/highway-refs.json on the first run, ~3 min)
"""
import heapq
import json
import math
import re
import struct
import sys
from array import array
from bisect import bisect_left
from functools import lru_cache
from pathlib import Path

from app import places

ROOT = Path(__file__).resolve().parent.parent
ROADS_PATH = ROOT / "data" / "roads.json"        # the graph as the build writes it (readable)
ROADS_BIN = ROOT / "data" / "roads.bin"          # the same graph packed for the server (python -m app.geo pack)
OSM_PATH = ROOT / "raw_data" / "osm" / "india-latest.osm.pbf"
# Map layers, so the browser shows only the important roads when zoomed out and more as you zoom in (like Google).
# Map ranks (not the routing ranks):
#   0 = the backbone: expressways + the BACKBONE_COUNT longest national highways (NH 44, 27, 48, 52, 16, …)
#   1 = other main national highways (1-2 digit numbers; in India's numbering the 3-digit ones are branches)
#   2 = other national highways / trunk roads,  3 = primary roads,  4 = secondary roads
# (file, map ranks, simplify tolerance in degrees, drop pieces shorter than km)
# Short pieces are only dropped a little (2 km): merged lines break at every junction, so a bigger cut-off left
# whole stretches of highway out and the rest looked like dashes.
LAYERS = [(ROOT / "static" / "roads-core.geojson", (0,), 0.015, 2),          # backbone, coarse: all of India
          (ROOT / "static" / "roads-overview.geojson", (0, 1), 0.008, 2),    # main highways: a long trip
          (ROOT / "static" / "roads.geojson", (0, 1, 2), 0.004, 0),          # all national highways: a few states
          (ROOT / "static" / "roads-main.geojson", (3,), 0.006, 3),          # primary roads: a region
          (ROOT / "static" / "roads-detail.geojson", (4,), 0.008, 5)]        # secondary roads: a district
BACKBONE_COUNT = 18
MAIN_NH = re.compile(r"\b(?:NH|AH|NE)[\s-]*(\d+)", re.I)   # "NH 44", "NH-48", "AH1", "NE 4" (national expressway)
TILE_DEG = 2   # lines are grouped into 2°×2° tiles, one map object each: Leaflet redraws ~100 objects, not ~14,000
INDIA_PATH = ROOT / "static" / "india.geojson"

ROAD_FACTOR = 1.25     # fallback when there is no road (islands): road km ≈ straight line × 1.25
SNAP_MAX_KM = 150      # a place further than this from any road gets the straight-line fallback
PATH_MAX_POINTS = 600  # points per leg sent to the browser
CELL = 0.1             # degrees: grid cell for finding the nearest road node
# Routes go by travel time, so they prefer expressways and highways like Google does (typical car speeds).
SPEED_KMH = {"motorway": 95, "trunk": 70, "primary": 50}

# The graph lives in packed arrays (4 bytes per number), not Python tuples and floats (24+ bytes each): the same
# 118k nodes and 170k edges take ~15 MB instead of ~175 MB. The server reads data/roads.bin straight into them.
_lon = array("f")          # node -> longitude
_lat = array("f")          # node -> latitude
_e_from = array("i")       # edge -> its two end nodes
_e_to = array("i")
_e_km = array("f")         # edge -> km
_e_min = array("f")        # edge -> driving minutes
_e_off = array("i")        # edge k's shape (between the two end nodes) is _shape[_e_off[k]:_e_off[k+1]] = lon, lat, lon, lat, ...
_shape = array("f")
_adj_off = array("i")      # node x's neighbours are entries _adj_off[x]:_adj_off[x+1] of the two arrays below
_adj_to = array("i")       # ... the other node
_adj_edge = array("i")     # ... and the edge used
_grid_keys = array("i")    # grid cells that have nodes, sorted (key = cell x * 10000 + cell y)
_grid_off = array("i")     # the nodes of cell number c are _grid_nodes[_grid_off[c]:_grid_off[c+1]]
_grid_nodes = array("i")
_FIELDS = ["_lon", "_lat", "_e_from", "_e_to", "_e_km", "_e_min", "_e_off", "_shape",
           "_adj_off", "_adj_to", "_adj_edge", "_grid_keys", "_grid_off", "_grid_nodes"]
MAGIC = b"RPG1"


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle (straight-line) distance between two lat/lon points, in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    h = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def _cell(lon: float, lat: float) -> tuple[int, int]:
    return int(math.floor(lon / CELL)), int(math.floor(lat / CELL))


def _cell_key(lon: float, lat: float) -> int:
    cx, cy = _cell(lon, lat)
    return cx * 10000 + cy


def pack() -> None:
    """data/roads.json -> data/roads.bin: the graph as raw arrays, so the server loads it with no JSON parsing
    (parsing the 8.6 MB file needs ~200 MB of memory at its peak). Run by `build`; run it alone after editing roads.json."""
    data = json.loads(ROADS_PATH.read_text())
    nodes, edges = data["nodes"], data["edges"]
    a = {name: array(globals()[name].typecode) for name in _FIELDS}
    for lon, lat in nodes:
        a["_lon"].append(lon)
        a["_lat"].append(lat)
    a["_e_off"].append(0)
    degree = [0] * len(nodes)
    for i, j, km, minutes, shape in edges:
        a["_e_from"].append(i)
        a["_e_to"].append(j)
        a["_e_km"].append(km)
        a["_e_min"].append(minutes)
        a["_shape"].extend(shape)
        a["_e_off"].append(len(a["_shape"]))
        degree[i] += 1
        degree[j] += 1
    a["_adj_off"].append(0)
    for d in degree:
        a["_adj_off"].append(a["_adj_off"][-1] + d)
    fill = list(a["_adj_off"][:-1])
    to, used = [0] * a["_adj_off"][-1], [0] * a["_adj_off"][-1]
    for k, (i, j, *_) in enumerate(edges):              # same order as the old lists: edge by edge, both ends
        to[fill[i]], used[fill[i]] = j, k
        fill[i] += 1
        to[fill[j]], used[fill[j]] = i, k
        fill[j] += 1
    a["_adj_to"].extend(to)
    a["_adj_edge"].extend(used)
    by_cell = sorted(range(len(nodes)), key=lambda x: _cell_key(a["_lon"][x], a["_lat"][x]))
    a["_grid_off"].append(0)
    last = None
    for x in by_cell:
        key = _cell_key(a["_lon"][x], a["_lat"][x])     # from the stored (float32) position, as at runtime
        if key != last:
            if last is not None:
                a["_grid_off"].append(len(a["_grid_nodes"]))
            a["_grid_keys"].append(key)
            last = key
        a["_grid_nodes"].append(x)
    a["_grid_off"].append(len(a["_grid_nodes"]))
    with open(ROADS_BIN, "wb") as f:
        f.write(MAGIC)
        for name in _FIELDS:
            f.write(struct.pack("<II", a[name].itemsize, len(a[name])))
            a[name].tofile(f)
    print(f"packed: {len(nodes)} nodes, {len(edges)} edges -> {ROADS_BIN} ({ROADS_BIN.stat().st_size / 1e6:.1f} MB)")


def load() -> None:
    if len(_lon):
        return
    if not ROADS_BIN.exists():
        if not ROADS_PATH.exists():
            return
        pack()
    with open(ROADS_BIN, "rb") as f:
        if f.read(4) != MAGIC:
            raise ValueError(f"{ROADS_BIN} is not a packed road graph: run python -m app.geo pack")
        for name in _FIELDS:
            size, count = struct.unpack("<II", f.read(8))
            if size != globals()[name].itemsize:
                raise ValueError(f"{ROADS_BIN} was packed on another machine type: run python -m app.geo pack")
            globals()[name].fromfile(f, count)


def _nodes_in_cell(cx: int, cy: int):
    c = bisect_left(_grid_keys, cx * 10000 + cy)
    if c < len(_grid_keys) and _grid_keys[c] == cx * 10000 + cy:
        return _grid_nodes[_grid_off[c]:_grid_off[c + 1]]
    return ()


def _snap(lat: float, lon: float) -> tuple[int, float]:
    """Nearest road node and its distance in km (search the grid ring by ring around the point)."""
    cx, cy = _cell(lon, lat)
    best, best_km = -1, math.inf
    for r in range(int(SNAP_MAX_KM / (111 * CELL)) + 2):
        if best >= 0 and (r - 1) * CELL * 111 * math.cos(math.radians(lat)) > best_km:
            break                                          # nothing in a farther ring can be closer
        for dx in range(-r, r + 1):
            for dy in range(-r, r + 1):
                if max(abs(dx), abs(dy)) != r:
                    continue
                for i in _nodes_in_cell(cx + dx, cy + dy):
                    km = haversine_km(lat, lon, _lat[i], _lon[i])
                    if km < best_km:
                        best, best_km = i, km
    return best, best_km


def _shortest(src: int, dst: int) -> tuple[float, list[tuple[int, int]]] | None:
    """Fastest route by A* on the road graph (each edge costs its driving minutes; the guide is the straight line
    at the top speed, so it never overestimates). Returns (km, [(node, edge used to reach it), ...])."""
    glon, glat = _lon[dst], _lat[dst]
    fastest = max(SPEED_KMH.values()) / 60                # km per minute
    guess = lambda i: haversine_km(_lat[i], _lon[i], glat, glon) / fastest
    cost, km_to, prev, todo, done = {src: 0.0}, {src: 0.0}, {}, [(guess(src), src)], set()
    while todo:
        _, x = heapq.heappop(todo)
        if x == dst:
            steps = [(dst, prev[dst][1])] if dst in prev else []
            while steps and steps[-1][0] != src:
                node = prev[steps[-1][0]][0]
                steps.append((node, prev[node][1] if node in prev else -1))
            return km_to[dst], steps[::-1]
        if x in done:
            continue
        done.add(x)
        for p in range(_adj_off[x], _adj_off[x + 1]):
            y, k = _adj_to[p], _adj_edge[p]
            km, c = _e_km[k], cost[x] + _e_min[k]
            if c < cost.get(y, math.inf):
                cost[y], km_to[y], prev[y] = c, km_to[x] + km, (x, k)
                heapq.heappush(todo, (c + guess(y), y))
    return None


def _edge_points(k: int, start: int) -> list[tuple[float, float]]:
    """The shape of edge k as (lat, lon) points, starting from node `start`, without either end node."""
    pts = [(_shape[n + 1], _shape[n]) for n in range(_e_off[k], _e_off[k + 1], 2)]
    return pts if start == _e_from[k] else pts[::-1]


@lru_cache(maxsize=256)       # each entry holds up to 600 points: a big cache would grow to hundreds of MB
def _leg_by_id(a_id: str, b_id: str) -> tuple[int, tuple | None]:
    a, b = places.get(a_id), places.get(b_id)
    straight = haversine_km(a["lat"], a["lon"], b["lat"], b["lon"])
    fallback = (round(straight * ROAD_FACTOR), None)
    load()
    if not len(_lon):
        return fallback
    (sa, da), (sb, db) = _snap(a["lat"], a["lon"]), _snap(b["lat"], b["lon"])
    if da > SNAP_MAX_KM or db > SNAP_MAX_KM or sa == sb:
        return fallback
    found = _shortest(sa, sb)
    if found is None:
        return fallback
    km, steps = found
    pts = [(a["lat"], a["lon"]), (_lat[sa], _lon[sa])]
    for (node, _), (nxt, k) in zip(steps, steps[1:]):
        pts += _edge_points(k, node) + [(_lat[nxt], _lon[nxt])]
    pts.append((b["lat"], b["lon"]))
    step = max(1, math.ceil(len(pts) / PATH_MAX_POINTS))
    pts = pts[:-1:step] + [pts[-1]]
    return round(km + da + db), tuple((round(lat, 4), round(lon, 4)) for lat, lon in pts)


def leg(a: dict, b: dict) -> tuple[int, list | None]:
    """Road km and the road's shape for one leg. Path is None when the fallback (straight line) was used."""
    km, path = _leg_by_id(a["id"], b["id"])
    return km, [list(p) for p in path] if path else None


def _score(p: dict, km: float) -> float:
    """Bigger towns rank higher (better stops), every 25 km away costs one "order of magnitude" of population."""
    return math.log10((p.get("population") or 0) + 1000) - km / 25


def _route_frame(a_id: str, b_id: str):
    """The road from a to b in a flat km grid: (points [(x, y)], cumulative km at each point, total km,
    kx = km per degree of longitude, scale = road km per path km (the path is simplified))."""
    a, b = places.get(a_id), places.get(b_id)
    km, path = _leg_by_id(a_id, b_id)
    pts = list(path) if path else [(a["lat"], a["lon"]), (b["lat"], b["lon"])]
    kx = 111 * math.cos(math.radians((a["lat"] + b["lat"]) / 2))   # around the route's middle latitude
    xy = [(lon * kx, lat * 111) for lat, lon in pts]
    cum = [0.0]
    for p, q in zip(xy, xy[1:]):
        cum.append(cum[-1] + math.dist(p, q))
    total = cum[-1] or 1
    return xy, cum, total, kx, km / total


def max_detour_km(total_km: float) -> float:
    """How far off the road a place may be: 4% of the route, between 20 and 60 km."""
    return min(60, max(20, 0.04 * total_km))


def enroute(a_id: str, b_id: str, limit: int | None = None, max_off_km: float | None = None) -> list[dict]:
    """Places close to the road from a to b (at most max_off_km off it, default max_detour_km(); not too near
    either end), the best `limit` by score, in travel order: [{id, name, state, population, km_from_start, detour_km}]."""
    xy, cum, total, kx, scale = _route_frame(a_id, b_id)
    max_off = max_detour_km(total) if max_off_km is None else max_off_km
    xs, ys = [x for x, _ in xy], [y for _, y in xy]
    x_lo, x_hi, y_lo, y_hi = min(xs) - max_off, max(xs) + max_off, min(ys) - max_off, max(ys) + max_off
    found = []
    for p in places.load():
        if p["id"] in (a_id, b_id):
            continue
        px, py = p["lon"] * kx, p["lat"] * 111
        if not (x_lo <= px <= x_hi and y_lo <= py <= y_hi):    # outside the route's box: can't be close to it
            continue
        off, along = math.inf, 0.0                       # km off the route, km along it
        for i, ((x1, y1), (x2, y2)) in enumerate(zip(xy, xy[1:])):
            dx, dy = x2 - x1, y2 - y1
            seg2 = dx * dx + dy * dy
            t = 0 if seg2 == 0 else max(0, min(1, ((px - x1) * dx + (py - y1) * dy) / seg2))
            d = math.dist((px, py), (x1 + t * dx, y1 + t * dy))
            if d < off:
                off, along = d, cum[i] + t * math.sqrt(seg2)
        if off <= max_off and 0.05 * total < along < 0.95 * total:
            found.append((_score(p, off), along, off, p))
    best = sorted(found, key=lambda f: -f[0])[:limit]
    return [{"id": p["id"], "name": p["name"], "state": p["state"], "population": p.get("population") or 0,
             "km_from_start": round(along * scale), "detour_km": round(off), "_score": score}
            for score, along, off, p in sorted(best, key=lambda f: f[1])]


def suggest(a_id: str, b_id: str, n: int, max_off_km: float | None = None, skip=()) -> list[dict]:
    """Up to n places on the road from a to b, in travel order: the route is split into n equal stretches and
    each gives its best place (enroute's score), so the picks split the drive evenly instead of bunching
    around the biggest cities. max_off_km as in enroute(); places in `skip` (ids) are never picked."""
    total = max(1, _leg_by_id(a_id, b_id)[0])
    best_in: dict[int, dict] = {}
    for c in enroute(a_id, b_id, max_off_km=max_off_km):
        if c["id"] in skip:
            continue
        k = min(n - 1, int(c["km_from_start"] / total * n))
        if k not in best_in or c["_score"] > best_in[k]["_score"]:
            best_in[k] = c
    return [{k: v for k, v in c.items() if k != "_score"} for c in sorted(best_in.values(), key=lambda c: c["km_from_start"])]


def nearby(place_id: str, radius_km: float, limit: int) -> list[dict]:
    """Places within radius_km (straight line) of one place, the best `limit` by score, nearest first:
    [{id, name, state, population, km_away}]."""
    c, found = places.get(place_id), []
    for p in places.load():
        km = haversine_km(c["lat"], c["lon"], p["lat"], p["lon"])
        if p["id"] != place_id and km <= radius_km:
            found.append((_score(p, km), km, p))
    best = sorted(found, key=lambda f: -f[0])[:limit]
    return [{"id": p["id"], "name": p["name"], "state": p["state"], "population": p.get("population") or 0,
             "km_away": round(km)} for _, km, p in sorted(best, key=lambda f: f[1])]


# ---------------------------------------------------------------- one-time build

HIGHWAY_RANK = {"motorway": 1, "trunk": 1, "primary": 2, "secondary": 3}   # 1 = highway, 2 = main road, 3 = other
ROUTE_RANKS = {1, 2}                     # the routing graph uses these; secondary roads are only drawn
SIMPLIFY_DEG = 0.002                     # build: road shape in the graph kept to ~200 m
CACHE_PATH = ROOT / "raw_data" / "osm" / "highways.json"   # build: parsed roads, so a rebuild skips reading the .pbf


def _read_osm() -> list[tuple[str, bool, list[tuple[float, float]]]]:
    """The roads we keep as (type: motorway | trunk | primary | secondary, is_link, [(lon, lat), ...]),
    read from the .pbf once and cached."""
    if CACHE_PATH.exists():
        return [(kind, link, [tuple(q) for q in pts]) for kind, link, pts in json.loads(CACHE_PATH.read_text())]
    import osmium
    tags = [("highway", h) for base in HIGHWAY_RANK for h in (base, base + "_link")]
    ways = []                                           # osmium filters the tags in C, so this is quick
    for w in osmium.FileProcessor(str(OSM_PATH), osmium.osm.WAY).with_filter(osmium.filter.TagFilter(*tags)):
        ways.append((w.tags["highway"].removesuffix("_link"), w.tags["highway"].endswith("_link"),
                     [n.ref for n in w.nodes]))
    need = {n for _, _, refs in ways for n in refs}
    loc = {n.id: (round(n.location.lon, 5), round(n.location.lat, 5)) for n in
           osmium.FileProcessor(str(OSM_PATH), osmium.osm.NODE).with_filter(osmium.filter.IdFilter(need))}
    out = [(kind, link, [loc[n] for n in refs if n in loc]) for kind, link, refs in ways]
    CACHE_PATH.write_text(json.dumps(out, separators=(",", ":")))
    return out


def build() -> None:
    import time

    from shapely import contains_xy, line_merge
    from shapely.geometry import LineString, MultiLineString, shape

    t0 = time.time()
    import shapely
    india = shape(json.loads(INDIA_PATH.read_text())["geometry"]).buffer(0.05)
    shapely.prepare(india)                               # makes the many point-in-India checks fast
    ways = _read_osm()
    print(f"{len(ways)} roads read ({time.time() - t0:.0f} s)")

    # 1. graph from the routing ranks: a point shared by two roads (a junction) or a road's end is a vertex,
    #    the stretch between two vertices an edge {km, minutes, shape}. Edges outside India are dropped.
    route = [(kind, pts) for kind, _, pts in ways if HIGHWAY_RANK[kind] in ROUTE_RANKS and len(pts) >= 2]
    uses: dict[tuple, int] = {}
    for _, pts in route:
        for q in pts[1:-1]:
            uses[q] = uses.get(q, 0) + 1
        for q in (pts[0], pts[-1]):
            uses[q] = uses.get(q, 0) + 2
    edges = []                                          # [a, b, km, minutes, [points between]]
    for kind, pts in route:
        start = 0
        for k in range(1, len(pts)):
            if uses[pts[k]] >= 2 or k == len(pts) - 1:
                piece = pts[start:k + 1]
                if contains_xy(india, *piece[0]) or contains_xy(india, *piece[-1]):
                    km = sum(haversine_km(y1, x1, y2, x2) for (x1, y1), (x2, y2) in zip(piece, piece[1:]))
                    edges.append([piece[0], piece[-1], km, km / SPEED_KMH[kind] * 60, piece[1:-1]])
                start = k

    # 2. a vertex with exactly two edges is just a point on one road: join the two edges into one
    inc: dict[tuple, list[int]] = {}
    for n, (a, b, *_) in enumerate(edges):
        inc.setdefault(a, []).append(n)
        inc.setdefault(b, []).append(n)
    alive = [True] * len(edges)
    for v, ns in inc.items():
        ns = [n for n in ns if alive[n]]
        if len(ns) != 2 or ns[0] == ns[1]:
            continue
        e1, e2 = edges[ns[0]], edges[ns[1]]
        p1 = e1[4] if e1[1] == v else e1[4][::-1]        # e1 as x -> v
        x = e1[0] if e1[1] == v else e1[1]
        p2 = e2[4] if e2[0] == v else e2[4][::-1]        # e2 as v -> y
        y = e2[1] if e2[0] == v else e2[0]
        if x == y:
            continue                                     # a small loop: leave it
        edges[ns[0]] = [x, y, e1[2] + e2[2], e1[3] + e2[3], p1 + [v] + p2]
        alive[ns[1]] = False
        for w in (y,):                                   # e2's other end now belongs to e1
            inc[w] = [ns[0] if n == ns[1] else n for n in inc[w]]
    edges = [e for e, ok in zip(edges, alive) if ok]

    # 3. keep the biggest connected piece (the rest are short roads that touch nothing we kept)
    adj: dict[tuple, list[tuple]] = {}
    for a, b, *_ in edges:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    start = max(adj, key=lambda q: len(adj[q]))
    keep, todo = {start}, [start]
    while todo:
        for q in adj[todo.pop()]:
            if q not in keep:
                keep.add(q)
                todo.append(q)
    index = {q: i for i, q in enumerate(sorted(keep))}

    def shape_of(a, b, between):
        if len(between) < 2:
            return [c for q in between for c in (round(q[0], 4), round(q[1], 4))]
        inner = list(LineString([a] + between + [b]).simplify(SIMPLIFY_DEG).coords)[1:-1]
        return [round(c, 4) for q in inner for c in q]

    out_edges = [[index[a], index[b], round(km, 2), round(mins, 1), shape_of(a, b, between)]
                 for a, b, km, mins, between in edges if a in index and b in index]
    out_nodes = [[round(q[0], 4), round(q[1], 4)] for q in sorted(keep)]
    ROADS_PATH.write_text(json.dumps({"nodes": out_nodes, "edges": out_edges}, separators=(",", ":")))
    print(f"graph: {len(out_nodes)} nodes, {len(out_edges)} edges -> {ROADS_PATH} "
          f"({ROADS_PATH.stat().st_size / 1e6:.1f} MB, {time.time() - t0:.0f} s)")
    pack()

    build_layers(ways, india)
    print(f"done in {time.time() - t0:.0f} s")


REFS_CACHE_PATH = ROOT / "raw_data" / "osm" / "highway-refs.json"   # build: motorways + trunk with route numbers


def _read_highway_refs() -> list[tuple[str, str, list[tuple[float, float]]]]:
    """Motorways and trunk roads (not link roads) as (type, route number "NH 44" or "", [(lon, lat), ...]), for
    ranking the map layers. Read from the .pbf once (~3 min) and cached; the routing cache doesn't keep numbers."""
    if REFS_CACHE_PATH.exists():
        return [(kind, ref, [tuple(q) for q in pts]) for kind, ref, pts in json.loads(REFS_CACHE_PATH.read_text())]
    import osmium
    ways = [(w.tags["highway"], w.tags.get("ref", ""), [n.ref for n in w.nodes])
            for w in osmium.FileProcessor(str(OSM_PATH), osmium.osm.WAY)
                           .with_filter(osmium.filter.TagFilter(("highway", "motorway"), ("highway", "trunk")))]
    need = {n for _, _, refs in ways for n in refs}
    loc = {n.id: (round(n.location.lon, 5), round(n.location.lat, 5)) for n in
           osmium.FileProcessor(str(OSM_PATH), osmium.osm.NODE).with_filter(osmium.filter.IdFilter(need))}
    out = [(kind, ref, [loc[n] for n in refs if n in loc]) for kind, ref, refs in ways]
    REFS_CACHE_PATH.write_text(json.dumps(out, separators=(",", ":")))
    return out


def _main_numbers(ref: str) -> set[str]:
    """The 1-2 digit highway numbers in an OSM ref: "NH44;SH 12" -> {"44"}."""
    return {n.lstrip("0") for n in MAIN_NH.findall(ref or "") if len(n) <= 2}


def backbone(roads) -> set[str]:
    """The BACKBONE_COUNT main highway numbers with the most road length mapped (the long national corridors)."""
    km: dict[str, float] = {}
    for _, ref, pts in roads:
        d = sum(haversine_km(a[1], a[0], b[1], b[0]) for a, b in zip(pts, pts[1:]))
        for n in _main_numbers(ref):
            km[n] = km.get(n, 0) + d
    return set(sorted(km, key=km.get, reverse=True)[:BACKBONE_COUNT])


def map_rank(kind: str, ref: str, core: set[str]) -> int:
    """0 = expressway or backbone highway, 1 = other main national highway, 2 = any other highway."""
    nums = _main_numbers(ref)
    if kind == "motorway" or nums & core:
        return 0
    return 1 if nums else 2


GAP_FILL_KM = 5   # a highway piece shorter than this between two more important pieces takes their rank


STRETCH_MAX_KM = 25   # an unnumbered stretch up to this long (both carriageways together) can fill a highway gap


def _fill_unnumbered_stretches(ranked: list[list], unnumbered: list[int]) -> None:
    """[[rank, points], ...] changed in place. A highway through a town or over a bridge is often a chain of short
    OSM pieces with no route number, so the highway was cut there when zoomed out. Connected unnumbered pieces form
    a stretch; if it is short and joins better-ranked highways at two places at least 1 km apart (a missing piece,
    not a dead-end branch), all of it takes the lesser of those ranks."""
    km = lambda pts: sum(haversine_km(a[1], a[0], b[1], b[0]) for a, b in zip(pts, pts[1:]))
    members = set(unnumbered)
    at: dict[tuple, list[int]] = {}                # point -> ways passing through it
    for i, (_, pts) in enumerate(ranked):
        for q in set(pts):
            at.setdefault(q, []).append(i)
    seen, filled = set(), 0
    for start in unnumbered:
        if start in seen:
            continue
        group, todo, length = [], [start], 0.0      # the connected unnumbered pieces around `start`
        seen.add(start)
        while todo:
            i = todo.pop()
            group.append(i)
            length += km(ranked[i][1])
            for q in ranked[i][1]:
                for j in at[q]:
                    if j in members and j not in seen:
                        seen.add(j)
                        todo.append(j)
        if length > STRETCH_MAX_KM:
            continue
        r = max(ranked[i][0] for i in group)
        joins = {}                                  # point -> best rank of a numbered highway meeting the stretch there
        for i in group:
            for q in ranked[i][1]:
                better = [ranked[j][0] for j in at[q] if j not in members and ranked[j][0] < r]
                if better:
                    joins[q] = min(better)
        far = any(haversine_km(a[1], a[0], b[1], b[0]) >= 1 for a in joins for b in joins)
        if len(joins) >= 2 and far:
            new = max(joins.values())
            for i in group:
                ranked[i][0] = min(ranked[i][0], new)
            filled += 1
    print(f"filled {filled} unnumbered stretches in highways")


def _fill_rank_gaps(ranked: list[list]) -> None:
    """[[rank, points], ...] of highway ways, changed in place. A bridge, flyover or short stretch often has no route
    number in OSM, so it ranks lower than the highway on both sides of it and the highway showed a gap there when
    zoomed out. Such a short piece takes the lesser of its two neighbours' ranks (repeated, for chains of pieces)."""
    for _ in range(3):
        at: dict[tuple, list[int]] = {}       # point -> ranks of the ways passing through it (ways join mid-way too)
        for r, pts in ranked:
            for q in set(pts):
                at.setdefault(q, []).append(r)
        changed = 0
        for item in ranked:
            r, pts = item
            if sum(haversine_km(a[1], a[0], b[1], b[0]) for a, b in zip(pts, pts[1:])) >= GAP_FILL_KM:
                continue
            best = []
            for q in (pts[0], pts[-1]):
                others = list(at[q]); others.remove(r)
                best.append(min(others) if others else 9)
            if max(best) < r:
                item[0] = max(best)
                changed += 1
        if not changed:
            break
        print(f"filled {changed} short gaps in highways")


def _simplify_keep_junctions(coords, count: dict, tolerance: float) -> list:
    """Simplify a line piece by piece between its junctions (points other lines pass through), so a junction is
    never simplified away. Simplifying the whole line could drop the point where another road joins it mid-way,
    and that road then ended up to ~1 km short of it: a visible gap."""
    from shapely.geometry import LineString
    out, start = [], 0
    for k in range(1, len(coords)):
        if k == len(coords) - 1 or count.get(coords[k], 0) > 1:
            piece = LineString(coords[start:k + 1]).simplify(tolerance).coords
            out.extend(piece if not out else list(piece)[1:])
            start = k
    return out


def build_layers(ways=None, india=None) -> None:
    """Map layers: whole roads (not link roads) merged into long lines, simplified, and grouped by tile into one
    MultiLineString per 2°×2° tile. Alone (python -m app.geo layers) it reads the cached roads: ~1 min
    (plus ~3 min the first time, to read the highways' route numbers)."""
    import shapely
    from shapely import contains_xy, line_merge
    from shapely.geometry import MultiLineString, shape

    if india is None:
        india = shape(json.loads(INDIA_PATH.read_text())["geometry"]).buffer(0.05)
        shapely.prepare(india)
    ways = ways if ways is not None else _read_osm()
    inside = lambda pts: len(pts) >= 2 and contains_xy(india, *pts[len(pts) // 2])
    by_rank: dict[int, list] = {0: [], 1: [], 2: [], 3: [], 4: []}
    highways = _read_highway_refs()
    core = backbone(highways)
    print("backbone highways:", ", ".join(f"NH {n}" for n in sorted(core, key=int)))
    ranked = [[map_rank(kind, ref, core), pts] for kind, ref, pts in highways if inside(pts)]
    unnumbered = [i for i, (kind, ref, pts) in enumerate(h for h in highways if inside(h[2])) if not ref]
    _fill_unnumbered_stretches(ranked, unnumbered)
    _fill_rank_gaps(ranked)
    for rank, pts in ranked:                                          # highways, ranked by their number
        by_rank[rank].append(pts)
    for kind, link, pts in ways:                                      # primary and secondary roads
        if kind in ("primary", "secondary") and not link and inside(pts):
            by_rank[3 if kind == "primary" else 4].append(pts)
    merged = {}
    for rank, lines in by_rank.items():
        m = line_merge(MultiLineString(lines))
        merged[rank] = list(getattr(m, "geoms", [m]))
    for path, ranks, tolerance, min_km in LAYERS:
        feats, n = [], 0
        # A short piece is kept only if both its ends join other roads in this layer: it is a link in a road, and
        # dropping it cut a hole (merged lines break at every junction). Short stubs that lead nowhere are dropped,
        # which keeps the files small enough to zoom smoothly.
        count: dict[tuple, int] = {}
        for rank in ranks:
            for part in merged[rank]:
                for q in part.coords:
                    count[q] = count.get(q, 0) + 1
        for rank in ranks:
            tiles: dict[tuple[int, int], list] = {}
            for part in merged[rank]:
                cs = part.coords
                if part.length * 111 < min_km and (count[cs[0]] == 1 or count[cs[-1]] == 1):   # degrees -> ~km
                    continue
                coords = [[round(x, 3), round(y, 3)] for x, y in _simplify_keep_junctions(cs, count, tolerance)]
                coords = [c for k, c in enumerate(coords) if k == 0 or c != coords[k - 1]]
                if len(coords) >= 2:
                    mid = coords[len(coords) // 2]
                    tiles.setdefault((int(mid[0] // TILE_DEG), int(mid[1] // TILE_DEG)), []).append(coords)
            feats += [{"type": "Feature", "properties": {"rank": rank},
                       "geometry": {"type": "MultiLineString", "coordinates": lines}} for lines in tiles.values()]
            n += sum(len(lines) for lines in tiles.values())
        path.write_text(json.dumps({"type": "FeatureCollection", "features": feats}, separators=(",", ":")))
        print(f"{path.name}: ranks {ranks}, {n} lines in {len(feats)} tiles ({path.stat().st_size / 1e6:.1f} MB)")


def build_borders() -> None:
    """static/states.geojson: the borders BETWEEN two states, from the same OpenStreetMap file as the roads.
    In OSM a state is a relation (boundary=administrative, admin_level=4) made of ways, and a way shared by two states
    is their common border. Ways that belong to one state only are the coast or the country's edge: the India outline
    already draws those. Build-only (osmium + shapely), like the road layers."""
    import osmium
    from shapely.geometry import LineString, MultiLineString, mapping
    owners: dict[int, set[int]] = {}                        # way id -> the state relations it belongs to
    for r in osmium.FileProcessor(str(OSM_PATH), osmium.osm.RELATION).with_filter(
            osmium.filter.TagFilter(("boundary", "administrative"))):
        if r.tags.get("admin_level") == "4":
            for m in r.members:
                if m.type == "w":
                    owners.setdefault(m.ref, set()).add(r.id)
    shared = {w for w, rels in owners.items() if len(rels) >= 2}
    ways = [[n.ref for n in w.nodes] for w in
            osmium.FileProcessor(str(OSM_PATH), osmium.osm.WAY).with_filter(osmium.filter.IdFilter(shared))]
    need = {n for refs in ways for n in refs}
    loc = {n.id: (n.location.lon, n.location.lat) for n in
           osmium.FileProcessor(str(OSM_PATH), osmium.osm.NODE).with_filter(osmium.filter.IdFilter(need))}
    from shapely.ops import linemerge
    pieces = [LineString([loc[n] for n in refs if n in loc]) for refs in ways if len(refs) > 1]
    merged = linemerge([l for l in pieces if len(l.coords) > 1])      # ways joined end to end: long lines, not 6,000 bits
    merged = list(merged.geoms) if hasattr(merged, "geoms") else [merged]

    def smooth(line, rounds=2):
        """Chaikin corner cutting: every corner is replaced by two points a quarter of the way along its sides, so a
        jagged border turns into a soft curve. The two end points stay where they are (borders must still meet)."""
        pts = list(line.coords)
        for _ in range(rounds):
            new = [pts[0]]
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                new += [(0.75 * x0 + 0.25 * x1, 0.75 * y0 + 0.25 * y1), (0.25 * x0 + 0.75 * x1, 0.25 * y0 + 0.75 * y1)]
            new.append(pts[-1])
            pts = new
        return LineString(pts)

    lines = [smooth(l.simplify(0.01)) for l in merged]               # ~1 km first (fewer corners), then smooth
    lines = [l for l in lines if not l.is_empty and len(l.coords) > 1]
    out = ROOT / "static" / "states.geojson"
    out.write_text(json.dumps({"type": "Feature", "properties": {}, "geometry": mapping(MultiLineString(lines))},
                              separators=(",", ":")))
    print(f"{len(shared)} shared ways -> {out} ({out.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    if sys.argv[1:] == ["borders"]:
        build_borders()
    elif sys.argv[1:] == ["build"]:
        build()
    elif sys.argv[1:] == ["layers"]:
        build_layers()
    elif sys.argv[1:] == ["pack"]:
        pack()
    else:
        print("usage: python -m app.geo build | layers | pack | borders   (layers = only the map layers, from the cached roads; "
              "pack = data/roads.json -> data/roads.bin, the file the server loads)")

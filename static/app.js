// Voice Route Painter — browser side.
// The rule: the server sends the FULL trip state, and render(state) redraws everything.
// A few things are remembered here, and all are display-only:
//   - `selected`: which pin/leg/row you clicked (highlighted on the map, the list and the trip bar)
//   - `drawn`:    which pins/legs are already on the map, so only NEW ones get animated
// While the agent works, the server also sends small "progress" messages ("Looking up Agra…").
// Those only drive the activity strip and faint preview pins; the trip itself always comes as full state.
// Page parts: headline (what you said + reply), trip bar, map, question card, side panel.

// ---- State (what the server sends over the WebSocket): {trip: {...}, ui: {...}} ----
// trip = the route itself (stops, legs, undo flags); ui = what to show right now (reply, question, cards, status).
// legs[i] goes from stops[i] to stops[i+1]. `path` is optional (road geometry); if missing we draw a straight line.
let state = { trip: { version: 0, stops: [], legs: [] }, ui: { status: "idle", heard: "", message: "" } };

let selected = null;   // {type: "stop" | "leg", index} after a click; null = nothing selected

const $ = id => document.getElementById(id);
const TOUCH = matchMedia("(pointer: coarse)").matches;   // phone/tablet: no Space key, hold the button instead

// Owner-only extras (session id, token count, Trace link): traces hold what people typed, so these show only on
// localhost, or in a browser where the owner saved the trace key (open /trace.html?key=YOUR_TRACE_KEY once).
const OWNER = (() => {
  try { if (localStorage.getItem("traceKey")) return true; } catch (e) {}
  return ["localhost", "127.0.0.1"].includes(location.hostname);
})();
$("dev").hidden = !OWNER;

// ---- Map setup (runs once) ----
// No tile layer (no third-party map server): the map is drawn from local files, in this order, bottom to top:
//   India (light, with a border) -> roads -> the trip (Leaflet's overlay pane, 400) -> pins and their names.
// "Panes" are those stacked layers, so files can load in any order and never cover the route.
// Zoom is limited to 3.5 (South Asia) … 12 (a town), and panning stays around India, so the map never shows
// empty space or zooms in past what the road data can show.
// Zoom moves in half steps: with 0.1 steps (before) one mouse-wheel notch zoomed only 0.1 level, so getting from
// all of India to a district took dozens of scrolls. wheelPxPerZoomLevel 50 (default 60) = a bit quicker per scroll.
const map = L.map("map", {
  zoomControl: false, zoomSnap: 0.5, zoomDelta: 1, wheelPxPerZoomLevel: 50, minZoom: 3.5, maxZoom: 12,
  maxBounds: [[-2, 58], [42, 106]], maxBoundsViscosity: 0.8,
});
map.attributionControl.setPrefix(false).addAttribution("Roads and borders © OpenStreetMap contributors");   // required by the ODbL licence
map.createPane("land").style.zIndex = 200;
let tripLayer = L.layerGroup().addTo(map);      // pins + legs + arrows live here; replaced on every render
const pendingLayer = L.layerGroup().addTo(map); // faint preview pins shown while the agent is still working
const sugLayer = L.layerGroup().addTo(map);     // suggested places as pins
const askLayer = L.layerGroup().addTo(map);     // a question's options as pins
let indiaBounds = null;
let fitDone = false;   // has the map been zoomed to a trip yet? (then the outline must not undo that zoom)

fetch("india.geojson")
  .then(r => r.json())
  .then(geo => {
    const india = L.geoJSON(geo, {
      renderer: L.canvas({ pane: "land" }),
      style: { color: "#b3ada1", weight: 1, fillColor: "#f3f2ee", fillOpacity: 1 },
      interactive: false,
    }).addTo(map);
    indiaBounds = india.getBounds();
    // Show all of India only if nothing was zoomed to yet. A shared link's trip can arrive before this file;
    // fitting India then would throw away the zoom to the trip.
    if (!fitDone) map.fitBounds(indiaBounds, { padding: [20, 20] });
  });
map.setView([22, 80], 4.5);   // fallback view until the outline loads

// State borders: only the lines between two states (the outline already draws the rest), a thin light line, under the roads.
// Built by `python -m app.geo borders` from OpenStreetMap.
map.createPane("borders").style.zIndex = 230;
fetch("states.geojson")
  .then(r => r.json())
  .then(geo => L.geoJSON(geo, {
    renderer: L.canvas({ pane: "borders" }),
    style: { color: "#b9b3a6", weight: 1.1, opacity: 0.9, lineCap: "round", lineJoin: "round", smoothFactor: 0.5 },
    interactive: false,
  }).addTo(map))
  .catch(() => {});                       // the map works without them

// ---- Place names ----
// static/places-labels.json = [name, lat, lon, population] for every place, biggest first (built by
// `python -m app.places labels`). Names are drawn on one canvas that sits above the roads and under the trip.
// How many names fit on the screen grows with the zoom: N = NAMES_BASE * 1.4^(zoom - 3.5) * boost, and the N biggest
// places inside the view get a name. (Whole India, zoom 4.5 -> ~20 names; zoom 6 -> ~40; zoom 8 -> ~90.) Counting
// only what is in view matters: zoomed in, the biggest towns of India are mostly off screen.
// The slider (0-100) is the boost: 50 = as above, each 12.5 steps doubles or halves it, 0 = off. On top of that a
// name is skipped when it would touch a name already drawn or a trip pin, so crowded areas thin out by themselves.
const NAMES_BASE = 14;
let nameLevel = 50;
try { const v = parseInt(localStorage.getItem("namesLevel"), 10); if (v >= 0 && v <= 100) nameLevel = v; } catch (e) {}
let allNames = [];
map.createPane("names").style.zIndex = 350;
const nameCanvas = L.DomUtil.create("canvas", "names-canvas", map.getPane("names"));
nameCanvas.style.position = "absolute";

fetch("places-labels.json").then(r => r.json()).then(rows => { allNames = rows; drawNames(); }).catch(() => {});

function drawNames() {
  const size = map.getSize(), dpr = window.devicePixelRatio || 1;
  nameCanvas.width = size.x * dpr; nameCanvas.height = size.y * dpr;
  nameCanvas.style.width = size.x + "px"; nameCanvas.style.height = size.y + "px";
  L.DomUtil.setPosition(nameCanvas, map.containerPointToLayerPoint([0, 0]));   // keep it over the visible area
  const ctx = nameCanvas.getContext("2d");
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, size.x, size.y);
  if (!allNames.length || nameLevel === 0) { $("names-val").textContent = nameLevel === 0 ? "Off" : ""; return; }

  const allowed = Math.round(NAMES_BASE * Math.pow(1.4, map.getZoom() - 3.5) * Math.pow(2, (nameLevel - 50) / 12.5));

  // Trip pins, suggestion pins and option pins are already named: keep clear of them.
  const taken = [];
  [tripLayer, askLayer, sugLayer, pendingLayer].forEach(g => g.eachLayer(m => {
    if (!m.getLatLng) return;
    const p = map.latLngToContainerPoint(m.getLatLng());
    taken.push({ x0: p.x - 14, x1: p.x + 14, y0: p.y - 14, y1: p.y + 14 });
  }));
  const free = b => !taken.some(o => b.x0 < o.x1 && o.x0 < b.x1 && b.y0 < o.y1 && o.y0 < b.y1);

  ctx.font = "500 11px Inter, -apple-system, sans-serif";
  ctx.textBaseline = "middle"; ctx.lineJoin = "round"; ctx.lineWidth = 3;
  const bounds = map.getBounds().pad(0.02);
  let shown = 0;
  for (const [name, lat, lon] of allNames) {                         // biggest first
    if (shown >= allowed) break;
    if (!bounds.contains([lat, lon])) continue;
    const p = map.latLngToContainerPoint([lat, lon]);
    const box = { x0: p.x - 4, x1: p.x + 10 + ctx.measureText(name).width + 2, y0: p.y - 8, y1: p.y + 8 };
    if (!free(box)) continue;
    taken.push(box);
    shown++;
    ctx.fillStyle = "#8a8a8a";
    ctx.beginPath(); ctx.arc(p.x, p.y, 2.2, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = "rgba(255,255,255,0.95)"; ctx.strokeText(name, p.x + 7, p.y);   // white halo so text reads over roads
    ctx.fillStyle = "#555"; ctx.fillText(name, p.x + 7, p.y);
  }
  $("names-val").textContent = `${shown} names`;
}
map.on("movestart zoomstart", () => { nameCanvas.style.visibility = "hidden"; });   // a stretched canvas would look wrong
map.on("moveend zoomend resize", () => { nameCanvas.style.visibility = ""; drawNames(); });

$("names-range").value = nameLevel;
$("names-range").addEventListener("input", e => {
  nameLevel = +e.target.value;
  try { localStorage.setItem("namesLevel", String(nameLevel)); } catch (err) {}
  drawNames();
});

// Roads from OpenStreetMap (built by `python -m app.geo layers`), drawn the way Google Maps does it:
// highways amber with a darker edge, other roads white with a grey edge, and only the important ones when zoomed out.
// Road ranks (properties.rank in the files, worked out by `python -m app.geo layers`):
//   0 = the backbone: expressways + the ~18 longest national highways (NH 44, 27, 48, 52, 16, 19 …)
//   1 = other main national highways (1-2 digit numbers), 2 = other national highways (3-digit branches),
//   3 = primary roads, 4 = secondary roads.
// From zoom 8 each road is drawn twice: a wider darker line (the "edge"), then a lighter line on top (the "fill").
// Further out the lines are thin, and an edge would only make them look muddy, so only the fill is drawn.
// Each rank has its own pane (a stacked layer) with its own canvas, so all edges of a rank sit under all its fills
// (crossings look joined, not cut) and more important roads are drawn above less important ones.
// Canvas, not SVG: thousands of road lines as SVG elements would make panning and zooming slow.
const ROAD_LOOK = {
  //   fill colour, edge colour, fill width in px at zoom ≤5, 6, 7, 8, 9, 10+
  0: { fill: "#f3c55f", edge: "#d9a23d", w: [1.0, 1.3, 1.7, 2.2, 3.0, 4.0] },
  1: { fill: "#f6d78f", edge: "#dfb465", w: [0.8, 1.0, 1.4, 1.8, 2.5, 3.4] },
  2: { fill: "#f9e6b8", edge: "#e3c88f", w: [0.6, 0.8, 1.1, 1.4, 2.0, 2.8] },
  3: { fill: "#ffffff", edge: "#d3cdc1", w: [0.5, 0.6, 0.8, 1.1, 1.6, 2.4] },
  4: { fill: "#ffffff", edge: "#dedad0", w: [0.4, 0.5, 0.6, 0.8, 1.2, 1.8] },
};
const roadRenderers = {};
for (const rank of [4, 3, 2, 1, 0]) {                    // created in this order, so rank 0 ends up on top
  map.createPane(`roads${rank}`).style.zIndex = 250 - rank;
  roadRenderers[rank] = L.canvas({ pane: `roads${rank}` });
}
// Line widths for the current zoom.
function roadStyle(rank, part) {
  const look = ROAD_LOOK[rank], z = map.getZoom();
  const w = look.w[Math.max(0, Math.min(5, Math.round(z) - 5))];
  const round = { lineCap: "round", lineJoin: "round" };
  if (part === "edge") return { ...round, color: look.edge, weight: w + (z < 9 ? 1 : 1.4) };
  return { ...round, color: look.fill, weight: w };
}
const EDGE_MIN_ZOOM = 8;   // below this the edges are left off the map entirely (drawing them hidden still cost time)
// One road file -> a few Leaflet layers (edge + fill for each rank in it), each remembering its rank and part.
// smoothFactor 2 = Leaflet drops points closer than ~2 px on screen: fewer points to draw, no visible difference.
const roadLayers = file => fetch(file)
  .then(r => r.json())
  .then(geo => {
    const ranks = [...new Set(geo.features.map(f => f.properties.rank))];
    return ranks.flatMap(rank => ["edge", "fill"].map(part => {
      const layer = L.geoJSON(geo, {
        filter: f => f.properties.rank === rank,
        renderer: roadRenderers[rank],
        style: () => roadStyle(rank, part),
        smoothFactor: 2,
        interactive: false,
      });
      layer.roadRank = rank;
      layer.roadPart = part;
      return layer;
    }));
  });
// More roads the further you zoom in, so a zoomed-out map shows only the main highways:
//   below 6.5 (all of India, any trip: the map zooms to at most 6 for a trip): the backbone, coarse;
//   6.5-8.5 (zoomed in on a few states): all main highways;   from 8.5 (a region): all national highways;
//   from 9 (a few districts): primary roads;   from 10 (a district): secondary roads.
// Each file holds ~100-250 objects (one per 2°×2° tile and rank), is fetched the first time it is needed, and is
// hidden outside its zoom range.
const ROAD_LAYERS = [
  { file: "roads-core.geojson", minZoom: 0, maxZoom: 6.5 },
  { file: "roads-overview.geojson", minZoom: 6.5, maxZoom: 8.5 },
  { file: "roads.geojson", minZoom: 8.5, maxZoom: 99 },
  { file: "roads-main.geojson", minZoom: 9, maxZoom: 99 },
  { file: "roads-detail.geojson", minZoom: 10, maxZoom: 99 },
];
function updateRoads() {
  const z = map.getZoom();
  const inRange = r => z >= r.minZoom && z < r.maxZoom;
  for (const r of ROAD_LAYERS) {
    if (inRange(r) && !r.layers) r.layers = roadLayers(r.file).catch(() => []);   // the map works without roads
    if (r.layers) r.layers.then(ls => {
      let added = false;
      ls.forEach(l => {
        const show = inRange(r) && (l.roadPart === "fill" || z >= EDGE_MIN_ZOOM);
        if (!show) { l.remove(); return; }
        // Widths follow the zoom, but only restyle when they changed: each restyle redraws the whole layer.
        const style = roadStyle(l.roadRank, l.roadPart), key = JSON.stringify(style);
        if (key !== l.styleKey) { l.setStyle(style); l.styleKey = key; }
        if (!map.hasLayer(l)) { l.addTo(map); added = true; }
      });
      // A canvas draws layers in the order they were added. Edges come back on top when zooming in past
      // EDGE_MIN_ZOOM, so put the fills back above them (else roads look dark and joins look broken).
      if (added) ls.forEach(l => { if (l.roadPart === "fill" && map.hasLayer(l)) l.bringToFront(); });
    });
  }
}
map.on("zoomend", updateRoads);
map.on("zoomend", declutterLabels);   // names that overlapped at one zoom may fit at another
updateRoads();

// ---- Helpers ----
function fmtTime(min) {
  if (min == null) return "—";
  const h = Math.floor(min / 60), m = min % 60;
  if (h >= 24) return `${Math.round(min / 60)} h (~${Math.ceil(min / 600)} days)`;   // long drives: hours + days at ~10 h/day
  return `${h} h ${String(m).padStart(2, "0")} min`;
}

// Text that goes into HTML strings must not be able to add tags.
function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

// Make a div behave like a button for the keyboard too: Tab reaches it, Enter clicks it.
function clickable(el, onClick) {
  el.tabIndex = 0;
  el.setAttribute("role", "button");
  el.onclick = onClick;
  el.onkeydown = e => { if (e.key === "Enter" && e.target === el) { e.preventDefault(); onClick(); } };
}

const ROUTE_COLOR = "#2457d6";   // the route on the map and the trip bar
const STATUS_TEXT = { idle: "Ready", listening: "Listening…", transcribing: "Transcribing…", thinking: "Thinking…",
                      offline: "Offline" };
const PLACEHOLDER = matchMedia("(max-width: 760px)").matches ? "Type your trip…"   // phone: the example doesn't fit
                  : "Type your trip… e.g. Lucknow se Jaipur, beech mein Agra";
// A pool of example sentences; 3 random ones are shown each time the page opens.
const EXAMPLE_POOL = [
  "Lucknow se Jaipur jaana hai, beech mein Agra",
  "Delhi to Manali and back",
  "Varanasi se Udaipur, raste mein kya dekh sakte hain?",
  "Mumbai se Goa jaana hai, raste mein Pune",
  "Bangalore to Mysore to Ooty",
  "Chennai se Pondicherry, wapas Chennai",
  "Kolkata se Darjeeling jaana hai",
  "Hyderabad to Hampi, stop at Kurnool",
  "Delhi se Amritsar, beech mein Chandigarh",
  "Jaipur se Jodhpur, phir Jaisalmer",
  "Ahmedabad to Udaipur via Mount Abu",
  "Dehradun se Rishikesh, phir Haridwar",
];
const EXAMPLES = EXAMPLE_POOL.map(t => [Math.random(), t])   // shuffle, then take 3
                             .sort((a, b) => a[0] - b[0])
                             .slice(0, 3).map(p => p[1]);

// ---- Animation timings (milliseconds) ----
// The trip draws in order (pin, line, pin, …), but a whole new trip takes at most DRAW_BUDGET_MS:
// the agent already took a few seconds, so the drawing must not add many more.
const PIN_MS = 90;      // gap between one pin popping and the next step
const LEG_MS = 300;     // how long a line takes to draw
const FLY_MS = 500;     // map zoom-to-trip; drawing waits for it
const DRAW_BUDGET_MS = 1400;

// What is already drawn: stop ids ("agra") and legs ("agra>jaipur").
let drawn = { stops: new Set(), legs: new Set() };
const NOTHING_FRESH = { freshStops: new Set(), freshLegs: new Set() };
let lastSig = "";      // fingerprint of what the map/list were last drawn from
let lastFitKey = "";    // ids of the stops the map was last zoomed to
let drawEndsAt = 0;     // time (performance.now()) when the current route animation finishes

// A round trip ends where it starts (A → B → A): the server allows only the start to appear twice.
const isRoundTrip = s => s.trip.stops.length >= 3 && s.trip.stops[0].id === s.trip.stops[s.trip.stops.length - 1].id;
const legKey = (a, b) => `${a.id}>${b.id}`;
const isSel = (type, index) => !!selected && selected.type === type && selected.index === index;

// Which stops/legs in `s` were not on the map before? Only these get animated.
function findFresh(s) {
  const freshStops = new Set(s.trip.stops.filter(x => !drawn.stops.has(x.id)).map(x => x.id));
  const freshLegs = new Set();
  s.trip.legs.forEach((_, i) => {
    const k = legKey(s.trip.stops[i], s.trip.stops[i + 1]);
    if (!drawn.legs.has(k)) freshLegs.add(k);
  });
  return { freshStops, freshLegs };
}

// ---- render(state): redraw the whole page from the state ----
function render(s) {
  setStatus(s.ui.status);
  drawHeard(s);
  drawHeardRings(s);
  setMessage(headline(s));
  drawDev(s);
  drawModel(s);
  drawQuestion(s);
  drawSuggestions(s);
  drawFollowups(s);
  drawProposal(s);
  drawExamples(s);
  drawTools(s);
  // While a turn runs, a new message waits for it (the server runs one turn at a time): say so.
  $("text").placeholder = s.ui.status === "idle" ? PLACEHOLDER : "Working… your next message will run after this one";

  // The server pushes the state several times per turn (trip changed, message set, idle). Only redraw
  // the map and the list when the trip or the selection actually changed; otherwise a second push
  // would restart (and cut short) the animation of the first one.
  const sig = JSON.stringify([s.trip.stops.map(x => x.id), s.trip.legs.map(l => l.km), selected]);
  if (sig === lastSig) return;
  lastSig = sig;

  const fresh = findFresh(s);
  const startDelay = fitMapToTrip(s);
  drawMap(s, fresh, startDelay);
  drawStops(s, fresh);
  drawTripBar(s, fresh);

  // Remember what is on the map now, for the next render.
  drawn = {
    stops: new Set(s.trip.stops.map(x => x.id)),
    legs: new Set(s.trip.legs.map((_, i) => legKey(s.trip.stops[i], s.trip.stops[i + 1]))),
  };
}

function setStatus(name) {
  $("status").textContent = STATUS_TEXT[name] || name;
  $("status").className = "status " + name;
}

// The big line under "Heard": the agent's reply; before anything happened, how to start.
function headline(s) {
  // When the card shows the same text as the headline (the agent asked), don't show it twice.
  const sameAsCard = s.ui.question && s.ui.question.options.length && s.ui.question.text === s.ui.message;
  if (sameAsCard) return "";
  if (s.ui.message) return s.ui.message;
  if (!s.trip.stops.length && !s.ui.heard && s.ui.status === "idle")
    return TOUCH ? "Hold 🎙 and say your trip, or type it below." : "Hold Space and say your trip, or type it below.";
  return "";
}

// "Heard: “…”" with the places the server found in it (state.ui.heard_marks) in bold.
// Each mark = {start, end} character positions in `heard`. Text is escaped piece by piece.
function drawHeard(s) {
  if (!s.ui.heard) { $("heard").textContent = ""; return; }
  let html = "", at = 0;
  for (const m of s.ui.heard_marks || []) {
    if (m.start < at) continue;                       // overlapping marks: keep the first
    html += escapeHtml(s.ui.heard.slice(at, m.start)) + `<b>${escapeHtml(s.ui.heard.slice(m.start, m.end))}</b>`;
    at = m.end;
  }
  $("heard").innerHTML = `Heard: “${html}${escapeHtml(s.ui.heard.slice(at))}”`;
}

// A small ring on the map around each place you said. Pixel radius, so it stays small at any zoom.
// Not part of the trip drawing: it has its own layer and is rebuilt only when the marks change.
const ringLayer = L.layerGroup().addTo(map);
let lastRingSig = "";
function drawHeardRings(s) {
  // Rings are for while the agent works; once the turn is over (status idle) they go. The words stay bold.
  const marks = s.ui.status === "idle" ? [] : (s.ui.heard_marks || []);
  const sig = JSON.stringify(marks.map(m => [m.lat, m.lon]));
  if (sig === lastRingSig) return;
  lastRingSig = sig;
  ringLayer.clearLayers();
  marks.forEach(m => L.circleMarker([m.lat, m.lon], {
    radius: 15, color: "#2457d6", weight: 2, fill: false, interactive: false, className: "heard-ring",
  }).addTo(ringLayer));
}

// Fade the reply in when the text changes, so it doesn't just snap. Up to 2 lines; longer text ends in "…"
// and shows in full on hover.
function setMessage(text) {
  const el = $("message");
  if (el.textContent === text) return;
  el.textContent = text;
  el.title = text;
  el.classList.remove("fade-in");
  void el.offsetWidth;           // forces the browser to notice the class was removed, so the animation restarts
  if (text) el.classList.add("fade-in");
}

// When the set of stops changes, glide the map to show the whole trip.
// Returns how long drawing should wait (so lines don't draw while the map is still moving).
// `extra` = more [lat, lon] points to keep in view (a question's options); passing it always refits.
function fitMapToTrip(s, extra) {
  const key = s.trip.stops.map(x => x.id).join(",");
  if (key === lastFitKey && !extra) return 0;
  lastFitKey = key;
  const points = s.trip.stops.map(x => [x.lat, x.lon]).concat(extra || []);
  if (points.length) {
    fitDone = true;
    const bounds = L.latLngBounds(points);
    // Leave room at the top for the question card when it is showing.
    const card = $("ask");
    const top = card.hidden ? 80 : card.offsetHeight + 50;
    map.flyToBounds(bounds, { paddingTopLeft: [80, top], paddingBottomRight: [140, 60], maxZoom: 6,
                              duration: FLY_MS / 1000 });
    return FLY_MS;
  }
  if (indiaBounds) map.flyToBounds(indiaBounds, { padding: [20, 20], duration: FLY_MS / 1000 });
  return 0;
}

// Pins are HTML (a div), not a canvas circle, so CSS can animate them.
// Leaflet moves the OUTER div to the right place, so the animated shape is an inner div.
// Each pin shows its number inside the dot and the city name in a small tag to its right.
function pinIcon(extraClass, number, name, delay) {
  const tag = name ? `<span class="pin-name">${escapeHtml(name)}</span>` : "";
  return L.divIcon({
    className: "pin-wrap",
    html: `<div class="pin ${extraClass}" style="animation-delay:${delay}ms"><span class="pin-num">${number}</span>${tag}</div>`,
    iconSize: [22, 22], iconAnchor: [11, 11], popupAnchor: [0, -12],
  });
}

// Direction arrow halfway along a leg, rotated to point the way the road goes there.
// "Halfway" by distance, not the middle point of the list: road points bunch up in towns, so the middle
// point can sit right next to a city. Worked out in Mercator "world pixels" at zoom 0, so it fits every zoom.
function arrowMarker(points, fresh, delay) {
  const px = points.map(p => map.project(L.latLng(p), 0));
  const seg = [];
  let total = 0;
  for (let j = 1; j < px.length; j++) { const d = px[j].distanceTo(px[j - 1]); seg.push(d); total += d; }
  let j = 0, run = 0;
  while (j < seg.length - 1 && run + seg[j] < total / 2) run += seg[j++];
  const a = px[j], b = px[j + 1] || px[j];
  const f = seg[j] ? (total / 2 - run) / seg[j] : 0;
  const mid = map.unproject(L.point(a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f), 0);
  // Direction over a few points around the middle, so one tiny zig-zag in the road doesn't turn the arrow.
  const from = px[Math.max(0, j - 2)], to = px[Math.min(px.length - 1, j + 3)];
  const deg = Math.atan2(to.y - from.y, to.x - from.x) * 180 / Math.PI;
  return L.marker(mid, {
    interactive: false, keyboard: false,
    icon: L.divIcon({
      className: "pin-wrap",
      html: `<div class="arrow${fresh ? " in" : ""}" style="animation-delay:${delay}ms"><i style="transform:rotate(${deg}deg)"></i></div>`,
      iconSize: [18, 18], iconAnchor: [9, 9],
    }),
  });
}

// A road driven both ways (A -> B -> A) would be drawn twice on the same pixels and look like one trip. Such legs are
// moved OVERLAP_PX to the left of their travel direction (India drives on the left), so the two ways sit side by side.
// The shift is in screen pixels, so it is worked out again after every zoom.
const OVERLAP_PX = 3;
let sideLines = [];   // [{line, points, arrow}] for the legs that share their road with another leg
function shiftLeft(points) {
  const pts = points.map(p => map.latLngToLayerPoint(p));
  return pts.map((p, i) => {
    const a = pts[Math.max(0, i - 2)], b = pts[Math.min(pts.length - 1, i + 2)];   // direction over a few points: steadier
    const dx = b.x - a.x, dy = b.y - a.y, len = Math.hypot(dx, dy) || 1;
    return map.layerPointToLatLng(L.point(p.x + dy / len * OVERLAP_PX, p.y - dx / len * OVERLAP_PX));
  });
}
map.on("zoomend", () => sideLines.forEach(o => {
  const moved = shiftLeft(o.points);
  o.line.setLatLngs(moved);
  tripLayer.removeLayer(o.arrow);
  o.arrow = arrowMarker(moved, false, 0).addTo(tripLayer);
}));

// Draw a line "growing" from its start: hide it, then at `delay` animate its dash offset to 0.
function animateLine(line, delay, ms) {
  const el = line.getElement();
  if (!el) return;
  el.style.opacity = 0;
  setTimeout(() => {
    try {
      const len = el.getTotalLength();
      el.style.strokeDasharray = len;
      el.style.strokeDashoffset = len;
      el.style.opacity = 1;
      void el.getBoundingClientRect();   // lets the browser register the start position before animating
      el.style.transition = `stroke-dashoffset ${ms}ms ease-out`;
      el.style.strokeDashoffset = 0;
      setTimeout(() => { el.style.strokeDasharray = ""; el.style.transition = ""; }, ms + 50);
    } catch (e) { el.style.opacity = 1; }   // the line was removed already: nothing to do
  }, delay);
}

function drawMap(s, fresh, startDelay) {
  // The old drawing fades out while the new one is already in place, so removed pins/legs fade away
  // and unchanged ones don't flicker.
  const old = tripLayer;
  old.eachLayer(l => {
    const el = l.getElement && l.getElement();
    if (el) { el.style.transition = "opacity 250ms"; el.style.opacity = 0; }
  });
  setTimeout(() => old.remove(), 300);
  tripLayer = L.layerGroup().addTo(map);
  sideLines = [];
  const pairCount = {};   // how many legs use the same two stops (either direction)
  s.trip.stops.forEach((stop, i) => {
    if (i) { const k = [s.trip.stops[i - 1].id, stop.id].sort().join(); pairCount[k] = (pairCount[k] || 0) + 1; }
  });

  // Speed up the steps when there are many, so the whole drawing fits in DRAW_BUDGET_MS.
  const pins = s.trip.stops.filter(x => fresh.freshStops.has(x.id)).length;
  const work = fresh.freshLegs.size * LEG_MS + pins * PIN_MS;
  const k = work > DRAW_BUDGET_MS ? DRAW_BUDGET_MS / work : 1;
  const legMs = LEG_MS * k, pinMs = PIN_MS * k;

  // Walk the trip in order, handing out start times: pin 1, line, pin 2, line, pin 3 …
  let t = startDelay;
  const loop = isRoundTrip(s);
  s.trip.stops.forEach((stop, i) => {
    if (i > 0) {
      const a = s.trip.stops[i - 1];
      const isFresh = fresh.freshLegs.has(legKey(a, stop));
      const leg = s.trip.legs[i - 1];
      const raw = leg.path || [[a.lat, a.lon], [stop.lat, stop.lon]];
      const shared = pairCount[[a.id, stop.id].sort().join()] > 1;
      const points = shared ? shiftLeft(raw) : raw;
      // Hovering a line shows the leg (sticky = the tooltip follows the mouse along the line).
      const line = L.polyline(points, { color: ROUTE_COLOR, weight: isSel("leg", i - 1) ? 6 : 4, className: "trip-el" })
        .bindTooltip(`${a.name} → ${stop.name}: ~${leg.km} km, ${fmtTime(leg.car_min)}`, { sticky: true })
        .on("click", () => select("leg", i - 1))
        .addTo(tripLayer);
      const arrow = arrowMarker(points, isFresh, t + legMs).addTo(tripLayer);
      if (shared) sideLines.push({ line, points: raw, arrow });
      if (isFresh) { animateLine(line, t, legMs); t += legMs; }
    }

    const isFresh = fresh.freshStops.has(stop.id);
    if (loop && i === s.trip.stops.length - 1) return;      // round trip: back at the start, whose pin is already there
    L.marker([stop.lat, stop.lon], {
      icon: pinIcon((isFresh ? "pop " : "") + (isSel("stop", i) ? "sel" : ""), i + 1,
                    loop && i === 0 ? `${stop.name} ↺` : stop.name, isFresh ? t : 0),
      riseOnHover: true,    // a hovered pin comes to the front
      prio: isSel("stop", i) ? 3 : 2,   // used by declutterLabels(): the selected stop's name wins
    })
      .on("click", () => select("stop", i))
      .addTo(tripLayer);
    if (isFresh) t += pinMs;
  });
  drawEndsAt = performance.now() + t + 150;
  declutterLabels();
}

// Pin names can cover each other (and other pins) when the map is zoomed out. Show as many as fit:
// each name goes right of its pin, else left of it, else it is hidden (hovering the pin shows it).
// Priority: the selected stop, then the trip in order, then a question's options, then suggestions.
function declutterLabels() {
  const pins = [];
  [tripLayer, askLayer, sugLayer].forEach((group, g) => group.eachLayer(m => {
    const el = m.getElement && m.getElement();
    const label = el && el.querySelector(".pin-name");
    if (label) pins.push({ m, label, prio: m.options.prio ?? (g === 1 ? 1 : 0), p: map.latLngToContainerPoint(m.getLatLng()) });
  }));
  drawNames();   // the name layer keeps clear of whatever pins are on the map now
  pins.sort((a, b) => b.prio - a.prio);   // stable: same priority keeps trip order
  const taken = pins.map(({ p }) => ({ x0: p.x - 11, x1: p.x + 11, y0: p.y - 11, y1: p.y + 11 }));   // the dots
  const free = b => !taken.some(o => b.x0 < o.x1 && o.x0 < b.x1 && b.y0 < o.y1 && o.y0 < b.y1);
  for (const { label, p } of pins) {
    label.classList.remove("left", "hide");
    const w = label.offsetWidth, h = label.offsetHeight;
    const right = { x0: p.x + 13, x1: p.x + 13 + w, y0: p.y - h / 2, y1: p.y + h / 2 };
    const left = { x0: p.x - 13 - w, x1: p.x - 13, y0: right.y0, y1: right.y1 };
    if (free(right)) taken.push(right);
    else if (free(left)) { label.classList.add("left"); taken.push(left); }
    else label.classList.add("hide");
  }
}

// ---- Stops card: summary on top, then stop / leg / stop … rows ----
function drawStops(s, fresh) {
  const box = $("stops");
  box.innerHTML = "";
  const stops = s.trip.stops;
  if (!stops.length) {
    box.innerHTML = `<p class="label">TRIP</p><p class="empty">Your stops will show here, with the km and driving time of each leg.</p>`;
    return;
  }

  const km = s.trip.legs.reduce((sum, l) => sum + l.km, 0);
  const min = s.trip.legs.reduce((sum, l) => sum + (l.car_min || 0), 0);
  const places = isRoundTrip(s) ? stops.length - 1 : stops.length;
  const head = document.createElement("div");
  head.className = "list-head";
  head.innerHTML = `<p class="label">TRIP · ${places} ${places === 1 ? "PLACE" : "PLACES"}</p>` +
                   (s.trip.legs.length ? `<p class="trip-total">~${km} km · ${fmtTime(min)} driving</p>` : "");
  box.appendChild(head);

  // Add one clickable row to the list. New rows slide in, one after another.
  let n = 0;
  const addRow = (cls, left, right, onClick, isFresh, extra) => {
    const div = document.createElement("div");
    div.className = "item " + cls + (isFresh ? " enter" : "");
    if (isFresh) div.style.animationDelay = `${n++ * 60}ms`;
    div.innerHTML = `<span>${left}</span><span>${right}</span>`;
    clickable(div, onClick);
    if (extra) div.appendChild(extra);
    box.appendChild(div);
  };

  stops.forEach((stop, i) => {
    // ✕ removes this stop (the server does it, no agent call). A trip needs 2 stops, so it is off at 2.
    const rm = document.createElement("button");
    rm.className = "rm";
    rm.textContent = "✕";
    rm.title = stops.length > 2 ? `Remove ${stop.name}` : "A trip needs at least 2 stops";
    rm.setAttribute("aria-label", rm.title);
    rm.disabled = stops.length <= 2;
    rm.onclick = e => { e.stopPropagation(); send({ type: "remove", index: i }); };   // by position: a round trip has its start twice
    const back = isRoundTrip(s) && i === stops.length - 1;
    addRow("stop" + (isSel("stop", i) ? " selected" : ""),
           `${i + 1} ${escapeHtml(stop.name)}${back ? " (back to start)" : ""}`, escapeHtml(stop.state),
           () => select("stop", i), fresh.freshStops.has(stop.id), rm);

    const leg = s.trip.legs[i];
    if (leg) addRow("leg" + (isSel("leg", i) ? " selected" : ""), `↓ ~${leg.km} km`, `🚗 ${fmtTime(leg.car_min)}`,
                    () => select("leg", i), fresh.freshLegs.has(legKey(stop, stops[i + 1])));
  });
}

// ---- Tools row: Undo / Redo (no agent call), Share link, Open in Google Maps ----
function drawTools(s) {
  const has = s.trip.stops.length >= 2;
  $("tools").hidden = !(has || s.trip.can_undo || s.trip.can_redo);   // nothing to do yet: don't show 4 grey buttons
  $("undo").disabled = !s.trip.can_undo;
  $("redo").disabled = !s.trip.can_redo;
  $("share").disabled = !has;
  // Google Maps directions take at most 9 stops in between (11 in all): longer trips get no link.
  const fits = s.trip.stops.length <= 11;
  $("gmaps").classList.toggle("disabled", !has || !fits);
  $("gmaps").title = fits ? "Driving directions in Google Maps" : "Google Maps directions take at most 11 stops";
  if (has && fits) $("gmaps").href = googleMapsUrl(s);
}

// Google Maps directions for the whole trip (driving; at most 9 waypoints). No API key needed.
// Place names, not coordinates, so Google lists "Agra, Uttar Pradesh" instead of "27.18,78.01".
function googleMapsUrl(s) {
  const at = x => `${x.name}, ${x.state}, India`;
  const st = s.trip.stops;
  const q = new URLSearchParams({ api: "1", travelmode: "driving", origin: at(st[0]), destination: at(st[st.length - 1]) });
  if (st.length > 2) q.set("waypoints", st.slice(1, -1).map(at).join("|"));
  return "https://www.google.com/maps/dir/?" + q;
}

// Share link: the trip lives in the URL after "#", e.g. #trip=jammu,delhi,panaji
// The address bar always holds the current trip (syncHash), so a refresh or a reconnect brings it back,
// and Share just copies it. Opening such a link sends {"type":"load"} once connected.
function shareUrl(s) {
  return `${location.origin}${location.pathname}#trip=${s.trip.stops.map(x => x.id).join(",")}`;
}
function syncHash(s) {
  history.replaceState(null, "", s.trip.stops.length ? shareUrl(s) : location.pathname + location.search);
}
function tripFromHash() {
  const ids = new URLSearchParams(location.hash.slice(1)).get("trip");
  return ids ? ids.split(",") : null;
}
$("undo").onclick = () => send({ type: "undo" });
$("redo").onclick = () => send({ type: "redo" });
$("share").onclick = () => {
  navigator.clipboard.writeText(shareUrl(state)).then(() => {
    $("share").textContent = "Copied ✓";
    setTimeout(() => { $("share").textContent = "🔗 Share"; }, 1200);
  }).catch(() => {});
};

// ---- Trip bar: the whole trip as one straight line, start on the left, end on the right ----
// Each stop sits at (road km from the start ÷ total km), so the gaps show how long each leg is.
// The stop's name goes above its dot and the running km below. If a name would overlap the one before it,
// it moves below the line (in place of its km); if that is taken too, it is left out (hover shows it).
function drawTripBar(s, fresh) {
  const bar = $("tripbar");
  bar.innerHTML = "";
  if (s.trip.stops.length < 2) {
    bar.innerHTML = `<p class="tb-empty">Your trip will show here, start to end</p>`;
    return;
  }
  const total = s.trip.legs.reduce((sum, l) => sum + l.km, 0) || 1;
  const at = [0];
  s.trip.legs.forEach(l => at.push(at[at.length - 1] + l.km));
  const pct = km => (km / total) * 100;

  const track = document.createElement("div");
  track.className = "tb-track";
  bar.appendChild(track);

  s.trip.legs.forEach((leg, i) => {
    const seg = document.createElement("div");
    seg.className = "tb-seg" + (isSel("leg", i) ? " sel" : "");
    seg.style.left = pct(at[i]) + "%";
    seg.style.width = pct(leg.km) + "%";
    seg.title = `${s.trip.stops[i].name} → ${s.trip.stops[i + 1].name}: ~${leg.km} km, ${fmtTime(leg.car_min)}`;
    clickable(seg, () => select("leg", i));
    track.appendChild(seg);
  });

  const els = s.trip.stops.map((stop, i) => {
    const el = document.createElement("div");
    el.className = "tb-stop" + (isSel("stop", i) ? " sel" : "") + (fresh.freshStops.has(stop.id) ? " enter" : "");
    el.style.left = pct(at[i]) + "%";
    el.title = `${i + 1} ${stop.name}, ${stop.state} · ${at[i]} km from the start`;
    el.innerHTML = `<span class="tb-name">${escapeHtml(stop.name)}</span><span class="tb-dot">${i + 1}</span>` +
                   `<span class="tb-km">${at[i]} km</span>`;
    clickable(el, () => select("stop", i));
    track.appendChild(el);
    return el;
  });

  // Measured in pixels after the labels are on the page, so it works at any screen width.
  // A name matters more than a running km: to fit a name below the line, km labels before it give way.
  const GAP = 6;
  let topEnd = -Infinity;   // right edge of the last name above the line
  const low = [];           // labels below the line so far: {right, el, isKm}
  const fitsLow = left => !low.length || left > low[low.length - 1].right + GAP;
  for (const el of els) {
    const name = el.querySelector(".tb-name").getBoundingClientRect();
    const km = el.querySelector(".tb-km").getBoundingClientRect();
    if (name.left > topEnd + GAP) {
      topEnd = name.right;
      if (fitsLow(km.left)) low.push({ right: km.right, el, isKm: true }); else el.classList.add("nokm");
      continue;
    }
    const dropped = [];
    while (!fitsLow(name.left) && low.length && low[low.length - 1].isKm) dropped.push(low.pop());
    if (fitsLow(name.left)) {
      dropped.forEach(d => d.el.classList.add("nokm"));
      el.classList.add("low");
      low.push({ right: name.right, el, isKm: false });
    } else {
      low.push(...dropped.reverse());   // no room even so: keep those km labels, leave this name out
      el.classList.add("noname");
    }
  }
}
// The label fitting depends on the bar's width: redo it when the window size changes.
let resizeTimer = null;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => drawTripBar(state, NOTHING_FRESH), 150);
});

// ---- Owner extras: Trace link, token count, session id ----
function drawDev(s) {
  if (!OWNER) return;
  $("tokens").textContent = `${(s.ui.tokens || 0).toLocaleString()} tokens`;
  const a = $("trace-link");
  if (s.ui.trace_id) {
    a.href = `trace.html?id=${s.ui.trace_id}`;
    a.classList.remove("disabled");
    a.title = "See every LLM and tool call of the last turn";
  } else {
    a.removeAttribute("href");
    a.classList.add("disabled");
  }
  drawSession(s);
}

// Session id: click to copy, so a test case can be referred to.
function drawSession(s) {
  const b = $("session");
  if (!s.ui.session_id) return;
  b.hidden = false;
  b.textContent = `session ${s.ui.session_id} ⧉`;
  b.onclick = () => {
    const done = () => { b.textContent = "copied ✓"; setTimeout(() => drawSession(state), 1200); };
    try { navigator.clipboard.writeText(s.ui.session_id).then(done, () => {}); } catch (e) {}
  };
}

// ---- Model switch: a toggle between providers (Sarvam | Groq), plus a picker for that provider's models ----
// The server sends the list (ui.models: only models whose API key it has) and the one in use (ui.model).
// The browser only shows them and sends {"type":"model","id"}; the server answers with a new state.
// The choice is also remembered in this browser (localStorage), so a refresh or reconnect keeps it.
const MODEL_KEY = "llmModel";
let modelListSig = "";
let providerSig = "";

function drawModel(s) {
  const models = s.ui.models || [];
  $("llm").hidden = !models.length;
  if (!models.length) return;
  const current = models.find(m => m.id === s.ui.model) || models[0];

  // One button per provider, in the server's order; built once (rebuilt only if the providers change).
  const providers = [...new Map(models.map(m => [m.provider, m.provider_label])).entries()];
  const psig = providers.map(p => p[0]).join(",");
  const box = $("llm-providers");
  if (psig !== providerSig) {
    providerSig = psig;
    box.innerHTML = "";
    for (const [id, label] of providers) {
      const b = document.createElement("button");
      b.type = "button";
      b.dataset.provider = id;
      b.textContent = label;
      b.setAttribute("role", "radio");
      b.addEventListener("click", () => chooseProvider(id));
      box.appendChild(b);
    }
  }
  for (const b of box.children) b.setAttribute("aria-checked", String(b.dataset.provider === current.provider));

  // Rebuild the <select> only when its options change (rebuilding closes it if it is open).
  const mine = models.filter(m => m.provider === current.provider);
  const sig = mine.map(m => m.id).join(",");
  const sel = $("llm-model");
  if (sig !== modelListSig) {
    modelListSig = sig;
    sel.innerHTML = "";
    for (const m of mine) {
      const o = document.createElement("option");
      o.value = m.id;
      o.textContent = m.label;
      o.title = m.note;
      sel.appendChild(o);
    }
  }
  sel.value = current.id;
  sel.title = current.note;
  sel.disabled = mine.length < 2;
}

function chooseModel(id) {
  const m = (state.ui.models || []).find(x => x.id === id);
  try {
    localStorage.setItem(MODEL_KEY, id);
    if (m) localStorage.setItem(MODEL_KEY + ":" + m.provider, id);   // what the switch goes back to
  } catch (e) {}
  send({ type: "model", id });
}

// Provider toggle: go to that provider's model last used in this browser, else its first model.
function chooseProvider(provider) {
  const models = state.ui.models || [];
  const now = models.find(m => m.id === state.ui.model);
  if (now && now.provider === provider) return;
  let last = null;
  try { last = localStorage.getItem(MODEL_KEY + ":" + provider); } catch (e) {}
  const pick = models.find(m => m.id === last && m.provider === provider) || models.find(m => m.provider === provider);
  if (pick) chooseModel(pick.id);
}
$("llm-model").addEventListener("change", e => chooseModel(e.target.value));

// After (re)connecting: ask for the model this browser used last, if the server offers it.
function restoreModel(s) {
  let saved = null;
  try { saved = localStorage.getItem(MODEL_KEY); } catch (e) {}
  if (saved && saved !== s.ui.model && (s.ui.models || []).some(m => m.id === saved)) send({ type: "model", id: saved });
}

// ---- Examples: shown on an empty map, so a first-time visitor knows what to say ----
// A click sends the sentence like typed text (no microphone needed).
function drawExamples(s) {
  const q = s.ui.question;
  const show = !s.trip.stops.length && !s.ui.heard && s.ui.status === "idle" &&
               !(q && q.options.length) && !(s.ui.suggestions || []).length;
  const box = $("examples");
  box.hidden = !show;
  if (!show || box.childElementCount) return;
  const title = document.createElement("p");
  title.className = "label";
  title.textContent = "TRY ONE";
  const row = document.createElement("div");
  EXAMPLES.forEach(text => {
    const b = document.createElement("button");
    b.className = "chip";
    b.textContent = text;
    b.onclick = () => send({ type: "text", text });
    row.appendChild(b);
  });
  box.append(title, row);
}

// ---- A small popup on a suggestion or option pin: what it is, plus the action buttons ----
// Clicking such a pin only opens this; the button does the action (a click on a map is easy to make by mistake).
function pinPopup(title, note, actions) {
  const box = document.createElement("div");
  box.className = "pop";
  const t = document.createElement("b");
  t.textContent = title;                  // textContent only: names and notes go in as plain text
  box.appendChild(t);
  if (note) {
    const p = document.createElement("p");
    p.textContent = note;
    box.appendChild(p);
  }
  const row = document.createElement("div");
  row.className = "pop-actions";
  for (const [label, cls, fn] of actions) {
    const b = document.createElement("button");
    b.className = cls;
    b.textContent = label;
    b.onclick = () => { map.closePopup(); fn(); };
    row.appendChild(b);
  }
  box.appendChild(row);
  return box;
}

// ---- Suggestions: state.ui.suggestions = [{id, name, state, note, group, lat, lon}] ----
// Towns that split a long drive (made in code), or places the agent's suggest tools found (on the way / nearby).
// A card in the side panel (under its group's header) + a dashed pin on the map for each.
// "✓ Add" sends {"type":"add","pick":i}: the server puts the place on the way, no agent call.
// "✕" sends "dismiss": the card goes away and that place is not suggested again. "Hide" only hides the card.
let lastSuggestKey = "";
let shownSugIds = new Set();
let oldSugIds = new Set();     // the ones before the last change: those rows are drawn without the pop-in   // cards already on screen: they stay still; only new ones pop in
let sugHidden = false;      // "Hide" folds the card to its header and takes the pins off the map; "Show" brings both back

function drawSuggestions(s) {
  const items = s.ui.suggestions || [];
  const key = JSON.stringify(items);
  if (key === lastSuggestKey) return;
  lastSuggestKey = key;
  const added = items.some(x => !shownSugIds.has(x.id));
  oldSugIds = shownSugIds;
  shownSugIds = new Set(items.map(x => x.id));
  if (added) sugHidden = false;            // new suggestions: show them (only removed ones: leave the card as it is)
  renderSuggestions(s, items, added);      // no map re-fit when cards only went away: the trip draws undisturbed
}

function renderSuggestions(s, items, refit) {
  const box = $("suggest");
  box.innerHTML = "";
  sugLayer.clearLayers();
  box.hidden = items.length === 0;
  if (!items.length) { declutterLabels(); return; }

  // Header: title + Hide / Show
  const head = document.createElement("div");
  head.className = "sug-head";
  const title = document.createElement("p");
  title.className = "label";
  title.textContent = sugHidden ? `SUGGESTIONS (${items.length})` : "SUGGESTIONS";
  const toggle = document.createElement("button");
  toggle.className = "sug-hide";
  toggle.textContent = sugHidden ? "Show" : "Hide";
  toggle.onclick = () => { sugHidden = !sugHidden; oldSugIds = new Set(); renderSuggestions(s, items, !sugHidden); };
  head.append(title, toggle);
  box.appendChild(head);
  box.classList.toggle("folded", sugHidden);
  if (sugHidden) { declutterLabels(); return; }   // folded: no rows, no pins on the map

  const add = i => send({ type: "add", pick: i });
  const skip = i => send({ type: "dismiss", pick: i });
  let group = null;
  items.forEach((x, i) => {
    // A small header above each search's cards ("Within 200 km of Visakhapatnam"), written by the server.
    // (No group = no extra header: the card's own "SUGGESTIONS" title already says it.)
    if (x.group !== group) {
      group = x.group;
      if (group) {
        const g = document.createElement("p");
        g.className = "sug-group";
        g.textContent = group;
        box.appendChild(g);
      }
    }
    const row = document.createElement("div");
    row.className = "sug";
    if (oldSugIds.has(x.id)) row.style.animation = "none";   // a card that was already there does not pop in again
    else row.style.animationDelay = `${i * 60}ms`;
    // textContent only: names and notes go in as plain text
    const body = document.createElement("div");
    body.className = "sug-body";
    const name = document.createElement("div");
    name.className = "sug-name";
    name.textContent = x.name + " ";
    const st = document.createElement("span");
    st.textContent = x.state;
    name.appendChild(st);
    const note = document.createElement("p");
    note.className = "sug-note";
    note.textContent = x.note;
    body.append(name, note);
    const yes = document.createElement("button");
    yes.className = "sug-add";
    yes.textContent = "✓ Add";
    yes.onclick = () => add(i);
    const no = document.createElement("button");
    no.className = "sug-skip";
    no.textContent = "✕";
    no.title = `Skip ${x.name}`;
    no.setAttribute("aria-label", no.title);
    no.onclick = () => skip(i);
    row.append(body, yes, no);
    box.appendChild(row);
    if (x.lat != null)
      L.marker([x.lat, x.lon], { icon: pinIcon("sug-pin pop", "★", x.name, 0), riseOnHover: true })
        .bindPopup(pinPopup(`${x.name}, ${x.state}`, x.note, [["✓ Add", "sug-add", () => add(i)],
                                                                ["Skip", "pop-skip", () => skip(i)]]),
                   { closeButton: false })
        .addTo(sugLayer);
  });
  if (refit) fitMapToTrip(s, items.filter(x => x.lat != null).map(x => [x.lat, x.lon]));
  declutterLabels();
}

// ---- Follow-ups: state.ui.followups = ["I'm going by train", ...]; a click sends that text as a normal message ----
let lastFollowKey = "";
function drawFollowups(s) {
  const items = s.ui.followups || [];
  const key = JSON.stringify(items);
  if (key === lastFollowKey) return;
  lastFollowKey = key;
  const box = $("followups");
  box.innerHTML = "";
  box.hidden = items.length === 0;
  items.forEach(text => {
    const b = document.createElement("button");
    b.className = "chip";
    b.textContent = text;                  // textContent only: it comes from the model
    b.onclick = () => send({ type: "text", text });
    box.appendChild(b);
  });
}

// ---- Proposal from the frontier planner: state.ui.proposal (a ghost route + a card; NOT the trip) ----
// kind "itinerary": {title, tagline, stops:[{id,name,state,lat,lon,why}], legs, total_km, total_min, days_estimate,
//                    reasoning, dropped}  -> red dashed route with numbered ghost pins.
// kind "compare":   {options:[{key:"A"|"B", label, stops, legs, total_km, total_min, pros, cons}], recommended,
//                    short_answer}  -> route A red, route B purple, each with a label chip halfway along.
// Dashed = proposed, solid blue = the trip. The trip is dimmed while a proposal is up. Buttons send
// {type:"proposal", action:"accept"|"decline"|"remove", option, index}; the server changes the trip (or not).
const PROPOSAL_COLORS = { itinerary: "#d92d20", A: "#d92d20", B: "#7a5af8" };
const ASK_MORE = ["Kam din karo", "Aur hill stations", "Budget friendly"];
map.createPane("proposal").style.zIndex = 450;               // above the trip's lines, under all pins
const proposalRenderer = L.svg({ pane: "proposal" });
const proposalLayer = L.layerGroup().addTo(map);
let lastProposalKey = "null";
let viewBeforeProposal = null;   // {center, zoom, trip}: put the map back when a proposal is declined
let ghostPins = [];

function halfwayPoint(points) {   // the point halfway along a line, by distance (same idea as arrowMarker)
  const px = points.map(p => map.project(L.latLng(p), 0));
  let total = 0;
  const seg = px.slice(1).map((p, j) => { const d = p.distanceTo(px[j]); total += d; return d; });
  let j = 0, run = 0;
  while (j < seg.length - 1 && run + seg[j] < total / 2) run += seg[j++];
  const a = px[j], b = px[j + 1] || px[j], f = seg[j] ? (total / 2 - run) / seg[j] : 0;
  return map.unproject(L.point(a.x + (b.x - a.x) * f, a.y + (b.y - a.y) * f), 0);
}

function routePoints(route) {     // all points of a route, leg after leg (straight line where a leg has no path)
  const pts = [];
  route.legs.forEach((leg, i) => {
    const a = route.stops[i], b = route.stops[i + 1];
    pts.push(...(leg.path || [[a.lat, a.lon], [b.lat, b.lon]]));
  });
  return pts;
}

function drawGhostRoute(route, color, numbered) {
  route.legs.forEach((leg, i) => {
    const a = route.stops[i], b = route.stops[i + 1];
    L.polyline(leg.path || [[a.lat, a.lon], [b.lat, b.lon]],
               { color, weight: 4, dashArray: "8 7", lineCap: "round", renderer: proposalRenderer, interactive: false })
      .addTo(proposalLayer);
  });
  if (!numbered) return;
  route.stops.forEach((st, i) => {
    const m = L.marker([st.lat, st.lon], {
      icon: L.divIcon({
        className: "pin-wrap ghost-wrap",
        html: `<div class="pin ghost pop" style="--c:${color};animation-delay:${i * 60}ms"><span class="pin-num">${i + 1}</span>` +
              `<span class="pin-name">${escapeHtml(st.name)}</span></div>`,
        iconSize: [22, 22], iconAnchor: [11, 11],
      }),
      zIndexOffset: 500,
    }).addTo(proposalLayer);
    ghostPins.push(m);
  });
}

function drawProposal(s) {
  const p = s.ui.proposal;
  const key = JSON.stringify(p);
  $("stops").hidden = !!p;                              // the proposal card takes the trip card's place
  if (key === lastProposalKey) return;
  const wasShown = lastProposalKey !== "null";
  lastProposalKey = key;
  proposalLayer.clearLayers();
  ghostPins = [];
  const box = $("proposal");
  box.innerHTML = "";
  box.hidden = !p;
  $("map").classList.toggle("proposing", !!p);
  const tripKey = s.trip.stops.map(x => x.id).join(",");
  if (!p) {
    // Declined (trip unchanged): go back to where the map was. Accepted: the trip's own fit takes over.
    if (viewBeforeProposal && viewBeforeProposal.trip === tripKey)
      map.flyTo(viewBeforeProposal.center, viewBeforeProposal.zoom, { duration: FLY_MS / 1000 });
    viewBeforeProposal = null;
    return;
  }
  if (!wasShown) viewBeforeProposal = { center: map.getCenter(), zoom: map.getZoom(), trip: tripKey };

  const routes = p.kind === "itinerary" ? [p] : p.options;
  // The recommended option is drawn last, so it sits on top where the two share a road.
  const order = p.kind === "compare" ? [...routes].sort((a, b) => (a.key === p.recommended) - (b.key === p.recommended)) : routes;
  order.forEach(r => {
    const color = PROPOSAL_COLORS[p.kind === "itinerary" ? "itinerary" : r.key];
    drawGhostRoute(r, color, p.kind === "itinerary");
    if (p.kind === "compare" && r.stops.length > 1) {
      L.marker(halfwayPoint(routePoints(r)), {
        interactive: false, keyboard: false, zIndexOffset: 800,
        icon: L.divIcon({ className: "route-chip-wrap", iconSize: [0, 0],
                          html: `<div class="route-chip ${r.key === "A" ? "left" : "right"}" style="--c:${color}">${escapeHtml(r.key)} · ${escapeHtml(r.label)} · ${Math.round(r.total_min / 60)} h</div>` }),
      }).addTo(proposalLayer);
    }
  });
  const pts = routes.flatMap(r => r.stops.map(x => [x.lat, x.lon]));
  if (pts.length) map.flyToBounds(L.latLngBounds(pts), { paddingTopLeft: [60, 60], paddingBottomRight: [80, 60],
                                                         maxZoom: 7, duration: FLY_MS / 1000 });
  if (p.kind === "itinerary") buildItineraryCard(p, box); else buildCompareCard(p, box);
  // Phone: the side panel sits under the map, so a new proposal's card (and its buttons) would be off screen.
  // Bring it up once the map has flown to the ghost route.
  if (!wasShown && window.matchMedia("(max-width: 760px)").matches)
    setTimeout(() => box.scrollIntoView({ behavior: "smooth", block: "start" }), FLY_MS + 300);
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;           // textContent: everything here comes from a model
  return e;
}

function proposalButtons(box, buttons) {
  const row = el("div", "prop-actions");
  buttons.forEach(([label, cls, onClick]) => { const b = el("button", cls, label); b.onclick = onClick; row.appendChild(b); });
  const more = el("div", "prop-more");
  more.hidden = true;
  ASK_MORE.forEach(t => { const c = el("button", "chip", t); c.onclick = () => send({ type: "text", text: t }); more.appendChild(c); });
  box.append(row, more);
  return () => { more.hidden = !more.hidden; $("text").focus(); };
}

function buildItineraryCard(p, box) {
  const head = el("div", "prop-head");
  head.append(el("p", "label", "PROPOSED TRIP"), el("span", "prop-badge", "Planner"));
  box.append(head, el("h2", "prop-title", p.title));
  if (p.tagline) box.appendChild(el("p", "prop-tagline", p.tagline));
  const list = el("ol", "prop-stops");
  p.stops.forEach((st, i) => {
    const li = el("li", "prop-stop");
    const num = el("span", "prop-num", String(i + 1));
    const body = el("div", "prop-body");
    const name = el("div", "prop-name", st.name);
    name.appendChild(el("span", "", ` ${st.state}`));
    body.appendChild(name);
    if (st.why) body.appendChild(el("div", "prop-why", st.why));
    const x = el("button", "prop-x", "✕");
    x.title = `Remove ${st.name} from the plan`;
    x.onclick = () => send({ type: "proposal", action: "remove", index: i });
    li.append(num, body, x);
    // Hovering a row lights up its pin on the map.
    const pinEl = () => ghostPins[i] && ghostPins[i].getElement();
    li.onmouseenter = () => { const e = pinEl(); if (e) e.classList.add("hl"); };
    li.onmouseleave = () => { const e = pinEl(); if (e) e.classList.remove("hl"); };
    list.appendChild(li);
  });
  box.appendChild(list);
  const days = p.days_estimate ? ` · ~${p.days_estimate} ${p.days_estimate === 1 ? "day" : "days"}` : "";
  box.appendChild(el("p", "prop-total", `~${p.total_km} km · ${fmtTime(p.total_min)} driving${days}`));
  if (p.reasoning) box.appendChild(el("p", "prop-reason", p.reasoning));
  if (p.dropped && p.dropped.length) box.appendChild(el("p", "prop-reason", `Not in our place list: ${p.dropped.join(", ")}`));
  const toggleMore = proposalButtons(box, [
    ["✓ Accept", "prop-accept", () => send({ type: "proposal", action: "accept" })],
    ["Decline", "prop-plain", () => send({ type: "proposal", action: "decline" })],
    ["Ask more", "prop-plain", () => toggleMore()],
  ]);
}

function buildCompareCard(p, box) {
  const head = el("div", "prop-head");
  head.append(el("p", "label", "COMPARE ROUTES"), el("span", "prop-badge", "Planner"));
  box.appendChild(head);
  if (p.short_answer) box.appendChild(el("p", "prop-tagline", p.short_answer));
  const cols = el("div", "prop-cols");
  p.options.forEach(o => {
    const col = el("div", "prop-col" + (o.key === p.recommended ? " rec" : ""));
    col.style.setProperty("--c", PROPOSAL_COLORS[o.key]);
    const t = el("div", "prop-col-title");
    t.append(el("span", "prop-dot"), el("span", "", `${o.key} · ${o.label}`));
    col.appendChild(t);
    if (o.key === p.recommended) col.appendChild(el("span", "prop-rec", "Recommended"));
    col.appendChild(el("p", "prop-via", o.stops.map(x => x.name).join(" → ")));
    col.appendChild(el("p", "prop-stats", `${o.total_km} km · ${fmtTime(o.total_min)}`));
    const ul = el("ul", "prop-pc");
    o.pros.forEach(x => ul.appendChild(el("li", "pro", x)));
    o.cons.forEach(x => ul.appendChild(el("li", "con", x)));
    col.appendChild(ul);
    cols.appendChild(col);
  });
  box.appendChild(cols);
  const toggleMore = proposalButtons(box, [
    ["Choose A", "prop-accept", () => send({ type: "proposal", action: "accept", option: "A" })],
    ["Choose B", "prop-accept b", () => send({ type: "proposal", action: "accept", option: "B" })],
    ["Keep current", "prop-plain", () => send({ type: "proposal", action: "decline" })],
    ["Ask more", "prop-plain", () => toggleMore()],
  ]);
}

// ---- Spoken lines (voice turns only): {type:"speech", mime, data}. Played one after another. ----
let speechQueue = [], speaking = null;
function playSpeech(msg) {
  speechQueue.push(`data:${msg.mime};base64,${msg.data}`);
  if (!speaking) playNextSpeech();
}
function playNextSpeech() {
  const src = speechQueue.shift();
  if (!src) { speaking = null; return; }
  speaking = new Audio(src);
  speaking.onended = speaking.onerror = playNextSpeech;
  speaking.play().catch(playNextSpeech);         // autoplay blocked: skip it, the text is on screen
}
function stopSpeech() {
  speechQueue = [];
  if (speaking) { speaking.onended = speaking.onerror = null; speaking.pause(); speaking = null; }
}

// ---- Question card: the agent asked something and offered options ----
// state.ui.question = {text, options: [...], spots: [...]}, or null when nothing is pending.
// Clicking an option's button (or "Choose" in its pin's popup) sends {"type":"choose","text":label}:
// the agent gets it as a "choice".
let lastQuestionKey = "";

function drawQuestion(s) {
  const q = s.ui.question;
  const key = JSON.stringify(q);
  if (key === lastQuestionKey) return;     // same question as before: leave it alone
  lastQuestionKey = key;

  const box = $("ask");
  if (!q || !q.options.length) { hideQuestion(); return; }

  // If the route is still drawing, open the card when it is done, so the two don't compete.
  const wait = drawEndsAt - performance.now();
  if (wait > 0) { setTimeout(() => { if (lastQuestionKey === key) buildQuestion(s, q, box); }, wait); return; }
  buildQuestion(s, q, box);
}

function buildQuestion(s, q, box) {
  box.innerHTML = "";
  box.className = "ask";
  box.hidden = false;

  const title = document.createElement("p");
  title.className = "ask-q";
  title.textContent = q.text;                // textContent, not innerHTML: the text comes from the model
  box.appendChild(title);

  const opts = document.createElement("div");
  opts.className = "ask-opts";
  box.appendChild(opts);

  const choose = i => {
    send({ type: "choose", text: q.options[i] });
    hideQuestion();
  };

  q.options.forEach((label, i) => {
    const b = document.createElement("button");
    b.className = "ask-opt";
    b.textContent = label;
    b.style.animationDelay = `${120 + i * 50}ms`;   // options appear one after another
    b.onclick = () => choose(i);
    opts.appendChild(b);
  });

  // The same options on the map, so you can see where each one is before choosing.
  askLayer.clearLayers();
  const spots = q.spots || [];
  q.options.forEach((label, i) => {
    const spot = spots[i];
    if (spot) L.marker([spot.lat, spot.lon], { icon: pinIcon("cand pop", "+", spot.name, 0), riseOnHover: true })
      .bindPopup(pinPopup(label, null, [["Choose", "sug-add", () => choose(i)]]), { closeButton: false })
      .addTo(askLayer);
  });

  const foot = document.createElement("div");
  foot.className = "ask-foot";
  foot.textContent = "Or type your own answer below";
  box.appendChild(foot);

  // The card sits over the top of the map: move the map so the trip and the options show below it.
  const extra = spots.filter(Boolean).map(x => [x.lat, x.lon]);
  if (s.trip.stops.length || extra.length) fitMapToTrip(s, extra);
  declutterLabels();
}

function hideQuestion() {
  const box = $("ask");
  askLayer.clearLayers();
  declutterLabels();
  if (box.hidden) return;
  box.classList.add("leaving");                     // fade out, then hide
  setTimeout(() => { if (box.classList.contains("leaving")) box.hidden = true; }, 200);
}

// A click on a pin, a line, a row or the trip bar. Clicking the selected thing again unselects it.
function select(type, index) {
  selected = isSel(type, index) ? null : { type, index };
  render(state);   // nothing is "fresh" now (drawn is up to date), so nothing replays
  // On a wide screen the side panel scrolls on its own: bring the selected row into view.
  if (selected && matchMedia("(min-width: 761px)").matches) {
    const row = document.querySelector("#stops .selected");
    if (row) row.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }
}

// ---- Progress: what the agent is doing right now ----
// Server messages: {type:"progress", event:"start"} / {event:"step", id, label, status, pin} / {event:"end"}.
// Steps often arrive in a burst (a tool call takes 0.1 ms), so they go through a small queue that shows
// each new step ~140 ms after the previous one. Otherwise the strip would just flash.
let queue = [];
let pumping = false;
const STEP_GAP_MS = 140;

function handleProgress(msg) {
  queue.push(msg);
  if (!pumping) pump();
}

function pump() {
  const msg = queue.shift();
  if (!msg) { pumping = false; return; }
  pumping = true;
  const created = applyProgress(msg);
  setTimeout(pump, created ? STEP_GAP_MS : 0);
}

// Applies one progress message to the page. Returns true if it added a new step (so the queue waits a bit).
function applyProgress(msg) {
  const strip = $("activity");
  if (msg.event === "start") {
    strip.innerHTML = "";
    strip.classList.add("on");
    startTrail();
    return false;
  }
  if (msg.event === "end") {
    pendingLayer.clearLayers();
    finishTrail();
    // Let the last step be seen for a moment, then swap the strip for the reply (both sit in the same slot).
    setTimeout(() => {
      strip.classList.remove("on");
      const m = $("message");
      m.classList.remove("fade-in"); void m.offsetWidth; if (m.textContent) m.classList.add("fade-in");
    }, 150);
    return false;
  }
  if (msg.event !== "step") return false;

  let step = strip.querySelector(`[data-id="${msg.id}"]`);
  const isNew = !step;
  if (isNew) {
    step = document.createElement("span");
    step.dataset.id = msg.id;
    strip.appendChild(step);
    while (strip.children.length > 5) strip.firstChild.remove();   // one row only: keep the latest steps
  }
  step.className = "step " + msg.status + (isNew ? " enter" : "");
  step.textContent = msg.label;
  trailRow(msg);

  if (msg.pin && msg.status === "done") showPendingPin(msg.pin);
  return isNew;
}

// ---- Agent activity card: one row per LLM step / tool call, kept after the turn until the next one starts ----
let trailStart = 0;
function startTrail() {
  $("trail-list").innerHTML = "";
  $("trail-sum").textContent = "working…";
  $("trail").hidden = false;
  trailStart = performance.now();
}

function trailRow(msg) {
  if (!msg.kind) return;
  const list = $("trail-list");
  let row = list.querySelector(`[data-id="${msg.id}"]`);
  const isNew = !row;
  if (isNew) {
    row = document.createElement("li");
    row.dataset.id = msg.id;
    list.appendChild(row);
  }
  row.className = `tr ${msg.kind} ${msg.status}` + (isNew ? " enter" : "");
  row.textContent = "";                                    // textContent only: tool arguments come from the model
  const add = (parent, tag, cls, text) => {
    const el = document.createElement(tag); el.className = cls; el.textContent = text; parent.appendChild(el); return el;
  };
  add(row, "span", "tr-dot", "");
  const main = add(row, "div", "tr-main", "");
  const name = add(main, "div", "tr-name", msg.kind === "llm" ? "Thinking" : msg.name);
  if (msg.kind === "llm") add(name, "span", "tr-tag", msg.name);
  if (msg.detail) add(main, "div", "tr-args", msg.detail);
  if (msg.result) add(main, "div", "tr-res", msg.result);
  add(row, "span", "tr-ms", msg.ms == null ? "" : msg.ms >= 1000 ? (msg.ms / 1000).toFixed(1) + " s" : msg.ms + " ms");
  list.scrollTop = list.scrollHeight;
}

function finishTrail() {
  const rows = $("trail-list").children.length;
  const tools = $("trail-list").querySelectorAll(".tr.tool").length;
  $("trail-sum").textContent = rows
    ? `${tools} tool call${tools === 1 ? "" : "s"} · ${((performance.now() - trailStart) / 1000).toFixed(1)} s`
    : "";
  if (!rows) $("trail").hidden = true;
}

// A faint dashed pin for a place the agent just found, before the route is drawn.
function showPendingPin(pin) {
  L.marker([pin.lat, pin.lon], { icon: pinIcon("pop pending", "", "", 0), interactive: false })
    .bindTooltip(pin.name, { permanent: true, direction: "top", offset: [0, -8], className: "pending-tip" })
    .addTo(pendingLayer);
}

// ---- WebSocket: the one connection to the backend ----
// Up:   {"type": "text", "text": "..."}  or  {"type": "audio", "mime": "audio/webm", "data": "<base64>"}  (and clicks)
// Down: the full trip state -> render it; or {"type":"progress", ...} -> activity strip
// If the connection drops, it reconnects by itself (waiting 1, 2, 4 … up to 10 s). The server keeps no trip for a
// new connection, so the trip in the address bar (#trip=…) is loaded again.
let ws = null;
let retries = 0;
function connect() {
  ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  let first = true;                                         // the first message after connecting = the server's state
  ws.onopen = () => {
    retries = 0;
    const ids = tripFromHash();
    if (ids) send({ type: "load", ids });
  };
  ws.onmessage = e => {
    const msg = JSON.parse(e.data);
    if (msg.type === "progress") handleProgress(msg);
    else if (msg.type === "speech") playSpeech(msg);
    else {
      setState(msg);
      if (first) { first = false; restoreModel(msg); }
    }
  };
  ws.onclose = e => {
    setStatus("offline");
    if (e.code === 1008) { setMessage("This page can't connect to this server."); return; }   // wrong site: retrying won't help
    // 1013 = server full (too many tabs): wait longer before trying again.
    const wait = e.code === 1013 ? 10000 : Math.min(1000 * 2 ** retries, 10000);
    retries++;
    setMessage(e.code === 1013 ? "The server is busy. Trying again shortly…" : "Connection lost. Reconnecting…");
    setTimeout(connect, wait);
  };
}
if (location.protocol.startsWith("http")) connect();   // the page must be served by app/main.py (not opened as a file)

// A different shared link pasted into this tab only changes the part after "#": the page doesn't reload,
// so load that trip here. (syncHash uses replaceState, which doesn't fire this event.)
window.addEventListener("hashchange", () => {
  const ids = tripFromHash();
  if (ids && ids.join(",") !== state.trip.stops.map(x => x.id).join(",")) send({ type: "load", ids });
});

// Replace the whole state and redraw.
function setState(newState) {
  const tripChanged = newState.trip.version !== state.trip.version;
  state = newState;
  if (selected && selected.type === "leg" && !state.trip.legs[selected.index]) selected = null;
  if (selected && selected.type === "stop" && !state.trip.stops[selected.index]) selected = null;
  if (tripChanged) {
    // The real route is here: preview pins that are still waiting in the queue are no longer useful.
    queue.forEach(m => { m.pin = null; });
    pendingLayer.clearLayers();
    syncHash(state);
  }
  render(state);
}

function send(msg) {
  if (msg.type === "text" || msg.type === "audio" || msg.type === "choose") stopSpeech();   // a new message: old audio is stale
  if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg));
}

// ---- Text box: Enter sends ----
$("text").addEventListener("keydown", e => {
  if (e.key === "Enter" && e.target.value.trim()) {
    send({ type: "text", text: e.target.value.trim() });
    e.target.value = "";
  }
});

// ---- Hold Space to talk ----
// How it works (browser recording, no library):
//   1. The first press asks for microphone permission (a browser popup). The mic stream is then kept, so
//      later presses start instantly and the first word is not cut off.
//   2. MediaRecorder records the stream while Space is down and hands over small chunks of audio.
//   3. On release, the chunks become one compressed clip (webm in Chrome/Firefox, mp4 in Safari). It is sent
//      as base64 text: {type:"audio", mime, data}. The server runs Sarvam speech-to-text, shows the
//      transcript as "Heard: ..." and continues exactly like typed text.
// Needs https:// or localhost: browsers block the microphone on plain http pages elsewhere.
const MAX_RECORD_MS = 15000;   // stop by itself after 15 s (the server rejects longer clips anyway)
const MIN_RECORD_MS = 400;     // shorter than this is an accidental tap
let mic = null;                // the microphone stream, requested once
let recorder = null, chunks = [], recordStart = 0, recordTimer = null;
let holding = false;           // is Space (or the talk button) down right now?

function setListening(on) {
  $("talk").classList.toggle("listening", on);
  $("talk").textContent = on ? "🎙 Listening…" : TOUCH ? "🎙 Hold" : "🎙 Hold Space";
  setStatus(on ? "listening" : state.ui.status);
}

async function startRecording() {
  stopSpeech();                 // the user is talking: don't talk over them
  holding = true;
  setListening(true);
  try {
    if (!mic) mic = await navigator.mediaDevices.getUserMedia({ audio: true });   // may show a permission popup
  } catch (e) {
    holding = false;
    setListening(false);
    setMessage("Allow the microphone in your browser to talk, or type below.");
    return;
  }
  if (!holding) return;   // Space was released while the permission popup was open
  const type = ["audio/webm;codecs=opus", "audio/webm", "audio/mp4"].find(t => MediaRecorder.isTypeSupported(t));
  chunks = [];
  recorder = new MediaRecorder(mic, type ? { mimeType: type } : {});
  recorder.ondataavailable = e => { if (e.data.size) chunks.push(e.data); };
  recorder.onstop = () => sendRecording(recorder.mimeType);
  recorder.start();
  recordStart = performance.now();
  recordTimer = setTimeout(stopRecording, MAX_RECORD_MS);
}

function stopRecording() {
  holding = false;
  setListening(false);
  clearTimeout(recordTimer);
  if (recorder && recorder.state === "recording") recorder.stop();   // triggers recorder.onstop -> sendRecording
}

function sendRecording(mime) {
  const tooShort = performance.now() - recordStart < MIN_RECORD_MS;
  const blob = new Blob(chunks, { type: mime });
  chunks = [];
  recorder = null;
  if (tooShort) {
    setMessage(TOUCH ? "Hold the button while you speak, then let go." : "Hold Space while you speak, then let go.");
    return;
  }
  const reader = new FileReader();
  reader.onload = () => send({ type: "audio", mime, data: reader.result.split(",")[1] });   // "data:...;base64,XXXX" -> XXXX
  reader.readAsDataURL(blob);
}

document.addEventListener("keydown", e => {
  if (e.code === "Space" && !e.repeat && document.activeElement !== $("text")) {
    e.preventDefault();
    startRecording();
  }
});
document.addEventListener("keyup", e => {
  if (e.code === "Space" && holding) stopRecording();
});
// If the window loses focus while Space is down, the browser never sends the keyup: treat it as a release.
window.addEventListener("blur", () => { if (holding) stopRecording(); });

// Phone: hold the 🎙 button (pointer events cover finger, pen and mouse). Pointer capture keeps the
// release event coming to the button even if the finger slides off it.
$("talk").addEventListener("pointerdown", e => { e.preventDefault(); $("talk").setPointerCapture(e.pointerId); startRecording(); });
$("talk").addEventListener("pointerup", stopRecording);
$("talk").addEventListener("pointercancel", stopRecording);
$("talk").addEventListener("contextmenu", e => e.preventDefault());   // long-press menu would cancel the hold
setListening(false);

render(state);

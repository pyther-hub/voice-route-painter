"""The web server: serves static/ and one WebSocket per browser tab.

    uvicorn app.main:app --reload        then open http://localhost:8000

Protocol (matches static/app.js):
    browser -> server   {"type": "text", "text": "Agra hata do"}
    browser -> server   {"type": "choose", "text": "Aurangabad, Bihar"}   (clicked option of the question card: runs
                        the agent with user_input.type = "choice")
    browser -> server   {"type": "add", "pick": 0}   ("✓ Add" on a long-drive card: inserted by the server, no agent call)
    browser -> server   {"type": "remove", "index": 2}   (✕ on the 3rd stop: removed by the server, no agent call)
    browser -> server   {"type": "undo"} / {"type": "redo"}   (buttons; no agent call)
    browser -> server   {"type": "load", "ids": ["jammu", "delhi"]}   (a shared #trip= link was opened)
    browser -> server   {"type": "dismiss", "pick": 0}   (skip a suggestion card: removed, never suggested again)
    browser -> server   {"type": "model", "id": "sarvam/gemma4"}   (the Sarvam ⇄ Groq switch / model picker: the
                        next turn uses it; no agent call)
    browser -> server   {"type": "audio", "mime": "audio/webm", "data": "<base64>"}   (hold Space, release)
                        the server turns the audio into text with Sarvam (app/stt.py), shows it as
                        "heard", and from there it is the same as a typed message.
    server -> browser   the FULL trip state (see state.empty_state()), after every change
    browser -> server   {"type": "proposal", "action": "accept" | "decline" | "remove", "option": "A", "index": 2}
                        (buttons on the planner's proposal card; no agent call)
    server -> browser   {"type": "speech", "mime": "audio/wav", "data": "<base64>"}   (voice turns only: the planner's
                        filler line and its short answer, spoken by Sarvam TTS in the language the user spoke)
    server -> browser   {"type": "progress", "event": "start" | "step" | "end", ...} while a turn runs:
                        what the agent is doing right now (steps: id, label, status, optional pin).
                        Progress is display-only; the trip itself always arrives as the full state.

One WebSocket = one session = one trip. Turns run one at a time per session: the next message
is read only after the current turn finishes.
"""
import asyncio
import base64
import binascii
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.staticfiles import StaticFiles

from app import agent, geo, guard, llm, places, state, stt, tools, trace, tts

# Product hygiene (Origin check, rate limits, spend cap, trace key) lives in app/guard.py.
# TODO(auth): no accounts; anyone who opens the site can use it, within the limits in guard.py.
# TODO(deploy): run behind HTTPS (wss://) somewhere public.

MAX_TEXT_LEN = 300   # input limit: caps cost per message and how much a pasted prompt can carry
MAX_MESSAGE_CHARS = 1_400_000   # a JSON message bigger than this (audio is base64, ~4/3 of its bytes) is dropped unread 15 sec is about 40-80kb

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
app = FastAPI()
# Compress responses: the road layers are up to ~1 MB of JSON each, a few hundred KB gzipped.
# Level 6, not the default 9: within 1% of the size at under half the CPU (compression runs on the event loop,
# so while a big file is being compressed every open socket waits).
app.add_middleware(GZipMiddleware, minimum_size=1000, compresslevel=6)
places.load()        # load data/places.json once at startup, not on the first message
geo.load()           # and the road graph (data/roads.bin, ~0.1 s, ~20 MB in memory)


def parse_message(raw: str) -> tuple[dict | None, str | None]:
    """Return (message dict, None), or (None, error to show the user)."""
    if len(raw) > MAX_MESSAGE_CHARS:
        return None, "Message bahut bada hai."
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return None, "Message samajh nahi aaya (invalid JSON)."
    if not isinstance(msg, dict):
        return None, "Message samajh nahi aaya."
    return msg, None


def check_text(text) -> tuple[str | None, str | None]:
    """Return (clean text, None), or (None, error to show the user). Typed and transcribed text both pass here."""
    if not isinstance(text, str):
        return None, 'Expected {"type": "text", "text": "..."}.'
    text = text.strip()
    if not text:
        return None, "Kuch likhiye, jaise: Lucknow se Jaipur jaana hai."
    if len(text) > MAX_TEXT_LEN:
        return None, f"Message bahut lamba hai (max {MAX_TEXT_LEN} characters)."
    return text, None


async def transcribe_audio(session_id: str, msg: dict) -> tuple[str | None, str | None]:
    """Audio message -> (transcript, None), or (None, error to show the user)."""
    try:
        audio = base64.b64decode(msg.get("data") or "", validate=True)
    except (binascii.Error, ValueError, TypeError):
        return None, "Audio samajh nahi aaya."
    if (blocked := guard.over_budget(session_id)):
        return None, blocked
    
    names = [stop["name"] for stop in state.trip(session_id)["stops"]]
    
    try:
        text, language = await stt.transcribe_detect(audio, str(msg.get("mime") or "audio/webm"),
                                                    stt.keyterms_for(names))
    except stt.STTError as e:
        return None, str(e)
    guard.charge(session_id, guard.STT_COST_USD)
    # A voice turn: the language the user spoke (one Bulbul can speak; others fall back to Hindi). The agent and the
    # planner write their spoken lines in it, and TTS speaks them with that language's voice.
    state.session(session_id).speak_in = tts.language_for(language)
    return text, None


async def read_input(session_id: str, raw: str, ip: str = "local") -> tuple[str | None, str | None, str]:
    """One WebSocket message -> (text for the agent, None, kind), or (None, error to show the user, kind).
    kind = "text" (typed or spoken) or "choice" (a clicked question option). (None, None, _) = handled here."""
    msg, error = parse_message(raw)
    if error:
        return None, error, "text"
    text, error = await _read(session_id, msg, ip)
    return text, error, "choice" if msg.get("type") == "choose" else "text"


async def _read(session_id: str, msg: dict, ip: str) -> tuple[str | None, str | None]:
    if not guard.allow(ip, "any", guard.ANY_PER_MIN) or (
            msg.get("type") in ("text", "audio", "choose") and not guard.allow(ip, "paid", guard.PAID_PER_MIN)):
        return None, guard.OVER_LIMIT
    if msg.get("type") in ("text", "choose"):
        return check_text(msg.get("text"))
    if msg.get("type") == "audio":
        # Show "transcribing" while Sarvam works, and clear the previous reply and question.
        state.set_display(session_id, heard="", heard_marks=[], status="transcribing", message="", question=None,
                          suggestions=[])
        
        text, error = await transcribe_audio(session_id, msg)
        return (None, error) if error else check_text(text)     # a transcript can be too long too
    direct = {   # clicks whose effect is known: applied here with no agent call (label = what the agent sees later)
        "remove": lambda: tools.remove_at(session_id, msg.get("index")),
        "add": lambda: tools.add_suggestion(session_id, msg.get("pick")),
        "undo": lambda: tools.undo_redo(session_id, "undo"),
        "redo": lambda: tools.undo_redo(session_id, "redo"),
        "load": lambda: tools.load_trip(session_id, msg.get("ids")),
        "proposal": lambda: tools.proposal_click(session_id, msg.get("action"), msg.get("option"), msg.get("index")),
    }
    
    if msg.get("type") in direct:
        kind = msg["type"]
        state.set_display(session_id, question=None, status="thinking")      # other cards stay: add several
        with trace.span("agent", kind, input={**msg, "session": session_id}) as root:
            reply = direct[kind]()
            root["output"] = {"reply": reply}
        if reply:
            state.add_turn(session_id, f"[clicked {kind}]", reply)
        state.set_display(session_id, status="idle", trace_id=root["trace_id"],
                          message=reply or f"Can't {kind} that.")
        return None, None
    if msg.get("type") == "dismiss":                          # "Skip" on a suggestion card: display only
        tools.dismiss_suggestion(session_id, msg.get("pick"))
        return None, None
    if msg.get("type") == "model":                            # the switch / picker: a turn already running keeps its model
        if not llm.is_available(msg.get("id")):
            return None, "That model isn't available here."
        state.session(session_id).model = msg["id"]
        state.set_display(session_id, model=msg["id"])
        return None, None
    return None, ('Expected {"type": "text" | "audio" | "choose" | "add" | "dismiss" | "remove" | "undo" | "redo" | '
                  '"load" | "model" | "proposal", ...}.')


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    ip = guard.client_ip(ws.headers, ws.client.host if ws.client else None)
    if not guard.origin_ok(ws.headers.get("origin"), ws.headers.get("host")):
        await ws.close(code=1008)                             # policy violation: not our own page
        return
    if not guard.connect(ip, len(state._sessions)):
        await ws.close(code=1013)                             # try again later: too many tabs
        return
    await ws.accept()
    session_id = uuid.uuid4().hex[:12]
    state.session(session_id).ip = ip                         # the planner's per-IP cap
    loop = asyncio.get_running_loop()

    def push(message: dict) -> None:
        # Called by state.update_state()/set_display()/emit_progress(), often from the agent's worker
        # thread. Hand the send over to the event loop that owns the socket. All sends come from one
        # thread, so the browser sees them in the order they happened.
        asyncio.run_coroutine_threadsafe(ws.send_json(message), loop)

    def speaker(input_no: int, language: str):
        """For a voice turn: a function the agent's thread calls to have one short line spoken (Sarvam TTS).
        Lines go out in the order they were asked for (the filler before the answer, even when the answer's
        audio would be ready first): one lock per turn, and asyncio.Lock wakes waiters first come, first served.
        The audio is dropped if the user has sent something newer, or the spend cap is reached."""
        lock = asyncio.Lock()

        async def say(text: str) -> None:
            async with lock:
                await say_now(text)

        async def say_now(text: str) -> None:
            if guard.over_budget(session_id):
                return
            try:
                wav = await tts.speak(text, language)
            except tts.TTSError:
                return                                        # the text is on screen already
            guard.charge(session_id, guard.TTS_COST_USD)
            if state.session(session_id).input_no == input_no:
                await ws.send_json({"type": "speech", "mime": "audio/wav", "data": base64.b64encode(wav).decode()})
        return lambda text: asyncio.run_coroutine_threadsafe(say(text), loop)

    state.session(session_id).listener = push
    state.session(session_id).on_progress = push
    state.ui(session_id)["session_id"] = session_id   # the UI shows it, so a test case can be named
    state.ui(session_id)["model"] = state.session(session_id).model
    state.ui(session_id)["models"] = llm.available()
    await ws.send_json(state.get_state(session_id))          # current (empty) state on connect

    try:
        while True:
            raw = await ws.receive_text()
            n = state.session(session_id).input_no = state.session(session_id).input_no + 1
            state.set_display(session_id, followups=[])           # the old chips belong to the old trip
            sess = state.session(session_id)
            sess.speak_in = None                                  # set by transcribe_audio for a voice message
            text, error, kind = await read_input(session_id, raw, ip)
            sess.speak = speaker(n, sess.speak_in) if sess.speak_in else None
            if error:
                state.set_display(session_id, status="idle", message=error)
                continue
            if text is None:                                  # a click the server already applied
                continue
            state.set_display(session_id, heard=text, heard_marks=agent.heard_marks_now(text), status="thinking",
                              message="", question=None, suggestions=[])      # bold + rings show now, not after the LLM
            # TODO(async): a new message should cancel the running turn; today it waits.
            state.emit_progress(session_id, {"event": "start"})
            await asyncio.to_thread(agent.run_turn, session_id, text, kind)   # pushes on every change
            state.emit_progress(session_id, {"event": "end"})
            state.set_display(session_id, status="idle")             # final push with the reply
            asyncio.create_task(asyncio.to_thread(agent.followups, session_id, text, n))   # chips arrive a moment later
    except WebSocketDisconnect:
        pass
    finally:
        guard.disconnect(ip, session_id)
        state.drop_session(session_id)


@app.middleware("http")
async def always_fresh_static(request, call_next):
    """Tell the browser to re-check app.js/style.css on every page load. Without this header the
    browser may reuse an old cached copy for hours after the file changed (it guesses a cache time).
    Re-checking is cheap: an unchanged file gets a tiny "304 Not Modified" answer via its ETag."""
    response = await call_next(request)
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.get("/api/trace/{trace_id}")
def get_trace(trace_id: str, request: Request) -> dict:
    """All spans of one turn plus the other turns of the same session (for static/trace.html)."""
    # Traces contain what users typed: needs the TRACE_KEY (header X-Trace-Key); with no key set, local only.
    if not guard.trace_ok(request.headers.get("x-trace-key"), request.client.host if request.client else None):
        raise HTTPException(401, "trace key needed")
    data = trace.read_trace(trace_id)
    if data is None:
        raise HTTPException(404, "trace not found")
    return data


# Last, so /ws and /api are matched first. html=True serves static/index.html at "/".
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")

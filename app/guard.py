"""Product hygiene in one place: who may connect, how fast, and how much money a person / the day may cost.

Everything here is in memory (one server process). A restart resets the counters, so also set a spending
limit in the Sarvam and Groq dashboards: that is the backstop this file can't be.

Settings come from environment variables (see .env.example); the defaults are safe for a public demo.
"""
import hmac
import json
import os
import time
from collections import defaultdict, deque
from pathlib import Path
from urllib.parse import urlparse

from app import llm

# ---- Money ----
# LLM prices are per model, in llm.MODELS (USD per million tokens; Sarvam's ₹ prices converted there).
STT_COST_USD = 0.002        # Sarvam speech-to-text, per clip (estimate: ~15 s of audio)
TTS_COST_USD = 0.01         # Sarvam text-to-speech, per spoken question (estimate: ~500 characters)
DAILY_BUDGET_USD = float(os.getenv("DAILY_BUDGET_USD", "2.0"))     # whole server, per day (UTC)
SESSION_BUDGET_USD = float(os.getenv("SESSION_BUDGET_USD", "0.25"))   # one browser tab (≈ 400 text turns)

# ---- Speed ----
PAID_PER_MIN = 12           # messages per IP per minute that cost money (typed text, voice)
ANY_PER_MIN = 60            # every message per IP per minute (clicks included)
MAX_CONNECTIONS_PER_IP = 4
MAX_SESSIONS = 200          # open tabs on the whole server (memory)

# The frontier planner costs ~20-50x a cheap step: its own caps, so one visitor can't use up the day's budget.
FRONTIER_PER_SESSION = int(os.getenv("FRONTIER_PER_SESSION", "5"))     # planner calls per browser tab
FRONTIER_PER_IP_HOUR = int(os.getenv("FRONTIER_PER_IP_HOUR", "10"))    # planner calls per IP per hour

OVER_LIMIT = "Bahut tez ho gaya, thoda ruk ke dobara try kijiye."
OVER_BUDGET = "Aaj ka free limit khatam ho gaya. Kal dobara aaiye!"
OVER_SESSION = "Is session ka limit ho gaya. Page refresh karke naya trip shuru kijiye."


FRONTIER_SESSION_LIMIT = "Is session mein planner ka limit ho gaya. Jagah ke naam boliye, main route bana deta hoon."
FRONTIER_IP_LIMIT = "Planner abhi busy hai, thodi der baad try kijiye. Tab tak jagah ke naam boliye."


def llm_cost(usage: dict, model: str) -> float:
    return llm.cost_usd(model, usage)


# ---- Spend cap ----
_day = ""
_day_spent = 0.0
_session_spent: dict[str, float] = defaultdict(float)

# Deployed (DATA_DIR set, a Fly volume): today's spend is saved to a file after every charge, so a restart or a
# redeploy can't reset the daily cap. Locally (DATA_DIR unset) it stays in memory, as before, so tests and dev runs
# never touch a real counter.
SPEND_PATH = Path(os.environ["DATA_DIR"]) / "spend.json" if os.environ.get("DATA_DIR") else None


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def _load_spend() -> None:
    global _day, _day_spent
    try:
        saved = json.loads(SPEND_PATH.read_text())
        if saved.get("day") == _today():
            _day, _day_spent = saved["day"], float(saved["usd"])
    except Exception:                       # no file yet, or unreadable: start the day at 0
        pass


def _save_spend() -> None:
    try:
        tmp = SPEND_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps({"day": _day, "usd": round(_day_spent, 6)}))
        os.replace(tmp, SPEND_PATH)         # atomic: a crash mid-write never leaves half a file
    except Exception as e:
        print(f"[guard] could not save spend (ignored): {e!r}")


if SPEND_PATH:
    _load_spend()


def charge(session_id: str, usd: float) -> None:
    """Add the cost of one paid call. Called right after the call, so the cap holds from the next one."""
    global _day, _day_spent
    if _day != _today():
        _day, _day_spent = _today(), 0.0
    _day_spent += usd
    _session_spent[session_id] += usd
    if SPEND_PATH:
        _save_spend()


def over_budget(session_id: str) -> str | None:
    """None if this session may spend more, else the message to show."""
    if _day == _today() and _day_spent >= DAILY_BUDGET_USD:
        return OVER_BUDGET
    if _session_spent[session_id] >= SESSION_BUDGET_USD:
        return OVER_SESSION
    return None


def spent_today() -> float:
    return _day_spent if _day == _today() else 0.0


# ---- Rate limit: sliding window per IP ----
_hits: dict[tuple[str, str], deque] = defaultdict(deque)


def allow(ip: str, kind: str, limit: int, window_s: float = 60.0) -> bool:
    """True if `ip` has made fewer than `limit` calls of this kind in the last window; records this one."""
    now = time.monotonic()
    q = _hits[(ip, kind)]
    while q and now - q[0] > window_s:
        q.popleft()
    if len(q) >= limit:
        return False
    q.append(now)
    return True


def frontier_blocked(ip: str, calls_this_session: int) -> str | None:
    """None if one more planner call is allowed (and records it for the IP), else the message to show."""
    if calls_this_session >= FRONTIER_PER_SESSION:
        return FRONTIER_SESSION_LIMIT
    if not allow(ip, "frontier", FRONTIER_PER_IP_HOUR, 3600):
        return FRONTIER_IP_LIMIT
    return None


_connections: dict[str, int] = defaultdict(int)


def connect(ip: str, open_sessions: int) -> bool:
    if _connections[ip] >= MAX_CONNECTIONS_PER_IP or open_sessions >= MAX_SESSIONS:
        return False
    _connections[ip] += 1
    return True


def disconnect(ip: str, session_id: str) -> None:
    _connections[ip] = max(0, _connections[ip] - 1)
    _session_spent.pop(session_id, None)


# ---- Who is calling ----
def client_ip(headers, peer: str | None) -> str:
    """Behind a proxy (Fly, Render, Railway) the peer is the proxy, so the real address is in a header.
    Only trusted when TRUST_PROXY=1: otherwise anyone could send the header and pick their own IP."""
    if os.getenv("TRUST_PROXY") == "1":
        ip = headers.get("fly-client-ip") or (headers.get("x-forwarded-for") or "").split(",")[0].strip()
        if ip:
            return ip
    return peer or "unknown"


def origin_ok(origin: str | None, host: str | None) -> bool:
    """A browser always sends the page's Origin on a WebSocket, and a web page on another site can't fake it.
    Allowed: the same host as this server, or one listed in ALLOWED_ORIGINS (comma separated, e.g. https://x.app).
    No Origin header at all = not a browser (a script): refused."""
    if not origin:
        return False
    extra = [o.strip().rstrip("/") for o in os.getenv("ALLOWED_ORIGINS", "").split(",") if o.strip()]
    return origin.rstrip("/") in extra or urlparse(origin).netloc == (host or "")


# ---- Trace viewer key ----
def trace_ok(key: str | None, client_host: str | None) -> bool:
    """TRACE_KEY set: the request must carry it. Not set: only the machine itself (local development)."""
    wanted = os.getenv("TRACE_KEY", "")
    if wanted:
        return hmac.compare_digest(key or "", wanted)
    return client_host in ("127.0.0.1", "::1", "localhost", "testclient")

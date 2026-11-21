"""Product hygiene (app/guard.py): origin check, rate limit, spend cap, trace key."""
import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import agent, guard, llm, main


@pytest.fixture(autouse=True)
def fresh_guard(monkeypatch):
    guard._hits.clear(); guard._connections.clear(); guard._session_spent.clear()
    monkeypatch.setattr(guard, "_day_spent", 0.0)


def test_origin_must_be_our_own_site(monkeypatch):
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    assert guard.origin_ok("https://app.example.com", "app.example.com")
    assert guard.origin_ok("http://localhost:8000", "localhost:8000")
    assert not guard.origin_ok("https://evil.com", "app.example.com")
    assert not guard.origin_ok(None, "app.example.com")                    # a script, not a browser
    monkeypatch.setenv("ALLOWED_ORIGINS", "https://other.app/")
    assert guard.origin_ok("https://other.app", "app.example.com")


def test_websocket_refuses_a_foreign_origin():
    c = TestClient(main.app)
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers={"origin": "https://evil.com"}):
            pass
    with c.websocket_connect("/ws", headers={"origin": "http://testserver"}) as ws:
        assert ws.receive_json()["trip"]["version"] == 0


def test_rate_limit_per_ip_and_kind():
    assert all(guard.allow("1.1.1.1", "paid", 3) for _ in range(3))
    assert not guard.allow("1.1.1.1", "paid", 3)
    assert guard.allow("2.2.2.2", "paid", 3)                               # another visitor is not affected


def test_read_input_stops_a_flood():
    results = [asyncio.run(main.read_input("rl1", '{"type": "text", "text": "Delhi"}', "9.9.9.9"))
               for _ in range(guard.PAID_PER_MIN + 1)]
    assert results[-1][:2] == (None, guard.OVER_LIMIT) and results[0][1] is None


def test_connection_limit_per_ip():
    assert all(guard.connect("3.3.3.3", 0) for _ in range(guard.MAX_CONNECTIONS_PER_IP))
    assert not guard.connect("3.3.3.3", 0)
    guard.disconnect("3.3.3.3", "s")
    assert guard.connect("3.3.3.3", 0) and not guard.connect("4.4.4.4", guard.MAX_SESSIONS)


def test_proxy_header_trusted_only_when_asked(monkeypatch):
    h = {"fly-client-ip": "5.5.5.5"}
    monkeypatch.delenv("TRUST_PROXY", raising=False)
    assert guard.client_ip(h, "10.0.0.1") == "10.0.0.1"
    monkeypatch.setenv("TRUST_PROXY", "1")
    assert guard.client_ip(h, "10.0.0.1") == "5.5.5.5"


def test_session_and_daily_budget(monkeypatch):
    assert guard.over_budget("b1") is None
    guard.charge("b1", guard.SESSION_BUDGET_USD)
    assert guard.over_budget("b1") == guard.OVER_SESSION and guard.over_budget("b2") is None
    monkeypatch.setattr(guard, "DAILY_BUDGET_USD", 0.01)
    guard.charge("b3", 0.02)
    assert guard.over_budget("b2") == guard.OVER_BUDGET


def test_agent_makes_no_llm_call_when_over_budget(monkeypatch):
    guard.charge("b4", guard.SESSION_BUDGET_USD)
    monkeypatch.setattr(llm._openai.responses, "create", lambda **kw: pytest.fail("LLM was called"))
    assert agent.run_turn("b4", "Delhi se Agra") == guard.OVER_SESSION


def test_trace_key(monkeypatch):
    monkeypatch.setenv("TRACE_KEY", "s3cret")
    assert guard.trace_ok("s3cret", "8.8.8.8") and not guard.trace_ok("nope", "127.0.0.1") and not guard.trace_ok(None, "127.0.0.1")
    monkeypatch.delenv("TRACE_KEY")
    assert guard.trace_ok(None, "127.0.0.1") and not guard.trace_ok(None, "8.8.8.8")

"""Speech-to-text: size limits, the request sent to Sarvam, error messages, and the audio path of main.py.
Sarvam is replaced by a fake HTTP client, so no key or network is needed."""
import asyncio
import base64
import json

import pytest

from app import main, state, stt

AUDIO = b"x" * 5000


class FakeResponse:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body if body is not None else {"transcript": "Agra se Jaipur"}
        self.text = json.dumps(self._body)

    def json(self):
        return self._body


def fake_client(response=None, error=None, seen=None):
    class Client:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, headers, data, files):
            if seen is not None:
                seen.update(url=url, headers=headers, data=data, files=files)
            if error:
                raise error
            return response or FakeResponse()
    return Client


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "test-key")


def run(coro):
    return asyncio.run(coro)


def test_sends_the_expected_request(monkeypatch):
    seen = {}
    monkeypatch.setattr(stt.httpx, "AsyncClient", fake_client(seen=seen))
    assert run(stt.transcribe(AUDIO, "audio/webm;codecs=opus", ["Agra"])) == "Agra se Jaipur"
    assert seen["headers"] == {"api-subscription-key": "test-key"}
    assert seen["data"]["model"] == "saaras:v4" and seen["data"]["mode"] == "translit"
    assert json.loads(seen["data"]["keyterms"]) == ["Agra"]
    assert seen["files"]["file"] == ("speech.webm", AUDIO, "audio/webm")


def test_too_short_and_too_long_are_rejected_without_calling_sarvam(monkeypatch):
    monkeypatch.setattr(stt.httpx, "AsyncClient", fake_client(error=AssertionError("must not be called")))
    for audio in (b"x" * 10, b"x" * (stt.MAX_AUDIO_BYTES + 1)):
        with pytest.raises(stt.STTError):
            run(stt.transcribe(audio))


@pytest.mark.parametrize("client", [
    fake_client(response=FakeResponse(500, {"error": "boom"})),
    fake_client(response=FakeResponse(200, {"transcript": "  "})),
    fake_client(error=stt.httpx.ConnectError("down")),
])
def test_failures_become_user_messages(monkeypatch, client):
    monkeypatch.setattr(stt.httpx, "AsyncClient", client)
    with pytest.raises(stt.STTError):
        run(stt.transcribe(AUDIO))


def test_keyterms_trip_first_no_duplicates_max_50():
    terms = stt.keyterms_for(["Kasol", "agra"])
    assert terms[0] == "Kasol" and terms[1] == "agra"
    assert len({t.lower() for t in terms}) == len(terms) <= stt.MAX_KEYTERMS


def test_audio_message_becomes_text_for_the_agent(monkeypatch):
    monkeypatch.setattr(stt.httpx, "AsyncClient", fake_client())
    sid = "stt-test"
    raw = json.dumps({"type": "audio", "mime": "audio/webm", "data": base64.b64encode(AUDIO).decode()})
    assert run(main.read_input(sid, raw)) == ("Agra se Jaipur", None, "text")
    assert state.ui(sid)["status"] == "transcribing"
    state.drop_session(sid)


def test_bad_audio_messages_give_errors():
    for msg in ({"type": "audio", "data": "!!not base64!!"}, {"type": "audio"}, {"type": "video"}):
        text, error, _ = run(main.read_input("stt-test2", json.dumps(msg)))
        assert text is None and error
    state.drop_session("stt-test2")


def test_text_messages_still_work():
    assert run(main.read_input("s", json.dumps({"type": "text", "text": " Agra hata do "}))) == ("Agra hata do", None, "text")
    assert run(main.read_input("s", json.dumps({"type": "text", "text": "x" * 301})))[0] is None


def test_clicked_option_reaches_the_agent_as_a_choice():
    msg = {"type": "choose", "text": "Aurangabad, Maharashtra"}
    assert run(main.read_input("s", json.dumps(msg))) == ("Aurangabad, Maharashtra", None, "choice")

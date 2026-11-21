"""Text-to-speech module (not used by the app for now). Sarvam is a fake HTTP client: no key or network needed."""
import asyncio
import base64
import json

import pytest

from app import tts


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code, self._body = status, body
        self.text = json.dumps(body)

    def json(self):
        return self._body


def client(resp, seen):
    class C:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

        async def post(self, url, headers, json=None, data=None, files=None):
            seen.update(url=url, json=json, data=data)
            return resp
    return C


@pytest.fixture(autouse=True)
def key(monkeypatch):
    monkeypatch.setenv("SARVAM_API_KEY", "test-key")


def test_speak_sends_language_and_decodes_wav(monkeypatch):
    seen = {}
    wav = b"RIFF....WAVEfmt "
    monkeypatch.setattr(tts.httpx, "AsyncClient", client(Resp(200, {"audios": [base64.b64encode(wav).decode()]}), seen))
    assert asyncio.run(tts.speak("Agra hata diya.", "ta-IN")) == wav
    assert seen["url"] == tts.URL and seen["json"]["language_code"] == "ta-IN" and seen["json"]["text"] == "Agra hata diya."


def test_language_fallback_and_errors(monkeypatch):
    assert tts.language_for("en-IN") == "en-IN"
    assert tts.language_for("as-IN") == tts.language_for(None) == tts.DEFAULT_LANGUAGE   # Bulbul has no Assamese
    monkeypatch.setattr(tts.httpx, "AsyncClient", client(Resp(429, {"error": {}}), {}))
    with pytest.raises(tts.TTSError):
        asyncio.run(tts.speak("hi", "hi-IN"))

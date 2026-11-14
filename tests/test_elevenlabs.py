"""Jarvis's ElevenLabs voice (elevenlabs.py), with Kokoro as the fallback: in the browser
interface's /tts and in the voice layer. No network: a mock transport plays ElevenLabs."""
import asyncio, json, os, socket, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
os.environ["ELEVENLABS_API_KEY"] = "from-the-environment"   # must be ignored: shell commands can see it
sys.path.insert(0, str(ROOT))
import httpx
import numpy as np
import elevenlabs as el

# --- settings: secrets.env only ---
assert el.settings() is None
(home / ".jarvis").mkdir()
secrets = home / ".jarvis" / "secrets.env"
secrets.write_text("ELEVENLABS_API_KEY=sk_test\n", encoding="utf-8")
cfg = el.settings()
assert cfg == {"key": "sk_test", "voice": el.DEFAULT_VOICE, "model": el.DEFAULT_MODEL}, cfg
secrets.write_text("ELEVENLABS_API_KEY=sk_test\nELEVENLABS_VOICE_ID=abc123\nELEVENLABS_MODEL=eleven_multilingual_v2\n")
assert el.settings() == {"key": "sk_test", "voice": "abc123", "model": "eleven_multilingual_v2"}
assert "sent to ElevenLabs" in el.describe(cfg) and "Kokoro" in el.describe(None)

# --- the request, and what happens when ElevenLabs says no ---
calls = []


def answer(status, body=b"ID3-mp3-bytes"):
    def handler(request):
        calls.append(request)
        return httpx.Response(status, content=body)
    return httpx.Client(transport=httpx.MockTransport(handler))


assert el.mp3("Good evening.", cfg, answer(200)) == b"ID3-mp3-bytes"
req = calls[-1]
assert req.url.path == f"/v1/text-to-speech/{el.DEFAULT_VOICE}" and req.url.params["output_format"] == "mp3_44100_128"
assert req.headers["xi-api-key"] == "sk_test"
body = json.loads(req.content)
assert body["text"] == "Good evening." and body["model_id"] == el.DEFAULT_MODEL and body["voice_settings"]["speed"] == 1.05

assert el.mp3("x", cfg, answer(500, b"server error")) is None        # one bad sentence: just this one
assert el.mp3("x", cfg, answer(200)) == b"ID3-mp3-bytes"
n = len(calls)
assert el.mp3("x", cfg, answer(401, b'{"detail":"invalid key"}')) is None   # a bad key: stop asking for a while
assert el.mp3("x", cfg, answer(200)) is None and len(calls) == n + 1
el._paused_until = 0.0
assert el.mp3("x", cfg, answer(400, b"quota_exceeded")) is None and el._paused_until > 0   # out of credits
el._paused_until = 0.0


def unreachable(request):
    raise httpx.ConnectError("no network")

assert el.mp3("x", cfg, httpx.Client(transport=httpx.MockTransport(unreachable))) is None

# samples() decodes for the speakers; undecodable audio falls back too.
import voice
voice.decode_audio = lambda audio, rate: np.ones(rate // 2, np.float32) if audio.startswith(b"ID3") else 1 / 0
s = el.samples("x", cfg, 24000, answer(200))
assert s.dtype == np.float32 and s.size == 12000
assert el.samples("x", cfg, 24000, answer(200, b"garbage")) is None

# --- the voice layer: ElevenLabs first, Kokoro when it doesn't answer ---
class FakeKokoro:
    async def create_stream(self, text, voice, lang):
        yield np.zeros(100, np.float32), 24000


async def say_one(cloud):
    v = voice.Voice.__new__(voice.Voice)
    v._speech, v._audio = asyncio.Queue(), asyncio.Queue()
    v._speech_generation, v._in_flight, v._path_names = 0, None, []
    v._eleven, v._tts = cfg, FakeKokoro()
    el.samples = lambda text, c, rate: cloud
    task = asyncio.create_task(v._synthesizer())
    await v._speech.put(("Good evening.", True))
    first = await asyncio.wait_for(v._audio.get(), 3)
    end = await asyncio.wait_for(v._audio.get(), 3)
    task.cancel()
    return first, end

first, end = asyncio.run(say_one(np.ones(500, np.float32)))
assert first[1] == "Good evening." and first[3].size == 500 and first[4] == 24000 and end[3] is None
first, end = asyncio.run(say_one(None))                      # ElevenLabs didn't answer: Kokoro
assert first[1] == "Good evening." and first[3].size == 100 and end[3] is None

# --- the browser interface: /tts gives ElevenLabs' MP3, or Kokoro's WAV when it fails ---
import web as w


class WavKokoro:
    def create(self, text, voice, lang):
        return np.zeros(2400, np.float32), 24000


async def interface():
    import speech as sp
    from hub import Hub
    sp.STT_AVAILABLE, sp.TTS_AVAILABLE = True, True
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    service = sp.Speech()
    assert service.eleven == el.settings() and service.speaks
    service._stt, service._tts = object(), WavKokoro()
    async with w.WebServer(Hub(), [], port=port, speech=service):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            assert (await c.get("/health")).json()["tts"] is True
            sp.elevenlabs.mp3 = lambda text, c: b"ID3-from-elevenlabs"
            r = await c.post("/tts", json={"text": "Good evening."})
            assert r.status_code == 200 and r.headers["content-type"] == "audio/mpeg" and r.content.startswith(b"ID3")
            sp.elevenlabs.mp3 = lambda text, c: None
            r = await c.post("/tts", json={"text": "Good evening."})
            assert r.status_code == 200 and r.headers["content-type"] == "audio/wav"
    # ElevenLabs alone, without Kokoro installed, still gives the page a voice.
    sp.TTS_AVAILABLE = False
    assert sp.Speech().speaks is True

asyncio.run(interface())
print("ALL OK")

"""The voice satellite (satellite/satellite.py) against a real interface server whose speech is
faked, with the satellite's microphone, wake word, VAD and speakers stubbed: pairing, the hello,
hearing through /stt and speaking through /tts with the media ticket, questions asked and answered
by voice, the charter question it can't answer, news, standby, close codes and the redial backoff.
Also the voice layer's local engines (Whisper and Kokoro, faked), which must behave as before."""
import asyncio, io, json, os, socket, sys, tempfile, types, wave
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
os.environ["APPDATA"] = str(home / "AppData"); os.environ["XDG_CONFIG_HOME"] = str(home / ".config")
os.environ.pop("JARVIS_CODEX", None)
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "satellite"))
import numpy as np

# --- the microphone, wake word, VAD and speakers, stubbed (nothing is downloaded or opened) ---
WAKE, SPEECH, QUIET = 1000, 500, 0   # a frame's sample value says what it is


def frame(value: int):
    return np.full(1280, value, np.int16)


class FakeWake:
    def predict(self, f):
        return {"hey_jarvis": 1.0 if f[0] == WAKE else 0.0}

    def reset(self):
        pass


class FakeVAD:
    def predict(self, f):
        return 1.0 if f[0] == SPEECH else 0.0

    def reset_states(self):
        pass


wake_downloads: list = []
oww = types.ModuleType("openwakeword")
for name, attrs in {"openwakeword.model": {"Model": lambda **k: FakeWake()},
                    "openwakeword.vad": {"VAD": FakeVAD},
                    "openwakeword.utils": {"download_models": wake_downloads.append}}.items():
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    sys.modules[name] = module
    setattr(oww, name.split(".")[1], module)
sys.modules["openwakeword"] = oww

played: list = []      # (samples, rate) as the speakers got them
PLAY_S = [0.0]         # how long each play takes


class FakeStream:
    def __init__(self, **kwargs):
        self.callback = kwargs["callback"]

    def start(self): pass
    def stop(self): pass
    def close(self): pass


fake_sd = types.SimpleNamespace(InputStream=FakeStream, play=lambda s, r: played.append((s, r)),
                                wait=lambda: __import__("time").sleep(PLAY_S[0]), stop=lambda: None)

import voice as v
v._SoundDevice._module = fake_sd
beeps: list = []
v._beep = lambda: beeps.append(1)

import agency as A
import guardrails as g
import protocol
import satellite as sat
import web as w
from hub import Hub

out: list[str] = []
sat.print = lambda *a, **k: out.append(" ".join(str(x) for x in a))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def until(check, what: str, timeout: float = 10):
    for _ in range(int(timeout / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}; satellite said: {out}")


def wav(samples: int, rate: int = 24000) -> bytes:
    b = io.BytesIO()
    with wave.open(b, "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(rate)
        f.writeframes(b"\x01\x00" * samples)
    return b.getvalue()


def _quiet_frames() -> int:
    """Frames of quiet that end a turn, exactly (a frame left over would start the next one)."""
    quiet, n = 0.0, 0
    while quiet < v.END_SILENCE_S:
        quiet, n = quiet + v.FRAME / v.RATE, n + 1
    return n


async def speak_to(voice, follow_up: bool = False):
    """Say something into the satellite: the wake word (or, in a follow-up, nothing), then speech."""
    before = len(beeps)
    voice._frames.put_nowait(frame(QUIET if follow_up else WAKE))
    await until(lambda: len(beeps) > before, "the mic to open")
    for f in [SPEECH] * 3 + [QUIET] * _quiet_frames():
        voice._frames.put_nowait(frame(f))


async def finished_speaking(voice):
    await until(lambda: voice._lines == 0, "the satellite to finish speaking")


# --- the satellite's own helpers ---
assert [sat.backoff(n) for n in range(8)] == [1, 2, 4, 8, 16, 30, 30, 30] and sat.backoff(10**6) == 30
assert sat.http_url("ws://127.0.0.1:8790/") == "http://127.0.0.1:8790"
assert sat.http_url("wss://jarvis.lan:9000/ws") == "https://jarvis.lan:9000"
assert sat.token_path().is_relative_to(home) and not sat.token_path().is_relative_to(ROOT)
assert [sat.parse_answer(t) for t in ("Yes.", "no thanks", "always", "yes always", "what time is it")] == \
    ["yes", "no", "always", "always", None]
for t in ("yes", "Nope", "go ahead", "do not", "Thank you."):   # the same words as the core's
    assert sat.parse_answer(t) == g.parse_answer(t), t
assert set(sat.STOP_MESSAGES) == {protocol.CLOSE_REVOKED, protocol.CLOSE_VERSION, protocol.CLOSE_PAIRING}
assert v._chunks("One. Two. Three.", 9) == ["One. Two.", "Three."] and v._chunks("x" * 12, 5) == ["xxxxx", "xxxxx", "xx"]


# --- the voice layer's local engines, as before: Whisper hears, Kokoro (warmed up) speaks ---
class FakeWhisper:
    def __init__(self):
        self.audio = []

    def transcribe(self, audio, language, beam_size):
        self.audio.append(audio)
        return [types.SimpleNamespace(text=" Hey Jarvis, how are the disks?")], None


class FakeKokoro:
    def __init__(self):
        self.lines = []

    def create(self, text, voice, lang):
        self.lines.append(("warm-up", text))
        return np.zeros(10, np.float32), 24000

    async def create_stream(self, text, voice, lang):
        self.lines.append(text)
        yield np.zeros(240, np.float32), 24000


async def local_voice():
    kokoro_downloads = []
    whisper, kokoro = FakeWhisper(), FakeKokoro()
    v._download_kokoro = lambda: kokoro_downloads.append(1)
    v._load_tts = lambda: (kokoro, "Kokoro (fake)")
    v._load_whisper = lambda: (whisper, "Whisper (fake)")
    voice = v.Voice({str(home): "home"})
    assert voice._stt is whisper and voice._tts is kokoro and kokoro_downloads == [1] and wake_downloads
    assert kokoro.lines == [("warm-up", "Ready.")] and voice._eleven is None   # no ElevenLabs key here
    async with voice:
        hearing = asyncio.create_task(voice.hear())
        await speak_to(voice)
        assert await asyncio.wait_for(hearing, 5) == "how are the disks?"   # "Hey Jarvis" dropped
        assert whisper.audio[0].dtype == np.float32 and whisper.audio[0].size == (3 + _quiet_frames()) * 1280
        voice.say(f"Saved to {home / 'notes.txt'}, on 2026-11-12.")
        await finished_speaking(voice)
        assert kokoro.lines[-1] == "Saved to home, notes.txt, on the 12th of November 2026.", kokoro.lines
        assert played[-1][0].size == 240 and voice._follow_up   # the mic opens for a follow-up


asyncio.run(local_voice())
played.clear()


# --- the core: a real interface server with its speech faked ---
class FakeSpeech:
    hears = speaks = True

    def __init__(self):
        self.heard, self.spoken, self.replies = [], [], []

    def warm(self, loop): pass
    def describe_voice(self): return "fake"

    def transcribe(self, audio: bytes) -> str:
        self.heard.append(audio)
        return self.replies.pop(0) if self.replies else ""

    def speak(self, text: str):
        self.spoken.append(text)
        return wav(100 + len(text)), "audio/wav"


async def main():
    port = free_port()
    url = f"ws://127.0.0.1:{port}/"
    hub = Hub()
    g._io = hub
    speech = FakeSpeech()
    server = w.WebServer(hub, [], port=port, speech=speech)
    devices = server.devices
    remote = v.RemoteSpeech(sat.http_url(url))
    voice = v.Voice(stt=v.RemoteSTT(remote), tts=v.RemoteTTS(remote))
    assert isinstance(voice._tts, v.RemoteTTS) and voice._eleven is None   # no Kokoro loaded or downloaded

    async with hub, server, voice:
        # Another paired device (a tablet), so a revoked token is told so (4001), not just "pair first".
        devices.pair(devices.new_code("tablet"), "face", "tablet")
        # Pairing with a code: the token is kept in the config folder, and it's on the hub as a
        # satellite: a paired device with no screen, named as `jarvisctl pair --name` said.
        code = devices.new_code("kitchen", "satellite")
        satellite = sat.Satellite(url, "box-17", voice, remote, sat.token_path(), pair=code)
        sent: list[dict] = []
        real_send = satellite.send

        async def logged(frame):
            sent.append(frame)
            await real_send(frame)
        satellite.send = logged
        running = asyncio.create_task(satellite.run())
        await until(lambda: len(hub.channels) == 1, "the satellite on the hub")
        ch = hub.channels[0]
        assert (ch.kind, ch.name, ch.source, ch.trust, ch.display) == \
            ("satellite", "kitchen", "satellite", "device", False), vars(ch)
        token = sat.read_token(sat.token_path())
        device = devices.check(token)
        assert device and device["id"] == ch.device_id and satellite.pair is None
        assert remote.ticket and devices.ticket_ok(remote.ticket)
        assert hub.display_target() is None and hub.primary() is ch

        # An utterance: /stt with the ticket (the core is paired, so nothing else gets through),
        # and the text reaches the hub as a line from the satellite.
        speech.replies.append("how are the disks?")
        await speak_to(voice)
        assert await asyncio.wait_for(hub.hear(), 10) == "how are the disks?"
        assert (hub.source, hub.trust, hub.kind) == ("satellite", "device", "satellite")
        assert speech.heard[0][:4] == b"RIFF" and len(speech.heard[0]) > (3 + _quiet_frames()) * 1280 * 2
        assert sent[-1] == {"type": "ask", "text": "how are the disks?", "id": "s1"}

        # The reply: /tts with the ticket, played here. The core says paths its own way.
        hub.say("All healthy, sir.")
        hub.say(r"Saved to C:\Users\you\jarvis-workspace\x.txt.")
        await until(lambda: len(speech.spoken) == 2, "both lines spoken")
        await finished_speaking(voice)
        assert speech.spoken == ["All healthy, sir.", r"Saved to C:\Users\you\jarvis-workspace\x.txt."]
        assert [p[0].size for p in played] == [100 + len(t) for t in speech.spoken] and played[0][1] == 24000

        # A question: asked aloud, answered by voice in the follow-up, sent as its answer.
        played.clear()
        q = g.open_question("restart the NAS", "ssh nas reboot", hard=False)
        await until(lambda: speech.spoken[-1] == "May I restart the NAS? Yes, no, or always.", "the question")
        await until(lambda: [o["id"] for o in satellite.questions] == [q.id], "the question frame")
        await finished_speaking(voice)
        speech.replies.append("Yes.")
        await speak_to(voice, follow_up=True)
        assert await asyncio.wait_for(q.answer, 10) == "yes"
        assert sent[-1] == {"type": "answer", "q": q.id, "decision": "yes"}
        await until(lambda: satellite.questions == [], "the question to close")
        assert hub._inbox.empty()   # an answer, not a line for Claude

        # The charter question: not sent to a satellite, said once where it waits, never answerable here.
        spoken = len(speech.spoken)
        charter = g.open_question("adopt your charter: new standing orders homelab", "the charter", hard=True,
                                  only=A.ANSWER_FROM, not_kinds=A.ANSWER_NOT_FROM)
        charter.charter_hash = "abc"
        elsewhere = "There's a charter change waiting for you at the PC or in the browser."
        await until(lambda: speech.spoken[spoken:] == [elsewhere], "where the charter waits")
        hub.ask_question(charter)   # asked again (a reconnect, a question before it closing): not said twice
        await asyncio.sleep(0.3)
        assert speech.spoken[spoken:] == [elsewhere] and satellite.questions == []
        await satellite.send({"type": "answer", "q": charter.id, "decision": "yes"})   # forged: refused
        await asyncio.sleep(0.3)
        assert not charter.answer.done()
        assert not g.answer_question(charter.id, "yes", by="device", kind="satellite")
        assert not g.answer_oldest_question("yes", by="device", kind="satellite")
        assert not g.may_answer(charter, "call") and g.may_answer(charter, "device", "face")
        assert g.may_answer(q, "device", "satellite")   # ordinary questions: yes
        assert g.answer_question(charter.id, "no", by="device", kind="face") and await charter.answer == "no"

        # News (tasks.announce -> hub.notify): spoken on its own.
        hub.notify("The backup finished.")
        await until(lambda: speech.spoken[-1] == "The backup finished.", "the news")
        await finished_speaking(voice)

        # Talking over Jarvis: the wake word cuts the speech off and stops the core's turn too.
        PLAY_S[0] = 0.5
        hub.say("Here is a rather long answer about the disks.")
        await until(lambda: voice._busy_speaking() and played and voice._lines, "speech to start")
        speech.replies.append("never mind")
        await speak_to(voice)
        await until(lambda: {"type": "interrupt"} in sent, "the interrupt")
        assert await asyncio.wait_for(hub.hear(), 10) == "never mind"
        PLAY_S[0] = 0.0

        # Standby: only "Hey Jarvis" opens the mic, and being called by name wakes it again.
        hub.standby = True
        await until(lambda: voice.standby, "standby")
        speech.replies.append("I'm back")
        await speak_to(voice)
        assert await asyncio.wait_for(hub.hear(), 10) == "I'm back" and not voice.standby
        hub.standby = False

        # A ticket the core turns down (12 hours old): it says hello again and gets a new one.
        satellite.sleep = lambda s: asyncio.sleep(0)
        old = remote.ticket
        remote.ticket = "stale"
        hub.say("One more thing.")
        await until(lambda: remote.ticket not in ("stale", old) and len(hub.channels) == 1
                    and hub.channels[0] is not ch, "a new hello")
        assert devices.ticket_ok(remote.ticket) and any("reconnecting" in o for o in out)

        # Revoked: closed with 4001 at once, said so, and it stops.
        assert devices.revoke(device["id"])
        await server.close_device(device["id"])
        assert await asyncio.wait_for(running, 10) == 1
        assert any("revoked" in o for o in out[-2:]), out
        # ... and a satellite still holding that token is refused the same way.
        again = sat.Satellite(url, "box-17", voice, remote, sat.token_path())
        assert await asyncio.wait_for(again.run(), 10) == 1 and "revoked" in out[-1]
        # A wrong pairing code (no pairing): 4003, and it stops.
        wrong = sat.Satellite(url, "box-17", voice, remote, home / "other" / "token", pair="000000")
        assert await asyncio.wait_for(wrong.run(), 10) == 1 and "isn't paired" in out[-1]
        assert not (home / "other" / "token").exists()
        # Never paired, no code: it says how, without dialling.
        fresh = sat.Satellite(url, "box-17", voice, remote, home / "none" / "token")
        assert await fresh.run() == 1 and "isn't paired" in out[-1]

    # The core away: it redials with backoff, capped at 30 s (counted here, not slept).
    class Enough(Exception):
        pass
    waits = []

    async def counted(s):
        waits.append(s)
        if len(waits) == 8:
            raise Enough
    away = sat.Satellite(f"ws://127.0.0.1:{free_port()}/", "box-17", voice, remote, sat.token_path())
    away.sleep = counted
    try:
        await away.run()
    except Enough:
        pass
    assert waits == [1, 2, 4, 8, 16, 30, 30, 30], waits
    remote.close()


asyncio.run(main())
print("ALL OK")

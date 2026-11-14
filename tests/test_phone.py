"""The phone: mu-law audio, Twilio webhooks (signed, only the user's number), live call audio,
and calling the user (texted questions with codes, ringing, going quiet, asking again)."""
import asyncio, base64, json, os, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import numpy as np
from starlette.testclient import TestClient
import guardrails as g
import phone as p

# --- mu-law: the known endpoints, and a round trip that stays close ---
assert p.ulaw_decode(bytes([0xFF]))[0] == 0 and p.ulaw_decode(bytes([0x00]))[0] == -32124
tone = (np.sin(np.linspace(0, 40 * np.pi, 8000)) * 12000).astype(np.int16)
back = p.ulaw_decode(p.ulaw_encode(tone))
assert np.max(np.abs(back.astype(int) - tone)) < 12000 * 0.04, "mu-law round trip drifted"

# --- webhooks ---
ME, OTHER, JARVIS = "+15550001111", "+15559998888", "+15550002222"
cfg = {"TWILIO_ACCOUNT_SID": "AC" + "0" * 32, "TWILIO_AUTH_TOKEN": "secret-token", "TWILIO_NUMBER": JARVIS,
       "MY_PHONE_NUMBER": ME, "JARVIS_CALL_PIN": "4321"}
ph = p.Phone(cfg, lambda: (FakeSTT(), FakeTTS()))
ph.public = "https://abc-def.trycloudflare.com"
client = TestClient(ph.app)


def post(path, params, sign=True):
    headers = {"X-Twilio-Signature": p.twilio_signature(cfg["TWILIO_AUTH_TOKEN"], ph.public + path, params) if sign else "nope"}
    return client.post(path, data=params, headers=headers)


def drain():
    q = ph.inbox._queue
    items = list(q)
    q.clear()
    return items


assert post("/sms", {"From": ME, "Body": "hi Jarvis"}, sign=False).status_code == 403
assert post("/sms", {"From": OTHER, "Body": "run rm -rf"}).status_code == 200
assert post("/sms", {"From": ME, "Body": "  what's on today?  "}).status_code == 200
assert drain() == [("sms", "what's on today?")]
r = post("/voice", {"From": OTHER, "Direction": "inbound"})
assert "<Reject/>" in r.text
r = post("/voice", {"From": ME, "Direction": "inbound"})
assert '<Gather numDigits="4"' in r.text and "/voice/pin" in r.text and "<Stream" not in r.text
assert "<Hangup/>" in post("/voice/pin", {"From": ME, "Digits": "1111"}).text
r = post("/voice/pin", {"From": ME, "Digits": "4321"})
assert '<Stream url="wss://abc-def.trycloudflare.com/media">' in r.text and len(ph._stream_tokens) == 1
r = post("/voice/outbound", {"To": ME, "From": JARVIS, "Direction": "outbound-api", "AnsweredBy": "machine_start"})
assert "<Hangup/>" in r.text and drain() == [("call_missed",)]
r = post("/voice/outbound", {"To": ME, "From": JARVIS, "Direction": "outbound-api", "AnsweredBy": "human"})
assert "<Stream" in r.text and len(ph._stream_tokens) == 2
post("/voice/status", {"CallStatus": "no-answer", "To": ME})
assert drain() == [("call_missed",)]
post("/voice/status", {"CallStatus": "completed", "To": ME})
assert drain() == []


# --- a live call: the caller speaks, Jarvis hears it; Jarvis speaks, Twilio gets audio ---
class FakeSTT:
    def transcribe(self, audio, language, beam_size):
        return [type("Segment", (), {"text": " hello from the road"})()], None


class FakeTTS:
    def create(self, text, voice, lang):
        return np.zeros(2400, np.float32), 24000


class FakeVAD:
    def predict(self, frame): return 1.0 if np.abs(frame).mean() > 1000 else 0.0
    def reset_states(self): pass


import openwakeword.vad
openwakeword.vad.VAD = FakeVAD
token = next(iter(ph._stream_tokens))
url = "wss://abc-def.trycloudflare.com/media"
frame = lambda pcm: {"event": "media", "media": {"payload": base64.b64encode(p.ulaw_encode(pcm)).decode()}}
loud, quiet = (np.ones(160, np.int16) * 9000), np.zeros(160, np.int16)
with client.websocket_connect("/media", headers={"X-Twilio-Signature": p.twilio_signature(cfg["TWILIO_AUTH_TOKEN"], url, {})}) as ws:
    ws.send_text(json.dumps({"event": "start", "start": {"streamSid": "MZ1", "callSid": "CA1",
                                                          "customParameters": {"token": token}}}))
    for pcm in [loud] * 40 + [quiet] * 60:              # 0.8 s of speech, then 1.2 s of quiet
        ws.send_text(json.dumps(frame(pcm)))
    deadline = time.time() + 5
    while time.time() < deadline and ("call", "hello from the road") not in list(ph.inbox._queue):
        time.sleep(0.05)
    assert ("call_start",) in list(ph.inbox._queue) and ("call", "hello from the road") in list(ph.inbox._queue)
    ph.call.speak("Good evening.")
    sent = [json.loads(ws.receive_text()) for _ in range(2)]
    assert sent[0]["event"] == "media" and sent[0]["streamSid"] == "MZ1" and len(base64.b64decode(sent[0]["media"]["payload"])) == 800
    assert sent[1]["event"] == "mark" and ph.call.speaking
    ws.send_text(json.dumps({"event": "mark", "mark": {"name": "said"}}))
    time.sleep(0.2)
    assert not ph.call.speaking
    ws.send_text(json.dumps({"event": "stop"}))
time.sleep(0.2)
assert ph.call is None and ("call_end",) in list(ph.inbox._queue)
from starlette.websockets import WebSocketDisconnect
try:                                                       # an unsigned media stream is refused outright
    with client.websocket_connect("/media", headers={"X-Twilio-Signature": "forged"}) as ws:
        ws.receive_text()
    raise AssertionError("an unsigned media stream must be refused")
except WebSocketDisconnect as refused:
    assert refused.code == 1008
drain()


# --- calling the user: texts with codes, ringing, going quiet, asking again ---
class FakePhone:
    def __init__(self):
        self.inbox, self.texts, self.rings, self.call, self.greeting, self.hung_up = asyncio.Queue(), [], 0, None, "", False
    async def text(self, body): self.texts.append(body); return True
    async def ring(self): self.rings += 1; return True
    async def hang_up(self): self.hung_up = True
    async def start(self): return True
    async def stop(self): pass


class FakeCall:
    def __init__(self): self.said = []
    def speak(self, text): self.said.append(text)


class LocalIO:
    standby = False
    def __init__(self): self.said, self.asked, self.lines = [], [], asyncio.Queue()
    def say(self, text): self.said.append(text)
    def ask(self, s, d): self.asked.append(s)
    async def hear(self): return await self.lines.get()
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): pass


async def calling():
    p.ANSWER_AFTER_S, p.WATCH_EVERY_S = 0.1, 0.05
    local, fake = LocalIO(), FakePhone()
    standby_calls = []
    ch = p.Channels(local, fake, lambda on, quiet=False: standby_calls.append((on, quiet)) or setattr(local, "standby", on))
    g._io = ch
    await ch.__aenter__()
    # a text from the phone: heard, and the reply goes back by text
    await fake.inbox.put(("sms", "how's the backup?"))
    assert await ch.hear() == "how's the backup?" and ch.source == "sms"
    ch.say("Half done.")
    ch.end_of_turn()
    await asyncio.sleep(0)
    assert fake.texts[-1] == "Half done."
    # while away by text, a question goes out by text with a code; only that code answers it
    task = asyncio.create_task(g.ask_before_risky("PowerShell", {"command": "Restart-Computer"}, None))
    await asyncio.sleep(0.02)
    code = ch._code
    assert f"Reply YES {code}, NO {code} or ALWAYS {code}." in fake.texts[-1]
    # A plain "yes" doesn't answer (caller ID can be faked): it gets the code back. A wrong code
    # doesn't either. The right code answers it right there, without going on to Jarvis.
    hearing = asyncio.create_task(ch.hear())
    await fake.inbox.put(("sms", "yes"))
    await fake.inbox.put(("sms", "yes 0000"))
    await fake.inbox.put(("sms", f"No {code}"))
    assert type(await asyncio.wait_for(task, 2)).__name__ == "PermissionResultDeny"
    assert any(t == f"To answer, reply YES {code}, NO {code} or ALWAYS {code}." for t in fake.texts)
    assert any("already been answered" in t for t in fake.texts) and not hearing.done()
    hearing.cancel()
    # Trust mode answering a question tells the phone, so it stops chasing it.
    task = asyncio.create_task(g.ask_before_risky("mcp__x__y", {}, None))
    await asyncio.sleep(0.02)
    assert ch._question
    g.set_trust(5)
    assert type(await task).__name__ == "PermissionResultAllow" and ch._question is None
    g.set_trust(0)
    # back at the PC; a question goes unanswered: texted, rung; nobody picks up: quiet and on standby
    await local.lines.put("hello")
    assert await ch.hear() == "hello" and ch.source == "local"
    fake.texts.clear()
    task = asyncio.create_task(g.ask_before_risky("PowerShell", {"command": "Stop-Computer"}, None))
    await asyncio.sleep(0.4)
    assert fake.rings == 1 and "Reply YES" in fake.texts[0]
    await fake.inbox.put(("call_missed",))
    hearing = asyncio.create_task(ch.hear())
    await asyncio.sleep(0.05)
    assert ch.parked and standby_calls == [(True, True)] and "Call or text me" in fake.texts[-1]
    await asyncio.sleep(0.3)
    assert fake.rings == 1, "it rings once per question, not again and again"
    # the user calls back: Jarvis asks the question on the call
    fake.call = FakeCall()
    await fake.inbox.put(("call_start",))
    await asyncio.sleep(0.05)
    assert not ch.parked and standby_calls[-1] == (False, True)
    assert fake.call.said[0] == "Thanks for picking up. One thing first." and fake.call.said[1].startswith("May I run a command")
    await fake.inbox.put(("call", "No."))
    assert await hearing == "No." and ch.source == "call"
    assert g.answer_oldest_question("No.") and type(await task).__name__ == "PermissionResultDeny"
    # on a call, Jarvis's replies are spoken on the call; "goodbye" hangs up and isn't passed on
    ch.say("Anything else?")
    assert fake.call.said[-1] == "Anything else?"
    await fake.inbox.put(("call", "Okay, goodbye."))
    await fake.inbox.put(("call_end",))
    fake.call_after = None
    hearing = asyncio.create_task(ch.hear())
    await asyncio.sleep(2.3)
    assert fake.hung_up and not hearing.done() and ch.source == "sms"
    hearing.cancel()
    await ch.__aexit__(None, None, None)


asyncio.run(calling())

# --- calls at a set time: kept in a file, due ones taken off and handed to Jarvis as a turn ---
from datetime import datetime
now = datetime(2026, 11, 13, 18, 30)
assert p.parse_when("19:05", now) == datetime(2026, 11, 13, 19, 5).timestamp()
assert p.parse_when("08:00", now) == datetime(2026, 11, 14, 8, 0).timestamp()      # the next 8:00
assert p.parse_when("2026-11-14 07:15", now) == datetime(2026, 11, 14, 7, 15).timestamp()
assert p.parse_when("seven-ish", now) is None
assert "couldn't read" in p.schedule_call("soon", "x") and "future" in p.schedule_call("2020-01-01 10:00", "x")
soon = datetime.fromtimestamp(time.time() + 3600).strftime("%Y-%m-%d %H:%M")
assert p.schedule_call(soon, "how the  backup went").startswith("Scheduled for")
assert [c["about"] for c in p.scheduled_calls()] == ["how the backup went"] and p.due_calls() == []
calls = p.scheduled_calls()
calls[0]["at"] = time.time() - 1
p._save_calls(calls)
assert p.due_calls() == ["how the backup went"] and p.scheduled_calls() == [] and p.due_calls() == []
assert "{about}" in p.CALL_DUE and "call_me" in p.CALL_DUE
assert {"mcp__phone__call_me_at", "mcp__phone__scheduled_calls"} <= set(g.AUTO_APPROVED)
print("ALL OK")

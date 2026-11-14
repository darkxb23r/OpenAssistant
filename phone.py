"""
Phone: Jarvis texts and calls the user through Twilio, and the user can text or call Jarvis.

secrets.env holds TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_NUMBER (Jarvis's number),
MY_PHONE_NUMBER (the user's), and optionally JARVIS_CALL_PIN.

- Twilio reaches Jarvis through a Cloudflare quick tunnel (cloudflared) to a small local server.
  The tunnel's address changes each start, so Jarvis points its number's webhooks at it then.
  Every request is checked against Twilio's signature, and only the user's number is served.
- Texts: the user texts Jarvis like talking to it; replies, task news and questions come back by
  text. A question carries a 4-digit code ("Reply YES 4821..."): caller ID can be faked, and a
  forger never sees the code.
- Calls: live speech both ways over Twilio Media Streams (8 kHz mu-law), through the same Whisper
  and Kokoro as at home. Calls in from the user's number also need JARVIS_CALL_PIN on the keypad
  when it's set. Saying "goodbye" on a call hangs up; it never stops Jarvis.
- Calling the user (Channels): a question unanswered at the PC for ANSWER_AFTER_S is texted and
  rung through. If the call isn't picked up (or voicemail answers), Jarvis texts that it'll wait,
  goes on standby, and asks again when the user calls or texts back.
"""
import asyncio
import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import random
import re
import secrets
import shutil
import subprocess
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

import httpx
import numpy as np
import uvicorn
from scipy.signal import resample_poly
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

import guardrails
import identity
import mcp_launch
from claude_agent_sdk import tool

log = logging.getLogger("jarvis.phone")
logging.getLogger("httpx").setLevel(logging.WARNING)      # request lines would include the account SID
logging.getLogger("uvicorn").setLevel(logging.WARNING)

PORT = 8765

# How a call opens: by the time of day, now and then the day of the week, or simply by name. Fixed
# text (said at once, no Claude turn), never the same twice running.
GREETINGS = {
    "morning": ["Good morning, sir.", "Good morning, sir. What can I do for you?", "Morning, sir.",
                "Good morning, sir. {name} here.", "Good morning, sir. Early start?"],
    "afternoon": ["Good afternoon, sir.", "Good afternoon, sir. How can I help?", "Afternoon, sir.",
                  "Good afternoon, sir. {name} here."],
    "evening": ["Good evening, sir.", "Good evening, sir. What can I do for you?", "Evening, sir.",
                "Good evening, sir. {name} here."],
    "night": ["You're up late, sir. What can I do for you?", "Burning the midnight oil, sir?",
              "{name} here, sir. Late one tonight?", "Good evening, sir. Still at it?"],
}
ANY_TIME = ["{name} here.", "{name} here, sir.", "{name} speaking.", "At your service, sir.", "Yes, sir?",
            "Sir. What do you need?"]
ON_DAY = {4: ["Happy Friday, sir.", "Good {part}, sir. Friday at last."],
          5: ["Good {part}, sir. Enjoying the weekend?"], 6: ["Good {part}, sir. Quiet Sunday?"],
          0: ["Good {part}, sir. Back to the week, then."]}
_last_greeting = ""


def part_of_day(now: datetime) -> str:
    h = now.hour
    return "morning" if 5 <= h < 12 else "afternoon" if 12 <= h < 17 else "evening" if 17 <= h < 22 else "night"


def salutation(now: datetime | None = None) -> str:
    """Just the time of day: "Good evening, sir." ("You're up late, sir." at night)."""
    part = part_of_day(now or datetime.now())
    return "You're up late, sir." if part == "night" else f"Good {part}, sir."


def call_greeting(now: datetime | None = None, rng=random) -> str:
    """What Jarvis says when a call connects."""
    global _last_greeting
    now = now or datetime.now()
    part = part_of_day(now)
    choices = GREETINGS[part] + ANY_TIME
    if part != "night":
        choices += ON_DAY.get(now.weekday(), [])
    choices = [c.format(name=identity.NAME, part=part) for c in choices]
    pick = rng.choice([c for c in choices if c != _last_greeting] or choices)
    _last_greeting = pick
    return pick
API = "https://api.twilio.com/2010-04-01"
CLOUDFLARED = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "cloudflared" / "cloudflared.exe"
TUNNEL_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
ANSWER_AFTER_S = 60       # a question unanswered at the PC this long gets texted and rung through
RING_S = 25
WATCH_EVERY_S = 5
CALL_RATE = 8000
END_SILENCE_S = 0.8
MAX_UTTERANCE_S = 20
SMS_ANSWER = re.compile(r"^\W*(yes|no|always)\W+(\d{4})\W*$", re.IGNORECASE)
ANSWER_ELSEWHERE = "That one needs your answer at the PC or in the browser."


def phone_may_answer(question) -> bool:
    """Whether a text (its code answers as by=None) or a call can answer this question. The standing
    orders' charter (Question.only) can't be, so it's never texted with a code or rung through."""
    return question is None or any(guardrails.may_answer(question, by) for by in (None, "call"))


waiting_elsewhere = guardrails.waiting_elsewhere   # the one plain text about a question only the PC or a screen can answer


GOODBYE = re.compile(r"^\W*(?:ok(?:ay)?\W+)?(?:good ?bye|bye|hang up|that'?s all|talk (?:to you )?later)\W*$", re.IGNORECASE)
current = None            # the running Phone, for the text_me / call_me tools


def settings() -> dict | None:
    """Twilio settings from secrets.env, or None if any are missing."""
    s = mcp_launch.read_secrets()
    keys = ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_NUMBER", "MY_PHONE_NUMBER")
    return {k: s[k] for k in keys} | {"JARVIS_CALL_PIN": s.get("JARVIS_CALL_PIN", "")} if all(s.get(k) for k in keys) else None


def twilio_signature(token: str, url: str, params: dict) -> str:
    """Twilio's request signature: HMAC-SHA1 over the URL and the sorted POST parameters."""
    data = url + "".join(k + params[k] for k in sorted(params))
    return base64.b64encode(hmac.new(token.encode(), data.encode(), hashlib.sha1).digest()).decode()


# --- 8 kHz mu-law, the phone network's audio ---------------------------------------------------
_EXPONENT = np.array([0, 0, 1, 1, 2, 2, 2, 2] + [3] * 8 + [4] * 16 + [5] * 32 + [6] * 64 + [7] * 128, dtype=np.int32)


def ulaw_decode(data: bytes) -> np.ndarray:
    u = ~np.frombuffer(data, np.uint8).astype(np.int32) & 0xFF
    magnitude = ((((u & 0x0F) << 3) + 0x84) << ((u >> 4) & 7)) - 0x84
    return np.where(u & 0x80, -magnitude, magnitude).astype(np.int16)


def ulaw_encode(pcm: np.ndarray) -> bytes:
    x = pcm.astype(np.int32)
    sign = np.where(x < 0, 0x80, 0)
    x = np.minimum(np.abs(x), 32635) + 0x84
    exponent = _EXPONENT[(x >> 7) & 0xFF]
    return (~(sign | (exponent << 4) | ((x >> (exponent + 3)) & 0x0F)) & 0xFF).astype(np.uint8).tobytes()


# --- one live call ---------------------------------------------------------------------------------
class CallSession:
    """Speech in and out of one call. Half duplex: while Jarvis talks, the caller isn't transcribed."""

    def __init__(self, ws: WebSocket, stream_sid: str, call_sid: str, stt, tts, vad, inbox: asyncio.Queue):
        self.ws, self.stream_sid, self.call_sid = ws, stream_sid, call_sid
        self.stt, self.tts, self.vad, self.inbox = stt, tts, vad, inbox
        self._speech: asyncio.Queue = asyncio.Queue()
        self._marks = 0               # audio chunks sent whose playback Twilio hasn't confirmed yet
        self._talking_until = 0.0     # when the audio sent so far ends, if Twilio's confirmation never comes
        self._buffer = np.zeros(0, np.int16)
        self._utterance: list[np.ndarray] = []
        self._heard, self._quiet = False, 0.0
        self._speaker = asyncio.create_task(self._speak_queued())

    @property
    def speaking(self) -> bool:
        if self._marks and time.monotonic() > self._talking_until:
            self._marks = 0           # no confirmation from Twilio: assume the audio has played
        return self._marks > 0 or not self._speech.empty()

    def speak(self, text: str) -> None:
        self._speech.put_nowait(text)

    async def _speak_queued(self) -> None:
        from voice import LANG, VOICE, _clean_for_speech
        while True:
            text = await self._speech.get()
            try:
                samples, rate = await asyncio.to_thread(self.tts.create, _clean_for_speech(text, []), voice=VOICE, lang=LANG)
                pcm = np.clip(resample_poly(samples, CALL_RATE, rate) * 32767, -32768, 32767).astype(np.int16)
                audio = ulaw_encode(pcm)
                for i in range(0, len(audio), 8000):       # one second per message
                    payload = base64.b64encode(audio[i:i + 8000]).decode()
                    await self.ws.send_json({"event": "media", "streamSid": self.stream_sid, "media": {"payload": payload}})
                self._marks += 1
                self._talking_until = max(self._talking_until, time.monotonic()) + len(pcm) / CALL_RATE + 1.0
                await self.ws.send_json({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": "said"}})
                log.info("call: said %.1fs of audio: %s", len(pcm) / CALL_RATE, guardrails.short(text, 80))
            except Exception as e:
                log.warning("couldn't speak on the call: %s", e)

    async def handle(self, event: dict) -> None:
        if event.get("event") == "mark":
            log.info("call: Twilio finished playing what Jarvis said")
            self._marks = max(0, self._marks - 1)
            if not self.speaking:
                self._reset()
        elif event.get("event") == "media" and not self.speaking:
            pcm8 = ulaw_decode(base64.b64decode(event["media"]["payload"]))
            self._buffer = np.concatenate([self._buffer, resample_poly(pcm8, 2, 1).astype(np.int16)])
            while len(self._buffer) >= 1280:             # 80 ms frames at 16 kHz, what the VAD expects
                frame, self._buffer = self._buffer[:1280], self._buffer[1280:]
                await self._frame(frame)

    def _reset(self) -> None:
        self._buffer, self._utterance, self._heard, self._quiet = np.zeros(0, np.int16), [], False, 0.0
        self.vad.reset_states()

    async def _frame(self, frame: np.ndarray) -> None:
        self._utterance.append(frame)
        if self.vad.predict(frame) >= 0.5:
            self._heard, self._quiet = True, 0.0
        else:
            self._quiet += 0.08
            if not self._heard and len(self._utterance) > 6:
                self._utterance = self._utterance[-6:]   # keep a little lead-in, drop the silence
        if self._heard and (self._quiet >= END_SILENCE_S or len(self._utterance) * 0.08 >= MAX_UTTERANCE_S):
            audio = np.concatenate(self._utterance).astype(np.float32) / 32768
            log.info("call: heard %.1fs of speech, transcribing", len(audio) / 16000)
            self._reset()

            def transcribe():
                segments, _ = self.stt.transcribe(audio, language="en", beam_size=1)
                return "".join(s.text for s in segments).strip()

            if text := await asyncio.to_thread(transcribe):
                await self.inbox.put(("call", text))

    def close(self) -> None:
        self._speaker.cancel()


# --- Twilio, the tunnel and the local server ------------------------------------------------------------
class _QuietServer(uvicorn.Server):
    def capture_signals(self):     # leave Ctrl+C to Jarvis
        return contextlib.nullcontext()


class Phone:
    def __init__(self, cfg: dict, load_models):
        self.cfg = cfg
        self._load_models = load_models          # () -> (stt, tts), loaded once, on first use
        self._models = None
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.public = ""                         # https://<random>.trycloudflare.com
        self.call: CallSession | None = None
        self.greeting = ""                       # what to say when the user picks up a call_me call
        self._stream_tokens: set[str] = set()    # issued only in TwiML answering a signed request
        self._http = httpx.AsyncClient(timeout=30, auth=(cfg["TWILIO_ACCOUNT_SID"], cfg["TWILIO_AUTH_TOKEN"]))
        self._account = f"{API}/Accounts/{cfg['TWILIO_ACCOUNT_SID']}"
        self._tunnel = None
        self._server = None
        self.app = Starlette(routes=[
            Route("/sms", self.sms_webhook, methods=["POST"]),
            Route("/voice", self.voice_webhook, methods=["POST"]),
            Route("/voice/pin", self.pin_webhook, methods=["POST"]),
            Route("/voice/outbound", self.outbound_webhook, methods=["POST"]),
            Route("/voice/status", self.status_webhook, methods=["POST"]),
            WebSocketRoute("/media", self.media_socket),
        ])

    # --- lifecycle ---
    async def start(self) -> bool:
        global current
        exe = str(CLOUDFLARED) if CLOUDFLARED.exists() else shutil.which("cloudflared")
        if not exe:
            print("   [phone: cloudflared isn't installed; texting and calls are off]")
            return False
        self._server = _QuietServer(uvicorn.Config(self.app, host="127.0.0.1", port=PORT, log_level="warning",
                                                   lifespan="off"))
        asyncio.create_task(self._server.serve())
        self.public = await asyncio.to_thread(self._start_tunnel, exe)
        if not self.public:
            print("   [phone: the tunnel didn't come up; texting and calls are off]")
            return False
        if not await self._point_number_here():
            print("   [phone: couldn't update the Twilio number; check the Twilio settings in secrets.env]")
            return False
        current = self
        asyncio.get_running_loop().run_in_executor(None, self.models)   # warm up speech for calls
        print(f"   [phone: texts and calls on {self.cfg['TWILIO_NUMBER']}]")
        log.info("phone up at %s", self.public)
        return True

    def _start_tunnel(self, exe: str) -> str:
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._tunnel = subprocess.Popen([exe, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{PORT}"],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                        text=True, encoding="utf-8", errors="replace", creationflags=flags)
        found = {}

        def read():
            for line in self._tunnel.stderr:     # keep draining, or cloudflared blocks on a full pipe
                if not found and (m := TUNNEL_URL.search(line)):
                    found["url"] = m[0]

        threading.Thread(target=read, daemon=True).start()
        for _ in range(120):
            if found or self._tunnel.poll() is not None:
                break
            time.sleep(0.25)
        return found.get("url", "")

    async def stop(self) -> None:
        global current
        current = None
        if self._server:
            self._server.should_exit = True
        if self._tunnel and self._tunnel.poll() is None:
            self._tunnel.terminate()
        await self._http.aclose()

    def models(self):
        if self._models is None:
            self._models = self._load_models()
        return self._models

    # --- Twilio's REST API ---
    async def _api(self, method: str, path: str, **data):
        try:
            r = await self._http.request(method, f"{self._account}/{path}", data=data or None)
            if r.status_code >= 400:
                log.warning("Twilio %s %s failed: %s %s", method, path.split("/")[0], r.status_code, r.text[:300])
                return None
            return r.json()
        except Exception as e:
            log.warning("Twilio %s failed: %s", path.split("/")[0], type(e).__name__)
            return None

    async def _point_number_here(self) -> bool:
        try:
            r = await self._http.get(f"{self._account}/IncomingPhoneNumbers.json",
                                     params={"PhoneNumber": self.cfg["TWILIO_NUMBER"]})
            numbers = r.json().get("incoming_phone_numbers", []) if r.status_code == 200 else []
        except Exception:
            numbers = []
        if not numbers:
            return False
        done = await self._api("POST", f"IncomingPhoneNumbers/{numbers[0]['sid']}.json",
                               SmsUrl=f"{self.public}/sms", SmsMethod="POST",
                               VoiceUrl=f"{self.public}/voice", VoiceMethod="POST")
        return done is not None

    async def text(self, body: str) -> bool:
        sent = await self._api("POST", "Messages.json", From=self.cfg["TWILIO_NUMBER"], To=self.cfg["MY_PHONE_NUMBER"],
                               Body=body[:1500])
        log.info("texted the user: %s", guardrails.short(body))
        return sent is not None

    async def ring(self) -> bool:
        """Call the user's phone. Voicemail is detected and hung up on (it counts as missed)."""
        placed = await self._api("POST", "Calls.json", From=self.cfg["TWILIO_NUMBER"], To=self.cfg["MY_PHONE_NUMBER"],
                                 Url=f"{self.public}/voice/outbound", Method="POST", Timeout=str(RING_S),
                                 MachineDetection="Enable", StatusCallback=f"{self.public}/voice/status",
                                 StatusCallbackMethod="POST")
        log.info("calling the user%s", "" if placed else " failed")
        return placed is not None

    async def hang_up(self) -> None:
        if self.call:
            await self._api("POST", f"Calls/{self.call.call_sid}.json", Status="completed")

    # --- webhooks: checked, and only for the user's number ---
    async def _verified(self, request: Request) -> dict | None:
        params = {k: v for k, v in (await request.form()).items()}
        url = self.public + request.url.path + (f"?{request.url.query}" if request.url.query else "")
        expected = twilio_signature(self.cfg["TWILIO_AUTH_TOKEN"], url, params)
        if not hmac.compare_digest(expected, request.headers.get("X-Twilio-Signature", "")):
            log.warning("rejected an unsigned request to %s", request.url.path)
            return None
        return params

    def _from_user(self, params: dict) -> bool:
        number = params.get("From") if params.get("Direction", "inbound").startswith("inbound") else params.get("To")
        return number == self.cfg["MY_PHONE_NUMBER"]

    @staticmethod
    def _twiml(body: str = "") -> Response:
        return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>', media_type="text/xml")

    def _connect(self, greeting: str = "") -> Response:
        token = secrets.token_urlsafe(16)
        self._stream_tokens.add(token)
        host = self.public.removeprefix("https://")
        say = f"<Say>{escape(greeting)}</Say>" if greeting else ""
        return self._twiml(f'{say}<Connect><Stream url={quoteattr(f"wss://{host}/media")}>'
                           f'<Parameter name="token" value={quoteattr(token)}/></Stream></Connect>')

    async def sms_webhook(self, request: Request) -> Response:
        params = await self._verified(request)
        if params is None:
            return Response(status_code=403)
        if params.get("From") == self.cfg["MY_PHONE_NUMBER"] and (body := params.get("Body", "").strip()):
            await self.inbox.put(("sms", body))
        return self._twiml()

    async def voice_webhook(self, request: Request) -> Response:
        params = await self._verified(request)
        if params is None:
            return Response(status_code=403)
        if params.get("From") != self.cfg["MY_PHONE_NUMBER"]:
            return self._twiml("<Reject/>")
        if pin := self.cfg.get("JARVIS_CALL_PIN"):
            return self._twiml(f'<Gather numDigits="{len(pin)}" action="{self.public}/voice/pin" timeout="10">'
                               f"<Say>PIN, please.</Say></Gather><Hangup/>")
        return self._connect()

    async def pin_webhook(self, request: Request) -> Response:
        params = await self._verified(request)
        if params is None:
            return Response(status_code=403)
        if params.get("From") == self.cfg["MY_PHONE_NUMBER"] and \
                hmac.compare_digest(params.get("Digits", ""), self.cfg.get("JARVIS_CALL_PIN", "")):
            return self._connect()
        log.warning("wrong PIN on an incoming call")
        return self._twiml("<Say>Sorry.</Say><Hangup/>")

    async def outbound_webhook(self, request: Request) -> Response:
        params = await self._verified(request)
        if params is None or not self._from_user(params):
            return Response(status_code=403)
        if params.get("AnsweredBy", "human").startswith(("machine", "fax")):
            await self.inbox.put(("call_missed",))
            return self._twiml("<Hangup/>")
        return self._connect()

    async def status_webhook(self, request: Request) -> Response:
        params = await self._verified(request)
        if params is None:
            return Response(status_code=403)
        if params.get("CallStatus") in ("no-answer", "busy", "failed", "canceled"):
            await self.inbox.put(("call_missed",))
        return self._twiml()

    async def media_socket(self, ws: WebSocket) -> None:
        url = f"wss://{self.public.removeprefix('https://')}/media"
        if not hmac.compare_digest(twilio_signature(self.cfg["TWILIO_AUTH_TOKEN"], url, {}),
                                   ws.headers.get("X-Twilio-Signature", "")):
            await ws.close(code=1008)
            return
        await ws.accept()
        session = None
        try:
            while True:
                event = json.loads(await ws.receive_text())
                if event.get("event") == "start":
                    start = event["start"]
                    if start.get("customParameters", {}).get("token") not in self._stream_tokens:
                        await ws.close(code=1008)
                        return
                    self._stream_tokens.discard(start["customParameters"]["token"])
                    from openwakeword.vad import VAD
                    stt, tts = await asyncio.to_thread(self.models)
                    session = self.call = CallSession(ws, start["streamSid"], start["callSid"], stt, tts, VAD(), self.inbox)
                    log.info("call connected")
                    await self.inbox.put(("call_start",))
                elif event.get("event") == "stop":
                    break
                elif session:
                    await session.handle(event)
        except WebSocketDisconnect:
            pass
        finally:
            if session:
                session.close()
                if self.call is session:
                    self.call = None
                log.info("call ended")
                await self.inbox.put(("call_end",))


# --- the PC and the phone, behind one interface ------------------------------------------------------
class _NoLocal:
    """Channels as one of the hub's channels (local=None): only the phone; the hub holds the rest."""
    standby = False
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): pass
    def say(self, text): pass
    def ask(self, summary, detail): pass
    async def hear(self): await asyncio.Event().wait()


class Channels:
    """Texts and calls, as a channel of the hub (jarvis.main passes local=None); or, the older way,
    voice or keyboard at the PC plus texts and calls behind one say/ask/hear interface."""

    kind = "phone"
    always_ask = True    # it tracks every question, to text and ring about the ones left unanswered

    def __init__(self, local, phone: Phone, set_standby):
        self.local, self.phone = local if local is not None else _NoLocal(), phone
        self._set_standby = set_standby          # jarvis.set_standby(on, quiet)
        self.source = "local"                    # where the user last spoke from: local, sms or call
        self._outbox: list[str] = []
        self._local_hear = self._phone_hear = None
        self._question = None                    # the question being asked now
        self._code = ""
        self._asked_at = 0.0
        self._called_for = None                  # the question Jarvis rang the user about
        self._elsewhere = None                   # the question now asked that the phone can't answer
        self._told_elsewhere = None              # ... and the one the user has been texted about
        self.parked = False                     # went quiet, waiting for the user to come back
        self._standby_by_call = False
        self._watcher = None
        guardrails.answer_hooks.append(self._answered)

    @property
    def away(self) -> bool:
        return self.source in ("sms", "call")

    @property
    def trust(self) -> str:
        """Who may answer what (guardrails.UNTRUSTED_ANSWERS): a text answers only with its code."""
        return self.source if self.away else "physical"

    def deliver(self, text: str) -> None:
        """Task news to someone texting or on a call: spoken on the call, or texted right now."""
        if self.phone and self.phone.call:
            self.phone.call.speak(text)
        else:
            self.text_user(text)

    def text_user(self, text: str) -> None:
        if self.phone:
            self._later(self.phone.text(text))

    @property
    def standby(self):
        return getattr(self.local, "standby", False)

    @standby.setter
    def standby(self, on):
        if hasattr(self.local, "standby"):
            self.local.standby = on

    def _busy_speaking(self) -> bool:
        return getattr(self.local, "_busy_speaking", lambda: False)()

    async def __aenter__(self):
        await self.local.__aenter__()
        if not await self.phone.start():
            self.phone = None
        self._watcher = asyncio.create_task(self._watch())
        return self

    async def __aexit__(self, *exc):
        if self._watcher:
            self._watcher.cancel()
        if self.phone:
            await self.phone.stop()
        await self.local.__aexit__(*exc)

    def _later(self, coro) -> None:
        asyncio.get_running_loop().create_task(coro)

    def say(self, text: str) -> None:
        if self.phone and self.phone.call:
            print(f"jarvis (call)> {text}")
            self.phone.call.speak(text)
            return
        self.local.say(text)
        if self.source == "sms":
            self._outbox.append(text)

    def notify(self, text: str) -> None:
        """Task news: said wherever the user is, and always texted."""
        self.say(text)
        if self.phone and not self.phone.call and self.source != "sms":
            self._later(self.phone.text(text))

    def ask(self, summary: str, detail: str) -> None:
        self.local.ask(summary, detail)
        question = next((q for q in guardrails._pending_questions
                         if getattr(q, "summary", None) == summary and getattr(q, "detail", None) == detail), None)
        if not phone_may_answer(question):
            # No code, no call: at most one plain text saying where it can be answered.
            self._question, self._code, self._elsewhere, self._asked_at = None, "", question, time.monotonic()
            if self.phone and (self.phone.call or self.source == "sms"):
                self._tell_elsewhere()
            return
        self._elsewhere = None
        self._question, self._code, self._asked_at = (summary, detail), f"{secrets.randbelow(9000) + 1000}", time.monotonic()
        if self.phone and self.phone.call:
            self.phone.call.speak(f"May I {summary}? Say yes, no, or always.")
        elif self.phone and self.source == "sms":
            self._text_question()

    def _tell_elsewhere(self) -> None:
        if self._elsewhere is not None and self._told_elsewhere is not self._elsewhere and self.phone:
            question = self._told_elsewhere = self._elsewhere

            async def send():   # worded when sent: the asker marks its question just after opening it
                await self.phone.text(waiting_elsewhere(question))
            self._later(send())

    def _text_question(self) -> None:
        summary, detail = self._question
        self._later(self.phone.text(f"May I {summary}?\n{detail[:300]}\n\nReply YES {self._code}, NO {self._code} "
                                    f"or ALWAYS {self._code}."))

    def end_of_turn(self) -> None:
        if self._outbox and self.phone:
            text, self._outbox = " ".join(self._outbox), []
            self._later(self.phone.text(text))
        if hasattr(self.local, "end_of_turn"):
            self.local.end_of_turn()

    # The older way (a local channel inside): it hears about turns and tools too.
    def turn_started(self) -> None:
        if hasattr(self.local, "turn_started"):
            self.local.turn_started()

    def tool(self, name: str) -> None:
        if hasattr(self.local, "tool"):
            self.local.tool(name)

    async def hear(self) -> str:
        while True:
            if self._local_hear is None:
                self._local_hear = asyncio.ensure_future(self.local.hear())
            waits = {self._local_hear}
            if self.phone:
                if self._phone_hear is None:
                    self._phone_hear = asyncio.ensure_future(self.phone.inbox.get())
                waits.add(self._phone_hear)
            done, _ = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            if self._local_hear in done:
                heard, self._local_hear = self._local_hear, None
                self.source = getattr(self.local, "source", "local")   # "web" for the browser
                return heard.result()
            item, self._phone_hear = self._phone_hear.result(), None
            if (line := await self._phone_item(item)) is not None:
                return line

    async def _phone_item(self, item: tuple) -> str | None:
        kind = item[0]
        if kind == "sms":
            self.source = "sms"
            oldest = guardrails._pending_questions[0] if guardrails._pending_questions else None
            if oldest is not None and not phone_may_answer(oldest) and (
                    SMS_ANSWER.match(item[1]) or len(item[1].split()) < 4 and guardrails.parse_answer(item[1]) is not None):
                await self.phone.text(ANSWER_ELSEWHERE)   # no code was ever given for it
                return None
            if m := SMS_ANSWER.match(item[1]):
                if self._question and guardrails._pending_questions and m[2] == self._code:
                    # Answered here, not by the main loop: only a text carrying the code may answer.
                    if not guardrails.answer_oldest_question(m[1].lower()):
                        # Some questions (adopting the standing-orders charter) never take a text.
                        await self.phone.text(ANSWER_ELSEWHERE)
                    return None
                await self.phone.text("That question has already been answered.")
                return None
            if (guardrails._pending_questions and len(item[1].split()) < 4
                    and guardrails.parse_answer(item[1]) is not None):
                await self.phone.text(f"To answer, reply YES {self._code}, NO {self._code} or ALWAYS {self._code}.")
                return None
            return item[1]
        if kind == "call_start":
            self.source = "call"
            if self.parked or (self._question and guardrails._pending_questions):
                self.returned(on_call=True)
            else:
                # A call it placed opens with the time of day and its reason; any other with a greeting.
                self.phone.call.speak(f"{salutation()} {self.phone.greeting}" if self.phone.greeting
                                      else call_greeting())
                self.phone.greeting = ""
            return None
        if kind == "call":
            if GOODBYE.match(item[1]):
                self.phone.call.speak("Goodbye.")
                await asyncio.sleep(2)
                await self.phone.hang_up()
                return None
            self.source = "call"
            return item[1]
        if kind == "call_end":
            self.source = "sms"                  # still away: replies go by text now
            return None
        if kind == "call_missed" and self._called_for and self._called_for == self._question:
            self._park()
        return None

    # --- calling: a question nobody answers ---
    def _answered(self, question, decision: str) -> None:
        if question == self._question:
            self._question, self._called_for, self.parked = None, None, False
        if self._elsewhere is not None and question == (self._elsewhere.summary, self._elsewhere.detail):
            self._elsewhere = None

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(WATCH_EVERY_S)
            if self.phone and on_call_due:
                for about in due_calls():
                    log.info("a scheduled call is due: %s", about)
                    on_call_due(about)
            if (self._elsewhere is not None and self._elsewhere in guardrails._pending_questions
                    and time.monotonic() - self._asked_at > ANSWER_AFTER_S):
                self._tell_elsewhere()   # once, and never a call: neither could answer it
            if (self.phone and self._question and self._called_for != self._question and not self.phone.call
                    and time.monotonic() - self._asked_at > ANSWER_AFTER_S):
                self._called_for = self._question
                log.info("no answer at the PC; texting and calling about %r", self._question[0])
                self._text_question()
                if not await self.phone.ring():
                    self._park()

    def _park(self) -> None:
        if self.parked:
            return
        self.parked = True
        if hasattr(self.local, "standby") and not self.local.standby:
            self._standby_by_call = True
            self._set_standby(True, quiet=True)
        log.info("no answer; waiting quietly until the user is back")
        self._later(self.phone.text("I couldn't reach you. Call or text me when you're free, and I'll ask again."))

    def returned(self, on_call: bool = False) -> bool:
        """The user is back after Jarvis went quiet: ask the waiting question again. True if it did."""
        if not (self.parked or on_call):
            return False
        self.parked = False
        if self._standby_by_call:
            self._standby_by_call = False
            self._set_standby(False, quiet=True)
        if not guardrails._pending_questions or not phone_may_answer(guardrails._pending_questions[0]):
            return False
        summary, detail = guardrails._pending_questions[0][0]
        if on_call and self.phone and self.phone.call:
            self.phone.call.speak("Thanks for picking up. One thing first.")
        else:
            self.say("Welcome back. One thing first.")
        self.ask(summary, detail)
        return True


# --- tools for Jarvis: only ever the user's own phone ---------------------------------------------
def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], **({"is_error": True} if error else {})}


@tool("text_me", "Send the user a text message on their phone.", {"message": str})
async def text_me_tool(args):
    if not current:
        return _text("The phone isn't set up.", error=True)
    return _text("Texted." if await current.text(args["message"]) else "The text didn't go through.")


@tool("call_me", "Ring the user's phone; when they pick up, you talk with them live. Give the reason you're calling "
                 "(said right after a 'Good evening, sir.', so don't greet them in it).",
      {"reason": str})
async def call_me_tool(args):
    if not current:
        return _text("The phone isn't set up.", error=True)
    current.greeting = args["reason"].strip()
    return _text("Ringing them now; if they pick up, you'll hear them." if await current.ring()
                 else "The call didn't go through.")


# --- calls the user asked for at a set time ("call me at 7 with how it went") ----------------------
# Kept in a file so a restart doesn't lose them. When one is due, Jarvis gets a turn of his own
# (on_call_due, set by jarvis.main) to find out where things stand and ring with the details.
CALLS_FILE = guardrails.JARVIS_HOME / "scheduled-calls.json"
MAX_AHEAD_S = 7 * 24 * 3600
on_call_due = None   # (about: str) -> None


def scheduled_calls() -> list[dict]:
    """[{"at": epoch seconds, "about": what to report}], soonest first."""
    try:
        calls = json.loads(CALLS_FILE.read_text(encoding="utf-8"))
        return sorted((c for c in calls if isinstance(c.get("at"), (int, float)) and c.get("about")),
                      key=lambda c: c["at"])
    except (OSError, ValueError, AttributeError, TypeError):
        return []


def _save_calls(calls: list[dict]) -> None:
    CALLS_FILE.write_text(json.dumps(calls, indent=1), encoding="utf-8")


def when_text(at: float) -> str:
    t = datetime.fromtimestamp(at)
    day = "today" if t.date() == datetime.now().date() else f"{t:%A}"
    return f"{t.hour % 12 or 12}:{t:%M %p} {day}"


def parse_when(text: str, now: datetime | None = None) -> float | None:
    """Local "YYYY-MM-DD HH:MM" (or with a T), or "HH:MM" for its next time today or tomorrow."""
    now, text = now or datetime.now(), text.strip()
    try:
        if re.fullmatch(r"\d{1,2}:\d{2}", text):
            hour, minute = map(int, text.split(":"))
            t = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            return (t if t > now else t + timedelta(days=1)).timestamp()
        return datetime.fromisoformat(text).timestamp()
    except ValueError:
        return None


def schedule_call(at_text: str, about: str) -> str:
    at, about = parse_when(at_text), " ".join(about.split())
    if at is None:
        return f"I couldn't read the time {at_text!r}: give it as YYYY-MM-DD HH:MM, local time."
    if not about:
        return "Say what the call is about."
    if not time.time() < at < time.time() + MAX_AHEAD_S:
        return "That time has to be in the future, and within a week."
    calls = scheduled_calls() + [{"at": at, "about": about}]
    _save_calls(calls)
    log.info("call scheduled for %s: %s", when_text(at), about)
    return f"Scheduled for {when_text(at)}."


def due_calls() -> list[str]:
    """Take the calls that are due off the list, and return what each is about."""
    calls = scheduled_calls()
    due = [c for c in calls if c["at"] <= time.time()]
    if due:
        _save_calls([c for c in calls if c not in due])
    return [c["about"] for c in due]


CALL_DUE = ("[Scheduled call: the user asked you to call them now about: {about}] Nobody is listening "
            "here, so say nothing. Find out where it stands (task_status, and what your background work "
            "has told you), then ring them with call_me, giving as the reason the outcome in two or three "
            "spoken sentences, plus anything they need to decide. If the call doesn't go through, text_me "
            "the same instead.")


@tool("call_me_at", "Ring the user at a set time, to report on something (a task's outcome, a reminder). "
      "At that time you get a turn to find out where it stands and call with the details. 'at' is local "
      "time as YYYY-MM-DD HH:MM.", {"at": str, "about": str})
async def call_me_at_tool(args):
    if not current:
        return _text("The phone isn't set up.", error=True)
    reply = schedule_call(str(args.get("at", "")), str(args.get("about", "")))
    return _text(reply, error=not reply.startswith("Scheduled"))


@tool("scheduled_calls", "List the calls you've scheduled, or cancel them: 'cancel' is 'all', a time "
      "as listed (HH:MM), or empty to only list.", {"cancel": str})
async def scheduled_calls_tool(args):
    calls, cancel = scheduled_calls(), str(args.get("cancel", "")).strip().lower()
    if cancel:
        keep = [] if cancel == "all" else [c for c in calls if datetime.fromtimestamp(c["at"]).strftime("%H:%M") != cancel]
        _save_calls(keep)
        return _text(f"Cancelled {len(calls) - len(keep)}.")
    return _text("; ".join(f"{when_text(c['at'])} ({datetime.fromtimestamp(c['at']):%H:%M}): {c['about']}"
                           for c in calls) or "None scheduled.")


TOOLS = [text_me_tool, call_me_tool, call_me_at_tool, scheduled_calls_tool]

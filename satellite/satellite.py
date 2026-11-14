"""
A voice satellite for Jarvis: a microphone and a speaker, in another room or on another machine,
that talk to a running Jarvis core (`run_jarvis.py --core`) over the client protocol (protocol.py).

- The wake word ("Hey Jarvis") and the voice-activity detector run here, as in voice mode
  (voice.Voice). Each utterance goes to the core's /stt, and the text is sent as the user's line.
- Jarvis's replies, news and questions arrive as frames and are spoken through the core's /tts, played
  here. Hearing and speaking carry the media ticket from the welcome as a bearer token.
- Questions are asked aloud and answered by voice: a yes, no or always while one is open is sent
  as its answer. The standing orders' charter can't be answered from here (a paired device, but a
  voice alone): the core says once where it waits instead.
- First run: `jarvisctl pair --kind satellite --name kitchen` on the core, then `satellite.py --pair CODE` here.
  The device token is kept in the user's config folder (never in the project), 0600 where it can be.
- If the core goes away, it redials with backoff (1 s, 2 s, 4 s ... at most 30 s). A revoked
  device (4001), a protocol mismatch (4002) or no pairing (4003) is shown, and it stops.

    python satellite/satellite.py --pair 123456 --name kitchen   # once
    python satellite/satellite.py                                # from then on

The core listens on 127.0.0.1 only: from another machine, tunnel to it first
(`ssh -N -L 8790:127.0.0.1:8790 user@core`). See satellite/README.md.
"""
import argparse
import asyncio
import contextlib
import json
import os
import re
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))   # voice.py and protocol.py, from the Jarvis folder

import protocol   # noqa: E402  (stdlib only)

VERSION = "1"
DEFAULT_URL = f"ws://127.0.0.1:{os.environ.get('JARVIS_WEB_PORT') or 8790}/"
BACKOFF_MAX_S = 30
HELLO_TIMEOUT_S = 10

STOP_MESSAGES = {
    protocol.CLOSE_REVOKED: "Jarvis says this satellite isn't paired any more (revoked). To pair it again: on the "
                            "core, jarvisctl pair --kind satellite --name NAME; here, satellite.py --pair CODE.",
    protocol.CLOSE_VERSION: "This satellite speaks a protocol version the core doesn't. Update it: on the core, "
                            "jarvisctl satellite-bundle DIR, and copy that folder here.",
    protocol.CLOSE_PAIRING: "This satellite isn't paired with Jarvis (or the code was wrong or expired). On the core: "
                            "jarvisctl pair --kind satellite --name NAME; here: satellite.py --pair CODE.",
}

# The same yes and no words as guardrails.parse_answer (the core's), which a satellite can't import.
YES_WORDS = {"y", "yes", "yeah", "yep", "sure", "ok", "okay", "affirmative", "allow", "approve", "approved"}
NO_WORDS = {"n", "no", "nope", "nah", "not", "don't", "dont", "deny", "denied", "decline", "declined", "stop",
            "cancel", "never", "negative", "skip", "refuse"}


def parse_answer(text: str) -> str | None:
    """'always', 'yes', 'no', or None when the line isn't an answer (then it goes to Jarvis)."""
    words = re.findall(r"[a-z']+", text.lower())
    if not words:
        return None
    if words[0] == "always":
        return "always"
    if words[0] in YES_WORDS or " ".join(words[:2]) in {"go ahead", "do it"}:
        return "always" if "always" in words[1:3] else "yes"
    if words[0] in NO_WORDS or " ".join(words[:2]) in {"do not", "hold off"}:
        return "no"
    return None


def backoff(attempt: int) -> float:
    """Seconds to wait before redialling, after `attempt` failed tries in a row: 1, 2, 4 ... 30."""
    return float(min(BACKOFF_MAX_S, 2 ** min(attempt, 10)))


def token_path() -> Path:
    """Where the device token lives: the user's config folder, never the project (a git repo)."""
    if os.name == "nt" and os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "jarvis-satellite" / "token"


def read_token(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def write_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    tmp.replace(path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def http_url(ws_url: str) -> str:
    """ws://host:port/ -> http://host:port (where /stt and /tts are)."""
    url = urlsplit(ws_url)
    return f"{'https' if url.scheme == 'wss' else 'http'}://{url.netloc}"


class Satellite:
    """One satellite: the protocol session with the core, around a voice.Voice whose speech engines
    are the core's (remote: voice.RemoteSpeech, shared with the Voice's RemoteSTT / RemoteTTS)."""

    def __init__(self, url: str, name: str, voice, remote, token_file: Path, pair: str | None = None):
        self.url, self.name, self.voice, self.remote = url, name, voice, remote
        self.token_file, self.pair = token_file, pair
        self.questions: list[dict] = []   # questions asked here and still open, oldest first
        self.ws = None
        self._asks = 0
        self._loop = None
        self.sleep = asyncio.sleep   # between redials (a test counts them instead)
        voice.on_barge_in = self._barge_in
        remote.on_refused = self._ticket_refused

    # --- the connection, kept up --------------------------------------------------------------------
    async def run(self) -> int:
        """Talk to the core until it refuses this satellite for good (then 1)."""
        import websockets
        self._loop = asyncio.get_running_loop()
        if not self.pair and not read_token(self.token_file):
            print(STOP_MESSAGES[protocol.CLOSE_PAIRING])
            return 1
        attempt = 0
        while True:
            welcomed = False
            try:
                code, welcomed = await self.session()
            except (OSError, asyncio.TimeoutError, websockets.InvalidHandshake, websockets.ConnectionClosed) as e:
                code = None
                print(f"   [Jarvis isn't answering at {self.url}: {e}]")
            if code in STOP_MESSAGES:
                print(STOP_MESSAGES[code])
                return 1
            if welcomed:   # it was up: start again from a short wait
                attempt = 0
            delay = backoff(attempt)
            attempt += 1
            print(f"   [reconnecting in {delay:.0f} s]")
            await self.sleep(delay)

    def hello(self) -> dict:
        frame = {"type": "hello", "protocol": protocol.PROTOCOL_VERSION,
                 "client": {"kind": "satellite", "name": self.name, "version": VERSION},
                 "caps": {"display": False, "gestures": False, "speech": "server", "audio_in": "segments",
                          "audio_out": "server"}}
        if self.pair:   # pairing: the code, not an older token (that would just come back as the old device)
            frame["pair"] = self.pair
        elif token := read_token(self.token_file):
            frame["token"] = token
        return frame

    async def session(self) -> tuple[int | None, bool]:
        """One connection: (its close code, whether it was welcomed)."""
        import websockets
        async with websockets.connect(self.url, open_timeout=HELLO_TIMEOUT_S) as ws:
            await ws.send(json.dumps(self.hello()))
            try:
                welcome = json.loads(await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT_S))
            except websockets.ConnectionClosed as e:
                return (e.rcvd.code if e.rcvd else None), False
            if welcome.get("type") != "welcome":
                return None, False
            self.welcomed(welcome)
            self.ws = ws
            frames = asyncio.create_task(self._frames(ws))
            hearing = asyncio.create_task(self._hearing(ws))
            try:
                await asyncio.wait({frames, hearing}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                self.ws = None
                for task in (frames, hearing):
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
            if hearing.done() and not hearing.cancelled() and hearing.exception():
                print(f"   [listening stopped: {hearing.exception()}]")
            return ws.close_code, True

    def welcomed(self, welcome: dict) -> None:
        if welcome.get("token"):   # just paired: keep it, the code is used up
            write_token(self.token_file, welcome["token"])
            self.pair = None
            print(f"Paired with Jarvis as {(welcome.get('device') or {}).get('name') or self.name}.")
        self.remote.ticket = welcome.get("media_ticket")
        state = welcome.get("state") or {}
        self.voice.standby = bool(state.get("standby"))
        self.questions = []   # the core sends the open ones again
        print(f"Connected to Jarvis at {self.url}. Say \"Hey Jarvis\".")

    async def send(self, frame: dict) -> None:
        if self.ws is not None:
            with contextlib.suppress(Exception):
                await self.ws.send(json.dumps(frame))

    # --- what the core sends ------------------------------------------------------------------------
    async def _frames(self, ws) -> None:
        import websockets
        with contextlib.suppress(websockets.ConnectionClosed):
            async for raw in ws:
                try:
                    frame = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(frame, dict):
                    self.handle(frame)

    def handle(self, frame: dict) -> None:
        kind = frame.get("type")
        if kind == "text":                       # the answer to this satellite's line, a sentence at a time
            self._say(frame.get("delta"))
        elif kind in ("say", "notify", "news"):  # speech between turns, task and order news
            self._say(frame.get("text"))
        elif kind == "question":
            q = frame.get("question") or {}
            if q.get("id") and all(o["id"] != q["id"] for o in self.questions):
                self.questions.append(q)   # its "May I ...?" comes as a say when this is where the user is
        elif kind == "question_closed":
            self.questions = [q for q in self.questions if q["id"] != frame.get("id")]
        elif kind == "standby":
            self.voice.standby = True     # only "Hey Jarvis" opens the mic now
        # ready, done, tool, blade, ui, console: nothing to say

    def _say(self, text) -> None:
        if isinstance(text, str) and text.strip():
            self.voice.say(text.strip())

    # --- what the user says -------------------------------------------------------------------------
    async def _hearing(self, ws) -> None:
        while True:
            text = await self.voice.hear()
            if self.voice.standby:   # heard after "Hey Jarvis": called by name wakes it, as a face does
                self.voice.standby = False
            await self.heard(text)

    async def heard(self, text: str) -> None:
        decision = parse_answer(text) if self.questions else None
        if decision is not None:
            await self.send({"type": "answer", "q": self.questions[0]["id"], "decision": decision})
            return
        self._asks += 1
        await self.send({"type": "ask", "text": text, "id": f"s{self._asks}"})

    def _barge_in(self) -> None:
        """The wake word cut Jarvis off: stop the core's turn too."""
        if self._loop is not None:
            self._loop.create_task(self.send({"type": "interrupt"}))

    def _ticket_refused(self) -> None:
        """/stt or /tts turned the ticket down (12 hours old, or revoked): say hello again for a
        new one. Called from a worker thread."""
        def close():
            if self.ws is not None:
                self._loop.create_task(self.ws.close())
        if self._loop is not None:
            self._loop.call_soon_threadsafe(close)


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="A voice satellite for Jarvis.")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"the core's socket (default {DEFAULT_URL})")
    parser.add_argument("--name", default=socket.gethostname() or "satellite",
                        help="what this satellite calls itself (`jarvisctl pair --name` wins)")
    parser.add_argument("--pair", metavar="CODE", help="the one-time code from `jarvisctl pair --kind satellite` (first run)")
    parser.add_argument("--token-file", type=Path, default=token_path(), help="where the device token is kept")
    args = parser.parse_args(argv)

    import voice
    remote = voice.RemoteSpeech(http_url(args.url))
    try:
        v = voice.Voice(stt=voice.RemoteSTT(remote), tts=voice.RemoteTTS(remote))
        async with v:
            return await Satellite(args.url, args.name, v, remote, args.token_file, args.pair).run()
    finally:
        remote.close()


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        print("\nSatellite stopped.")

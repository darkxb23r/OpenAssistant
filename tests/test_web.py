"""The browser interface (web.py): who may connect, the turn protocol the page speaks, and several
tabs at once, each a channel of the hub."""
import asyncio, base64, json, os, socket, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import guardrails as g
import speech as sp
import web as w
import websockets
from hub import Hub

# --- the checks on their own ---
assert w.host_allowed("localhost:8790") and w.host_allowed("127.0.0.1:8790") and w.host_allowed("[::1]:8790")
for host in (None, "", "evil.example:8790", "localhost.evil.example", "192.168.1.5:8790", "127.0.0.2"):
    assert not w.host_allowed(host), host
for origin in ("http://localhost:5173", "http://127.0.0.1:5199", "http://localhost:4173", "http://localhost:8790"):
    assert w.origin_allowed(origin, 8790), origin
for origin in (None, "", "null", "https://localhost:5173", "http://localhost:5200", "http://localhost",
               "http://evil.example:5173", "http://localhost:3000", "http://localhost:5173/x", "http://localhost:abc"):
    assert not w.origin_allowed(origin, 8790), origin


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def raw(port: int, path: str, headers: dict, upgrade: bool = False) -> int:
    """The status code for a hand-made request, so Host and Origin can be anything."""
    lines = [f"GET {path} HTTP/1.1"] + [f"{k}: {v}" for k, v in headers.items()]
    if upgrade:
        lines += ["Upgrade: websocket", "Connection: Upgrade", "Sec-WebSocket-Version: 13",
                  f"Sec-WebSocket-Key: {base64.b64encode(os.urandom(16)).decode()}"]
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        s.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        return int(s.recv(200).split()[1])


DEV = "http://localhost:5173"


class Tab:
    """One open page."""

    def __init__(self, ws):
        self.ws = ws

    async def frame(self):
        return json.loads(await asyncio.wait_for(self.ws.recv(), 3))

    async def frames(self, n):
        return [await self.frame() for _ in range(n)]

    async def nothing(self, wait=0.3):
        try:
            got = await asyncio.wait_for(self.ws.recv(), wait)
        except asyncio.TimeoutError:
            return True
        raise AssertionError(f"unexpected frame {got}")

    async def send(self, **frame):
        await self.ws.send(json.dumps(frame))


HELLO = {"type": "hello", "protocol": 1, "client": {"kind": "face", "name": "test"}, "caps": {"display": True}}


async def open_tab(port):
    tab = Tab(await websockets.connect(f"ws://127.0.0.1:{port}/", origin=DEV))
    await tab.send(**HELLO)
    welcome = await tab.frame()
    assert welcome["type"] == "welcome" and welcome["protocol"] == 1 and welcome["auth"] == "ok", welcome
    assert (await tab.frame())["type"] == "ready"
    return tab


async def main():
    port = free_port()
    hub = Hub()
    g._io = hub
    server = w.WebServer(hub, ["memory", "tasks"], port=port)
    interrupts, queried = [], []

    async def on_interrupt():
        interrupts.append(1)

    async def query(ch, text):
        queried.append(text)
    hub.on_interrupt = on_interrupt

    async with hub, server:
        sender = asyncio.create_task(hub.run_turns(query))
        ok_host = f"127.0.0.1:{port}"
        check = lambda *a, **k: asyncio.to_thread(raw, port, *a, **k)
        # Plain requests: a loopback Host; no Origin, or one of ours.
        assert await check("/health", {"Host": ok_host}) == 200
        assert await check("/health", {"Host": ok_host, "Origin": DEV}) == 200
        assert await check("/health", {"Host": f"localhost:{port}"}) == 200
        assert await check("/health", {"Host": f"evil.example:{port}"}) == 403        # DNS rebinding
        assert await check("/health", {"Host": ok_host, "Origin": "http://evil.example"}) == 403
        assert await check("/health", {}) in (400, 403)
        # The socket: a loopback Host and one of our Origins, both required.
        assert await check("/", {"Host": ok_host, "Origin": DEV}, upgrade=True) == 101
        assert await check("/", {"Host": ok_host, "Origin": f"http://localhost:{port}"}, upgrade=True) == 101
        # No Origin: not a page (a terminal, a satellite). The handshake goes through, and the hello
        # must then carry a token (test_protocol.py).
        assert await check("/", {"Host": ok_host}, upgrade=True) == 101
        assert await check("/", {"Host": ok_host, "Origin": "https://evil.example"}, upgrade=True) == 403
        assert await check("/", {"Host": f"evil.example:{port}", "Origin": DEV}, upgrade=True) == 403
        # Through an SSH tunnel on another laptop port: the page's origin is the address it talks to.
        assert await check("/", {"Host": "localhost:9000", "Origin": "http://localhost:9000"}, upgrade=True) == 101
        assert await check("/", {"Host": "localhost:9000", "Origin": "http://localhost:9001"}, upgrade=True) == 403
        assert await check("/", {"Host": "evil.example:9000", "Origin": "http://evil.example:9000"}, upgrade=True) == 403
        assert await check("/health", {"Host": "127.0.0.1:9000", "Origin": "http://127.0.0.1:9000"}) == 200
        await asyncio.sleep(0.3)

        # --- the protocol, as web/src/lib/bridge.ts speaks it ---
        a = await open_tab(port)

        # Speech between turns is a "say" frame, to the channel the user used last (the only tab).
        hub.say("The backup task is done.")
        assert await a.frame() == {"type": "say", "text": "The backup task is done."}

        # A question that becomes a turn: its text, tools and end carry its id.
        await a.send(type="ask", text="what's my disk space", id="a1")
        assert await hub.hear() == "what's my disk space"
        hub.submit("what's my disk space")
        await asyncio.sleep(0.05)
        assert queried == ["what's my disk space"] and hub.turn is not None
        hub.tool("mcp__time__get_current_time")
        hub.say("Plenty.")
        hub.end_turn()
        assert await a.frames(3) == [{"type": "tool", "name": "mcp__time__get_current_time", "ask": "a1"},
                                     {"type": "text", "delta": "Plenty. ", "ask": "a1"},
                                     {"type": "done", "ask": "a1"}]

        # A line Jarvis handles itself (no turn): its reply is the answer, then it's done when the
        # main loop comes back for more.
        await a.send(type="ask", text="stop asking", id="a2")
        assert await hub.hear() == "stop asking"
        hub.say("Okay.")
        hearing = asyncio.ensure_future(hub.hear())
        assert await a.frames(2) == [{"type": "text", "delta": "Okay. ", "ask": "a2"}, {"type": "done", "ask": "a2"}]

        # A permission question mid-turn: the tab gets it with its id and a card, hears it, and
        # talking over it is the answer, not a barge-in. The turn's later words go out on their own,
        # since the page has stopped listening for the old question.
        await a.send(type="ask", text="restart plex", id="a3")
        assert await asyncio.wait_for(hearing, 3) == "restart plex"
        hub.submit("restart plex")
        await asyncio.sleep(0.05)
        asked = asyncio.create_task(g.ask_user("restart Plex", "(Bash) docker restart plex", False))
        await asyncio.sleep(0.05)
        q = g._pending_questions[0]
        question, card, said = await a.frames(3)
        assert question == {"type": "question", "question": q.frame()}
        assert card["type"] == "blade" and card["blade"]["question"] == q.id and "docker restart plex" in card["blade"]["html"]
        assert said == {"type": "say", "text": "May I restart Plex? Yes, no, or always."}
        await a.send(type="interrupt")
        await a.send(type="ask", text="yes", id="a4")
        assert await hub.hear() == "yes"
        assert interrupts == []
        assert g.answer_oldest_question("yes", by=hub.trust) and await asked == "yes"
        assert await a.frame() == {"type": "question_closed", "id": q.id, "decision": "yes"}
        hearing = asyncio.ensure_future(hub.hear())
        assert await a.frame() == {"type": "done", "ask": "a4"}
        hub.say("Restarted.")
        assert await a.frame() == {"type": "say", "text": "Restarted."}
        hub.end_turn()
        assert (await a.frame()) == {"type": "done", "ask": "a3"}

        # A barge-in stops the turn; its leftovers aren't sent; the next line waits for it to end.
        await a.send(type="ask", text="tell me a long story", id="a5")
        assert await asyncio.wait_for(hearing, 3) == "tell me a long story"
        hub.submit("tell me a long story")
        await asyncio.sleep(0.05)
        await a.send(type="interrupt")
        await a.send(type="ask", text="never mind", id="a6")
        assert await hub.hear() == "never mind"
        hub.submit("never mind")
        await asyncio.sleep(0.2)
        assert interrupts == [1] and queried[-1] == "tell me a long story"   # not sent yet
        hub.say("And that is the end of the story.")   # the talked-over turn's last words
        hub.tool("mcp__time__get_current_time")
        hub.end_turn()
        assert await a.frame() == {"type": "done", "ask": "a5"}
        await asyncio.sleep(0.05)
        assert queried[-1] == "never mind"
        hub.say("Alright.")
        hub.end_turn()
        assert await a.frames(2) == [{"type": "text", "delta": "Alright. ", "ask": "a6"}, {"type": "done", "ask": "a6"}]

        # Junk is ignored rather than fatal.
        await a.send(type="nope")
        await a.ws.send("not json")
        await a.ws.send(json.dumps([1, 2]))
        await a.send(type="ask", text="   ", id="a7")
        hearing = asyncio.ensure_future(hub.hear())
        await asyncio.sleep(0.1)
        assert not hearing.done()

        # --- a second tab: no eviction, and each tab's answers stay with it ---
        b = await open_tab(port)
        await asyncio.sleep(0.05)
        assert hub.primary() is server.clients[1] and len(server.clients) == 2
        await b.send(type="ask", text="what time is it", id="b1")
        assert await asyncio.wait_for(hearing, 3) == "what time is it"
        hub.submit("what time is it")
        await asyncio.sleep(0.05)
        hub.say("Five.")
        hub.end_turn()
        assert await b.frames(2) == [{"type": "text", "delta": "Five. ", "ask": "b1"}, {"type": "done", "ask": "b1"}]
        assert await a.nothing()
        # Speech between turns goes to the tab used last; a "still here" makes a tab the one in use.
        await a.send(type="presence")
        await asyncio.sleep(0.05)
        hub.say("Your tea is ready.")
        assert await a.frame() == {"type": "say", "text": "Your tea is ready."}
        assert await b.nothing()

        # A question is shown on every tab, spoken only where the user is, and closed on all of them
        # when one tab's button answers it.
        asked = asyncio.create_task(g.ask_user("delete the old logs", "(Bash) rm -rf /srv/logs/old", False))
        await asyncio.sleep(0.05)
        q = g._pending_questions[0]
        assert [f["type"] for f in await a.frames(3)] == ["question", "blade", "say"]
        assert [f["type"] for f in await b.frames(2)] == ["question", "blade"]
        assert await b.nothing()
        await b.send(type="answer", q=q.id, decision="no")
        assert await asked == "no"
        assert await a.frame() == await b.frame() == {"type": "question_closed", "id": q.id, "decision": "no"}
        await b.send(type="answer", q=q.id, decision="yes")   # already answered: nothing happens
        assert await a.nothing() and await b.nothing(0.1)

        # A tab that opens while a question waits gets every open question. (Answering made the
        # second tab the one in use, so it's the one that says this question.)
        asked = asyncio.create_task(g.ask_user("restart the NAS", "(Bash) ssh nas sudo reboot", True))
        await asyncio.sleep(0.05)
        assert [f["type"] for f in await b.frames(3)] == ["question", "blade", "say"]
        assert [f["type"] for f in await a.frames(2)] == ["question", "blade"]
        c = Tab(await websockets.connect(f"ws://127.0.0.1:{port}/", origin=DEV))
        await c.send(**HELLO)
        frames = await c.frames(5)
        assert [f["type"] for f in frames] == ["welcome", "ready", "question", "blade", "say"]
        assert frames[0]["state"]["questions"] == [g._pending_questions[0].frame()]
        assert g.answer_oldest_question("no", by="sms") is False   # a text can't answer
        assert g.answer_oldest_question("no", by="device") and await asked == "no"
        await c.ws.close()
        await asyncio.sleep(0.1)
        assert len(server.clients) == 2

        assert (await a.frame())["type"] == (await b.frame())["type"] == "question_closed"

        # The interface tools draw on the tab in use (the second, which answered last); with no tab
        # open they say so.
        assert server.send({"type": "ui", "op": "reset", "args": {}})
        assert (await b.frame())["op"] == "reset" and await a.nothing()
        await a.ws.close(); await b.ws.close()
        await asyncio.sleep(0.2)
        assert server.send({"type": "say", "text": "x"}) is False and hub.primary() is None
        hub.say("Nobody is listening.")   # no channel: only logged
        sender.cancel()

    # Something else holding the port: a clear message, not uvicorn's exit; or, with a keyboard or
    # mic to fall back on, a warning and no interface.
    busy = socket.socket()
    busy.bind(("127.0.0.1", 0)); busy.listen()
    taken = busy.getsockname()[1]
    try:
        async with w.WebServer(Hub(), [], port=taken):
            raise AssertionError("a busy port must refuse")
    except SystemExit as e:
        assert "JARVIS_WEB_PORT" in str(e)
    async with w.WebServer(Hub(), [], port=taken, required=False) as optional:
        assert not optional.running
    busy.close()


asyncio.run(main())


# --- speech-to-text and speech for the page (/stt, /tts), from the speech service ---
class FakeWhisper:
    def transcribe(self, samples, **kw):
        assert samples.dtype.name == "float32" and kw.get("hotwords", "").startswith("Jarvis") and kw.get("beam_size") == 5
        return [type("S", (), {"text": " Hey, Jarvis, "})(), type("S", (), {"text": "what time is it?"})()], None


class FakeKokoro:
    def create(self, text, voice, lang):
        FakeKokoro.said = text
        return __import__("numpy").zeros(2400, "float32"), 24000


async def speech():
    import httpx, io, wave
    import voice
    sp.STT_AVAILABLE = sp.TTS_AVAILABLE = True
    voice.decode_audio = lambda audio, rate: __import__("numpy").ones(rate, "float32")
    port = free_port()
    async with w.WebServer(Hub(), [], port=port):   # without speech: the page uses the browser's own
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            assert (await c.get("/health")).json() == {"ok": True, "tts": False, "stt": False}
            assert (await c.post("/tts", json={"text": "hi"})).status_code == 503
            assert (await c.post("/stt", content=b"x" * 5000, headers={"content-type": "audio/webm"})).status_code == 503
    await asyncio.sleep(0.3)
    port = free_port()
    service = sp.Speech({str(home): "home"})
    service._stt, service._tts = FakeWhisper(), FakeKokoro()
    async with w.WebServer(Hub(), [], port=port, speech=service):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            post = lambda body, kind="audio/webm;codecs=opus": c.post("/stt", content=body, headers={"content-type": kind})
            assert (await c.get("/health")).json() == {"ok": True, "tts": True, "stt": True}
            # Jarvis's voice for the page: a WAV per sentence, with paths said the voice layer's way.
            r = await c.post("/tts", json={"text": f"It's in {home}/notes.txt."})
            assert r.status_code == 200 and r.headers["content-type"] == "audio/wav", r.text
            assert wave.open(io.BytesIO(r.content)).getframerate() == 24000
            assert "home, notes.txt" in FakeKokoro.said and str(home) not in FakeKokoro.said, FakeKokoro.said
            for bad in ({}, {"text": ""}, {"text": "x" * (w.MAX_TTS_CHARS + 1)}):
                assert (await c.post("/tts", json=bad)).status_code == 400, bad
            assert (await c.post("/tts", content=b"not json")).status_code == 400
            r = await post(b"x" * 5000)
            assert r.status_code == 200 and r.json() == {"text": "Hey, Jarvis, what time is it?"}, r.text
            assert (await post(b"x" * 100)).json() == {"text": ""}                     # a click, not a word
            assert (await post(b"x" * 5000, "text/html")).status_code == 415
            assert (await post(b"x" * (w.MAX_STT_BYTES + 1))).status_code == 413
            assert (await c.get("/stt")).status_code in (404, 405)                       # POST only
            cross = {"content-type": "audio/webm", "sec-fetch-site": "cross-site"}
            assert (await c.post("/stt", content=b"x" * 5000, headers=cross)).status_code == 403


asyncio.run(speech())

# The phone, as a channel of the hub: only texts and calls, and who may answer depends on how
# the user reached it (a text answers only with its code).
import phone

ch = phone.Channels(None, None, lambda *a: None)
assert ch.kind == "phone" and ch.always_ask and ch.trust == "physical" and not ch.away
ch.source = "sms"
assert ch.trust == "sms" and ch.away
print("ALL OK")

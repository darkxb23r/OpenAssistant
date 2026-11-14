"""The client protocol (protocol.py) and paired devices (devices.py), against a real interface
server: the hello and its close codes, pairing, revoking, media tickets on /file /img /stt /tts,
the scene a new screen is sent, and the [Screen: ...] note Claude gets when the screen changes."""
import asyncio, json, os, socket, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
os.environ.pop("JARVIS_CODEX", None)
sys.path.insert(0, str(ROOT))
import control
import devices as dv
import guardrails as g
import httpx
import jarvis as j
import protocol
import speech as sp
import web as w
import websockets
from hub import Hub

sp.STT_AVAILABLE = sp.TTS_AVAILABLE = False   # never load (or download) speech models here

DEV = "http://localhost:5173"
PICTURE = home / "picture.png"
PICTURE.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * 64)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def hello(**extra) -> dict:
    return {"type": "hello", "protocol": 1, "client": {"kind": "face", "name": "test"},
            "caps": {"display": True}, **extra}


class Client:
    """One connection: either welcomed (.welcome, .ws) or refused (.code)."""

    def __init__(self):
        self.ws, self.welcome, self.code = None, None, None

    async def frame(self):
        return json.loads(await asyncio.wait_for(self.ws.recv(), 3))

    async def closed(self) -> int:
        await asyncio.wait_for(self.ws.wait_closed(), 5)
        return self.ws.close_code


async def connect(port: int, first: dict, origin: str | None = DEV) -> Client:
    c = Client()
    c.ws = await websockets.connect(f"ws://127.0.0.1:{port}/", origin=origin) if origin else \
        await websockets.connect(f"ws://127.0.0.1:{port}/")
    await c.ws.send(json.dumps(first))
    try:
        got = await c.frame()
    except websockets.ConnectionClosed:
        c.code = await c.closed()
        return c
    assert got.get("type") == "welcome", got   # a client is welcomed first, or the socket is closed
    c.welcome = got
    assert (await c.frame())["type"] == "ready"
    return c


async def refused(port: int, first: dict, origin: str | None = DEV) -> int | None:
    c = await connect(port, first, origin)
    if c.code is None:
        await c.ws.close()
    return c.code


async def until(check, what: str, timeout: float = 5):
    for _ in range(int(timeout / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


def other(code: str) -> str:
    return f"{(int(code) + 1) % 10**6:06d}"


# Protocol v1 is frozen (protocol.py): its constants and the hello it reads stay exactly these.
assert (protocol.PROTOCOL_VERSION, protocol.KINDS) == (1, ("face", "terminal", "satellite"))
assert (protocol.CLOSE_REVOKED, protocol.CLOSE_VERSION, protocol.CLOSE_PAIRING) == (4001, 4002, 4003)
assert protocol.parse_hello({"type": "hello", "protocol": 1}) == {
    "kind": "face", "name": "face", "version": "", "display": True, "gestures": False, "token": None, "pair": None}
for bad in ({"protocol": 1}, {"type": "ask", "protocol": 1}, {"type": "hello"}, {"type": "hello", "protocol": 2}):
    try:
        protocol.parse_hello(bad)
        raise AssertionError(bad)
    except protocol.Refused as e:
        assert e.code == protocol.CLOSE_VERSION


async def main():
    port = free_port()
    hub = Hub()
    g._io = j._io = hub
    devices = dv.Devices(home / ".jarvis" / "devices.json")
    server = w.WebServer(hub, [], port=port, speech=sp.Speech(), devices=devices)
    token = control.new_token()
    server.control = control.Control(token)
    base = f"http://127.0.0.1:{port}"
    http = httpx.AsyncClient(base_url=base, timeout=5)
    file_url = f"/file?path={PICTURE.as_posix()}"
    audio = {"content-type": "audio/webm"}

    async with hub, server:
        # --- open mode (nothing paired) ---
        assert not devices.enforced
        # Another protocol version: 4002, whoever it is.
        assert await refused(port, hello(protocol=2)) == protocol.CLOSE_VERSION
        assert await refused(port, {**hello(), "protocol": "x"}) == protocol.CLOSE_VERSION
        # A client from before the protocol (its first frame isn't a hello): 4002 like any other
        # version, even a page in open mode, and its line never reaches Jarvis.
        assert await refused(port, {"type": "ask", "text": "hello there", "id": "v0"}) == protocol.CLOSE_VERSION
        assert await refused(port, {"type": "ask", "text": "hi"}, origin=None) == protocol.CLOSE_VERSION
        # A page saying hello: welcomed without a token or a ticket.
        page = await connect(port, hello())
        assert page.code is None and "token" not in page.welcome and "media_ticket" not in page.welcome
        # Protocol v1 is frozen: the welcome's shape stays exactly this.
        assert set(page.welcome) == {"type", "protocol", "client_id", "auth", "state"}
        assert page.welcome["protocol"] == 1 and page.welcome["auth"] == "ok"
        assert set(page.welcome["state"]) == {"standby", "questions", "screen", "assistant"}
        assert page.welcome["state"]["assistant"] == {"name": "Jarvis", "meaning": "Just A Rather Very Intelligent System"}
        await page.ws.send(json.dumps({"type": "ticket"}))   # open mode: no ticket to renew
        assert await page.frame() == {"type": "ticket"}
        await page.ws.close()
        # No Origin (not a page): a token is needed even now. Non-ASCII junk is refused just the same.
        assert await refused(port, hello(), origin=None) == protocol.CLOSE_PAIRING
        assert await refused(port, hello(token="é"), origin=None) == protocol.CLOSE_PAIRING
        assert await refused(port, hello(pair="é"), origin=None) == protocol.CLOSE_PAIRING
        # The control token: a terminal with the standing of the keyboard at the PC.
        term = await connect(port, hello(token=token, client={"kind": "terminal", "name": "ssh"}, caps={}), origin=None)
        assert term.code is None and term.welcome["auth"] == "ok"
        await until(lambda: any(c.kind == "terminal" for c in server.clients), "the terminal")
        t = next(c for c in server.clients if c.kind == "terminal")
        assert (t.trust, t.source, t.display) == ("physical", "terminal", False)
        await term.ws.close()
        # No tickets needed yet.
        assert (await http.get(file_url)).status_code == 200
        assert (await http.post("/stt", content=b"x" * 2000, headers=audio)).status_code == 503   # no model
        assert (await http.post("/tts", json={"text": "hi"})).status_code == 503

        # --- pairing ---
        code = devices.new_code("laptop")
        assert len(code) == 6 and code.isdigit()
        laptop = await connect(port, hello(pair=code))
        assert laptop.code is None, laptop.code
        device_token, ticket = laptop.welcome["token"], laptop.welcome["media_ticket"]
        assert laptop.welcome["device"]["name"] == "laptop"   # the name it was paired as
        assert set(laptop.welcome) == {"type", "protocol", "client_id", "auth", "state", "device", "token",
                                       "media_ticket"} and set(laptop.welcome["device"]) == {"id", "name"}
        device_id = laptop.welcome["device"]["id"]
        assert devices.enforced and [d["id"] for d in devices.listed()] == [device_id]
        assert device_token not in (home / ".jarvis" / "devices.json").read_text()   # only its hash is kept
        await until(lambda: any(c.device_id == device_id for c in server.clients), "the laptop")
        assert next(c for c in server.clients if c.device_id == device_id).trust == "device"
        # The code was single use.
        assert await refused(port, hello(pair=code)) == protocol.CLOSE_PAIRING
        # Now every page needs a token; a first frame that isn't a hello is still 4002.
        assert await refused(port, {"type": "ask", "text": "hi", "id": "1"}) == protocol.CLOSE_VERSION
        assert await refused(port, hello()) == protocol.CLOSE_PAIRING
        assert await refused(port, hello(token="made-up")) == protocol.CLOSE_REVOKED
        # The control token still works.
        assert await refused(port, hello(token=token), origin=None) is None

        # A code expires.
        code = devices.new_code("phone")
        devices._codes[code]["expires"] = time.time() - 1
        assert await refused(port, hello(pair=code)) == protocol.CLOSE_PAIRING
        assert await refused(port, hello(pair=code)) == protocol.CLOSE_PAIRING
        # Four wrong tries leave it usable; the fifth drops it.
        code = devices.new_code("tablet")
        for _ in range(4):
            assert await refused(port, hello(pair=other(code))) == protocol.CLOSE_PAIRING
        tablet = await connect(port, hello(pair=code))
        assert tablet.code is None and tablet.welcome["device"]["name"] == "tablet"
        await tablet.ws.close()
        code = devices.new_code("watch")
        for _ in range(5):
            assert await refused(port, hello(pair=other(code))) == protocol.CLOSE_PAIRING
        assert await refused(port, hello(pair=code)) == protocol.CLOSE_PAIRING
        # A name given at pairing wins; without one, the client's own.
        code = devices.new_code("")
        named = await connect(port, hello(pair=code, client={"kind": "satellite", "name": "kitchen"}), origin=None)
        assert named.code is None and named.welcome["device"]["name"] == "kitchen"
        await named.ws.close()

        # --- media tickets ---
        assert (await http.get(file_url)).status_code == 403
        assert (await http.get(file_url + "&mt=made-up")).status_code == 403
        assert (await http.get(file_url + f"&mt={ticket}")).status_code == 200
        assert (await http.get("/img?url=https://example.com/a.png")).status_code == 403
        assert (await http.get("/media?url=https://example.com/a.mp4")).status_code == 403
        assert (await http.post("/stt", content=b"x" * 2000, headers=audio)).status_code == 403
        assert (await http.post("/stt", content=b"x" * 2000,
                                headers={**audio, "authorization": "Bearer made-up"})).status_code == 403
        assert (await http.post("/stt", content=b"x" * 2000,
                                headers={**audio, "authorization": f"Bearer {ticket}"})).status_code == 503
        assert (await http.post("/tts", json={"text": "hi"})).status_code == 403
        assert (await http.post("/tts", json={"text": "hi"},
                                headers={"authorization": f"Bearer {ticket}"})).status_code == 503
        # A second tab of the same browser gets its own ticket, and the first tab's keeps working.
        tab = await connect(port, hello(token=device_token))
        assert tab.code is None and "token" not in tab.welcome
        newer = tab.welcome["media_ticket"]
        assert newer != ticket and tab.welcome["device"]["id"] == device_id
        for held in (ticket, newer):
            assert (await http.get(file_url + f"&mt={held}")).status_code == 200
            assert devices._tickets[held]["expires"] - time.time() > dv.TICKET_S - 60
        # A ticket frame renews without a new hello: that tab's old ticket gets 5 more minutes, the
        # other tab's is left alone, and the reply has exactly the new ticket.
        await tab.ws.send(json.dumps({"type": "ticket"}))
        renewed = await tab.frame()
        assert set(renewed) == {"type", "media_ticket"} and renewed["type"] == "ticket"
        renewed = renewed["media_ticket"]
        assert renewed not in (ticket, newer) and (await http.get(file_url + f"&mt={renewed}")).status_code == 200
        assert 0 < devices._tickets[newer]["expires"] - time.time() <= dv.TICKET_GRACE_S
        assert devices._tickets[ticket]["expires"] - time.time() > dv.TICKET_S - 60
        devices._tickets[newer]["expires"] = time.time() - 1
        assert (await http.get(file_url + f"&mt={newer}")).status_code == 403
        # Reconnecting (that tab's socket gone) gets a new ticket; the old one works 5 more minutes.
        await tab.ws.close()
        await until(lambda: sum(c.device_id == device_id for c in server.clients) == 1, "the tab to go")
        again = await connect(port, hello(token=device_token))
        newest = again.welcome["media_ticket"]
        assert (await http.get(file_url + f"&mt={renewed}")).status_code == 200
        assert 0 < devices._tickets[renewed]["expires"] - time.time() <= dv.TICKET_GRACE_S
        assert devices._tickets[ticket]["expires"] - time.time() > dv.TICKET_S - 60   # the first tab's
        devices._tickets[renewed]["expires"] = time.time() - 1
        assert (await http.get(file_url + f"&mt={renewed}")).status_code == 403
        assert (await http.get(file_url + f"&mt={newest}")).status_code == 200

        # --- revoking: the device's open sockets close at once (4001), and its token stops working ---
        assert devices.revoke(device_id)
        await server.close_device(device_id)
        assert await laptop.closed() == protocol.CLOSE_REVOKED
        assert await again.closed() == protocol.CLOSE_REVOKED
        assert await refused(port, hello(token=device_token)) == protocol.CLOSE_REVOKED
        assert (await http.get(file_url + f"&mt={newer}")).status_code == 403
        assert devices.enforced   # the tablet and the satellite are still paired

        # Forgetting every device: open mode again.
        devices.reset()
        assert not devices.enforced and (await http.get(file_url)).status_code == 200
        await until(lambda: not server.clients, "every client to go")

        # --- the scene: a screen that connects is shown what the last one was showing ---
        hub.display({"type": "ui", "op": "patch", "args": {"theme": {"hue": 200}}})
        hub.display({"type": "ui", "op": "orbit", "args": {"action": "add", "id": "o1", "label": "CPU"}})
        hub.display({"type": "blade", "blade": {"id": "b1", "title": "DISKS", "kind": "markup", "html": "<p>ok</p>"}})
        hub.display({"type": "blade", "blade": {"id": "q-q9", "question": "q9", "kind": "markup", "html": "?"}})
        first = await connect(port, hello())
        assert first.welcome["state"]["screen"] == "none"   # nobody else was showing anything
        assert [await first.frame() for _ in range(3)] == [
            {"type": "ui", "op": "patch", "args": {"theme": {"hue": 200}}},
            {"type": "ui", "op": "orbit", "args": {"action": "add", "id": "o1", "label": "CPU"}},
            {"type": "blade", "blade": {"id": "b1", "title": "DISKS", "kind": "markup", "html": "<p>ok</p>"}}]
        try:   # the question's card isn't part of the scene
            extra = await asyncio.wait_for(first.ws.recv(), 0.3)
            raise AssertionError(f"unexpected frame {extra}")
        except asyncio.TimeoutError:
            pass
        await first.ws.close()
        await until(lambda: not server.clients, "the screen to go")

        # --- [Screen: ...]: Claude is told which screen it can draw on, only when that changes ---
        j._screen_told = None
        assert "[Screen: none] " in j.prompt_for("hi")
        assert "[Screen:" not in j.prompt_for("hi again")
        code = devices.new_code("desk")
        desk = await connect(port, hello(pair=code))
        await until(lambda: hub.display_target() is not None, "the screen")
        assert "[Screen: desk] " in j.prompt_for("what's on")
        assert "[Screen:" not in j.prompt_for("and now")
        # A terminal is no screen: nothing changes.
        term = await connect(port, hello(token=token, client={"kind": "terminal", "name": "ssh"}, caps={}), origin=None)
        await until(lambda: len(server.clients) == 2, "the terminal")
        assert "[Screen:" not in j.prompt_for("still")
        await term.ws.close()
        await desk.ws.close()
        await until(lambda: hub.display_target() is None, "the screen to go")
        assert "[Screen: none] " in j.prompt_for("bye")
        assert "[Screen:" not in j.prompt_for("bye again")
    await http.aclose()


asyncio.run(main())
print("ALL OK")

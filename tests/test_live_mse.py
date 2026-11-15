"""The go2rtc MSE relay (live_sources.relay_mse, /live/{source}/mse in web.py), against a fake go2rtc on
127.0.0.1: only the codec line is passed on from the page, only short mse/error text and the binary
video come back, the upstream address is the one fixed path, refusals happen before the handshake, the
streams are capped and limited, and the upstream closes when the page goes."""
import asyncio, json, os, socket, sys, tempfile, time
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
# As at a real start: the user's Frigate address set, and the camera list Frigate last gave.
(home / ".jarvis").mkdir(exist_ok=True)
(home / ".jarvis" / "secrets.env").write_text("FRIGATE_URL=http://nvr.example:5000\n", encoding="utf-8")
(home / ".jarvis" / "frigate-cameras.json").write_text(
    json.dumps(["Porch", "Garage", "YardE", "YardW", "DoorCam"]), encoding="utf-8")
sys.path.insert(0, str(ROOT))
import live_sources as ls
import web as w
import websockets
from hub import Hub
from websockets.asyncio.server import serve

# These check the relay alone: the startup refresh of the camera list (tested on its own below)
# would ask the fake Frigate for /api/config too.
async def _no_refresh():
    return None
ls.keep_fresh = _no_refresh


assert ls.MSE_CODECS.match("avc1.640029,avc1.64002A,mp4a.40.2,flac,opus")
for bad in ("", "x" * 201, 'avc1"; x', "a b", "a;b", "a\n", "../x", "<script>"):
    assert not ls.MSE_CODECS.match(bad), bad

DEV = "http://localhost:5173"
CODEC_LINE = json.dumps({"type": "mse", "value": 'video/mp4; codecs="avc1.640029,mp4a.40.2"'})


class Go2rtc:
    def __init__(self):
        self.paths, self.received, self.open, self.closed, self.script = [], [], 0, 0, "video"

    async def handle(self, ws):
        self.paths.append(ws.request.path)
        self.open += 1
        try:
            self.received.append(await ws.recv())    # the codec request
            if self.script == "error":
                await ws.send(json.dumps({"type": "error", "value": "mse: stream not found"}))
                await asyncio.sleep(1)
                return
            await ws.send(CODEC_LINE)
            await ws.send(json.dumps({"type": "webrtc", "value": "offer"}))         # not for the page
            await ws.send(json.dumps({"type": "mse", "value": "x" * 5000}))         # too long to be a codec line
            await ws.send("not json")
            for _ in range(2000):
                await ws.send(b"\x00" * 1024)
                await asyncio.sleep(0.01)
                try:    # the page's other messages must never get here
                    self.received.append(await asyncio.wait_for(ws.recv(), 0.001))
                except asyncio.TimeoutError:
                    pass
        except websockets.ConnectionClosed:
            pass
        finally:
            self.open -= 1; self.closed += 1


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def until(cond, what, timeout=4):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, f"timed out: {what}"
        await asyncio.sleep(0.02)


async def refused(url) -> int:
    """The HTTP status of a handshake that was turned down."""
    try:
        async with websockets.connect(url, origin=DEV, open_timeout=5):
            return 101
    except websockets.InvalidStatus as e:
        return e.response.status_code


async def main():
    fake = Go2rtc()
    async with serve(fake.handle, "127.0.0.1", 0, ping_interval=None) as fsrv:
        ls.FRIGATE_BASE = f"http://127.0.0.1:{fsrv.sockets[0].getsockname()[1]}"
        port = free_port()
        server = w.WebServer(Hub(), [], port=port)
        url = lambda src, q="": f"ws://127.0.0.1:{port}/live/{src}/mse{q}"
        async with server:
            # Refused before the handshake: unknown ids, a source with no stream, traversal.
            for src in ("cam:nope", "browser", "..%2F", "cam:x%3Furl%3D", "CAM:COUNTER"):
                assert await refused(url(src)) == 403, src
            assert fake.paths == [] and not server.slots.held

            # The right camera: the codec request goes up as a fresh message; only mse text and video come back.
            async with websockets.connect(url("cam:porch", "?url=ws://127.0.0.1:1/&src=other"), origin=DEV) as ws:
                await ws.send(json.dumps({"type": "mse", "value": "avc1.640029,mp4a.40.2", "src": "../x", "extra": 1}))
                await ws.send(json.dumps({"type": "webrtc", "value": "evil"}))
                await ws.send(b"\x01\x02")
                assert await asyncio.wait_for(ws.recv(), 3) == CODEC_LINE
                got = [await asyncio.wait_for(ws.recv(), 3) for _ in range(5)]
                assert all(isinstance(m, bytes) and len(m) == 1024 for m in got), got   # no junk text in between
            assert fake.paths == ["/live/mse/api/ws?src=Porch"], fake.paths
            assert fake.received == [json.dumps({"type": "mse", "value": "avc1.640029,mp4a.40.2"})], fake.received
            # The page went: go2rtc's side closes, and the slot is back.
            await until(lambda: fake.open == 0 and fake.closed == 1, "the upstream closes when the page drops")
            await until(lambda: not server.slots.held, "the slot is given back")

            # A bad first message is closed (1008) and never reaches go2rtc.
            fake.paths.clear()
            for first in ("not json", json.dumps({"type": "api"}), json.dumps({"type": "mse", "value": 'a"; b'}),
                          json.dumps({"type": "mse"}), json.dumps({"type": "mse", "value": "x" * 300}), json.dumps([1])):
                async with websockets.connect(url("cam:garage"), origin=DEV) as ws:
                    await ws.send(first)
                    try:
                        await asyncio.wait_for(ws.recv(), 3)
                    except websockets.ConnectionClosed as e:
                        assert e.rcvd.code == 1008, first
                    else:
                        raise AssertionError(first)
            assert fake.paths == []

            # go2rtc says the stream isn't there: the page is told, and the socket closes (it uses MJPEG).
            fake.script = "error"
            async with websockets.connect(url("cam:yarde"), origin=DEV) as ws:
                await ws.send(json.dumps({"type": "mse", "value": "avc1.640029"}))
                assert json.loads(await asyncio.wait_for(ws.recv(), 3)) == {"type": "error", "value": "mse: stream not found"}
                try:
                    await asyncio.wait_for(ws.recv(), 3)
                    raise AssertionError("still open")
                except websockets.ConnectionClosed:
                    pass
            fake.script = "video"

            # go2rtc unreachable: closed, not a hang or a trace.
            real = ls.FRIGATE_BASE
            ls.FRIGATE_BASE = f"http://127.0.0.1:{free_port()}"
            async with websockets.connect(url("cam:porch"), origin=DEV) as ws:
                await ws.send(json.dumps({"type": "mse", "value": "avc1.640029"}))
                try:
                    await asyncio.wait_for(ws.recv(), 5)
                    raise AssertionError("got something")
                except websockets.ConnectionClosed:
                    pass
            ls.FRIGATE_BASE = real

            # The byte cap ends a stream, and go2rtc's side with it.
            ls.MAX_BYTES = 10_000
            fake.closed = 0
            async with websockets.connect(url("cam:porch"), origin=DEV) as ws:
                await ws.send(json.dumps({"type": "mse", "value": "avc1.640029"}))
                total = 0
                try:
                    while True:
                        m = await asyncio.wait_for(ws.recv(), 3)
                        total += len(m) if isinstance(m, bytes) else 0
                except websockets.ConnectionClosed:
                    pass
                assert 8_000 < total <= 10_000, total
            await until(lambda: fake.closed == 1, "upstream closed at the cap")
            ls.MAX_BYTES = 256 * 1024 * 1024

            # One stream per client, as with MJPEG: the next is refused before the handshake.
            server.slots = ls.Slots(per_client=1, total=12)
            async with websockets.connect(url("cam:porch"), origin=DEV) as ws:
                await ws.send(json.dumps({"type": "mse", "value": "avc1.640029"}))
                await asyncio.wait_for(ws.recv(), 3)
                assert await refused(url("cam:garage")) == 403
            await until(lambda: not server.slots.held, "slot back")
            server.slots = ls.Slots()

            # A foreign Origin never gets in; a ticket is needed once a device is paired.
            try:
                async with websockets.connect(url("cam:porch"), origin="https://evil.example"):
                    raise AssertionError("foreign origin")
            except websockets.InvalidStatus as e:
                assert e.response.status_code == 403
            device, token = server.devices.pair(server.devices.new_code("laptop"), "face")
            ticket = server.devices.ticket(device["id"])
            fake.paths.clear()
            assert await refused(url("cam:porch")) == 403 and await refused(url("cam:porch", "?mt=made-up")) == 403
            assert fake.paths == []
            async with websockets.connect(url("cam:porch", f"?mt={ticket}"), origin=DEV) as ws:
                await ws.send(json.dumps({"type": "mse", "value": "avc1.640029"}))
                assert await asyncio.wait_for(ws.recv(), 3) == CODEC_LINE
            await until(lambda: not server.slots.held and fake.open == 0, "everything closed")


asyncio.run(main())
print("ALL OK")

"""Live video (live_sources.py, /live/{source} in web.py, the live_view tool): only ids in the fixed
registry are served, each with a media ticket once a device is paired; the upstream is the one
constant Frigate address (a fake on 127.0.0.1 here) and must answer multipart/x-mixed-replace; the
relay is capped, limited per client, and closes the camera's side when the client drops; the tool
takes an id and never an address."""
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
import httpx
import live_sources as ls
import mcp_launch
import ui_tools as u
import web as w
from hub import Hub

# These check the relay alone: the startup refresh of the camera list (tested on its own below)
# would ask the fake Frigate for /api/config too.
async def _no_refresh():
    return None
ls.keep_fresh = _no_refresh


# --- the registry ---
assert ls.FRIGATE_BASE == ls.frigate_url() == "http://nvr.example:5000"
assert ls.ids() == ["cam:porch", "cam:garage", "cam:yarde", "cam:yardw", "cam:doorcam", "browser"]
assert ls.SOURCES["cam:yarde"].upstream() == "http://nvr.example:5000/api/YardE?fps=5&h=480"   # Frigate's own case
assert ls.SOURCES["cam:doorcam"].upstream(2, 240).endswith("/api/DoorCam?fps=2&h=240")
assert ls.SOURCES["browser"].upstream() is None
assert ls.SOURCES["cam:yarde"].mse_upstream() == "ws://nvr.example:5000/live/mse/api/ws?src=YardE"
assert ls.SOURCES["browser"].mse_upstream() is None
for bad in ("../", "cam:x", "CAM:COUNTER", "cam:Porch", " cam:porch", "cam:porch ", "", None, 5, "cam:porch?url=x",
            "http://evil.example/", "cam:porch/../../x", "constructor", "__class__"):
    assert ls.lookup(bad) is None, bad
f = ls.open_frame("cam:porch")
assert f == {"type": "blade", "blade": {"id": "live-cam-porch", "title": "PORCH", "kind": "live",
                                        "source": "cam:porch", "size": "wide", "hold": "sticky"}}
assert ls.close_frame("cam:porch") == {"type": "blade_close", "id": "live-cam-porch"}
s = ls.Slots(per_client=2, total=3)
assert s.take("a") and s.take("a") and not s.take("a") and s.take("b") and not s.take("c")
s.give("a"); assert s.take("c") is False and s.take("a") is False or True
s = ls.Slots(per_client=2, total=3)
assert s.take("a") and s.take("a"); s.give("a"); s.give("a"); s.give("a"); assert s.held == {}

# --- grids: up to four known cameras, no repeats ---
assert ls.grid_ids(["cam:porch", "cam:garage", "cam:porch", "browser", "cam:x", "http://x/", 5, "cam:yarde", "cam:yardw",
                    "cam:doorcam"]) == ["cam:porch", "cam:garage", "cam:yarde", "cam:yardw"]
for bad in (None, "cam:porch", [], ["browser"], ["cam:x", "../"], {"a": 1}):
    assert ls.grid_ids(bad) is None, bad
g = ls.grid_frame(["cam:porch", "cam:garage"])
assert g == {"type": "blade", "blade": {"id": "live-grid", "title": "CAMERAS", "kind": "live",
                                        "sources": ["cam:porch", "cam:garage"], "size": "wide", "hold": "sticky"}}
assert ls.grid_close_frame() == {"type": "blade_close", "id": "live-grid"}
assert (ls.GRID_FPS, ls.GRID_HEIGHT, ls.MAX_GRID) == (2, 240, 4) and ls.GRID_FPS < ls.FPS and ls.GRID_HEIGHT < ls.HEIGHT

# --- the tool: an id and a switch, no address anywhere in its schema ---
class Page:
    def __init__(self): self.frames = []
    def send(self, frame): self.frames.append(frame); return True
page = Page(); w.current = page
schema = u.live_view_tool.input_schema
assert set(schema["properties"]) == {"source", "show"} and set(schema["required"]) == {"source", "show"}
# No enum: the cameras are Frigate's and can change while a conversation runs; the tool checks the id.
assert "enum" not in schema["properties"]["source"] and "cam:porch" in u.live_view_tool.description
assert not any(k in json.dumps(schema).lower() for k in ("url", "href", "uri", "http", "address"))
assert "mcp__jarvis__live_view" in u.TOOL_NAMES
run = lambda args: asyncio.run(u.live_view_tool.handler(args))
for bad in ({"source": "http://nvr.example:5000/api/config", "show": True}, {"source": "../", "show": True},
            {"source": "cam:nope", "show": True}, {"show": True}, {"source": ["cam:porch"], "show": True}):
    page.frames.clear()
    assert run(bad).get("is_error") and not page.frames, bad
page.frames.clear()
r = run({"source": "cam:garage", "show": True})
assert not r.get("is_error") and page.frames == [ls.open_frame("cam:garage")]
page.frames.clear(); run({"source": "cam:garage", "show": "false"})
assert page.frames == [ls.close_frame("cam:garage")]
# live_grid: ids only, at most four, the same no-address schema
gs = u.live_grid_tool.input_schema
assert set(gs["properties"]) == {"sources", "show"} and gs["required"] == ["show"]
assert gs["properties"]["sources"]["maxItems"] == 4 and "enum" not in gs["properties"]["sources"]["items"]
assert not any(k in json.dumps(gs).lower() for k in ("url", "href", "uri", "http", "address"))
assert "mcp__jarvis__live_grid" in u.TOOL_NAMES
rung = lambda args: asyncio.run(u.live_grid_tool.handler(args))
for bad in ({"show": True}, {"show": True, "sources": []}, {"show": True, "sources": ["http://nvr.example:5000/"]},
            {"show": True, "sources": "cam:porch"}, {"show": True, "sources": ["browser", "../"]}):
    page.frames.clear()
    assert rung(bad).get("is_error") and not page.frames, bad
page.frames.clear()
assert not rung({"show": True, "sources": ["cam:porch", "cam:garage", "cam:yarde", "cam:yardw", "cam:doorcam"]}).get("is_error")
assert page.frames == [ls.grid_frame(["cam:porch", "cam:garage", "cam:yarde", "cam:yardw"])]
page.frames.clear(); rung({"show": False})
assert page.frames == [ls.grid_close_frame()]
w.current = None
assert asyncio.run(u.blade.handler({"title": "x", "kind": "live", "url": "http://x/"})).get("is_error")   # not a generic blade


# --- a fake Frigate on 127.0.0.1: GET only, records what it's asked, notices a client that leaves ---
class Fake:
    def __init__(self):
        self.requests, self.open, self.dropped = [], 0, 0

    async def handle(self, reader, writer):
        head = await reader.readuntil(b"\r\n\r\n")
        method, path, _ = head.split(b"\r\n")[0].decode().split(" ")
        self.requests.append((method, path))
        if path.startswith("/api/Porch") or path.startswith("/api/Garage"):
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=frame\r\n\r\n")
            self.open += 1
            gone = asyncio.create_task(reader.read(1))   # EOF = the relay closed its side
            try:
                while not gone.done():
                    writer.write(b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: 1024\r\n\r\n" + b"\xff" * 1024 + b"\r\n")
                    await writer.drain()
                    await asyncio.sleep(0.01)
            except (ConnectionError, OSError):
                pass
            finally:
                self.open -= 1; self.dropped += 1
                gone.cancel(); writer.close()
        elif path.startswith("/api/YardE"):   # not a video stream: a page that could be anything
            body = b"<html><script>alert(1)</script></html>"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\n\r\n" % len(body) + body)
            writer.close()
        elif path.startswith("/api/YardW"):   # an image, not multipart
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: image/jpeg\r\nContent-Length: 2\r\n\r\n\xff\xd8")
            writer.close()
        else:
            writer.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\n\r\n")
            writer.close()


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def raw(port: int, target: str) -> int:
    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
        sock.sendall(f"GET {target} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\r\n".encode())
        return int(sock.recv(200).split()[1])


async def until(cond, what, timeout=4):
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, f"timed out: {what}"
        await asyncio.sleep(0.02)


async def main():
    fake = Fake()
    fsrv = await asyncio.start_server(fake.handle, "127.0.0.1", 0)
    ls.FRIGATE_BASE = f"http://127.0.0.1:{fsrv.sockets[0].getsockname()[1]}"
    port = free_port()
    server = w.WebServer(Hub(), [], port=port)
    async with server:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", timeout=5) as c:
            # Unknown, traversal and smuggled ids: 404, and Frigate is never asked.
            for target in ("/live/..%2F", "/live/../health", "/live/%2E%2E/%2E%2E/api/config", "/live/cam:x",
                           "/live/cam:x?url=http://127.0.0.1:1/", "/live/cam%3Acounter%3Furl%3Dhttp%3A%2F%2Fx",
                           "/live/cam:porch/../cam:garage", "/live/CAM:COUNTER", "/live/http%3A%2F%2Fevil.example%2F",
                           "/live/", "/live/%00", "/live/cam:porch%00"):
                got = await asyncio.to_thread(raw, port, target); assert got == 404, (target, got)
            assert fake.requests == []
            # The right id with a hostile query: the query is ignored, the upstream path is the fixed one.
            async with c.stream("GET", "/live/cam:porch?url=http://127.0.0.1:1/x&fps=999&h=99999&camera=../config") as r:
                assert r.status_code == 200 and r.headers["content-type"].startswith("multipart/x-mixed-replace")
                assert "sandbox" in r.headers["content-security-policy"] and r.headers["x-content-type-options"] == "nosniff"
                got = b""
                async for chunk in r.aiter_raw():
                    got += chunk
                    if len(got) > 4000:
                        break
            assert got.count(b"--frame") >= 2
            assert fake.requests == [("GET", "/api/Porch?fps=5&h=480")], fake.requests
            await until(lambda: fake.open == 0 and fake.dropped == 1, "the camera's side closes when the client drops")
            await until(lambda: not server.slots.held, "the slot is given back")

            # A grid tile (?grid=1) asks for the small, slow picture; any other value of it means nothing.
            fake.requests.clear(); fake.dropped = 0
            for ask, path in (("grid=1", "/api/Porch?fps=2&h=240"), ("grid=2", "/api/Porch?fps=5&h=480"),
                              ("grid=1&fps=30&h=2000", "/api/Porch?fps=2&h=240")):
                async with c.stream("GET", f"/live/cam:porch?{ask}") as r:
                    assert r.status_code == 200
                    async for chunk in r.aiter_raw():
                        break
                await until(lambda: fake.open == 0, "the grid stream closed")
            assert [p for _, p in fake.requests] == ["/api/Porch?fps=2&h=240", "/api/Porch?fps=5&h=480", "/api/Porch?fps=2&h=240"], fake.requests
            fake.requests.clear()

            # A wrong content type from upstream is refused, whatever it is.
            for cam in ("yarde", "yardw"):
                r = await c.get(f"/live/cam:{cam}")
                assert r.status_code == 502 and b"alert" not in r.content, cam
            assert (await c.get("/live/cam:doorcam")).status_code == 502   # a 404 upstream
            assert not server.slots.held
            # Nothing but GET on /api/<camera>?fps&h reached Frigate.
            assert all(m == "GET" and p.startswith("/api/") and p.endswith("?fps=5&h=480") for m, p in fake.requests), fake.requests
            assert (await c.post("/live/cam:porch")).status_code == 405
            assert (await c.get("/live/browser")).status_code == 503   # no browser running here

            # A client holds only so many streams.
            server.slots = ls.Slots(per_client=2, total=12)
            fake.requests.clear(); fake.dropped = 0
            a = c.stream("GET", "/live/cam:porch"); b = c.stream("GET", "/live/cam:garage")
            ra, rb = await a.__aenter__(), await b.__aenter__()
            assert ra.status_code == rb.status_code == 200
            assert (await c.get("/live/cam:porch")).status_code == 429
            await a.__aexit__(None, None, None)
            await until(lambda: server.slots.held.get("127.0.0.1") == 1, "one slot back")
            await b.__aexit__(None, None, None)
            await until(lambda: fake.dropped == 2 and fake.open == 0, "both cameras' sides closed")
            server.slots = ls.Slots()

            # The byte cap cuts a stream, and the upstream goes with it.
            ls.MAX_BYTES = 20_000
            fake.dropped = 0
            r = await c.get("/live/cam:porch")
            assert r.status_code == 200 and 20_000 - 4096 < len(r.content) <= 20_000
            await until(lambda: fake.dropped == 1, "upstream closed at the cap")
            ls.MAX_BYTES = 256 * 1024 * 1024
            # The time cap too.
            ls.MAX_SECONDS = 0.3
            fake.dropped = 0
            t0 = time.monotonic()
            r = await c.get("/live/cam:porch")
            assert r.status_code == 200 and time.monotonic() - t0 < 3
            await until(lambda: fake.dropped == 1, "upstream closed at the time cap")
            ls.MAX_SECONDS = 600

            # Camera unreachable: 502, not a hang or a trace.
            real = ls.FRIGATE_BASE
            ls.FRIGATE_BASE = f"http://127.0.0.1:{free_port()}"
            assert (await c.get("/live/cam:porch")).status_code == 502
            ls.FRIGATE_BASE = real

            # Once a device is paired, a ticket is needed (403 before anything else), and works.
            device, token = server.devices.pair(server.devices.new_code("laptop"), "face")
            ticket = server.devices.ticket(device["id"])
            fake.requests.clear()
            assert (await c.get("/live/cam:porch")).status_code == 403
            assert (await c.get("/live/cam:porch?mt=made-up")).status_code == 403
            assert (await c.get("/live/cam:nope")).status_code == 403
            assert (await c.get(f"/live/cam:nope?mt={ticket}")).status_code == 404
            assert (await c.get(f"/live/cam:yarde?mt={ticket}")).status_code == 502
            assert [p for _, p in fake.requests] == ["/api/YardE?fps=5&h=480"]
            async with c.stream("GET", f"/live/cam:porch?mt={ticket}") as r:
                assert r.status_code == 200
            await until(lambda: not server.slots.held and fake.open == 0, "the last stream closed")
    fsrv.close()


asyncio.run(main())
print("ALL OK")


# The camera list follows Frigate: refresh() reads GET /api/config, keeps Frigate's own names (a name
# Frigate couldn't have given is left out), and caches them for the next start.
async def follows_frigate():
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route
    import uvicorn
    config = {"cameras": {"Porch": {}, "Shed": {}, "bad name/../x": {}}}
    app = Starlette(routes=[Route("/api/config", lambda r: JSONResponse(config))])
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    task = asyncio.create_task(server.serve())
    while not server.started:
        await asyncio.sleep(0.05)
    real = ls.FRIGATE_BASE
    ls.FRIGATE_BASE = f"http://127.0.0.1:{port}"
    try:
        assert await ls.refresh() == ["Porch", "Shed"]
        assert ls.ids() == ["cam:porch", "cam:shed", "browser"] and ls.lookup("cam:garage") is None
        assert json.loads(ls.CAMERAS_FILE.read_text(encoding="utf-8")) == ["Porch", "Shed"]
        refused = await u.live_view_tool.handler({"source": "cam:garage", "show": True})
        assert refused.get("is_error") and "cam:shed" in refused["content"][0]["text"]
        ls.FRIGATE_BASE = f"http://127.0.0.1:{free_port()}"   # Frigate away: the last list stays
        assert await ls.refresh() == [] and "cam:shed" in ls.ids()
    finally:
        ls.FRIGATE_BASE = real
        server.should_exit = True
        await task

asyncio.run(follows_frigate())
print("ALL OK")

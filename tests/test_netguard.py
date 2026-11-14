"""Remote media and local images for the browser interface (netguard.py, web.py's /img /media /file):
nothing on the LAN is reachable, and only real pictures and video come back."""
import asyncio, os, socket, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
(home / "ws").mkdir()
sys.path.insert(0, str(ROOT))
import httpcore, httpx, uvicorn
from starlette.applications import Starlette
from starlette.responses import RedirectResponse, Response
from starlette.routing import Route
import netguard as n
import web as w

# --- which addresses and URLs are refused before anything is sent ---
for a in ("127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "100.100.1.1", "0.0.0.0",
          "224.0.0.1", "198.18.0.1", "::1", "::", "fe80::1", "fd00::1", "::ffff:127.0.0.1", "::ffff:7f00:1",
          "2002:c0a8:0101::1", "not an ip"):
    assert n.blocked_address(a), a
for a in ("93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"):
    assert not n.blocked_address(a), a
for url in ("", "ftp://example.com/x", "file:///etc/passwd", "javascript:alert(1)", "http://", "//example.com/x",
            "http://example.com:99999/x"):
    try:
        n.vet(url); raise AssertionError(url)
    except n.Refused as e:
        assert e.status == 400, url
for url in ("http://localhost/x", "http://nas.local/x", "http://router.lan/x", "http://box.internal/x",
            "http://127.0.0.1:8790/health", "http://[::1]/x", "http://169.254.169.254/latest/meta-data",
            "http://10.0.0.1/", "http://[::ffff:127.0.0.1]/x"):
    try:
        n.vet(url); raise AssertionError(url)
    except n.Refused as e:
        assert e.status == 403, url
assert n.vet("https://images.example.com/a.jpg?x=1") == "https://images.example.com/a.jpg?x=1"

# --- a pretend internet: names resolve to public addresses, but every connection lands on a
# local test server; "lan.example" answers with a private address, like a rebinding attack ---
PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 100
VIDEO = bytes(range(256)) * 4


async def video(request):
    if (r := request.headers.get("range", "")).startswith("bytes="):
        start = int(r[6:].split("-")[0])
        return Response(VIDEO[start:], status_code=206, media_type="video/mp4",
                        headers={"content-range": f"bytes {start}-{len(VIDEO) - 1}/{len(VIDEO)}"})
    return Response(VIDEO, media_type="video/mp4")


upstream = Starlette(routes=[
    Route("/pic.png", lambda r: Response(PNG, media_type="image/png")),
    Route("/page", lambda r: Response("<script>drive jarvis</script>", media_type="text/html")),
    Route("/svg", lambda r: Response("<svg/>", media_type="image/svg+xml")),
    Route("/huge", lambda r: Response(b"x" * 100, media_type="image/png", headers={"content-length": str(10**9)})),
    Route("/gone", lambda r: Response("no", status_code=404)),
    Route("/to-pic", lambda r: RedirectResponse("/pic.png")),
    Route("/to-metadata", lambda r: RedirectResponse("http://169.254.169.254/latest/meta-data")),
    Route("/to-lan", lambda r: RedirectResponse("http://lan.example/pic.png")),
    Route("/loop", lambda r: RedirectResponse("/loop")),
    Route("/video", video),
])


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


UP = free_port()


class ToTestServer:
    def __init__(self):
        self.real = httpcore.AnyIOBackend()

    async def connect_tcp(self, host, port, **kw):
        assert host == "93.184.216.34", host   # only ever the address that was checked
        return await self.real.connect_tcp("127.0.0.1", UP, **kw)

    async def sleep(self, s):
        await self.real.sleep(s)


async def resolve(host, port):
    return {"lan.example": ["93.184.216.34", "192.168.1.1"]}.get(host, ["93.184.216.34"])

backend = n.GuardedBackend(resolve)
backend._inner = ToTestServer()
n._pool = httpcore.AsyncConnectionPool(network_backend=backend)

# --- local images ---
(home / "shots").mkdir()
(home / "shots" / "a.png").write_bytes(PNG)
(home / "shots" / "a.svg").write_text("<svg/>")
(home / ".ssh").mkdir()
(home / ".ssh" / "key.png").write_bytes(PNG)
(home / ".ssh" / "id_ed25519").write_text("secret")
try:
    (home / "shots" / "link.png").symlink_to(home / ".ssh" / "id_ed25519")
    (home / "shots" / "key-link.png").symlink_to(home / ".ssh" / "key.png")
    links = True
except OSError:   # Windows without the symlink privilege
    links = False


async def main():
    up = uvicorn.Server(uvicorn.Config(upstream, host="127.0.0.1", port=UP, log_level="warning", lifespan="off"))
    asyncio.create_task(up.serve())
    while not up.started:
        await asyncio.sleep(0.05)
    port = free_port()
    from hub import Hub
    async with w.WebServer(Hub(), [], port=port):
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            async def get(route, headers=None, **params):
                return await c.get(route, params=params, headers=headers or {})

            r = await get("/img", url="http://cdn.example/pic.png")
            assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png", r
            assert r.headers["x-content-type-options"] == "nosniff" and "sandbox" in r.headers["content-security-policy"]
            assert (await get("/img", url="http://cdn.example/to-pic")).content == PNG          # redirect, checked
            assert (await get("/img", url="http://cdn.example/page")).status_code == 415        # not an image
            assert (await get("/img", url="http://cdn.example/svg")).status_code == 415         # SVG can run script
            assert (await get("/img", url="http://cdn.example/huge")).status_code == 413
            assert (await get("/img", url="http://cdn.example/gone")).status_code == 404
            assert (await get("/img", url="http://cdn.example/to-metadata")).status_code == 403
            assert (await get("/img", url="http://lan.example/pic.png")).status_code == 403     # any private answer
            assert (await get("/img", url="http://cdn.example/to-lan")).status_code == 403
            assert (await get("/img", url="http://cdn.example/loop")).status_code == 502
            assert (await get("/img", url="http://127.0.0.1:8790/health")).status_code == 403
            assert (await get("/img", url="file:///etc/passwd")).status_code == 400
            assert (await get("/media", url="http://cdn.example/pic.png")).status_code == 415   # not video
            r = await get("/media", url="http://cdn.example/video", headers={"Range": "bytes=1000-"})
            assert r.status_code == 206 and r.content == VIDEO[1000:] and r.headers["accept-ranges"] == "bytes"
            assert r.headers["content-range"].startswith("bytes 1000-")

            # Local images: real images in the allowed folders, after symlinks; never secrets.
            r = await get("/file", path=str(home / "shots" / "a.png"))
            assert r.status_code == 200 and r.content == PNG and r.headers["content-type"] == "image/png"
            for bad in (home / "shots" / "a.svg", home / ".ssh" / "key.png", home / ".ssh" / "id_ed25519",
                        home / "shots" / "missing.png", "shots/a.png", ""):
                assert (await get("/file", path=str(bad))).status_code == 400, bad
            if links:
                assert (await get("/file", path=str(home / "shots" / "link.png"))).status_code == 400
                assert (await get("/file", path=str(home / "shots" / "key-link.png"))).status_code == 400

            # Another site's page can't use these (an <img> sends no Origin, but does say where it's from).
            cross = {"Sec-Fetch-Site": "cross-site"}
            assert (await get("/file", path=str(home / "shots" / "a.png"), headers=cross)).status_code == 403
            assert (await get("/img", url="http://cdn.example/pic.png", headers=cross)).status_code == 403
            same = {"Sec-Fetch-Site": "same-site", "Origin": "http://localhost:5173"}
            r = await get("/file", path=str(home / "shots" / "a.png"), headers=same)
            assert r.status_code == 200 and r.headers["access-control-allow-origin"] == "http://localhost:5173"
    up.should_exit = True


asyncio.run(main())

# Windows paths the page sends: /C:/x from file:///C:/x, and C:\\x.
if os.name == "nt":
    p = str(home / "shots" / "a.png")
    assert w.image_file("/" + p.replace("\\", "/")) and w.image_file(p)
print("ALL OK")

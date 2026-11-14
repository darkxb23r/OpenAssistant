"""Jarvis's browser (browser_launch.py, netguard.FilteringProxy, browser_view.py): the proxy refuses
loopback and the LAN however they're named and connects to the address it checked; Chromium starts
headless in its own profile behind the proxy, and the pinned Playwright server drives it over
DevTools; every browser tool is automatic except the one that runs code outside the browser; "show
the browser" and "hide the browser" put the live view on the screen in use, or take it down, without
a Claude turn; /browser/live streams it as MJPEG, with a ticket once a device is paired."""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import browser_launch as b
import guardrails as g
import netguard
from hub import Hub

DNS = {"example.com": ["93.184.215.14"], "evil.example": ["93.184.215.14", "192.168.1.1"],
       "rebind.example": ["127.0.0.1"], "v6.example": ["::1"]}


async def resolve(host, port):
    if host in DNS:
        return DNS[host]
    try:
        import ipaddress
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        raise OSError("no such host")


async def exchange(port, request: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(request)
    await writer.drain()
    data = await asyncio.wait_for(reader.read(4096), 5)
    writer.close()
    return data


async def proxy_checks():
    proxy = netguard.FilteringProxy(resolve=resolve)
    # Refused before any connection: loopback, the LAN, link-local, names only a LAN has, and names
    # that resolve to any of those (even when one of their addresses is public).
    for host in ("127.0.0.1", "::1", "[::1]", "192.168.1.1", "10.0.0.2", "169.254.169.254", "100.64.0.1",
                 "localhost", "nas.local", "router.lan", "evil.example", "rebind.example", "v6.example",
                 "::ffff:127.0.0.1"):
        try:
            await proxy.vetted(host, 80)
            raise AssertionError(f"{host} must be refused")
        except netguard.Refused as e:
            assert e.status == 403, (host, e.status)
    try:
        await proxy.vetted("nowhere.example", 443)
        raise AssertionError("an unknown host must fail")
    except netguard.Refused as e:
        assert e.status == 502
    # A public name connects to the address that was checked.
    assert await proxy.vetted("example.com", 443) == "93.184.215.14"

    port = await proxy.start()
    # Over the wire: CONNECT (https, wss) and plain http both get a 403 for a local target, and a
    # request that isn't an absolute http URL is refused too.
    for request in (b"CONNECT 127.0.0.1:8790 HTTP/1.1\r\nHost: 127.0.0.1:8790\r\n\r\n",
                    b"CONNECT [::1]:8790 HTTP/1.1\r\n\r\n",
                    b"GET http://127.0.0.1:8790/ HTTP/1.1\r\nHost: 127.0.0.1:8790\r\n\r\n",
                    b"GET http://evil.example/ HTTP/1.1\r\nHost: evil.example\r\n\r\n"):
        assert (await exchange(port, request)).startswith(b"HTTP/1.1 403"), request
    assert (await exchange(port, b"GET /relative HTTP/1.1\r\n\r\n")).startswith(b"HTTP/1.1 400")

    # A public host is reached, at the checked address. Here "example.com" is pointed at a local
    # server by a resolver that skips the check, only to see the forwarding work.
    async def upstream(reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError:   # the CONNECT check hangs up once it's through
            writer.close()
            return
        seen.append(head)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        await writer.drain()
        writer.close()
    seen = []
    server = await asyncio.start_server(upstream, "127.0.0.1", 0)
    up = server.sockets[0].getsockname()[1]
    open_proxy = netguard.FilteringProxy()
    async def loopback_for_the_test(host, p):
        return "127.0.0.1"
    open_proxy.vetted = loopback_for_the_test
    open_port = await open_proxy.start()
    reply = await exchange(open_port, f"GET http://example.com:{up}/a?b=1 HTTP/1.1\r\nHost: example.com\r\n"
                                      f"Proxy-Authorization: x\r\nConnection: keep-alive\r\n\r\n".encode())
    assert reply.endswith(b"ok"), reply
    sent = seen[0].decode()
    assert sent.startswith("GET /a?b=1 HTTP/1.1\r\n") and "Proxy-" not in sent and "Connection: close" in sent
    reply = await exchange(open_port, f"CONNECT example.com:{up} HTTP/1.1\r\n\r\n".encode())
    assert reply.startswith(b"HTTP/1.1 200 Connection established"), reply
    for p in (proxy, open_proxy):
        await p.close()
    server.close()


asyncio.run(proxy_checks())

# The launch: Chromium headless, own profile, behind the proxy, loopback through it too, DevTools
# on a random 127.0.0.1 port; Playwright pinned and driving that Chromium.
chrome = Path("/pw/chromium-1254/chrome-linux64/chrome")
cmd = b.chromium_command(chrome, 4242)
assert cmd[0] == str(chrome) and cmd[-1] == "about:blank"      # str(): backslashes on Windows
for flag in ("--headless=new", "--proxy-server=http://127.0.0.1:4242", "--proxy-bypass-list=<-loopback>",
             "--disable-quic", "--remote-debugging-address=127.0.0.1", "--remote-debugging-port=0",
             f"--user-data-dir={home / '.jarvis' / 'browser' / 'profile'}"):
    assert flag in cmd, flag
mcp = b.mcp_command("/usr/bin/npx", 9333, "/ws/browser")
assert mcp[:3] == ["/usr/bin/npx", "-y", "@playwright/mcp@0.0.85"]
assert mcp[mcp.index("--cdp-endpoint") + 1] == "http://127.0.0.1:9333"
assert mcp[mcp.index("--output-dir") + 1] == "/ws/browser" and "--headless" not in mcp

# The newest full Chromium Playwright installed (never its headless shell).
pw = home / "pw"
for build in ("chromium-1208/chrome-linux64/chrome", "chromium-1254/chrome-linux64/chrome",
              "chromium_headless_shell-1300/chrome-headless-shell-linux64/chrome-headless-shell",
              "chromium-1260/chrome-win64/chrome.exe"):
    (pw / build).parent.mkdir(parents=True, exist_ok=True)
    (pw / build).write_text("")
assert b.chromium(pw) == pw / "chromium-1260/chrome-win64/chrome.exe"
assert b.chromium(home / "nowhere") is None

# The DevTools port, as Chromium writes it; "is it there now?" answers at once.
import browser_view as bv
assert b.devtools_port(timeout_s=0) is None and not bv.running()
b.PORT_FILE.parent.mkdir(parents=True, exist_ok=True)
b.PORT_FILE.write_text("9333\n/devtools/browser/abc\n")
assert b.devtools_port(timeout_s=0) == 9333 and bv.running()

# Permissions: every browser tool automatic, except the one that runs code outside the browser.
import jarvis as j
for name in g.BROWSER_AUTO:
    assert f"mcp__browser__{name}" in g.AUTO_APPROVED
assert "mcp__browser__browser_run_code_unsafe" not in g.AUTO_APPROVED
assert j.build_options(False).disallowed_tools == ["mcp__browser__browser_run_code_unsafe"]
assert "browser" not in j.WORKER_SERVERS
assert "mcp__jarvis__browser_view" in g.AUTO_APPROVED   # only changes what's on screen

# What the user says.
for line in ("show the browser", "Jarvis, show me the browser please", "open your browser window",
             "make the browser visible"):
    assert j.browser_request(line) is True, line
for line in ("hide the browser", "put away the browser, thanks", "close the browser", "browse headless"):
    assert j.browser_request(line) is False, line
for line in ("open the browser and go to bbc.co.uk", "what is a browser", "show the browser history"):
    assert j.browser_request(line) is None, line


class Screen:
    """A face: it has a screen."""
    pushes, display, kind, source, trust = True, True, "face", "web", "device"
    def __init__(self): self.frames = []
    def say(self, text): pass
    def send(self, frame): self.frames.append(frame); return True


class Said:
    def __init__(self, hub): self.hub, self.said = hub, []
    def say(self, text): self.said.append(text)
    def display(self, frame): return self.hub.display(frame)


async def toggling():
    import shutil as sh
    hub = Hub()
    j._io = io = Said(hub)
    real_npx = b.npx
    b.npx = lambda: "/usr/bin/npx"
    # No screen yet: nothing to show it on, said so; the scene remembers it for the next screen.
    j.set_browser(True)
    assert "No screen" in io.said[-1] and hub.scene.blades[-1]["id"] == bv.BLADE_ID
    face = Screen()
    hub.register(face)
    hub.present(face)
    j.set_browser(True)
    assert io.said[-1] == "Here's the browser."
    blade = face.frames[-1]["blade"]
    assert blade["kind"] == "browser" and blade["hold"] == "sticky" and blade["id"] == bv.BLADE_ID
    assert [b["id"] for b in hub.scene.blades].count(bv.BLADE_ID) == 1
    j.set_browser(False)
    assert io.said[-1] == "Browser view closed." and face.frames[-1] == {"type": "blade_close", "id": bv.BLADE_ID}
    assert all(b["id"] != bv.BLADE_ID for b in hub.scene.blades)   # a new screen won't get it back
    # Without Node.js there's no browser.
    b.npx = lambda: None
    j.set_browser(True)
    assert "Node.js" in io.said[-1]
    b.npx = real_npx


asyncio.run(toggling())


async def live_route():
    import httpx, socket
    import web as w
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = w.WebServer(Hub(), [], port=port)

    async def frames():
        for jpeg in (b"\xff\xd8one", b"\xff\xd8two"):
            yield jpeg
    server.live.frames = frames
    async with server:
        async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as c:
            b.PORT_FILE.unlink()
            assert (await c.get("/browser/live")).status_code == 503   # no browser running
            b.PORT_FILE.write_text("9333\n")
            r = await c.get("/browser/live")
            assert r.status_code == 200 and r.headers["content-type"].startswith("multipart/x-mixed-replace")
            assert r.content.count(b"--frame\r\nContent-Type: image/jpeg") == 2 and b"\xff\xd8two" in r.content
            assert "sandbox" in r.headers["content-security-policy"]
            # Once a device is paired, it needs the device's media ticket like /file.
            server.devices.pair(server.devices.new_code("laptop"), "face")
            assert (await c.get("/browser/live")).status_code == 403


asyncio.run(live_route())
print("ALL OK")

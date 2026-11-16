"""
The browser interface (web/): a holographic face for Jarvis, served on this PC only.

Jarvis always runs this server; any number of browser tabs can open the interface at once. Each
open tab is its own channel of the hub (WebClient): its questions, and the answers to them, stay
with that tab, and Jarvis's other speech goes to whichever tab (or other channel) was used last.
The browser does the wake word; hearing and speaking run here when the speech service can
(speech.py, /stt and /tts), else in the browser.

The socket can drive Jarvis with everything it's allowed to do, so who may connect is checked
before anything else (see allowed()):
- it listens on 127.0.0.1 only;
- every request must be addressed to a loopback name (Host), which stops DNS rebinding: a web
  page that points its own name at 127.0.0.1 still sends its own name;
- the socket only accepts this interface's own pages (Origin): Jarvis's own port, or the Vite
  dev server on localhost:5173-5199 or 4173-4199. Any other tab in the browser is refused; a
  client with no Origin (a terminal, a satellite) must show a token in its hello (protocol.py);
- plain requests (an <img> sends no Origin) are refused when the browser says another site made
  them (Sec-Fetch-Site: cross-site), so other pages can't probe /file for which images exist.

The page loads pictures and video through Jarvis, never directly:
- /file serves image files from this PC: images only (no SVG, which can run script), after
  symlinks are resolved, inside the home folder, the free zones or the temp folder, and never
  from a secret folder (~/.jarvis, ~/.ssh, ...);
- /img and /media fetch remote images and video, through netguard.py's checks, and only hand
  back what really is an image (or video/audio), capped in size;
- /browser/live is Jarvis's own browser as an MJPEG stream (browser_view.py), with a ticket like
  /file; it only shows, nothing goes back to the browser;
- /live/{source} relays a source from live_sources.py's fixed registry (the Frigate cameras on the
  LAN, and the same browser): the page names an id, never an address, and it needs a ticket too;
  /live/{source}/mse is the same camera as H.264 over a WebSocket (go2rtc's MSE), for a MediaSource.

The protocol is the one web/src/lib/bridge.ts speaks, after the hello and welcome (protocol.py):
  browser -> Jarvis: {"type": "ask", "text", "id"}, {"type": "interrupt"},
                     {"type": "answer", "q", "decision"} (a question's button), {"type": "presence"},
                     {"type": "ticket"} (a new media ticket, before the 12 h one runs out),
                     {"type": "settings_get"}, {"type": "settings_set", "values"},
                     {"type": "settings_restart"} (the Settings panel, settings.py),
                     {"type": "browser_control", "on"} (the "YOU HAVE CONTROL" toggle, off when the
                     browser blade opens) and {"type": "browser_input", "event"} (browser_input.py: only
                     from a face trusted as "physical" or "device", with the blade open and the toggle on;
                     otherwise ignored)
  Jarvis -> browser: {"type": "ready", "servers"}, {"type": "text", "delta", "ask"},
                     {"type": "tool", "name", "ask"}, {"type": "done", "ask"},
                     {"type": "say", "text"} (speech between turns), {"type": "standby"},
                     {"type": "question", "question"}, {"type": "question_closed", "id", "decision"},
                     {"type": "blade", "blade"}, {"type": "blade_close", "id"},
                     {"type": "ui", "op", "args"} (these from ui_tools.py, questions, and the browser's
                     live view),
                     {"type": "ticket", "media_ticket"?} (the answer to a ticket frame; none in
                     open mode or without a device),
                     {"type": "settings", "fields" | "error"}, {"type": "settings_saved", "saved" | "errors"},
                     {"type": "browser_control", "on": false} (control refused or taken away),
                     {"type": "tasks", "items"} (what's on the go by name: tasks and research).
"""
import asyncio
import contextlib
import getpass
import html
import io
import json
import logging
import os
import re
import secrets
import socket
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Mount, Route, WebSocketRoute
from starlette.staticfiles import StaticFiles
from starlette.websockets import WebSocket, WebSocketDisconnect

import browser_input
import browser_view
import guardrails
import hmac
import identity
import live_sources
import netguard
import protocol
import settings
from devices import Devices
from speech import Speech

log = logging.getLogger("jarvis.web")

WORK_EVERY_S = 3   # how often the task list is checked for changes
HOST = "127.0.0.1"   # never another interface: the socket can drive Jarvis
PORT = int(os.environ.get("JARVIS_WEB_PORT") or 8790)   # 8787 is the Caveman proxy's
DIST = Path(__file__).resolve().parent / "web" / "dist"  # `npm run build` in web/
LOOPBACK_NAMES = {"localhost", "127.0.0.1", "[::1]", "::1"}

IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp",
               ".gif": "image/gif"}   # no .svg: it's a document that can run script on our origin
MAX_FILE_BYTES = 25 * 1024 * 1024
# On every picture and video served: never sniffed into something else, and if one is ever opened
# as a page, it runs in a sandbox with no script and no access to this origin.
SANDBOX = {"x-content-type-options": "nosniff", "content-security-policy": "default-src 'none'; sandbox",
           "referrer-policy": "no-referrer"}   # a ticket in the URL is never passed on
HELLO_TIMEOUT_S = 10   # a client says hello first, or the socket is closed
MAX_IMG_BYTES, IMG_TIMEOUT_S = 15 * 1024 * 1024, 10
MAX_MEDIA_BYTES, MEDIA_TIMEOUT_S = 200 * 1024 * 1024, 30
# The page records each spoken segment (WebM/Opus from MediaRecorder) and posts it to /stt.
# Browsers' own recognisers can't be relied on: Brave and Chromium have the API but no service
# behind it, and Firefox has neither. Brave and Chromium on Linux have no voices either, so the page
# fetches each sentence it speaks from /tts.
MAX_STT_BYTES = 25 * 1024 * 1024
MIN_STT_BYTES = 1200   # a segment shorter than this is a click, not a word
MAX_TTS_CHARS = 2000

def port_free(port: int, host: str = HOST) -> bool:
    """Whether the interface can listen on the port, asked the way uvicorn binds it. On Linux and
    macOS that's with SO_REUSEADDR: without it, the connections the last Jarvis closed (a tab, the
    SSH tunnel) keep the port "in use" in TIME_WAIT for a minute after it stopped, though nothing
    is listening. On Windows SO_REUSEADDR would let a second listener share a live port, so there
    it's SO_EXCLUSIVEADDRUSE (TIME_WAIT doesn't block a bind there)."""
    with socket.socket() as probe:
        if os.name == "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
            return True
        except OSError:
            return False


current = None  # the running WebServer, for ui_tools.py (its send() draws on the screen in use)


def host_allowed(host: str | None) -> bool:
    """A request addressed to this PC by a loopback name (the Host header, port included)."""
    if not host:
        return False
    name = urlsplit(f"http://{host}").hostname or ""
    return name.lower() in LOOPBACK_NAMES


def origin_allowed(origin: str | None, port: int = PORT) -> bool:
    """A page of this interface: served by Jarvis itself, or by the Vite dev server."""
    if not origin:
        return False
    try:
        url = urlsplit(origin)
        origin_port = url.port
    except ValueError:
        return False
    if url.scheme != "http" or (url.hostname or "").lower() not in LOOPBACK_NAMES or url.path not in ("", "/"):
        return False
    return origin_port == port or (origin_port is not None and (5173 <= origin_port <= 5199 or
                                                                4173 <= origin_port <= 4199))


def same_origin(origin: str | None, host: str | None) -> bool:
    """The page is talking to the address it was loaded from: our own page, even when an SSH
    tunnel puts it on another port of the laptop (`ssh -L 9000:127.0.0.1:8790`). The Host is
    already known to be a loopback name, so this can't be another site."""
    return bool(origin and host) and origin.lower().rstrip("/") == f"http://{host.lower()}"


def allowed(scope: dict, port: int = PORT) -> bool:
    """Every request needs a loopback Host, and may come without an Origin (an <img> load, a
    terminal or satellite: no browser) but never with a foreign one. A socket without an Origin
    isn't a page, so it must then prove itself with a token in its hello (WebServer.socket)."""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
    host = headers.get("host")
    if not host_allowed(host):
        return False
    origin = headers.get("origin")
    ours = origin_allowed(origin, port) or same_origin(origin, host)
    if scope["type"] == "websocket":
        return origin is None or ours
    if headers.get("sec-fetch-site") == "cross-site":
        return False
    return origin is None or ours


class Guard:
    """ASGI middleware: refuse anything allowed() turns down before it reaches a route."""

    def __init__(self, app, port: int):
        self.app, self.port = app, port

    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket") and not allowed(scope, self.port):
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            log.warning("refused a %s request (host %r, origin %r)", scope["type"], headers.get("host"),
                        headers.get("origin"))
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await PlainTextResponse("forbidden", status_code=403)(scope, receive, send)
            return
        if scope["type"] == "http" and scope.get("method") == "OPTIONS":
            # A CORS preflight from the Vite dev server (another origin): what /stt and /tts send
            # (an audio type, the ticket as a bearer token) isn't a "simple" request.
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
            origin = headers.get("origin")
            if origin and (origin_allowed(origin, self.port) or same_origin(origin, headers.get("host"))):
                await Response(status_code=204, headers={
                    "access-control-allow-origin": origin, "vary": "origin", "access-control-max-age": "600",
                    "access-control-allow-methods": "GET, POST",
                    "access-control-allow-headers": "content-type, authorization"})(scope, receive, send)
                return
        await self.app(scope, receive, send)


def cors(request: Request, response: Response) -> Response:
    """Let the Vite dev server read what we serve (it's a different origin from ours)."""
    if origin := request.headers.get("origin"):
        response.headers["access-control-allow-origin"] = origin
        response.headers["vary"] = "origin"
    return response


def file_roots() -> list[Path]:
    roots = [Path.home(), guardrails.WORKSPACE, *guardrails.FREE_ZONES, Path(tempfile.gettempdir())]
    return [r.resolve() for r in roots]


def image_file(asked: str) -> Path | None:
    """The image file /file may serve for this path, or None."""
    if re.match(r"^/[A-Za-z]:[\\/]", asked):   # /C:/x, from file:///C:/x
        asked = asked[1:]
    path = Path(guardrails.native(asked))
    if not path.is_absolute():
        return None
    try:
        real = path.resolve(strict=True)   # symlinks first: a .png can point at anything
    except (OSError, RuntimeError):
        return None
    if real.suffix.lower() not in IMAGE_TYPES or not real.is_file():
        return None
    if any(real.is_relative_to(d) for d in guardrails.SECRET_DIRS):
        return None
    if not (guardrails.CONTAINED or any(real.is_relative_to(r) for r in file_roots())):
        return None
    return real


class _QuietServer(uvicorn.Server):
    def capture_signals(self):     # leave Ctrl+C to Jarvis
        return contextlib.nullcontext()


def question_blade(summary: str, detail: str, question_id: str | None = None) -> dict:
    """The permission question on screen: what it's for, and exactly what would run. With the
    question's id, the page adds yes/no/always buttons and takes the card down once it's answered."""
    return {"id": f"q-{question_id or secrets.token_hex(4)}", "title": "PERMISSION", "kind": "markup",
            "size": "compact", "hold": "turn", **({"question": question_id} if question_id else {}),
            "html": f'<p class="hud-note">May I {html.escape(summary)}?</p><pre>{html.escape(detail)}</pre>'
                    '<p class="hud-note hud-dim">Say yes, no, or always.</p>'}


class WebServer:
    """The interface's server: the page itself, its socket, and the speech, image and video
    endpoints. Each socket becomes a WebClient channel of the hub."""

    def __init__(self, hub, servers: list[str], port: int = PORT, speech: Speech | None = None,
                 required: bool = True, devices: Devices | None = None):
        self.hub, self.servers, self.port = hub, servers, port
        self.work = lambda: []   # what's running (jarvis.current_work), for the page's task list
        self._work_seen: list[str] = []
        self.speech = speech or Speech(enabled=False)
        self.devices = devices or Devices()   # paired devices; none paired = open mode
        self.required = required      # a busy port stops Jarvis; else it carries on without the interface
        self.running = False
        self.clients: list[WebClient] = []
        self.control = None           # control.Control, set by jarvis.main: what jarvisctl talks to
        self.told_elsewhere: set = set()   # questions a satellite has said once it can't answer
        self.live = browser_view.LiveView()   # Jarvis's browser, screencast to /browser/live
        self.slots = live_sources.Slots()     # open /live streams, per client
        self._cameras: asyncio.Task | None = None   # live_sources.keep_fresh: Frigate's camera list
        self._server = None
        self.app = Guard(Starlette(routes=[
            Route("/health", self.health),
            Route("/control/{command}", self.control_command, methods=["GET", "POST"]),
            Route("/file", self.file),
            Route("/img", self.img),
            Route("/media", self.media),
            Route("/browser/live", self.browser_live),
            Route("/live/{source}", self.live_source),
            WebSocketRoute("/live/{source}/mse", self.live_mse),
            Route("/live/{rest:path}", self.live_unknown),   # anything with a slash in it: no redirect, a 404
            Route("/stt", self.stt, methods=["POST"]),
            Route("/tts", self.tts, methods=["POST"]),
            WebSocketRoute("/", self.socket),
            WebSocketRoute("/ws", self.socket),
            *([Mount("/", StaticFiles(directory=DIST, html=True))] if DIST.is_dir() else []),
        ]), port)

    # --- lifecycle ---
    async def __aenter__(self):
        global current
        if not port_free(self.port):   # uvicorn would exit the whole process on a busy port
            problem = (f"Port {self.port} on {HOST} is already in use. Stop whatever holds it, or set "
                       "JARVIS_WEB_PORT to another port (and VITE_BRIDGE_URL in web/.env.local).")
            if self.required:
                raise SystemExit(problem)
            print(f"   [interface off: {problem}]")
            return self
        self._server = _QuietServer(uvicorn.Config(self.app, host=HOST, port=self.port, log_level="warning",
                                                   lifespan="off"))
        asyncio.create_task(self._server.serve())
        self._cameras = asyncio.create_task(live_sources.keep_fresh())
        self._work_watch = asyncio.create_task(self._watch_work())
        for _ in range(100):
            if self._server.started:
                break
            await asyncio.sleep(0.05)
        if not self._server.started:
            raise SystemExit(f"The interface didn't start on {HOST}:{self.port}.")
        current, self.running = self, True
        self.speech.warm(asyncio.get_running_loop())
        if self.speech.speaks:
            print(f"   [interface voice: {self.speech.describe_voice()}]")
        if not self.speech.hears:
            print("   [interface: faster-whisper isn't installed, so the page falls back to the browser's own "
                  "speech recognition, which only Chrome and Edge have]")
        where = f"http://localhost:{self.port}" if DIST.is_dir() else "the Vite dev server (cd web && npm run dev)"
        print(f"   [interface: open {where} in {'your browser' if self.speech.hears else 'Chrome or Edge'}]")
        if DIST.is_dir():   # from another computer, through SSH; Jarvis itself stays on 127.0.0.1
            print(f"   [from another computer: ssh -N -L {self.port}:127.0.0.1:{self.port} "
                  f"{getpass.getuser()}@{socket.gethostname()}, then open http://localhost:{self.port}]")
        log.info("web interface on %s:%s", HOST, self.port)
        return self

    async def __aexit__(self, *exc):
        global current
        if current is self:
            current = None
        for client in list(self.clients):   # 1012: restarting, come back shortly
            with contextlib.suppress(Exception):
                await client.ws.close(code=1012)
        if self._server:
            self._server.should_exit = True
        if self._cameras:
            self._cameras.cancel()
        if getattr(self, "_work_watch", None):
            self._work_watch.cancel()
        self.running = False

    async def _watch_work(self) -> None:
        """The left rail's task list: sent to every page whenever it changes ({"type": "tasks", "items"}:
        names only), and to each page as it connects."""
        while True:
            try:
                items = list(self.work())
            except Exception:
                log.exception("couldn't list the current tasks")
                items = self._work_seen
            if items != self._work_seen:
                self._work_seen = items
                for client in list(self.clients):
                    if client.display:
                        with contextlib.suppress(Exception):
                            await client.write({"type": "tasks", "items": items})
            await asyncio.sleep(WORK_EVERY_S)

    def send(self, frame: dict) -> bool:
        """Draw on the screen in use (the interface tools use this). False if none is connected."""
        return self.hub.display(frame)

    # --- endpoints ---
    async def health(self, request: Request) -> Response:
        # stt: the page sends its audio here (see stt()); otherwise it uses the browser's recogniser.
        # tts: the page fetches Jarvis's voice from here (see tts()); otherwise the browser speaks.
        return cors(request, JSONResponse({"ok": True, "tts": self.speech.speaks, "stt": self.speech.hears}))

    async def control_command(self, request: Request) -> Response:
        """jarvisctl's commands. Never from a page (no Origin), and only with the control token."""
        import control
        if self.control is None or request.headers.get("origin") is not None or \
                not control.authorized(request.headers.get("authorization"), self.control.token):
            return PlainTextResponse("forbidden", status_code=403)
        try:
            args = await request.json() if request.method == "POST" else dict(request.query_params)
        except Exception:
            args = {}
        status, body = await self.control.handle(request.path_params["command"], args if isinstance(args, dict) else {})
        return JSONResponse(body, status_code=status)

    async def tts(self, request: Request) -> Response:
        if not self._ticket_ok(request, header=True):
            return cors(request, PlainTextResponse("a media ticket is needed", status_code=403))
        if not self.speech.speaks:
            return cors(request, PlainTextResponse("text-to-speech isn't available here", status_code=503))
        try:
            text = str((await request.json()).get("text") or "").strip()
        except Exception:
            return cors(request, PlainTextResponse("expected {\"text\": ...}", status_code=400))
        if not text or len(text) > MAX_TTS_CHARS:
            return cors(request, PlainTextResponse("text missing or too long", status_code=400))
        try:
            audio, kind = await asyncio.to_thread(self.speech.speak, text)
        except Exception as e:
            log.warning("couldn't speak a sentence: %s", e)
            return cors(request, PlainTextResponse("couldn't speak that", status_code=500))
        return cors(request, Response(audio, media_type=kind, headers=SANDBOX))

    async def stt(self, request: Request) -> Response:
        if not self._ticket_ok(request, header=True):
            return cors(request, PlainTextResponse("a media ticket is needed", status_code=403))
        if not self.speech.hears:
            return cors(request, PlainTextResponse("speech-to-text isn't available here", status_code=503))
        if not request.headers.get("content-type", "audio/").startswith(("audio/", "video/webm")):
            return cors(request, PlainTextResponse("audio only", status_code=415))
        audio = bytearray()
        async for chunk in request.stream():
            audio += chunk
            if len(audio) > MAX_STT_BYTES:
                return cors(request, PlainTextResponse("audio too large", status_code=413))
        if len(audio) < MIN_STT_BYTES:
            return cors(request, JSONResponse({"text": ""}))
        try:
            text = await asyncio.to_thread(self.speech.transcribe, bytes(audio))
        except Exception as e:   # undecodable audio, or the model failed: the page drops this segment
            log.warning("couldn't transcribe a segment: %s", e)
            return cors(request, PlainTextResponse("couldn't transcribe that", status_code=422))
        return cors(request, JSONResponse({"text": text}))

    def _ticket_ok(self, request: Request, header: bool = False) -> bool:
        """Once a device is paired, pictures and video need a media ticket (?mt=, since an <img>
        can't send headers) and speech needs it as a bearer token. Before that: open mode."""
        if not self.devices.enforced:
            return True
        if header:
            auth = request.headers.get("authorization", "")
            return auth.startswith("Bearer ") and self.devices.ticket_ok(auth[7:].strip())
        return self.devices.ticket_ok(request.query_params.get("mt"))

    async def file(self, request: Request) -> Response:
        if not self._ticket_ok(request):
            return cors(request, PlainTextResponse("a media ticket is needed", status_code=403))
        real = image_file(request.query_params.get("path", ""))
        if not real:
            return cors(request, PlainTextResponse("images only", status_code=400))
        if real.stat().st_size > MAX_FILE_BYTES:
            return cors(request, PlainTextResponse("too large", status_code=413))
        body = await asyncio.to_thread(real.read_bytes)
        return cors(request, Response(body, media_type=IMAGE_TYPES[real.suffix.lower()], headers=SANDBOX))

    async def browser_live(self, request: Request) -> Response:
        """Jarvis's browser as it is now: an MJPEG stream an <img> plays (browser_view.py)."""
        if not self._ticket_ok(request):
            return cors(request, PlainTextResponse("a media ticket is needed", status_code=403))
        if not browser_view.running():
            return cors(request, PlainTextResponse("the browser isn't running", status_code=503))

        async def parts():
            async for jpeg in self.live.frames():
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode()
                       + b"\r\n\r\n" + jpeg + b"\r\n")

        return cors(request, StreamingResponse(parts(), media_type="multipart/x-mixed-replace; boundary=frame",
                                               headers=SANDBOX | {"cache-control": "no-store"}))

    async def live_source(self, request: Request) -> Response:
        """A source from the registry (live_sources.py) as a multipart stream. Only an id in the
        registry is served; what it stands for is decided there, never by the request."""
        if not self._ticket_ok(request):
            return cors(request, PlainTextResponse("a media ticket is needed", status_code=403))
        src = live_sources.lookup(request.path_params.get("source"))
        if src is None:
            return cors(request, PlainTextResponse("no such source", status_code=404))
        who = request.query_params.get("mt") or (request.client.host if request.client else "?")
        if not self.slots.take(who):
            return cors(request, PlainTextResponse("too many live streams open", status_code=429))
        headers = SANDBOX | {"cache-control": "no-store"}
        if src.kind == "browser":
            if not browser_view.running():
                self.slots.give(who)
                return cors(request, PlainTextResponse("the browser isn't running", status_code=503))
            return cors(request, StreamingResponse(self._browser_parts(who), headers=headers,
                                                   media_type="multipart/x-mixed-replace; boundary=frame"))
        try:
            # ?grid=1 is the one thing the query can ask for: a small, slow picture for a grid tile.
            small = request.query_params.get("grid") == "1"
            up = await live_sources.open_upstream(src, *((live_sources.GRID_FPS, live_sources.GRID_HEIGHT) if small else ()))
        except live_sources.Refused as e:
            self.slots.give(who)
            return cors(request, PlainTextResponse(str(e), status_code=e.status))
        except BaseException:
            self.slots.give(who)
            raise

        async def body():
            try:
                async for chunk in up.chunks(asyncio.get_running_loop()):
                    yield chunk
            finally:
                self.slots.give(who)
                await up.close()   # the client went, or the caps cut it: the camera's side closes too

        return cors(request, StreamingResponse(body(), headers=headers, media_type=up.content_type))

    async def live_mse(self, ws: WebSocket) -> None:
        """A source's go2rtc stream (H.264 for MediaSource), relayed: live_sources.relay_mse. Same ticket
        as the picture; a refusal before the handshake is a 403 to the page, which then uses MJPEG."""
        src = live_sources.lookup(ws.path_params.get("source"))
        if not self._ticket_ok(ws) or src is None or not src.mse:
            await ws.close(code=4403)
            return
        who = ws.query_params.get("mt") or (ws.client.host if ws.client else "?")
        if not self.slots.take(who):
            await ws.close(code=4429)
            return
        try:
            await live_sources.relay_mse(ws, src)
        finally:
            self.slots.give(who)

    async def live_unknown(self, request: Request) -> Response:
        return cors(request, PlainTextResponse("no such source", status_code=404))

    async def _browser_parts(self, who: str):
        loop, started = asyncio.get_running_loop(), asyncio.get_running_loop().time()
        frames = self.live.frames()
        try:
            async for jpeg in frames:
                yield (b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: " + str(len(jpeg)).encode()
                       + b"\r\n\r\n" + jpeg + b"\r\n")
                if loop.time() - started > live_sources.MAX_SECONDS:
                    return
        finally:
            await frames.aclose()
            self.slots.give(who)

    async def img(self, request: Request) -> Response:
        if not self._ticket_ok(request):
            return cors(request, PlainTextResponse("a media ticket is needed", status_code=403))
        return await self._proxy(request, ("image/",), MAX_IMG_BYTES, IMG_TIMEOUT_S, ranged=False)

    async def media(self, request: Request) -> Response:
        if not self._ticket_ok(request):
            return cors(request, PlainTextResponse("a media ticket is needed", status_code=403))
        return await self._proxy(request, ("video/", "audio/"), MAX_MEDIA_BYTES, MEDIA_TIMEOUT_S, ranged=True)

    async def _proxy(self, request: Request, kinds: tuple, max_bytes: int, timeout_s: float,
                     ranged: bool) -> Response:
        """Fetch remote media for the page. Only what really is one of `kinds` comes back: anything
        else served from this origin (the one that may open the socket) could be a page that drives Jarvis."""
        headers = {"accept": "*/*" if ranged else "image/*,*/*;q=0.8"}
        if ranged and (wanted := request.headers.get("range")):
            headers["range"] = wanted   # lets a <video> seek
        try:
            upstream, url = await netguard.open_remote(request.query_params.get("url", ""), headers, timeout_s)
        except netguard.Refused as e:
            log.info("refused to fetch %s: %s", guardrails.short(request.query_params.get("url", ""), 200), e)
            return cors(request, PlainTextResponse(str(e), status_code=e.status))
        kind = netguard.header(upstream, "content-type").split(";")[0].strip().lower()
        declared = netguard.header(upstream, "content-length")
        # No SVG or other XML: a document served from this origin can run script that opens the socket.
        problem = (f"the host said {upstream.status}" if upstream.status not in (200, 206)
                   else f"not {' or '.join(kinds)}" if not kind.startswith(kinds) or "xml" in kind
                   else "too large" if declared.isdigit() and int(declared) > max_bytes else "")
        if problem:
            await upstream.aclose()
            status = 404 if upstream.status == 404 else 413 if problem == "too large" else \
                415 if problem.startswith("not ") else 502
            return cors(request, PlainTextResponse(problem, status_code=status))
        out = {**SANDBOX, "cache-control": "private, max-age=600"}
        if declared.isdigit():
            out["content-length"] = declared
        if ranged:
            # Claim seeking only when the host showed it can, or the scrub bar misbehaves.
            if upstream.status == 206 or netguard.header(upstream, "accept-ranges") == "bytes":
                out["accept-ranges"] = "bytes"
            if content_range := netguard.header(upstream, "content-range"):
                out["content-range"] = content_range

        async def body():
            sent = 0
            async for chunk in upstream.aiter_stream():
                sent += len(chunk)
                if sent > max_bytes:   # a host can lie about its length; stop reading regardless
                    log.warning("cut %s at %d bytes", url, max_bytes)
                    return
                yield chunk

        return cors(request, StreamingResponse(body(), status_code=upstream.status, media_type=kind, headers=out,
                                               background=BackgroundTask(upstream.aclose)))

    # --- the socket: one WebClient per client (a tab, a terminal, a satellite) ---
    async def _introduce(self, ws: WebSocket, client: "WebClient", first: dict) -> None:
        """The hello (protocol.py): who this is and whether it may connect. Raises protocol.Refused,
        also for a first frame that isn't a hello (a client from before the protocol: 4002)."""
        is_page = ws.headers.get("origin") is not None
        enforced = self.devices.enforced
        hello = protocol.parse_hello(first)
        device, new_token, trust = None, None, None
        if hello["token"] and self.control and hmac.compare_digest(hello["token"].encode(), self.control.token.encode()):
            trust = "physical"   # the control token: the user at this PC (jarvisctl attach)
        elif hello["token"] and (device := self.devices.check(hello["token"])):
            trust = "device"
        elif hello["pair"] and (paired := self.devices.pair(hello["pair"], hello["kind"], hello["name"])):
            (device, new_token), trust = paired, "device"
            log.info("paired %s (%s) as %s", device["name"], device["kind"], device["id"])
        elif hello["token"] and enforced:
            raise protocol.Refused(protocol.CLOSE_REVOKED, "this device isn't paired any more")
        elif is_page and not enforced:
            trust = "device"     # open mode: a page of the interface, from this PC
        else:
            raise protocol.Refused(protocol.CLOSE_PAIRING, "pairing required: run jarvisctl pair")
        client.configure(hello, trust, device)
        ticket = client.ticket = self._new_ticket(client)
        state = {"standby": self.hub.standby, "questions": [q.frame() for q in self.hub.open_questions()],
                 "screen": self.hub.scene.describe(self.hub.display_target()),
                 "assistant": identity.frame()}   # its name and what it means (optional in v1)
        await client.write(protocol.welcome(client.id, device, new_token, ticket, state))

    def _new_ticket(self, client: "WebClient") -> str | None:
        """A fresh media ticket for this client's device. Its own older one gets the 5-minute grace;
        the ones the device's other open sockets (another tab) hold are left alone."""
        if not client.device_id:
            return None
        keep = {c.ticket for c in self.clients if c is not client and c.device_id == client.device_id}
        return self.devices.ticket(client.device_id, keep)

    async def socket(self, ws: WebSocket) -> None:
        await ws.accept()
        client = WebClient(self, ws)
        try:
            first = json.loads(await asyncio.wait_for(ws.receive_text(), HELLO_TIMEOUT_S))
            first = first if isinstance(first, dict) else {}
            await self._introduce(ws, client, first)
        except protocol.Refused as e:
            log.warning("refused a client: %s", e.reason)
            with contextlib.suppress(Exception):
                await ws.close(code=e.code, reason=e.reason[:120])
            return
        except (asyncio.TimeoutError, json.JSONDecodeError, WebSocketDisconnect):
            with contextlib.suppress(Exception):
                await ws.close(code=1008)
            return
        self.clients.append(client)
        self.hub.register(client)
        log.info("%s connected (%d open)", client.kind, len(self.clients))
        await client.write({"type": "ready", "servers": self.servers})
        if self._work_seen:   # the page starts each connection with none (bridge.ts, on ready)
            await client.write({"type": "tasks", "items": self._work_seen})
        if client.display:   # what the screen was showing, so this one carries on from there
            for frame in self.hub.scene.frames():
                await client.write(frame)
        # Still waiting on answers from before: every open question, the first one spoken here if
        # this is where the user is.
        questions = self.hub.open_questions()
        for q in questions:
            client.show_question(q, speak=q is questions[0] and self.hub.primary() is client)
        try:
            while True:
                try:
                    msg = json.loads(await ws.receive_text())
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                kind = msg.get("type")
                if kind == "ask" and isinstance(msg.get("text"), str) and msg["text"].strip():
                    client.live = str(msg.get("id") or "")
                    await self.hub.heard(client, msg["text"].strip(), client.live)
                elif kind == "interrupt":
                    await self.hub.interrupt(client)
                elif kind == "answer" and isinstance(msg.get("q"), str):
                    await self.hub.answer(client, msg["q"], str(msg.get("decision") or ""))
                elif kind == "presence":
                    self.hub.present(client)
                elif kind in ("settings_get", "settings_set", "settings_restart"):
                    await self._settings(client, kind, msg)
                elif kind == "browser_control":
                    await self._browser_control(client, msg.get("on") is True)
                elif kind == "browser_input":
                    await self._browser_input(client, msg.get("event"))
                elif kind == "ticket":   # renewing the media ticket without a new hello
                    client.ticket = self._new_ticket(client)
                    await client.write({"type": "ticket", **({"media_ticket": client.ticket} if client.ticket else {})})
        except WebSocketDisconnect:
            pass
        finally:
            self.hub.unregister(client)
            if client in self.clients:
                self.clients.remove(client)
            log.info("%s disconnected (%d open)", client.kind, len(self.clients))

    def browser_blade_open(self) -> bool:
        return any(b.get("id") == browser_view.BLADE_ID for b in self.hub.scene.blades)

    async def _browser_control(self, client, on: bool) -> None:
        """The toggle: on only for a trusted screen while the browser blade is open; anything else
        leaves it off, and the page is told so."""
        on = on and browser_input.may_control(client) and self.browser_blade_open()
        client.browser_control = on
        await client.write({"type": "browser_control", "on": on})

    async def _browser_input(self, client, event) -> None:
        """A pointer or key event for Jarvis's browser (browser_input.py). Ignored, silently, unless
        everything lines up: a trusted screen, the blade open, the browser watched, the toggle on."""
        if not (client.browser_control and browser_input.may_control(client) and self.browser_blade_open()
                and self.live.watched):
            if client.browser_control and not self.browser_blade_open():
                client.browser_control = False   # the blade went away: the toggle goes with it
                await client.write({"type": "browser_control", "on": False})
            return
        if not client.input_limit.take():
            return
        calls = browser_input.translate(event, self.live.size)
        if calls:
            await self.live.input_event(calls)

    async def _settings(self, client, kind: str, msg: dict) -> None:
        """The Settings panel (settings.py): only from a face the user is at, like a hard question's
        answer. A secret's value never goes back to the page."""
        if client.kind != "face" or client.trust not in ("physical", "device"):
            await client.write({"type": "settings", "error": "Settings can only be changed from the interface."})
            return
        if kind == "settings_set":
            result = await asyncio.to_thread(settings.save, msg.get("values"))
            await client.write({"type": "settings_saved", **result})
            if "saved" in result:
                print(f"   [settings changed in the interface ({client.name}): {', '.join(result['saved'])}]")
        elif kind == "settings_restart":
            if self.control:
                await self.control.handle("restart", {})
            return
        await client.write({"type": "settings", "fields": await asyncio.to_thread(settings.listing)})

    async def close_device(self, device_id: str) -> None:
        """A revoked device's open sockets close now, not at their next reconnect."""
        for client in [c for c in self.clients if c.device_id == device_id]:
            with contextlib.suppress(Exception):
                await client.ws.close(code=protocol.CLOSE_REVOKED, reason="this device isn't paired any more")


class WebClient:
    """One open tab, as a channel of the hub.

    A question from the tab carries an id (its ref), and the tab listens for the answer under that
    id until it gets "done". Only its latest question is still listened for: asking again, or
    talking over Jarvis, abandons the one before. So speech goes out as part of that answer while
    its turn is the one running, and as a "say" frame (spoken on its own) at any other time.
    """

    source = "web"       # where the user spoke from; jarvis.main's quit words only count at "local"
    kind = "face"
    trust = "device"
    display = True       # the interface tools draw here
    takes_refs = True
    pushes = True        # lines arrive from the socket, not from a hear() the hub pulls

    _ids = __import__("itertools").count(1)

    def __init__(self, server: WebServer, ws: WebSocket):
        self.server, self.ws = server, ws
        self.id = f"c{next(self._ids)}"
        self.name, self.device_id = None, None
        self.ticket = None      # its media ticket (devices.py), from the welcome or a ticket frame
        self.live = None        # the tab's latest question: the one it still listens for
        self._received = None   # handed to Jarvis; no turn started for it yet
        self._turn = None       # the question whose turn is running
        self._abandoned = None  # the turn a barge-in stopped: its last words are not spoken
        self.browser_control = False   # the "YOU HAVE CONTROL" toggle: off until the user switches it on
        self.input_limit = browser_input.Limiter()

    def configure(self, hello: dict, trust: str, device: dict | None) -> None:
        """What the hello said this client is: a face (a screen), a terminal, a satellite."""
        self.kind, self.trust = hello["kind"], trust
        self.name = device["name"] if device else hello["name"]   # "laptop", as it was paired
        self.display = hello["display"]
        self.device_id = device["id"] if device else None
        self.source = {"face": "web", "terminal": "terminal", "satellite": "satellite"}[self.kind]

    def send(self, frame: dict) -> bool:
        asyncio.get_running_loop().create_task(self.write(frame))
        return True

    async def write(self, frame: dict) -> None:
        with contextlib.suppress(Exception):   # a socket closing under us is handled by its reader
            await self.ws.send_text(json.dumps(frame))

    # --- what the hub routes here ---
    def say(self, text: str) -> None:
        print(f"jarvis> {text}")
        if self._turn and self._turn == self._abandoned:
            return   # talked over: the tab has moved on, and saying the rest would talk over the new answer
        # The answer to the tab's question: its turn's words, or Jarvis's own reply to a line it
        # handled itself ("stop asking for 10 minutes").
        answering = self._turn or self._received
        if answering and answering == self.live:
            self.send({"type": "text", "delta": f"{text} ", "ask": answering})
        else:
            self.send({"type": "say", "text": text})

    def notify(self, text: str) -> None:
        """Task and order news: always on its own, never folded into the answer being streamed."""
        print(f"jarvis> {text}")
        self.send({"type": "say", "text": text, "news": True})

    def ask(self, summary: str, detail: str) -> None:
        print(f"\n   Jarvis wants to {summary}\n   {detail}")
        self.send({"type": "blade", "blade": question_blade(summary, detail)})
        self.send({"type": "say", "text": f"May I {summary}? Yes, no, or always."})

    def show_question(self, q, speak: bool) -> None:
        """Every tab shows each open question (with its id, for buttons); only the tab where the
        user is says it out loud. A satellite (a voice, no screen) gets only the question being asked,
        and never one it can't answer (the charter): it hears once where that one waits instead."""
        if self.kind == "satellite":
            if q is not (guardrails._pending_questions or [None])[0]:
                return
            if not guardrails.may_answer(q, self.trust, self.kind):
                if speak and q.id not in self.server.told_elsewhere:
                    self.server.told_elsewhere.add(q.id)
                    # Worded when sent: agency marks its charter question just after opening it.
                    asyncio.get_running_loop().call_soon(
                        lambda: self.send({"type": "say", "text": guardrails.waiting_elsewhere(q)}))
                return
        self.send({"type": "question", "question": q.frame()})
        if q is (guardrails._pending_questions or [None])[0]:
            self.send({"type": "blade", "blade": question_blade(q.summary, q.detail, q.id)})
            if speak:
                print(f"\n   Jarvis wants to {q.summary}\n   {q.detail}")
                self.send({"type": "say", "text": f"May I {q.summary}? Yes, no, or always."})

    def question_closed(self, q, decision: str, by: str | None) -> None:
        self.send({"type": "question_closed", "id": q.id, "decision": decision})

    def delivered(self, ref) -> None:
        self._received = ref

    def handled(self, ref) -> None:
        """The line never became a turn (a permission answer, standby, noise): it has its answer."""
        if ref is not None:
            self.send({"type": "done", "ask": ref})
        if self._received == ref:
            self._received = None

    def turn_started(self, ref) -> None:
        self._turn = ref
        if self._received == ref:
            self._received = None

    def tool(self, name: str) -> None:
        if self._turn and self._turn == self.live and self._turn != self._abandoned:
            self.send({"type": "tool", "name": name, "ask": self._turn})

    def end_of_turn(self, ref=None) -> None:
        ref = ref if ref is not None else self._turn
        if ref is not None:
            self.send({"type": "done", "ask": ref})
        if self._turn == ref:
            self._turn = None
        if self._abandoned == ref:
            self._abandoned = None

    def abandon(self, ref) -> None:
        self._abandoned = ref

    def console(self, text: str) -> None:
        """Jarvis's notices (auto-approvals, tool use) for terminals; a face has no use for them."""
        if self.kind == "terminal":
            self.send({"type": "console", "text": text})

    # Standby (jarvis.set_standby): the page decides when it's listening, so going on standby tells
    # it to drop back to waiting for its name once it has said "Standing by.", with no follow-up
    # listening. Coming off standby needs nothing: being called by name is what wakes the page.
    @property
    def standby(self) -> bool:
        return False

    @standby.setter
    def standby(self, on: bool) -> None:
        if on:
            self.send({"type": "standby"})

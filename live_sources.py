"""
Live video on the interface: a fixed registry of sources, and the relay that serves them.

The model (and the page) only ever name a source by id ("cam:porch", "browser"); the address
behind an id is built here, on the server, from the user's FRIGATE_URL setting (~/.jarvis/secrets.env,
the Settings panel: a folder Jarvis's own tools can't write), never from a tool argument or anything
else the model can reach. The cameras are the ones Frigate itself lists (GET /api/config), read at
startup and every hour (refresh(), keep_fresh()) and kept in ~/.jarvis/frigate-cameras.json, so they
are known at once on the next start; without FRIGATE_URL there are none.

web.py serves GET /live/{source}?mt= (ticket like /file): an unknown id is a 404, the stream is
relayed only if the upstream really answers multipart/x-mixed-replace, it is cut at a byte cap and
a time cap, a client may hold only a few streams at once, and the upstream connection closes when
the client goes. Only GET, only these fixed paths: nothing here writes to Frigate.

Frigate lives on the LAN, so it can't go through netguard.open_remote (which refuses private
addresses, rightly, for everything else); the relay talks to this one configured address only.
"""
import asyncio
import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

import anyio
import httpx

import mcp_launch

log = logging.getLogger("jarvis.web")

FRIGATE_URL_SHAPE = re.compile(r"^https?://[^/\s?#@]+$")   # scheme and host[:port], nothing more
CAMERA_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")          # as Frigate allows them
CAMERAS_FILE = mcp_launch.JARVIS_HOME / "frigate-cameras.json"
REFRESH_S, RETRY_S = 3600, 300


def frigate_url() -> str:
    """The user's FRIGATE_URL setting (http(s)://host[:port]), or "" when unset or not that shape."""
    value = mcp_launch.read_secrets().get("FRIGATE_URL", "").strip().rstrip("/")
    return value if FRIGATE_URL_SHAPE.match(value) else ""


FRIGATE_BASE = frigate_url()   # read once at startup; tests point it at a fake on 127.0.0.1
MJPEG_TYPE = "multipart/x-mixed-replace"
MAX_BYTES = 256 * 1024 * 1024    # per stream; the page reconnects by itself when one is cut
MAX_SECONDS = 600
CONNECT_TIMEOUT_S, READ_TIMEOUT_S = 5.0, 15.0   # a camera sends several frames a second
MAX_PER_CLIENT, MAX_TOTAL = 6, 12
FPS, HEIGHT = 5, 480             # what the relay asks Frigate for
GRID_FPS, GRID_HEIGHT, MAX_GRID = 2, 240, 4   # a grid tile (?grid=1): up to four cameras, kept small
MSE_CODECS = re.compile(r"^[A-Za-z0-9.,_-]{1,200}\Z")   # what a page may say it can play
MAX_MSE_MESSAGE = 4096           # a go2rtc text message (the codec line, an error) is short


@dataclass(frozen=True)
class Source:
    id: str
    title: str
    kind: str             # "frigate" (an MJPEG relay) or "browser" (Jarvis's own screencast)
    camera: str = ""      # Frigate's name for it (its case matters)
    mse: str = ""         # its go2rtc stream name, if it has one: H.264 over a WebSocket (MediaSource)

    def upstream(self, fps: int = FPS, height: int = HEIGHT) -> str | None:
        """The one address this source is fetched from, or None for a source with no upstream."""
        if self.kind != "frigate":
            return None
        return f"{FRIGATE_BASE}/api/{self.camera}?fps={int(fps)}&h={int(height)}"

    def mse_upstream(self) -> str | None:
        """The one go2rtc WebSocket address (Frigate proxies it under /live/mse), or None."""
        if not self.mse or not FRIGATE_BASE.startswith("http"):
            return None
        return f"ws{FRIGATE_BASE[4:]}/live/mse/api/ws?src={quote(self.mse, safe='')}"


BROWSER = Source("browser", "Browser", "browser")
SOURCES: dict[str, Source] = {"browser": BROWSER}


def set_cameras(names) -> None:
    """The Frigate cameras, by Frigate's names (their case matters). Names it couldn't have given are
    left out. Changed in place: everything holds this one SOURCES."""
    cams = [n for n in dict.fromkeys(names or []) if isinstance(n, str) and CAMERA_NAME.match(n)]
    SOURCES.clear()
    SOURCES.update({f"cam:{n.lower()}": Source(f"cam:{n.lower()}", n, "frigate", n, mse=n) for n in cams})
    SOURCES["browser"] = BROWSER


def _cached() -> list:
    try:
        data = json.loads(CAMERAS_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


set_cameras(_cached() if FRIGATE_BASE else [])


async def refresh() -> list[str]:
    """Ask Frigate which cameras it has (GET /api/config) and keep them. Its names, or [] if it
    didn't answer: the last list is kept then."""
    if not FRIGATE_BASE:
        return []
    async with httpx.AsyncClient(follow_redirects=False, trust_env=False,
                                 timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)) as client:
        try:
            response = await client.get(f"{FRIGATE_BASE}/api/config")
            cameras = response.json().get("cameras") if response.status_code == 200 else None
        except (httpx.HTTPError, ValueError, AttributeError) as e:
            log.info("couldn't ask Frigate for its cameras: %r", e)
            return []
    if not isinstance(cameras, dict):
        return []
    set_cameras(list(cameras))
    names = [s.camera for s in SOURCES.values() if s.kind == "frigate"]
    try:
        CAMERAS_FILE.write_text(json.dumps(names), encoding="utf-8")
    except OSError:
        pass
    return names


async def keep_fresh() -> None:
    """Keep the camera list in step with Frigate: now, then hourly (every few minutes while it
    doesn't answer). web.py runs it while the interface is up."""
    while FRIGATE_BASE:
        await asyncio.sleep(REFRESH_S if await refresh() else RETRY_S)


def lookup(source_id) -> Source | None:
    """An exact id only: no trimming, no case folding, nothing else is ever looked up."""
    return SOURCES.get(source_id) if isinstance(source_id, str) else None


def ids() -> list[str]:
    return list(SOURCES)


def blade_id(source_id: str) -> str:
    return "live-" + source_id.replace(":", "-")


def open_frame(source_id: str) -> dict:
    """The blade that shows a source: sticky, like the browser's."""
    src = SOURCES[source_id]
    return {"type": "blade", "blade": {"id": blade_id(source_id), "title": src.title.upper(), "kind": "live",
                                       "source": source_id, "size": "wide", "hold": "sticky"}}


def close_frame(source_id: str) -> dict:
    return {"type": "blade_close", "id": blade_id(source_id)}


GRID_ID = "live-grid"


def grid_ids(wanted) -> list[str] | None:
    """The cameras for a grid: known Frigate ids only, no repeats, the first MAX_GRID. None if none are."""
    if not isinstance(wanted, list):
        return None
    out = []
    for item in wanted:
        src = lookup(item)
        if src is not None and src.kind == "frigate" and item not in out:
            out.append(item)
    return out[:MAX_GRID] or None


def grid_frame(source_ids: list[str]) -> dict:
    return {"type": "blade", "blade": {"id": GRID_ID, "title": "CAMERAS", "kind": "live", "sources": source_ids,
                                       "size": "wide", "hold": "sticky"}}


def grid_close_frame() -> dict:
    return {"type": "blade_close", "id": GRID_ID}


class Slots:
    """How many streams each client (its ticket, or its address in open mode) has open, and in all."""

    def __init__(self, per_client: int = MAX_PER_CLIENT, total: int = MAX_TOTAL):
        self.per_client, self.total, self.held = per_client, total, {}

    def take(self, who: str) -> bool:
        if self.held.get(who, 0) >= self.per_client or sum(self.held.values()) >= self.total:
            return False
        self.held[who] = self.held.get(who, 0) + 1
        return True

    def give(self, who: str) -> None:
        if self.held.get(who, 0) <= 1:
            self.held.pop(who, None)
        else:
            self.held[who] -= 1


class Upstream:
    """An open multipart answer from Frigate. close() is safe to call twice."""

    def __init__(self, client: httpx.AsyncClient, response: httpx.Response):
        self.client, self.response, self.closed = client, response, False

    @property
    def content_type(self) -> str:
        return self.response.headers.get("content-type", "")

    async def close(self) -> None:
        if not self.closed:
            self.closed = True
            with anyio.CancelScope(shield=True):   # runs when the client's task was cancelled: finish anyway
                try:
                    await self.response.aclose()
                finally:
                    await self.client.aclose()

    async def chunks(self, loop, max_bytes: int | None = None, max_seconds: float | None = None):
        """The body, until the caps, the end of the stream or the client going; then closes."""
        max_bytes, max_seconds = max_bytes or MAX_BYTES, max_seconds or MAX_SECONDS   # read now, so tests can lower them
        sent, started = 0, loop.time()
        try:
            async for chunk in self.response.aiter_raw():
                sent += len(chunk)
                if sent > max_bytes or loop.time() - started > max_seconds:
                    log.info("cut a live stream at %d bytes", sent)
                    return
                yield chunk
        finally:
            await self.close()


class Refused(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def open_upstream(src: Source, fps: int = FPS, height: int = HEIGHT) -> Upstream:
    """GET the source's one fixed address; the answer must be a multipart stream, or it's refused."""
    url = src.upstream(fps, height)
    if url is None:
        raise Refused(404, "no such source")
    client = httpx.AsyncClient(follow_redirects=False, trust_env=False, headers={"accept-encoding": "identity"},
                               timeout=httpx.Timeout(READ_TIMEOUT_S, connect=CONNECT_TIMEOUT_S))
    try:
        response = await client.send(client.build_request("GET", url), stream=True)
    except httpx.HTTPError as e:
        await client.aclose()
        log.info("live source %s unreachable: %r", src.id, e)
        raise Refused(502, "the camera isn't answering") from None
    up = Upstream(client, response)
    kind = up.content_type.split(";")[0].strip().lower()
    if response.status_code != 200 or kind != MJPEG_TYPE:
        await up.close()
        raise Refused(502, "the camera didn't send a video stream")
    return up


async def relay_mse(ws, src: Source) -> None:
    """Relay go2rtc's MSE WebSocket for `src` to the page's socket `ws` (a Starlette WebSocket, not yet
    accepted). The page says once which codecs it can play ({"type": "mse", "value": "avc1...,mp4a..."},
    checked); that is the only thing of the page's that is passed on, as a fresh message. Text from go2rtc
    is passed on only if it is a short {"type": "mse" | "error"} object; the video itself is binary.
    Capped in bytes and time, idle upstream is dropped, and the upstream closes when the page goes."""
    from websockets.asyncio.client import connect
    url = src.mse_upstream()
    if url is None:
        await ws.close(code=4404)
        return
    await ws.accept()
    try:
        first = json.loads(await asyncio.wait_for(ws.receive_text(), 10))
    except Exception:
        await ws.close(code=1008)
        return
    codecs = first.get("value") if isinstance(first, dict) and first.get("type") == "mse" else None
    if not isinstance(codecs, str) or not MSE_CODECS.match(codecs):
        await ws.close(code=1008)
        return
    loop = asyncio.get_running_loop()

    async def from_upstream(up) -> None:
        sent, started = 0, loop.time()
        while True:
            message = await asyncio.wait_for(up.recv(), READ_TIMEOUT_S)
            if isinstance(message, str):
                try:
                    note = json.loads(message) if len(message) <= MAX_MSE_MESSAGE else None
                except ValueError:
                    note = None
                if not isinstance(note, dict) or note.get("type") not in ("mse", "error"):
                    continue
                await ws.send_text(message)
                if note["type"] == "error":
                    return
            else:
                sent += len(message)
                if sent > MAX_BYTES or loop.time() - started > MAX_SECONDS:
                    log.info("cut a live MSE stream at %d bytes", sent)
                    return
                await ws.send_bytes(message)

    async def from_page() -> None:
        while True:   # anything more the page sends is read and dropped; this ends when it goes
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                return

    try:
        async with connect(url, max_size=1 << 22, open_timeout=CONNECT_TIMEOUT_S, proxy=None,
                           ping_interval=None, user_agent_header=None) as up:
            await up.send(json.dumps({"type": "mse", "value": codecs}))
            tasks = [asyncio.create_task(from_upstream(up)), asyncio.create_task(from_page())]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as e:   # unreachable, refused, timed out, or the page went mid-send
        log.info("live MSE %s: %r", src.id, e)
    try:
        await ws.close()
    except Exception:
        pass

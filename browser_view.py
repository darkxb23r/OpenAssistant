"""
The live view of Jarvis's browser, on the interface.

browser_launch.py runs Chromium with DevTools on a 127.0.0.1 port (in its profile's
DevToolsActivePort). While someone is watching, LiveView attaches to every tab and asks Chromium to
screencast it (Page.startScreencast: a JPEG each time the page repaints); the tab that changed last
is the one shown, which is the one Playwright is working in. web.py serves the frames at
/browser/live as an MJPEG stream (multipart/x-mixed-replace), which an <img> plays natively; the
face shows it in a blade ("show the browser"). Nobody watching: no screencast, nothing to encode.

The view is read-only unless the user takes control: input_event() (browser_input.py's events, which
web.py lets through only from a trusted screen with the blade open and the toggle on) goes to the
tab that was last shown, through the same DevTools socket. Without that, nothing from the interface
is sent to the browser.
"""
import asyncio
import base64
import json
import logging
import urllib.request

import browser_launch

log = logging.getLogger("jarvis.web")

BLADE_ID = "browser-live"
FPS = 8
KEEPALIVE_S = 10   # a still page sends no frames: the last one again, so the stream isn't dropped


def open_frame() -> dict:
    """The blade that shows the live view: sticky, so a new turn doesn't take it down."""
    return {"type": "blade", "blade": {"id": BLADE_ID, "title": "BROWSER", "kind": "browser", "size": "wide",
                                       "hold": "sticky"}}


def close_frame() -> dict:
    return {"type": "blade_close", "id": BLADE_ID}


def toggle(show: bool, display) -> str:
    """Put the live view on the screen in use, or take it down (`display` is hub.display); what to
    say about it."""
    if not browser_launch.npx():
        return "The browser isn't set up: it needs Node.js 20 or newer."
    shown = display(open_frame() if show else close_frame())
    if show and not shown:
        return "No screen is connected; open the interface and I'll show it there."
    return "Here's the browser." if show else "Browser view closed."


def running() -> bool:
    """Whether Jarvis's browser is up (it has written its DevTools port)."""
    return browser_launch.devtools_port(timeout_s=0) is not None


class LiveView:
    def __init__(self):
        self.frame: bytes | None = None
        self._seq = 0
        self._new = asyncio.Condition()
        self._viewers = 0
        self._task: asyncio.Task | None = None
        self.size: tuple[float, float] | None = None   # the shown tab's viewport, in CSS pixels
        self._send = None                               # sends a DevTools command while one is connected
        self._session: str | None = None                # the tab that was shown last

    async def frames(self):
        """JPEG frames for one viewer, at most FPS a second, until it goes away."""
        self._viewers += 1
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._watch())
        try:
            seen = -1
            while True:
                async with self._new:
                    try:
                        await asyncio.wait_for(self._new.wait_for(lambda: self._seq != seen), KEEPALIVE_S)
                    except asyncio.TimeoutError:
                        pass
                    seen = self._seq
                if self.frame:
                    yield self.frame
                await asyncio.sleep(1 / FPS)
        finally:
            self._viewers -= 1
            if not self._viewers and self._task:
                self._task.cancel()

    @property
    def watched(self) -> bool:
        return self._viewers > 0

    async def input_event(self, calls: list) -> bool:
        """Send translated input [(method, params)] to the tab on show. False if there's none now."""
        send, session = self._send, self._session
        if not (send and session and calls):
            return False
        try:
            for method, params in calls:
                await send(method, params, session=session)
        except Exception as e:   # the socket went away under us: the screencast reconnects by itself
            log.info("browser input: %r", e)
            return False
        return True

    async def _show(self, jpeg: bytes) -> None:
        async with self._new:
            self.frame = jpeg
            self._seq += 1
            self._new.notify_all()

    async def _watch(self) -> None:
        """Keep a screencast going while anyone watches; reconnect if the browser restarts."""
        while self._viewers:
            try:
                await self._screencast()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.info("browser live view: %r", e)
            await asyncio.sleep(2)

    async def _screencast(self) -> None:
        from websockets.asyncio.client import connect

        port = browser_launch.devtools_port(timeout_s=0)
        if port is None:
            raise ConnectionError("the browser isn't running")

        def version():
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=3) as r:
                return json.load(r)["webSocketDebuggerUrl"]

        url = await asyncio.to_thread(version)
        async with connect(url, max_size=None, ping_interval=None) as ws:
            ids = iter(range(1, 1 << 30))

            async def send(method, params=None, session=None):
                msg = {"id": next(ids), "method": method, "params": params or {}}
                if session:
                    msg["sessionId"] = session
                await ws.send(json.dumps(msg))

            await send("Target.setDiscoverTargets", {"discover": True})
            self._send = send
            try:
                await self._pump(ws, send)
            finally:
                self._send, self._session, self.size = None, None, None

    async def _pump(self, ws, send) -> None:
        async for raw in ws:
            msg = json.loads(raw)
            method, params = msg.get("method"), msg.get("params") or {}
            if method == "Target.targetCreated" and params.get("targetInfo", {}).get("type") == "page":
                await send("Target.attachToTarget", {"targetId": params["targetInfo"]["targetId"], "flatten": True})
            elif method == "Target.attachedToTarget" and params.get("targetInfo", {}).get("type") == "page":
                await send("Page.startScreencast", {"format": "jpeg", "quality": 60, "maxWidth": 1280,
                                                    "maxHeight": 800, "everyNthFrame": 1},
                           session=params["sessionId"])
            elif method == "Page.screencastFrame":
                await send("Page.screencastFrameAck", {"sessionId": params["sessionId"]},
                           session=msg.get("sessionId"))
                self._session = msg.get("sessionId")
                meta = params.get("metadata") or {}
                w, h = meta.get("deviceWidth"), meta.get("deviceHeight")
                if all(isinstance(n, (int, float)) and not isinstance(n, bool) and n > 0 for n in (w, h)):
                    self.size = (float(w), float(h))
                await self._show(base64.b64decode(params["data"]))

"""Taking control of Jarvis's browser from the interface (browser_input.py, web.py): events are
translated to a few fixed CDP Input calls, scaled to the page; they get through only from a face
trusted as "physical" or "device", while the browser blade is open, the browser is watched and that
client's toggle is on (off by default); anyone else's input is ignored."""
import asyncio, json, os, socket, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import browser_input as bi
import browser_view
import web as w
import websockets
from hub import Hub

# --- translate: scaled to the page, fixed shapes, junk dropped ---
SIZE = (1000, 500)
(m, p), = bi.translate({"t": "mouse", "a": "down", "x": 0.5, "y": 0.2, "b": "left", "c": 2}, SIZE)
assert m == "Input.dispatchMouseEvent" and p == {"type": "mousePressed", "x": 500.0, "y": 100.0, "modifiers": 0,
                                                 "button": "left", "buttons": 1, "clickCount": 2}, p
(m, p), = bi.translate({"t": "mouse", "a": "move", "x": 1, "y": 0}, SIZE)
assert p["type"] == "mouseMoved" and p["x"] == 1000.0 and p["button"] == "none" and "clickCount" not in p
(m, p), = bi.translate({"t": "mouse", "a": "up", "x": 0, "y": 1, "b": "right"}, SIZE)
assert p["type"] == "mouseReleased" and p["buttons"] == 0 and p["y"] == 500.0 and p["clickCount"] == 1
(m, p), = bi.translate({"t": "mouse", "a": "wheel", "x": 0.1, "y": 0.1, "dy": 99999, "dx": -3}, SIZE)
assert p["type"] == "mouseWheel" and p["deltaY"] == bi.MAX_WHEEL and p["deltaX"] == -3.0
(m, p), = bi.translate({"t": "mouse", "a": "move", "x": 7, "y": -2}, SIZE)   # off the picture: held to its edge
assert (p["x"], p["y"]) == (1000.0, 0.0)
(m, p), = bi.translate({"t": "key", "a": "down", "key": "a", "code": "KeyA"}, SIZE)
assert m == "Input.dispatchKeyEvent" and p["type"] == "keyDown" and p["text"] == "a" and p["windowsVirtualKeyCode"] == 65
(m, p), = bi.translate({"t": "key", "a": "down", "key": "a", "m": 2}, SIZE)    # Ctrl+A is a shortcut, not a letter
assert p["type"] == "rawKeyDown" and "text" not in p
(m, p), = bi.translate({"t": "key", "a": "down", "key": "Enter"}, SIZE)
assert p["type"] == "keyDown" and p["text"] == "\r" and p["windowsVirtualKeyCode"] == 13
(m, p), = bi.translate({"t": "key", "a": "up", "key": "ArrowLeft"}, SIZE)
assert p["type"] == "keyUp" and p["windowsVirtualKeyCode"] == 37
assert bi.translate({"t": "text", "text": "hello\nworld"}, SIZE) == [("Input.insertText", {"text": "hello\nworld"})]
bad = [None, [], "x", {}, {"t": "nope"}, {"t": "mouse"}, {"t": "mouse", "a": "down", "x": "0.5", "y": 0.5},
       {"t": "mouse", "a": "down", "x": float("nan"), "y": 0}, {"t": "mouse", "a": "down", "x": float("inf"), "y": 0},
       {"t": "mouse", "a": "down", "x": True, "y": 0}, {"t": "mouse", "a": "drag", "x": 0, "y": 0},
       {"t": "mouse", "a": "wheel", "x": 0, "y": 0, "dy": "9"},
       {"t": "key", "a": "down", "key": "F12"}, {"t": "key", "a": "down", "key": "ab"}, {"t": "key", "a": "press", "key": "a"},
       {"t": "key", "a": "down", "key": "\x00"}, {"t": "key", "a": "down", "key": 5}, {"t": "key", "a": "down"},
       {"t": "text", "text": ""}, {"t": "text", "text": "x" * 501}, {"t": "text", "text": "a\x1bb"}, {"t": "text", "text": 5},
       {"t": "Input.dispatchMouseEvent", "type": "mousePressed", "x": 1, "y": 1}]
for event in bad:
    assert bi.translate(event, SIZE) == [], event
assert bi.translate({"t": "mouse", "a": "move", "x": 0, "y": 0}, None) == []            # page size unknown
assert bi.translate({"t": "mouse", "a": "move", "x": 0, "y": 0}, (0, 500)) == []
(m, p), = bi.translate({"t": "key", "a": "down", "key": "a", "code": "../../x", "m": 99, "extra": 1}, SIZE)
assert "code" not in p and p["modifiers"] == 0 and "extra" not in p                    # nothing else is passed on
(m, p), = bi.translate({"t": "key", "a": "down", "key": "a", "code": "KeyA\n"}, SIZE)
assert "code" not in p                                                                  # no trailing newline slips by
now = [0.0]
lim = bi.Limiter(3, clock=lambda: now[0])
assert [lim.take() for _ in range(5)] == [True, True, True, False, False]
now[0] = 2.0
assert lim.take()


class Seat:
    def __init__(self, **kw): self.__dict__.update(kw)
assert bi.may_control(Seat(kind="face", display=True, trust="device")) and bi.may_control(Seat(kind="face", display=True, trust="physical"))
for seat in (Seat(kind="face", display=True, trust="call"), Seat(kind="face", display=True, trust="sms"),
             Seat(kind="face", display=True, trust=None), Seat(kind="face", display=True),
             Seat(kind="terminal", display=True, trust="physical"), Seat(kind="satellite", display=False, trust="device"),
             Seat(kind="face", display=False, trust="device")):
    assert not bi.may_control(seat), seat.__dict__

# --- over the socket ---
DEV = "http://localhost:5173"
HELLO = {"type": "hello", "protocol": 1, "client": {"kind": "face", "name": "test"}, "caps": {"display": True}}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Tab:
    def __init__(self, ws): self.ws = ws
    async def frame(self): return json.loads(await asyncio.wait_for(self.ws.recv(), 3))
    async def send(self, **frame): await self.ws.send(json.dumps(frame))
    async def settle(self):   # the server handles frames in order: a ticket answer means the ones before it are done
        await self.send(type="ticket")
        while (f := await self.frame())["type"] != "ticket":
            pass


async def ctl(tab):   # the next browser_control frame (blade frames from the hub come between)
    while (f := await tab.frame())["type"] != "browser_control":
        pass
    return f


async def open_tab(port, hello=HELLO, origin=DEV):
    tab = Tab(await websockets.connect(f"ws://127.0.0.1:{port}/", origin=origin))
    await tab.send(**hello)
    assert (await tab.frame())["type"] == "welcome"
    while (f := await tab.frame())["type"] != "ready":
        pass
    return tab


class Cdp:
    def __init__(self): self.sent = []
    async def __call__(self, method, params=None, session=None): self.sent.append((method, params, session))


CLICK = {"t": "mouse", "a": "down", "x": 0.25, "y": 0.5, "b": "left"}
KEY = {"t": "key", "a": "down", "key": "x"}


async def main():
    port = free_port()
    hub = Hub()
    server = w.WebServer(hub, [], port=port)
    cdp = Cdp()
    async with hub, server:
        live = server.live
        live.size, live._send, live._session, live._viewers = (800.0, 600.0), cdp, "S1", 1   # a browser being watched
        tab = await open_tab(port)
        me = server.clients[0]
        assert me.trust == "device" and me.browser_control is False        # off by default

        # Toggle off (the default): input goes nowhere, blade open or not.
        hub.display(browser_view.open_frame())
        await tab.send(type="browser_input", event=CLICK); await tab.settle()
        assert cdp.sent == []
        # Control can't be taken with no browser blade open.
        hub.display(browser_view.close_frame())
        await tab.send(type="browser_control", on=True)
        assert await ctl(tab) == {"type": "browser_control", "on": False} and not me.browser_control
        # Only a real True counts.
        hub.display(browser_view.open_frame())
        for junk in ("true", 1, "yes", None):
            await tab.send(type="browser_control", on=junk)
            assert await ctl(tab) == {"type": "browser_control", "on": False}
        # Blade open, toggle on: the click lands, scaled to the page, on the tab shown last.
        await tab.send(type="browser_control", on=True)
        assert await ctl(tab) == {"type": "browser_control", "on": True} and me.browser_control
        await tab.send(type="browser_input", event=CLICK); await tab.settle()
        assert cdp.sent == [("Input.dispatchMouseEvent", {"type": "mousePressed", "x": 200.0, "y": 300.0, "modifiers": 0,
                                                          "button": "left", "buttons": 1, "clickCount": 1}, "S1")], cdp.sent
        # Malformed, or raw CDP smuggled in: nothing is sent.
        cdp.sent.clear()
        for event in ({"t": "mouse", "a": "down", "x": "a", "y": 0}, "Input.dispatchKeyEvent", None,
                      {"t": "Page.navigate", "url": "http://127.0.0.1:1/"}, {"method": "Page.navigate", "params": {}}):
            await tab.send(type="browser_input", event=event)
        await tab.send(type="browser_input"); await tab.settle()
        assert cdp.sent == []
        # Not being watched (no screencast): nowhere to send it.
        live._viewers = 0
        await tab.send(type="browser_input", event=KEY); await tab.settle()
        assert cdp.sent == []
        live._viewers = 1
        # The blade goes: the toggle goes with it, and the page is told.
        hub.display(browser_view.close_frame())
        await tab.send(type="browser_input", event=KEY)
        assert await ctl(tab) == {"type": "browser_control", "on": False} and not me.browser_control
        await tab.settle()
        assert cdp.sent == []
        hub.display(browser_view.open_frame())
        await tab.send(type="browser_input", event=KEY); await tab.settle()
        assert cdp.sent == []                                              # open again, still off until asked

        # Another tab hasn't got control because this one has.
        other = await open_tab(port)
        await other.send(type="browser_input", event=CLICK); await other.settle()
        assert cdp.sent == []
        await other.ws.close()

        # Untrusted: a screen with another trust level can't switch it on, and its input is ignored even
        # if the flag were set some other way.
        for trust in ("call", "sms", None, "stranger"):
            me.trust = trust
            await tab.send(type="browser_control", on=True)
            assert await ctl(tab) == {"type": "browser_control", "on": False}
            me.browser_control = True
            await tab.send(type="browser_input", event=CLICK); await tab.settle()
            assert cdp.sent == [], trust
            me.browser_control = False
        # Not a screen: a terminal, whatever its trust, and a face with no display.
        me.trust = "physical"
        for kind, display in (("terminal", False), ("satellite", False), ("face", False)):
            me.kind, me.display = kind, display
            await tab.send(type="browser_control", on=True)
            assert await ctl(tab) == {"type": "browser_control", "on": False}, kind
            me.browser_control = True
            await tab.send(type="browser_input", event=CLICK); await tab.settle()
            assert cdp.sent == [], kind
            me.browser_control = False
        # The user at the PC (physical) is fine.
        me.kind, me.display = "face", True
        await tab.send(type="browser_control", on=True)
        assert await ctl(tab) == {"type": "browser_control", "on": True}
        await tab.send(type="browser_input", event=KEY); await tab.settle()
        assert [c[0] for c in cdp.sent] == ["Input.dispatchKeyEvent"]
        # Too many events a second: the extra ones are dropped.
        cdp.sent.clear()
        me.input_limit = bi.Limiter(5)
        for _ in range(20):
            await tab.send(type="browser_input", event=CLICK)
        await tab.settle()
        assert len(cdp.sent) == 5

        # A client with no token and no Origin (not a page) never gets as far as sending input.
        try:
            await open_tab(port, origin=None)
            raise AssertionError("a token-less client was let in")
        except AssertionError:
            raise
        except Exception:
            pass
        assert len(server.clients) == 1
        await tab.ws.close()

        # Once devices are paired, only a paired (token) client is let in at all, and it gets control.
        device, token = server.devices.pair(server.devices.new_code("laptop"), "face")
        cdp.sent.clear()
        try:
            await open_tab(port)   # an unpaired page: refused
            raise AssertionError("an unpaired page was let in")
        except AssertionError:
            raise
        except Exception:
            pass
        paired = await open_tab(port, hello={**HELLO, "token": token})
        await paired.send(type="browser_control", on=True)
        assert await ctl(paired) == {"type": "browser_control", "on": True}
        await paired.send(type="browser_input", event=CLICK); await paired.settle()
        assert len(cdp.sent) == 1
        await paired.ws.close()


asyncio.run(main())
print("ALL OK")

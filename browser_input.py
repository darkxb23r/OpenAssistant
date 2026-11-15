"""
Pointer and key input from the interface to Jarvis's browser, over the interface's own socket.

The page sends {"type": "browser_control", "on": true} when the user switches the visible "YOU HAVE
CONTROL" toggle on (it is off whenever the blade is opened), then {"type": "browser_input", ...}
events. web.py acts on them only from a face with trust "physical" or "device", only while the browser
blade is open and the browser is being watched, and only while that client's toggle is on. What gets
through is translated here into a few CDP Input.* calls of fixed shape: nothing the page sends is
passed on as it is, unknown fields and kinds are dropped, and strings are length-limited.

Events (all numbers finite; x and y are 0..1 across the picture shown, which is scaled here to the
page's own size, so the page's layout, zoom and letterboxing never matter on the server):
  {"t": "mouse", "a": "move" | "down" | "up" | "wheel", "x", "y", "b": "left" | "middle" | "right",
   "c": clicks 1..3, "dx", "dy" (wheel), "m": modifiers}
  {"t": "key", "a": "down" | "up", "key": "a" | "Enter" | ..., "code": "KeyA", "m": modifiers}
  {"t": "text", "text": "pasted or composed text"}
The browser keeps its netguard.FilteringProxy: input can type into pages, never open a way around it.
"""
import math
import re
import time

TRUSTED = ("physical", "device")   # who is at this PC or a paired screen (see web.py's hello)
MAX_PER_SECOND = 200               # a pointer moves a lot, a hand doesn't type this fast
MAX_TEXT = 500
MAX_WHEEL = 2000
BUTTONS = {"left": 1, "middle": 4, "right": 2}   # CDP's `buttons` bitmask
MOUSE_TYPES = {"move": "mouseMoved", "down": "mousePressed", "up": "mouseReleased", "wheel": "mouseWheel"}
# Named keys and their Windows virtual-key codes, which pages' key handlers read.
NAMED_KEYS = {"Enter": 13, "Backspace": 8, "Tab": 9, "Escape": 27, "Delete": 46, "ArrowLeft": 37, "ArrowUp": 38,
              "ArrowRight": 39, "ArrowDown": 40, "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34,
              "Shift": 16, "Control": 17, "Alt": 18, "Meta": 91}
CODE = re.compile(r"^[A-Za-z0-9]{1,20}\Z")
CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _num(value, lo: float, hi: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return min(max(float(value), lo), hi)


def may_control(client) -> bool:
    """A screen the user is at: a face that can display, trusted as the PC or a paired device."""
    return getattr(client, "kind", None) == "face" and bool(getattr(client, "display", False)) \
        and getattr(client, "trust", None) in TRUSTED


class Limiter:
    """At most MAX_PER_SECOND events a second per client; the rest are dropped."""

    def __init__(self, per_second: int = MAX_PER_SECOND, clock=time.monotonic):
        self.per_second, self.clock, self.window, self.count = per_second, clock, 0.0, 0

    def take(self) -> bool:
        now = self.clock()
        if now - self.window >= 1:
            self.window, self.count = now, 0
        self.count += 1
        return self.count <= self.per_second


def translate(event, size) -> list[tuple[str, dict]]:
    """One page event as CDP calls [(method, params)], scaled to `size` = the page's (width, height)
    in CSS pixels; [] for anything that isn't a well-formed event of the kinds above."""
    if not isinstance(event, dict) or not size or not all(isinstance(n, (int, float)) and n > 0 for n in size):
        return []
    kind = event.get("t")
    mods = event.get("m", 0)
    mods = int(mods) if isinstance(mods, int) and not isinstance(mods, bool) and 0 <= mods <= 15 else 0
    if kind == "mouse":
        mouse_type = MOUSE_TYPES.get(event.get("a"))
        x, y = _num(event.get("x"), 0, 1), _num(event.get("y"), 0, 1)
        if mouse_type is None or x is None or y is None:
            return []
        params = {"type": mouse_type, "x": round(x * size[0], 1), "y": round(y * size[1], 1), "modifiers": mods}
        button = event.get("b") if event.get("b") in BUTTONS else "none"
        if mouse_type == "mouseWheel":
            dx, dy = _num(event.get("dx", 0), -MAX_WHEEL, MAX_WHEEL), _num(event.get("dy", 0), -MAX_WHEEL, MAX_WHEEL)
            if dx is None or dy is None:
                return []
            params |= {"deltaX": dx, "deltaY": dy}
        else:
            params["button"] = button if mouse_type != "mouseMoved" else "none"
            params["buttons"] = BUTTONS.get(button, 0) if mouse_type != "mouseReleased" else 0
            if mouse_type != "mouseMoved":
                clicks = event.get("c", 1)
                params["clickCount"] = clicks if isinstance(clicks, int) and not isinstance(clicks, bool) and 1 <= clicks <= 3 else 1
        return [("Input.dispatchMouseEvent", params)]
    if kind == "key":
        key, action = event.get("key"), event.get("a")
        if action not in ("down", "up") or not isinstance(key, str):
            return []
        named = key in NAMED_KEYS
        if not named and len(key) != 1:   # one character, or a name from the list
            return []
        if not named and CONTROL_CHARS.search(key):
            return []
        code = event.get("code")
        params = {"key": key, "modifiers": mods}
        if isinstance(code, str) and CODE.match(code):
            params["code"] = code
        if named:
            params["windowsVirtualKeyCode"] = NAMED_KEYS[key]
        elif key.isascii() and key.isalnum():
            params["windowsVirtualKeyCode"] = ord(key.upper())
        typed = not named and not mods & 0b0111   # no alt, ctrl or meta (CDP: 1 alt, 2 ctrl, 4 meta, 8 shift)
        if action == "down":
            params["type"] = "keyDown" if (typed or key == "Enter") else "rawKeyDown"
            if typed:
                params["text"] = key
            elif key == "Enter":
                params["text"] = "\r"
        else:
            params["type"] = "keyUp"
        return [("Input.dispatchKeyEvent", params)]
    if kind == "text":
        text = event.get("text")
        if not isinstance(text, str) or not text or len(text) > MAX_TEXT or CONTROL_CHARS.search(text):
            return []
        return [("Input.insertText", {"text": text})]
    return []

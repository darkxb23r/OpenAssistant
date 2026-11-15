"""The interface tools (ui_tools.py): loose input in, one well-formed frame out, or a refusal."""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import ui_tools as u
import web


class Page:
    def __init__(self):
        self.frames = []

    def send(self, frame):
        self.frames.append(frame)
        return True


def run(t, args):
    page.frames.clear()
    result = asyncio.run(t.handler(args))
    return result, page.frames[0] if page.frames else None


page = Page()
web.current = page
error = lambda r: r.get("is_error") is True

# Theme: colours, "auto" hands back to the phase, unknown phases and empty input do nothing.
r, f = run(u.ui_theme, {"accent": "crimson", "background": "auto", "phase_colors": {"thinking": "#f00", "bogus": "x"}})
assert f == {"type": "ui", "op": "patch", "args": {"accent": "crimson", "background": None,
                                                  "palette": {"thinking": "#f00"}}}, f
r, f = run(u.ui_theme, {})
assert f is None and "No change" in r["content"][0]["text"]

# Reactor: quoted numbers are read, out-of-range ones clamped, junk ignored.
r, f = run(u.ui_reactor, {"scale": "9", "intensity": -1, "spin": "fast", "style": "wire", "visible": "no"})
assert f["args"] == {"reactor": {"scale": 3.0, "intensity": 0.0, "style": "wire", "visible": False}}, f
r, f = run(u.ui_reactor, {"style": "cube"})
assert f is None

# Orbits: local files only; ids kept; angles wrapped; defaults filled in.
r, f = run(u.ui_orbit, {"src": "https://example.com/x.png"})
assert error(r) and f is None
r, f = run(u.ui_orbit, {"src": f"http://localhost:{web.PORT}/file?path=/x.png", "id": "shot", "phase": 400,
                        "speed": "-50", "tilt": 0})
a = f["args"]
assert a["action"] == "add" and a["id"] == "shot" and a["phase"] == 40 and a["speed"] == -30 and a["tilt"] == 0
assert (a["radius"], a["size"], a["opacity"]) == (0.55, 96, 0.9)
r, f = run(u.ui_orbit, {"src": "C:/renders/suit.png"})
assert f["args"]["id"].startswith("o") and 0 <= f["args"]["phase"] < 360
r, f = run(u.ui_orbit, {"action": "remove"})
assert error(r) and f is None
r, f = run(u.ui_orbit, {"action": "remove", "id": "shot"})
assert f["args"] == {"action": "remove", "id": "shot"}
r, f = run(u.ui_orbit, {"action": "clear"})
assert f["args"] == {"action": "clear"}

# Chrome: snake case in, the store's camel case out.
r, f = run(u.ui_chrome, {"tool_badge": "hide", "transcript": 1})
assert f["args"] == {"chrome": {"toolBadge": False, "transcript": True}}, f

# Effects and clearing: unknown values fall back to the safe default.
assert run(u.ui_effect, {"kind": "explode"})[1]["args"] == {"kind": "pulse"}
assert run(u.ui_screen, {})[1]["args"] == {"what": "all"}
assert run(u.ui_reset, {})[1] == {"type": "ui", "op": "reset", "args": {}}

# display: an empty body is refused; a picture with no text is fine; it lands as a markup blade.
r, f = run(u.display, {"title": "X", "html": "<div> </div>"})
assert error(r) and f is None
r, f = run(u.display, {"title": "Shot", "html": '<img class="hud-img" src="/x.png">', "slot": "wide", "hold": "sticky"})
b = f["blade"]
assert f["type"] == "blade" and b["kind"] == "markup" and b["size"] == "wide" and b["hold"] == "sticky"

# blade: each kind needs what it shows; kinds without a back end here are refused.
assert error(run(u.blade, {"title": "x", "kind": "gallery"})[0])
assert error(run(u.blade, {"title": "x", "kind": "image"})[0])
assert error(run(u.blade, {"title": "x", "kind": "article", "url": "https://example.com"})[0])
assert error(run(u.blade, {"title": "x", "kind": "camera"})[0])
r, f = run(u.blade, {"title": "Pics", "kind": "gallery", "images": [f"https://e.com/{i}.jpg" for i in range(12)]})
assert len(f["blade"]["images"]) == 8 and f["blade"]["size"] == "wide"
r, f = run(u.blade, {"title": "Clip", "kind": "embed", "url": "https://youtu.be/dQw4w9WgXcQ", "size": "huge"})
assert f["blade"]["url"].startswith("https://youtu.be/") and f["blade"]["size"] == "wide"

# Ids never repeat, even in a burst.
ids = {run(u.display, {"title": "x", "html": "hi"})[1]["blade"]["id"] for _ in range(50)}
assert len(ids) == 50

# No page open: say so, so Jarvis doesn't describe something nobody can see.
web.current = None
r = asyncio.run(u.ui_reset.handler({}))
assert error(r) and "No screen is connected" in r["content"][0]["text"]

# Every tool is listed for auto-approval, under the names the SDK gives them.
assert len(u.TOOL_NAMES) == 12 and "mcp__jarvis_ui__ui_theme" in u.TOOL_NAMES and "mcp__jarvis__display" in u.TOOL_NAMES
assert "mcp__jarvis__browser_view" in u.TOOL_NAMES
assert set(u.servers()) == {"jarvis_ui", "jarvis"}
# Jarvis always has them, auto-approved (the interface can be opened at any time); task workers
# never get them.
import jarvis as j
from hub import Hub
options = j.build_options(True)
assert {"jarvis_ui", "jarvis"} <= set(options.mcp_servers) and set(u.TOOL_NAMES) <= set(options.allowed_tools)
assert "jarvis_ui" in j.web_server(Hub(), j.Speech(), required=True).servers
assert not {"jarvis_ui", "jarvis"} & set(j.worker_options(None).mcp_servers)
print("ALL OK")

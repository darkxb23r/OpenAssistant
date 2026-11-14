"""
Jarvis's control of the browser interface: the screen and the face. Registered in every mode (the
interface can be opened at any time); with no screen connected the tools say so.

Ported from adewaskar/jarvis (bridge/ui.mjs and bridge/panels.mjs, MIT). The tool descriptions
are the product: they're the only place the model learns what the interface can do and when
it's worth doing, so they're kept close to the originals.

Every handler pushes one frame to the screen in use (web.current.send, which goes through the
hub to the tab used most recently) and nothing else, so
these tools are auto-approved: the worst they can do is redress the screen. What they put on
screen is still checked where it's loaded: the page sanitises markup, and web.py decides which
files and remote media it will fetch (/file, /img, /media).

Left out until their back ends exist here: article blades (the reader view), the camera, and
probe_url.
"""
import itertools
import math
import re
import time

import browser_launch
import browser_view
import web
from claude_agent_sdk import create_sdk_mcp_server, tool

# --- loose values: the model writes "0.5", 1 for true, or null for "leave it" -----------------
FALSEY = {"false", "0", "no", "off", "hide", "hidden", "none"}
AUTOMATIC = {"auto", "default", "none", "null", "reset", "clear", "stock"}   # follow the phase again
UNSET = object()   # "not mentioned", as opposed to None ("follow the phase")


def clamp(value, lo: float, hi: float):
    if value is None or isinstance(value, bool):
        return None
    try:
        n = float(str(value).strip()) if not isinstance(value, (int, float)) else float(value)
    except ValueError:
        return None
    return None if not math.isfinite(n) else min(hi, max(lo, n))


def to_bool(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    s = str(value).strip().lower()
    return None if not s else s not in FALSEY


def to_colour(args: dict, key: str):
    """UNSET when not given, None for "follow the phase", else the CSS colour."""
    if key not in args:
        return UNSET
    value = args[key]
    if value is None:
        return None
    s = str(value).strip()
    return None if not s or s.lower() in AUTOMATIC else s


def choice(value, allowed: tuple, fallback=None):
    return value if value in allowed else fallback


def put(target: dict, key: str, value) -> None:
    """Only real values reach the patch, so the page's deep merge never clears by accident."""
    if value is not None and value is not UNSET:
        target[key] = value


def put_colour(target: dict, key: str, value) -> None:
    """A colour can be cleared on purpose: None means follow the phase again."""
    if value is not UNSET:
        target[key] = value


def ok(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def refuse(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "is_error": True}


def emit(frame: dict) -> dict | None:
    """Push to the browser. A refusal to hand back if no page is open."""
    if web.current and web.current.send(frame):
        return None
    return refuse("No screen is connected; say it instead, or text a link if it's something to look at. "
                  "It will be on the screen when one opens.")


_seq = itertools.count()


def new_id(prefix: str) -> str:
    # Ids are React keys and arrive in bursts, so a counter carries the uniqueness.
    return f"{prefix}{int(time.time() * 1000):x}-{next(_seq):x}"


LOOSE_NUMBER = {"type": ["number", "string"]}
LOOSE_BOOL = {"type": ["boolean", "string", "number"]}
COLOUR = {"type": ["string", "null"]}


def _with(schema: dict, description: str) -> dict:
    return {**schema, "description": description}


def _object(properties: dict, required: tuple = ()) -> dict:
    return {"type": "object", "properties": properties, **({"required": list(required)} if required else {})}


# --- ui_* -------------------------------------------------------------------------------------
PHASE_NOTES = {
    "offline": "before the user has unlocked audio",
    "boot": "the startup sequence",
    "dormant": "powered down, waiting for the wake word",
    "waking": "the wake word just landed",
    "listening": "capturing speech",
    "thinking": "you are composing an answer",
    "tooling": "a tool is running",
    "speaking": "you are reading the answer back",
}

THEME_DESCRIPTION = """Retint the whole interface.

The HUD is drawn in one colour identity that normally follows your state: cyan
while listening, amber while thinking, violet while a tool runs, green while you
speak. An accent overrides that everywhere, at once.

Use it when the colour MEANS something. Red because a check came back bad.
Amber because you are waiting on something out of your control. A colour pulled
out of an image you just generated, so the room matches the picture. Deep blue
because it is three in the morning and they are still working.

Do not redecorate for the sake of it, and do not leave a strange colour up after
the moment that earned it has passed — call `ui_reset` when it is over.

Never announce that you have done it. The user is looking at the screen."""

THEME_SCHEMA = _object({
    "accent": _with(COLOUR, 'One CSS colour that overrides the phase colour everywhere at once — the reactor, the '
                    'panel borders, the rails, the type. e.g. "#ff2d2d", "crimson", "rgb(20 200 255)". Pass null or '
                    '"auto" to hand the interface back to its phase colours.'),
    "background": _with(COLOUR, "The page behind everything. Near-black by default and it must stay dark — a pale "
                        'background destroys the glow and makes the display unreadable. Nudge it instead: "#080d18" '
                        'for a colder room, "#150808" under an alert. Pass null or "auto" for the stock near-black.'),
    "phase_colors": {
        "type": "object",
        "properties": {p: _with(COLOUR, f"Colour for {p} — {note}.") for p, note in PHASE_NOTES.items()},
        "description": 'Recolour individual states rather than overriding all of them. Use this when one moment '
                       'deserves its own identity — a red "thinking" while you work through something grim — and '
                       "the rest of the interface should carry on as normal.",
    },
})


@tool("ui_theme", THEME_DESCRIPTION, THEME_SCHEMA)
async def ui_theme(args):
    patch = {}
    put_colour(patch, "accent", to_colour(args, "accent"))
    put_colour(patch, "background", to_colour(args, "background"))
    palette = {}
    colours = args.get("phase_colors") if isinstance(args.get("phase_colors"), dict) else {}
    for phase in PHASE_NOTES:
        c = to_colour(colours, phase)
        if isinstance(c, str):   # a phase colour has no "unset": a string, or absent
            palette[phase] = c
    if palette:
        patch["palette"] = palette
    if not patch:
        return ok("No change — no colours were given.")
    return emit({"type": "ui", "op": "patch", "args": patch}) or ok("Interface retinted.")


REACTOR_DESCRIPTION = """Reshape the reactor at the centre of the display.

The reactor is you. It is the one element the user watches while they wait, so
its size, brightness and speed are read as your state whether you intend them to
be or not. Slowing it and dimming it says the system is idling; brightening and
speeding it says you are working hard on this.

Change one property at a time and mean it. A reactor that is huge, scarlet,
motionless and wireframed all at once is not a state, it is a mess.

Everything omitted stays as it is."""

REACTOR_SCHEMA = _object({
    "color": _with(COLOUR, "The reactor core on its own, without touching the rest of the interface. Any CSS colour. "
                   'Pass null or "auto" to follow the accent and phase again.'),
    "scale": _with(LOOSE_NUMBER, "Size multiplier, 0.2 to 3, default 1. Below 1 the reactor recedes and the panels "
                   "dominate; above 1.5 it owns the frame and everything else reads as trim."),
    "intensity": _with(LOOSE_NUMBER, "Glow and brightness, 0 to 3, default 1. 0.3 reads as a system on standby, 2 or "
                       "more as strain — a hard computation, a warning, a surge."),
    "spin": _with(LOOSE_NUMBER, "Rotation-rate multiplier, 0 to 5, default 1. 0 stops it dead, which reads as powered "
                  "down or frozen and is worth exactly one dramatic moment. 3 and up reads as effort."),
    "style": {"type": "string", "enum": ["ring", "sphere", "wire"],
              "description": "ring = the stock arc-reactor halo. sphere = a solid core, heavier and more present, "
                             "good when you are the subject of the conversation. wire = a skeletal lattice, good for "
                             "diagnostics, degraded states, and anything that should look like it is being taken apart."},
    "visible": _with(LOOSE_BOOL, "false removes the reactor entirely. Only when something else has earned the centre "
                     "of the screen — a full-frame image the user is studying. Put it back the moment that is over."),
})


@tool("ui_reactor", REACTOR_DESCRIPTION, REACTOR_SCHEMA)
async def ui_reactor(args):
    reactor = {}
    put_colour(reactor, "color", to_colour(args, "color"))
    put(reactor, "scale", clamp(args.get("scale"), 0.2, 3))
    put(reactor, "intensity", clamp(args.get("intensity"), 0, 3))
    put(reactor, "spin", clamp(args.get("spin"), 0, 5))
    put(reactor, "style", choice(args.get("style"), ("ring", "sphere", "wire")))
    put(reactor, "visible", to_bool(args.get("visible")))
    if not reactor:
        return ok("No change — no reactor properties were given.")
    return emit({"type": "ui", "op": "patch", "args": {"reactor": reactor}}) or ok("Reactor adjusted.")


ORBIT_DESCRIPTION = """Hang an image in orbit around the reactor.

This is the interface showing what you have been doing. A render you just
generated, a photograph you were asked about, a screenshot you took — put it in
orbit and it lives in the room instead of sitting in a card.

Rules that matter:
  - Only images YOU produced or captured. Absolute path or file:/// URL.
    Anything from the web is refused outright.
  - Three or four objects is a system. Eight is a mess and the frame stops
    reading as anything at all.
  - An orbit persists until you take it down. Clear it when the subject
    changes — a render still circling during a conversation about mail is
    just litter.
  - This is not a substitute for `display`. Orbit it when the user should
    feel it; put it on screen when they need to look at it."""

ORBIT_SCHEMA = _object({
    "action": {"type": "string", "enum": ["add", "remove", "clear"], "default": "add",
               "description": "add = put an image in orbit (replaces the one with the same id). remove = take one "
                              "down by id. clear = take them all down."},
    "id": {"type": "string", "description": 'A short name you choose, e.g. "suit", "mars", "shot-1". Required to '
           "remove. On add it lets you move an object later instead of stacking a second copy on top of it."},
    "src": {"type": "string", "description": "The image. Give an absolute disk path or a file:/// URL — a render you "
            "generated, a screenshot you took, a file on this machine. http:// and https:// are blocked by the page "
            "and will be refused, so never orbit a picture found on the web."},
    "radius": _with(LOOSE_NUMBER, "Orbit radius as a fraction of the smaller screen axis, 0.1 to 1.2, default 0.55. "
                    "0.3 hugs the reactor, 0.8 sweeps out past the panels."),
    "speed": _with(LOOSE_NUMBER, "Revolutions per minute, -30 to 30, default 4. Two to six is stately and nothing "
                   "above ten is watchable for long. Negative goes the other way — a second object counter-rotating "
                   "reads as machinery rather than decoration."),
    "size": _with(LOOSE_NUMBER, "Rendered size in pixels, 16 to 400, default 96."),
    "tilt": _with(LOOSE_NUMBER, "Tilt of the orbital plane in degrees, -80 to 80, default 24. 0 is a flat circle "
                  "facing the user; tipping it flattens the path into an ellipse, which is what sells the depth. 20 "
                  "to 50 is the sweet spot."),
    "opacity": _with(LOOSE_NUMBER, "0 to 1, default 0.9. Drop to 0.4 when the object is atmosphere rather than the point."),
    "phase": _with(LOOSE_NUMBER, "Starting angle in degrees. Omit it and objects are spaced apart automatically; set it "
                   "only when you want two things deliberately opposed, e.g. 0 and 180."),
})

GOLDEN_ANGLE = 137.507764   # successive orbits never land on top of one another
_orbits = itertools.count()


def _remote(src: str) -> bool:
    """A web address, other than Jarvis's own /file endpoint."""
    lower = src.lower()
    own = (f"http://localhost:{web.PORT}/", f"http://127.0.0.1:{web.PORT}/")
    return lower.startswith(("http://", "https://")) and not lower.startswith(own)


@tool("ui_orbit", ORBIT_DESCRIPTION, ORBIT_SCHEMA)
async def ui_orbit(args):
    action = choice(args.get("action"), ("add", "remove", "clear"), "add")
    if action == "clear":
        return emit({"type": "ui", "op": "orbit", "args": {"action": "clear"}}) or ok("Orbits cleared.")
    oid = str(args.get("id") or "").strip()
    if action == "remove":
        # Handed back rather than guessed at: removing "probably the last one" is how an image the
        # user was still looking at disappears.
        if not oid:
            return refuse('Not removed: remove needs the id the object was added with. Call ui_orbit with '
                          'action "clear" to take them all down.')
        return emit({"type": "ui", "op": "orbit", "args": {"action": "remove", "id": oid}}) or ok("Orbit removed.")
    src = str(args.get("src") or "").strip()
    if not src:
        return refuse("Not added: an orbiting object needs a src. Give an absolute disk path or a file:/// URL to "
                      "an image you generated or captured.")
    # Refused rather than sent: the page's CSP would drop it silently, and you'd describe something
    # the user can't see.
    if _remote(src):
        return refuse("Not added: remote images are blocked by the page. Orbit a file on this machine instead — "
                      "an absolute path or a file:/// URL to something you generated, rendered or captured.")
    n = next(_orbits)
    angle = clamp(args.get("phase"), -1e6, 1e6)   # wrapped, not clamped: 400 degrees means 40
    obj = {
        "id": oid or new_id("o"),
        "src": src,
        "radius": clamp(args.get("radius"), 0.1, 1.2) or 0.55,
        "speed": d if (d := clamp(args.get("speed"), -30, 30)) is not None else 4,
        "size": clamp(args.get("size"), 16, 400) or 96,
        "tilt": t if (t := clamp(args.get("tilt"), -80, 80)) is not None else 24,
        "opacity": o if (o := clamp(args.get("opacity"), 0, 1)) is not None else 0.9,
        "phase": (n * GOLDEN_ANGLE) % 360 if angle is None else angle % 360,
    }
    return emit({"type": "ui", "op": "orbit", "args": {"action": "add", **obj}}) or ok(f'In orbit as "{obj["id"]}".')


CHROME_DESCRIPTION = """Show or hide the furniture around the display.

Everything is up by default and that is the right default — the rails are how
the user knows what you are connected to and what you just did.

Hide it only when the absence helps. A photograph they are studying, a single
number they need to hold in their head, a moment you want to land. Strip the
chrome, let the screen be quiet, and put it back when the moment is over. The
user cannot restore it themselves, so leaving it off is taking something from
them.

Pass true to show, false to hide. Anything omitted stays as it is."""

CHROME_SCHEMA = _object({
    "systems": _with(LOOSE_BOOL, "The SYSTEMS rail down the left — connected servers and status."),
    "transcript": _with(LOOSE_BOOL, "The running conversation log."),
    "tool_badge": _with(LOOSE_BOOL, "The active-tool readout under the reactor."),
    "suggestions": _with(LOOSE_BOOL, 'The "try saying…" hint.'),
    "brand": _with(LOOSE_BOOL, "The J.A.R.V.I.S. wordmark and status line."),
})


@tool("ui_chrome", CHROME_DESCRIPTION, CHROME_SCHEMA)
async def ui_chrome(args):
    chrome = {}
    # Snake case for the model, camel case in the page's store.
    for key, store_key in (("systems", "systems"), ("transcript", "transcript"), ("tool_badge", "toolBadge"),
                           ("suggestions", "suggestions"), ("brand", "brand")):
        put(chrome, store_key, to_bool(args.get(key)))
    if not chrome:
        return ok("No change — nothing was named.")
    return emit({"type": "ui", "op": "patch", "args": {"chrome": chrome}}) or ok("Chrome updated.")


EFFECT_DESCRIPTION = """Fire a one-off effect across the interface.

  glitch = corruption, interference, something wrong with the data itself.
  pulse  = acknowledgement. Something completed, something arrived.
  scan   = a sweep across the display. Searching, analysing, reading.
  shake  = impact, or a hard stop. The strongest thing here; use it once.
  flash  = a sudden alert. Reserve it for something the user must notice now.

At most one per turn, and only when something actually happened. An effect on
every answer is punctuation, and punctuation stops meaning anything by the third
sentence. Silence on the screen is the default, exactly as it is in your speech."""

EFFECTS = ("glitch", "pulse", "scan", "shake", "flash")


@tool("ui_effect", EFFECT_DESCRIPTION,
      _object({"kind": {"type": "string", "enum": list(EFFECTS), "description": "Which effect to fire."}}))
async def ui_effect(args):
    kind = choice(args.get("kind"), EFFECTS, "pulse")
    return emit({"type": "ui", "op": "effect", "args": {"kind": kind}}) or ok("Fired.")


SCREEN_DESCRIPTION = """Clear the display.

  panels     = take down everything on screen, including the sticky things.
  transcript = wipe the conversation log.
  all        = both.

Use it when the user says clear the screen, or when a topic is finished and the
leftovers from the last one would confuse what comes next. It does not touch the
theme, the reactor or the orbits — `ui_reset` does that."""


@tool("ui_screen", SCREEN_DESCRIPTION,
      _object({"what": {"type": "string", "enum": ["all", "panels", "transcript"], "default": "all",
                        "description": "What to clear."}}))
async def ui_screen(args):
    what = choice(args.get("what"), ("all", "panels", "transcript"), "all")
    return emit({"type": "ui", "op": "screen", "args": {"what": what}}) or ok("Cleared.")


RESET_DESCRIPTION = """Put the entire interface back to stock.

Colours, reactor, orbits, chrome — everything returns to the way it looks on a
fresh page. What's on screen and the transcript are left alone.

Call it when the user asks for normal, and call it yourself when whatever
justified a change is over. It is never the wrong thing to do."""


@tool("ui_reset", RESET_DESCRIPTION, _object({}))
async def ui_reset(args):
    return emit({"type": "ui", "op": "reset", "args": {}}) or ok("Interface restored.")


# --- display and blade: putting things on screen ----------------------------------------------
DESIGN_SYSTEM = """
LAYOUT CLASSES — compose these, and use NOTHING else. The renderer strips any
class name that is not on this list, so an invented one silently loses its
styling and the row lands as unformatted text.
  .hud-rows            vertical list container
  .hud-row             one row: put .hud-idx, .hud-main, .hud-tag inside
  .hud-idx             leading index or glyph, dim and monospaced
  .hud-main            the row's text column
  .hud-label           primary line (clamped to 2 lines)
  .hud-sub             secondary line, dimmed
  .hud-tag             small trailing tag, right-aligned
  .hud-metric          huge numeral, for a single headline figure
  .hud-unit            small caption under a metric
  .hud-note            a short passage of prose
  .hud-img             full-width image (use a plain <img> inside)
  .hud-caption         one line under an image
  .hud-grid            two-column grid
  .hud-bar             thin progress bar; set style="--v:0.62" for 62%
  .hud-dim             de-emphasise anything
  .hud-hot             emphasise anything (picks up the accent colour)
  .hud-gallery         grid container for several images at once
  .hud-thumb           one thumbnail, inside a .hud-gallery or beside a .hud-row
  .hud-video           a <video> player, full width of the panel
  .hud-embed           16:9 wrapper for an <iframe>; put the iframe inside it
  .hud-figure          an image or video with its .hud-caption grouped beneath

PICTURES AND VIDEO — these work. Use them.
  - Images from the web render. Paste the URL exactly as a tool or subagent
    result gave it and it appears: Jarvis fetches remote images on this machine
    and hands the bytes to the display, so hosts that refuse to be hotlinked
    still render.
  - Images off this machine work the same way: a render you generated, a
    screenshot you took, any image file on disk. Give it as
    file:///absolute/path or a bare absolute path.
  - If a search came back with pictures, SHOW the pictures.
  - Video results are for playing, not describing. A YouTube or Vimeo result
    goes in an <iframe> inside .hud-embed; a direct .mp4 or .webm goes in a
    <video class="hud-video" controls>.
  - Never invent a URL. Use only ones that appeared verbatim in a tool or
    subagent result. A guessed address is a broken image, and a broken image is
    worse than none.

RULES
  - No inline colours. The accent is themed; use the classes and it follows.
  - No <style>, <script>, <form>, links or event handlers. They are stripped.
  - <iframe> is allowed for exactly three hosts: www.youtube-nocookie.com/embed,
    www.youtube.com/embed and player.vimeo.com/video. Any other src and the
    whole element is removed. A youtube.com/watch?v=ID or youtu.be/ID link is
    fine to paste — it is rewritten into the embed form for you.
  - Every panel must have visible text or a working image. An empty body is
    rejected outright.
  - Keep it to roughly 6 rows or 40 words. This is a heads-up display glanced at
    while listening, not a document. Four thumbnails in a gallery, six at the
    outside; one video, never two.

EXAMPLES

Search results:
<div class="hud-rows">
  <div class="hud-row"><span class="hud-idx">01</span><span class="hud-main"><span class="hud-label">Plex 1.42 released</span><span class="hud-sub">Faster library scans</span></span><span class="hud-tag">forums</span></div>
  <div class="hud-row"><span class="hud-idx">02</span><span class="hud-main"><span class="hud-label">Jellyfin 10.11 beta</span></span><span class="hud-tag">github</span></div>
</div>

A single figure:
<div><span class="hud-metric">1.2 TB</span><span class="hud-unit">free on the NAS</span></div>

An image:
<div><img class="hud-img" src="file:///C:/Users/you/shot.png"><span class="hud-caption">Dashboard, 9:41 PM</span></div>

A video result — embedded and playable:
<div class="hud-figure">
  <div class="hud-embed"><iframe src="https://www.youtube-nocookie.com/embed/dQw4w9WgXcQ" title="Full replay" allow="accelerometer; encrypted-media; picture-in-picture" allowfullscreen></iframe></div>
  <span class="hud-caption">Channel · 4:32</span>
</div>

A short readout:
<p class="hud-note">Three of the four services are up. <span class="hud-hot">Sonarr is down</span> on the NAS.</p>
""".strip()

DISPLAY_DESCRIPTION = f"""Put something on the heads-up display in the browser interface.

You are designing the panel, not filling in a template — compose the markup for
the content at hand.

Use it whenever the answer has substance worth seeing rather than hearing:
search results, images, screenshots, lists of mail or events, a figure, a short
readout. If you searched, show the results. If you generated an image, show it.

Call it BEFORE or WHILE you speak, so it is up as you start talking. Never read
it aloud — say what it means, not what it contains. Speaking stays one or two
sentences even when the screen is dense.

{DESIGN_SYSTEM}"""

DISPLAY_SCHEMA = _object({
    "title": {"type": "string", "description": 'Short heading, two to four words. e.g. "SEARCH RESULTS", "INBOX".'},
    "html": {"type": "string", "description": "The body as an HTML fragment, composed using the design system in "
             "this tool description. Author it for the specific content — a list, an image, a number and a "
             "caption, whatever fits."},
    "slot": {"type": "string", "enum": ["right", "left", "wide"], "default": "right",
             "description": "wide = a broad frame, for images or dense tables. Otherwise a compact card."},
    "hold": {"type": "string", "enum": ["turn", "sticky"], "default": "turn",
             "description": "turn = clears when the user next speaks, the default. sticky = stays until replaced; "
                            "use only when the user will refer back to it."},
}, required=("title", "html"))

MEDIA_TAG = ("<img", "<video", "<iframe", "<source")


def _visible(markup: str) -> bool:
    return bool(re.sub(r"<[^>]*>", "", markup).strip()) or any(t in markup.lower() for t in MEDIA_TAG)


@tool("display", DISPLAY_DESCRIPTION, DISPLAY_SCHEMA)
async def display(args):
    markup = str(args.get("html") or "")
    # Refused rather than shown: a blank card says nothing, and an error gets you another go.
    if not _visible(markup):
        return refuse("Not shown: the body was empty. It needs visible text or an image — call display again "
                      "with the content composed into the html argument.")
    blade = {"id": new_id("p"), "title": str(args.get("title") or "").strip() or "DISPLAY", "kind": "markup",
             "html": markup, "size": "wide" if args.get("slot") == "wide" else "compact",
             "hold": choice(args.get("hold"), ("turn", "sticky"), "turn")}
    return emit({"type": "blade", "blade": blade}) or ok("On screen.")


BLADE_DESCRIPTION = """Open something on the blades — the big surface.

`display` puts up a card you glance at. A blade is a thing you LOOK at: a
photograph worth seeing properly, a gallery, a video worth watching. Blades
stack, the newest in front, and the user can pull an older one forward or throw
one to full screen.

Choosing what to open:
  image   — one picture, full width of the blade.
  gallery — several pictures at once. This is the answer to an image search.
  video   — a direct .mp4/.webm file.
  embed   — a YouTube or Vimeo watch URL. It is turned into a player.
  markup  — your own composed HTML, in the same .hud-* system `display` uses,
            when none of the above is the shape of the answer.

Size is about looking, not decoration. `wide` suits images, video and tables.
`full` takes the screen and should be reserved for the moment the content IS the
answer. `compact` is a thumbnail that stays out of the way. `tall` is a column.

Use only URLs that appeared verbatim in a tool or subagent result. Never open a
blade the user did not ask for and does not need."""

BLADE_KINDS = ("image", "gallery", "video", "embed", "markup")
BLADE_SCHEMA = _object({
    "title": {"type": "string", "description": 'Two to four words naming what this is, e.g. "MARK VII".'},
    "kind": {"type": "string", "enum": list(BLADE_KINDS), "description": "What is being opened. See the tool description."},
    "url": {"type": "string", "description": "The address, for image / video / embed."},
    "images": {"type": "array", "items": {"type": "string"},
               "description": 'Image URLs, for kind "gallery". Four is a good number, eight the most.'},
    "html": {"type": "string", "description": 'Your own markup, for kind "markup", in the .hud-* design system.'},
    "size": {"type": "string", "enum": ["compact", "tall", "wide", "full"],
             "description": "wide = pictures and tables. full = the screen."},
    "hold": {"type": "string", "enum": ["turn", "sticky"],
             "description": "turn = closes when the user next speaks. sticky = stays until replaced."},
}, required=("title", "kind"))


@tool("blade", BLADE_DESCRIPTION, BLADE_SCHEMA)
async def blade(args):
    kind = args.get("kind")
    if kind not in BLADE_KINDS:
        return refuse(f"Not opened: kind must be one of {', '.join(BLADE_KINDS)}.")
    url = str(args.get("url") or "").strip()
    images = [str(i).strip() for i in args.get("images") or [] if str(i).strip()] \
        if isinstance(args.get("images"), list) else []
    markup = str(args.get("html") or "")
    if kind == "gallery" and not images:
        return refuse("Not opened: a gallery needs at least one image URL in `images`.")
    if kind == "markup" and not _visible(markup):
        return refuse('Not opened: kind "markup" needs an `html` body with something visible in it.')
    if kind in ("image", "video", "embed") and not url:
        return refuse(f'Not opened: kind "{kind}" needs a `url`.')
    frame = {"id": new_id("b"), "title": str(args.get("title") or "").strip() or "DISPLAY", "kind": kind,
             "size": choice(args.get("size"), ("compact", "tall", "wide", "full"), "wide"),
             "hold": choice(args.get("hold"), ("turn", "sticky"), "turn")}
    if url:
        frame["url"] = url
    if images:
        frame["images"] = images[:8]
    if markup:
        frame["html"] = markup
    return emit({"type": "blade", "blade": frame}) or ok(f'Open on the blades as "{frame["title"]}".')


@tool("browser_view",
      "Show the user your browser live on their screen (show: true), or take the view down (show: false). "
      "Use it when they want to watch what you're doing in the browser.",
      _object({"show": {"type": "boolean", "description": "true to show the live view, false to close it."}},
              required=("show",)))
async def browser_view_tool(args):
    if not browser_launch.npx():
        return refuse("There's no browser: it needs Node.js 20 or newer.")
    if not to_bool(args.get("show")):
        emit(browser_view.close_frame())   # with no screen there's nothing to take down
        return ok("The browser view is closed.")
    return emit(browser_view.open_frame()) or ok("The browser is on the screen, live.")


UI_TOOLS = [ui_theme, ui_reactor, ui_orbit, ui_chrome, ui_effect, ui_screen, ui_reset]
SCREEN_TOOLS = [display, blade, browser_view_tool]


def servers() -> dict:
    """The interface's MCP servers."""
    return {"jarvis_ui": create_sdk_mcp_server("jarvis_ui", tools=UI_TOOLS),
            "jarvis": create_sdk_mcp_server("jarvis", tools=SCREEN_TOOLS)}


# Auto-approved: they only change what the browser shows.
TOOL_NAMES = [f"mcp__jarvis_ui__{t.name}" for t in UI_TOOLS] + [f"mcp__jarvis__{t.name}" for t in SCREEN_TOOLS]

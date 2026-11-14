"""
The channel hub: one Jarvis, one conversation, many ways in.

Every way of reaching Jarvis is a channel: the keyboard or the voice layer at the PC, each open
browser tab, terminal and voice satellite (web.WebClient), and the phone (phone.Channels).
The hub is what jarvis.main and guardrails talk to: it has the same say/ask/hear interface a single
channel used to have, and decides which channel each of those goes to.

- **Lines in.** A channel either has its own hear() (pulled by a pump task here) or pushes lines
  with heard(). jarvis.main reads them one at a time with hear().
- **One turn at a time.** Lines for Claude go to submit(); run_turns() sends the next one only
  when the last turn has ended, so each turn's words go back to the channel that asked. A line
  the main loop handles itself (a permission answer, standby, trust mode) is marked handled when
  it comes back for the next line.
- **Turns Claude starts itself** (a background subagent finishing): there's no channel that asked,
  so they go to the primary channel, the one the user used most recently.
- **Questions** go to every channel that shows them (each browser tab gets the question with its
  id), and are spoken on the primary one only.
- **Notices** (task news) go to the primary channel, and by text when nobody has been around.

A channel is duck-typed. Everything is optional except say():
  source (str)            where a line came from, for the main loop ("local", "web", "sms", "call")
  trust (str)             who may answer what: "physical", "device", "call", "sms"
  kind (str)              "local", "face", "phone", "terminal", "satellite"
  hear()                  a pull channel; or call hub.heard(ch, text, ref) for a push channel
  ask(summary, detail)    a plain question; show_question(q, speak) / question_closed(q, ...) for ids
  always_ask (bool)       ask it even when it isn't the primary channel (the phone tracks questions)
  takes_refs (bool)       turn_started/end_of_turn/handled/delivered get the line's ref (an ask id)
  turn_started, end_of_turn, handled, delivered, tool, abandon, standby, _busy_speaking
  notify(text)            task and order news, when it differs from say() (a socket client: never mid-answer)
  display (bool) + send(frame)   a screen the interface tools can draw on
  text_user(text)         the phone: send the user a text
"""
import asyncio
import collections
import contextlib
import logging
import time

import guardrails

log = logging.getLogger("jarvis.hub")

PRESENT_S = 15 * 60       # active this recently: where the user is
AWAY_S = 2 * 60           # nobody active this recently: task news also goes by text
HERE_S = 15 * 60          # nobody active this recently: what Jarvis says on his own isn't said aloud
SETTLE_S = 3.0            # after a barge-in, the stopped turn gets this long to end
_EOF = object()           # a pull channel ended (Ctrl+D in text mode)


class Hub:
    def __init__(self, channels=()):
        self.channels: list = []
        self._inbox: asyncio.Queue = asyncio.Queue()    # (channel, text, ref, source, trust, kind) for the main loop
        self._pumps: dict = {}                          # pull channel -> task feeding its lines in
        self._present: dict = {}                        # channel -> when the user last used it
        self.origin = None                              # the channel of the line being handled
        self._source, self._trust = "local", "physical"  # where that line came from, as it arrived
        self._kind = "local"                            # ... and the kind of channel it came by
        self._received = None                           # (channel, ref): handed to main, no turn yet
        self._queue = collections.deque()               # lines for Claude: (channel, ref, text)
        self.unheard: list[str] = []                    # what Jarvis said on his own with nobody around
        self._queued = asyncio.Event()                  # something is in _queue
        self.turn = None                                # (channel, ref) of the turn running
        self.injected = False                           # the turn running is one Claude started itself
        self._injected_to = None                        # where that turn's words went
        self.idle = asyncio.Event()                     # no turn running
        self.idle.set()
        self._interrupted = False
        self.on_interrupt = None                        # async: stop Claude's turn (jarvis.main sets it)
        self._standby = False
        self._entered = []
        self.scene = Scene()                             # what the screen shows, for screens that join
        self._started = False                           # pumps run once the channels have started
        for ch in channels:
            self.register(ch)

    # --- channels -----------------------------------------------------------------------------
    def register(self, ch, present: bool = True) -> None:
        if ch in self.channels:
            return
        self.channels.append(ch)
        if present:
            self.present(ch)
        if hasattr(ch, "standby"):
            with contextlib.suppress(Exception):
                ch.standby = self._standby
        if hasattr(ch, "hear") and not getattr(ch, "pushes", False):
            self._pumps[ch] = asyncio.get_running_loop().create_task(self._pump(ch)) if self._started else None

    def unregister(self, ch) -> None:
        if ch in self.channels:
            self.channels.remove(ch)
        self._present.pop(ch, None)
        if (pump := self._pumps.pop(ch, None)) is not None:
            pump.cancel()

    def present(self, ch) -> None:
        """The user just used this channel (a line, a connection, a "still here" from the page)."""
        self._present[ch] = time.monotonic()

    def primary(self):
        """Where the user is: the channel used most recently (the phone counts only while they're
        texting or on a call), or None when there's nobody to talk to."""
        candidates = [c for c in self.channels if not _is_phone(c) or getattr(c, "away", False)]
        if not candidates:
            return None
        return max(candidates, key=lambda c: self._present.get(c, 0.0))

    def display_target(self):
        """The screen the interface tools draw on: the one used most recently."""
        screens = [c for c in self.channels if getattr(c, "display", False)]
        return max(screens, key=lambda c: self._present.get(c, 0.0)) if screens else None

    def display(self, frame: dict) -> bool:
        """Draw on the screen in use. False when no screen is connected (the scene still keeps it,
        so it appears when one connects)."""
        self.scene.record(frame)
        target = self.display_target()
        return bool(target) and target.send(frame) is not False

    def phone(self):
        return next((c for c in self.channels if _is_phone(c)), None)

    async def __aenter__(self):
        for ch in list(self.channels):
            if hasattr(ch, "__aenter__"):
                await ch.__aenter__()
                self._entered.append(ch)
        self._started = True
        for ch in self.channels:   # a voice channel opens its mic in __aenter__: listen only after
            if ch in self._pumps and self._pumps[ch] is None:
                self._pumps[ch] = asyncio.get_running_loop().create_task(self._pump(ch))
        return self

    async def __aexit__(self, *exc):
        for pump in self._pumps.values():
            if pump:
                pump.cancel()
        for ch in reversed(self._entered):
            with contextlib.suppress(Exception):
                await ch.__aexit__(*exc)

    async def _pump(self, ch) -> None:
        try:
            while True:
                text = await ch.hear()
                if text is None:
                    continue
                await self._inbox.put(_line(ch, text, None))
        except (EOFError, KeyboardInterrupt):
            await self._inbox.put(_line(ch, _EOF, None))
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("a channel stopped listening")

    # --- lines in ------------------------------------------------------------------------------
    async def heard(self, ch, text: str, ref=None) -> None:
        """A push channel's line (a browser tab's question, with its ask id as ref)."""
        await self._inbox.put(_line(ch, text, ref))

    async def hear(self) -> str:
        """The next line for the main loop. Coming back for it means the last line was handled
        here, unless it was submitted for a turn: so a line that never reaches Claude still ends."""
        if self._received:
            ch, ref = self._received
            self._received = None
            _call(ch, "handled", ref)
        ch, text, ref, self._source, self._trust, self._kind = await self._inbox.get()
        if text is _EOF:
            raise EOFError
        self.origin = ch
        self.present(ch)
        self._received = (ch, ref)
        _call(ch, "delivered", ref)
        return text

    # Read when the line arrived: the phone's source changes from one text or call to the next, and
    # a text must never pass for a PIN-checked call.
    @property
    def source(self) -> str:
        return self._source

    @property
    def trust(self) -> str:
        return self._trust

    @property
    def kind(self) -> str:
        """The kind of channel the line came by ("satellite" can't answer every question)."""
        return self._kind

    def submit(self, text: str) -> None:
        """The line being handled goes to Claude, as a turn of its own once the last one ends."""
        ch, ref = self._received or (self.origin, None)
        self._received = None
        self._queue.append((ch, ref, text))
        self._queued.set()

    async def run_turns(self, query) -> None:
        """Send queued lines to Claude one turn at a time. query(channel, text) sends one."""
        while True:
            while not self._queue:
                self._queued.clear()
                await self._queued.wait()
            await self._settled()
            if not self._queue:   # dropped by a barge-in while waiting
                continue
            ch, ref, text = self._queue.popleft()
            self.begin_turn(ch, ref)
            try:
                await query(ch, text)
            except Exception:
                log.exception("couldn't send a line to Claude")
                self.end_turn()

    async def _settled(self) -> None:
        if not self._interrupted:
            await self.idle.wait()
            return
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self.idle.wait(), SETTLE_S)
        self._interrupted = False
        if not self.idle.is_set():   # the stopped turn never said it ended: carry on regardless
            self.end_turn()

    # --- turns ---------------------------------------------------------------------------------
    def begin_turn(self, ch, ref=None) -> None:
        self.turn, self.injected = (ch, ref), False
        self.idle.clear()
        _call(ch, "turn_started", ref)

    def begin_injected(self) -> None:
        """Claude started a turn itself (a background subagent's result). Said where the user is, or
        nowhere when nobody has been around for HERE_S: then it goes to `unheard`, for Claude's next turn."""
        if self.turn is None and not self.injected:
            self.injected, self._injected_to = True, self.primary() if self.someone_here() else None
            self.idle.clear()

    def someone_here(self) -> bool:
        """Someone used a channel lately (a line, a connection, a page's "still here")."""
        return any(time.monotonic() - t < HERE_S for c, t in self._present.items() if c in self.channels)

    def own_turn(self, text: str) -> None:
        """A turn Jarvis starts for himself (a scheduled call): no channel, so nothing is said."""
        self._queue.append((None, None, text))
        self._queued.set()

    def end_turn(self, injected: bool | None = None) -> None:
        """A ResultMessage: the turn that was running is over."""
        if self.turn:
            ch, ref = self.turn
            _call(ch, "end_of_turn", ref)
        elif self.injected and self._injected_to is not None:
            _call(self._injected_to, "end_of_turn", None)
        self.turn, self.injected, self._injected_to = None, False, None
        self.idle.set()

    async def interrupt(self, ch) -> None:
        """A channel talked over Jarvis. Its queued lines are dropped; its running turn is
        stopped. Talking while a question is open is its answer, not a barge-in."""
        if guardrails._pending_questions:
            return
        for queued in [q for q in self._queue if q[0] is ch]:
            self._queue.remove(queued)
            _call(ch, "handled", queued[1])
        running = self.turn and self.turn[0] is ch
        injected = self.injected and ch is self._injected_to
        if not (running or injected):
            return
        self._interrupted = True
        if running:
            _call(ch, "abandon", self.turn[1])
        if self.on_interrupt:
            try:
                await self.on_interrupt()
            except Exception as e:
                log.warning("couldn't interrupt the turn: %s", e)

    # --- output --------------------------------------------------------------------------------
    def _speaker(self):
        if self.turn:
            return self.turn[0]
        if self._received:
            return self._received[0]   # the reply to a line handled here ("Standing by.")
        if self.injected:
            return self._injected_to
        return self.primary()

    def say(self, text: str) -> None:
        ch = self._speaker()
        if ch is None:
            log.info("said with nobody around: %s", text)
            if self.injected:
                self.unheard.append(text)
            return
        ch.say(text)

    def tool(self, name: str) -> None:
        if self.turn:
            _call(self.turn[0], "tool", name)

    def turn_started(self) -> None:   # the old single-channel interface; run_turns does this now
        pass

    def end_of_turn(self) -> None:
        self.end_turn()

    def notify(self, text: str) -> None:
        """Task news: said where the user is, and texted when nobody has been around lately."""
        ch, phone = self.primary(), self.phone()
        if ch is not None and ch is phone:
            phone.deliver(text)   # texting or on a call with Jarvis: now, not at the end of a turn
            return
        if ch is not None:
            (getattr(ch, "notify", None) or ch.say)(text)   # a socket client: news, not part of an answer
        recent = max((t for c, t in self._present.items() if not _is_phone(c)), default=0.0)
        if phone and hasattr(phone, "text_user") and (ch is None or time.monotonic() - recent > AWAY_S):
            phone.text_user(text)

    # --- questions -----------------------------------------------------------------------------
    def ask_question(self, q) -> None:
        primary = self.primary()
        for ch in list(self.channels):
            speak = ch is primary
            if hasattr(ch, "show_question"):
                ch.show_question(q, speak)
            elif speak or getattr(ch, "always_ask", False):
                ch.ask(q.summary, q.detail)

    def ask(self, summary: str, detail: str) -> None:
        primary = self.primary()
        for ch in list(self.channels):
            if ch is primary or getattr(ch, "always_ask", False):
                ch.ask(summary, detail)

    def question_closed(self, q, decision: str, by: str | None) -> None:
        for ch in list(self.channels):
            _call(ch, "question_closed", q, decision, by)

    def open_questions(self) -> list:
        return [q for q in guardrails._pending_questions if hasattr(q, "frame")]

    async def answer(self, ch, question_id: str, decision: str) -> bool:
        """A button on a screen answered one question."""
        self.present(ch)
        return guardrails.answer_question(question_id, decision, by=getattr(ch, "trust", None),
                                          kind=getattr(ch, "kind", None))

    # --- shared state --------------------------------------------------------------------------
    @property
    def standby(self) -> bool:
        return self._standby

    @standby.setter
    def standby(self, on: bool) -> None:
        self._standby = bool(on)
        for ch in self.channels:
            if hasattr(ch, "standby"):
                with contextlib.suppress(Exception):
                    ch.standby = on

    def console(self, text: str) -> None:
        """A notice for whoever watches Jarvis work: this process's output and attached terminals."""
        print(text)
        for ch in list(self.channels):
            if hasattr(ch, "console"):
                with contextlib.suppress(Exception):
                    ch.console(text)

    def show(self, title: str, text: str) -> None:
        """Something to read in full (a diff, a setup script): on the screen in use, and in terminals."""
        import html
        self.console(f"\n   [{title}]\n{text}\n")
        self.display({"type": "blade", "blade": {
            "id": f"show-{abs(hash((title, text))) % 10**8}", "title": title[:60].upper(), "kind": "markup",
            "size": "wide", "hold": "sticky", "html": f"<pre>{html.escape(text)}</pre>"}})

    def returned(self) -> bool:
        phone = self.phone()
        return bool(phone and hasattr(phone, "returned") and phone.returned())

    def _busy_speaking(self) -> bool:
        return any(getattr(ch, "_busy_speaking", lambda: False)() for ch in self.channels)


class Scene:
    """What Jarvis has put on the screen: the interface's look (theme, reactor, chrome), what's in
    orbit, and the open blades. A screen that connects (a new tab, a reconnect, another device) is
    sent it, so it shows what the last one did."""

    MAX_BLADES = 20

    def __init__(self):
        self.look: dict = {}
        self.orbits: dict = {}
        self.blades: list = []

    def record(self, frame: dict) -> None:
        kind = frame.get("type")
        if kind == "blade" and isinstance(frame.get("blade"), dict):
            blade = frame["blade"]
            if blade.get("question"):   # a question's card: shown again from the open questions
                return
            self.blades = [b for b in self.blades if b.get("id") != blade.get("id")][-(self.MAX_BLADES - 1):] + [blade]
        elif kind == "blade_close":
            self.blades = [b for b in self.blades if b.get("id") != frame.get("id")]
        elif kind == "ui":
            op, args = frame.get("op"), frame.get("args") or {}
            if op == "patch":
                _merge(self.look, args)
            elif op == "orbit":
                if args.get("action") == "add":
                    self.orbits[args.get("id")] = args
                elif args.get("action") == "remove":
                    self.orbits.pop(args.get("id"), None)
                else:
                    self.orbits.clear()
            elif op == "reset":
                self.look.clear()
                self.orbits.clear()
            elif op == "screen" and args.get("what") in ("all", "panels"):
                self.blades.clear()

    def frames(self) -> list[dict]:
        out = [{"type": "ui", "op": "patch", "args": self.look}] if self.look else []
        out += [{"type": "ui", "op": "orbit", "args": o} for o in self.orbits.values()]
        out += [{"type": "blade", "blade": b} for b in self.blades]
        return out

    def describe(self, target) -> str:
        return f"{getattr(target, 'name', None) or getattr(target, 'kind', 'screen')}" if target else "none"


def _merge(into: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = value


def _line(ch, text, ref) -> tuple:
    return ch, text, ref, getattr(ch, "source", "local"), getattr(ch, "trust", "physical"), getattr(ch, "kind", "local")


def _is_phone(ch) -> bool:
    return getattr(ch, "kind", "") == "phone"


def _call(ch, name: str, *args) -> None:
    """Call an optional channel method, passing the line's ref only to channels that take refs."""
    fn = getattr(ch, name, None)
    if fn is None:
        return
    try:
        if name in ("turn_started", "end_of_turn", "handled", "delivered", "abandon") and not getattr(ch, "takes_refs", False):
            if name in ("turn_started", "end_of_turn"):
                fn()
            return
        fn(*args)
    except Exception:
        log.exception("channel %s.%s failed", type(ch).__name__, name)


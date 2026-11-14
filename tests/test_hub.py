"""The channel hub (hub.py): one conversation, many channels. Turns one at a time and answered on the
channel that asked; turns Claude starts itself go to the channel in use; questions shown everywhere
and answered once; task news by text when nobody is around; a line's source kept as it arrived."""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import guardrails as g
import hub as h
from hub import Hub


class Pull:
    """A keyboard-like channel: the hub pulls its lines."""
    def __init__(self, name, source="local", trust="physical"):
        self.name, self.source, self.trust = name, source, trust
        self.lines, self.said, self.asked, self.ended = asyncio.Queue(), [], [], 0
    def say(self, text): self.said.append(text)
    def ask(self, summary, detail): self.asked.append(summary)
    def end_of_turn(self): self.ended += 1
    async def hear(self):
        return await self.lines.get()


class Push:
    """A browser-tab-like channel: it pushes its lines, takes refs, has a screen."""
    pushes, takes_refs, display, kind, source, trust = True, True, True, "face", "web", "device"
    def __init__(self, name):
        self.name, self.log, self.shown = name, [], []
    def say(self, text): self.log.append(("say", text))
    def send(self, frame): self.log.append(("frame", frame)); return True
    def show_question(self, q, speak): self.shown.append((q.id, speak))
    def question_closed(self, q, decision, by): self.log.append(("closed", q.id, decision))
    def turn_started(self, ref): self.log.append(("start", ref))
    def end_of_turn(self, ref=None): self.log.append(("end", ref))
    def handled(self, ref): self.log.append(("handled", ref))
    def abandon(self, ref): self.log.append(("abandon", ref))


class Phone:
    kind, always_ask = "phone", True
    def __init__(self):
        self.source, self.texts, self.asked, self.lines = "local", [], [], asyncio.Queue()
    @property
    def away(self): return self.source in ("sms", "call")
    @property
    def trust(self): return self.source if self.away else "physical"
    def say(self, text): self.texts.append(("say", text))
    def ask(self, summary, detail): self.asked.append(summary)
    def deliver(self, text): self.texts.append(("deliver", text))
    def text_user(self, text): self.texts.append(("text", text))
    async def hear(self):
        self.source, text = await self.lines.get()
        return text


async def settle():
    await asyncio.sleep(0.05)


async def main():
    hub = Hub()
    g._io = hub
    kb, tab, phone = Pull("kb"), Push("tab"), Phone()
    hub.register(kb)
    hub.register(phone, present=False)
    queried = []

    async def query(ch, text):
        queried.append((ch, text))

    async with hub:
        sender = asyncio.create_task(hub.run_turns(query))

        # A line from the keyboard becomes a turn; its words go back there.
        await kb.lines.put("how's the backup?")
        assert await hub.hear() == "how's the backup?" and hub.source == "local" and hub.trust == "physical"
        hub.submit("how's the backup?")
        await settle()
        assert queried == [(kb, "how's the backup?")] and not hub.idle.is_set()
        hub.say("Half done.")
        assert kb.said == ["Half done."]

        # One turn at a time: a second line waits for the first turn to end.
        hub.register(tab)
        await hub.heard(tab, "and the NAS?", "a1")
        assert await hub.hear() == "and the NAS?" and hub.source == "web"
        hub.submit("and the NAS?")
        await settle()
        assert len(queried) == 1, "the second line must wait"
        hub.end_turn()
        assert kb.ended == 1
        await settle()
        assert queried[-1] == (tab, "and the NAS?") and tab.log[-1] == ("start", "a1")
        hub.say("Fine.")
        hub.end_turn()
        assert tab.log[-2:] == [("say", "Fine."), ("end", "a1")]

        # A turn Claude starts itself goes to the channel in use (the tab, used last), and ends there.
        hub.begin_injected()
        assert not hub.idle.is_set()
        hub.say("The download finished.")
        hub.end_turn()
        assert tab.log[-2:] == [("say", "The download finished."), ("end", None)] and hub.idle.is_set()

        # ... but with nobody around for a while it isn't said into an empty room: it's kept, unheard.
        before = dict(hub._present)
        for ch in list(hub._present):
            hub._present[ch] -= h.HERE_S + 1
        n = len(tab.log)
        hub.begin_injected()
        hub.say("The other download finished too.")
        hub.end_turn()
        assert len(tab.log) == n and hub.unheard == ["The other download finished too."] and hub.idle.is_set()
        hub._present.update(before)
        hub.unheard.clear()

        # A turn Jarvis starts for himself (a scheduled call) is sent to Claude, and nothing is said.
        hub.own_turn("[Scheduled call] ...")
        await settle()
        assert queried[-1] == (None, "[Scheduled call] ...")
        hub.say("Ringing now.")
        hub.end_turn()
        assert len(tab.log) == n and hub.unheard == [] and hub.idle.is_set()

        # A permission answer gets through while a turn is waiting on it.
        await kb.lines.put("restart plex")
        await hub.hear()
        hub.submit("restart plex")
        await settle()
        asked = asyncio.create_task(g.ask_user("restart Plex", "(Bash) docker restart plex", False))
        await settle()
        q = g._pending_questions[0]
        # shown on the tab (silently: the keyboard is in use), asked at the keyboard, tracked by the phone
        assert tab.shown[-1] == (q.id, False) and kb.asked[-1] == "restart Plex" and phone.asked[-1] == "restart Plex"
        await kb.lines.put("yes")
        assert await hub.hear() == "yes"
        assert g.answer_oldest_question("yes", by=hub.trust) and await asked == "yes"
        assert ("closed", q.id, "yes") in tab.log
        hub.end_turn()

        # A barge-in drops the channel's queued lines and stops its running turn.
        await hub.heard(tab, "tell me a story", "a2")
        await hub.hear()
        hub.submit("tell me a story")
        await settle()
        await hub.heard(tab, "and another", "a3")
        await hub.hear()
        hub.submit("and another")
        stopped = []

        async def stop():
            stopped.append(1)
        hub.on_interrupt = stop
        await hub.interrupt(tab)
        assert stopped == [1] and ("abandon", "a2") in tab.log and ("handled", "a3") in tab.log
        hub.end_turn()
        await asyncio.sleep(0.1)
        assert queried[-1][1] == "tell me a story", "the dropped line never reaches Claude"

        # Task news: said where the user is; texted too once nobody has been around for a while.
        hub.notify("The backup task is done.")
        assert tab.log[-1] == ("say", "The backup task is done.") and phone.texts == []
        for ch in list(hub._present):
            hub._present[ch] -= h.AWAY_S + 1
        hub.notify("The sync task failed.")
        assert phone.texts[-1] == ("text", "The sync task failed.")

        # The phone becomes where the user is while they text, and news goes to it right away.
        await phone.lines.put(("sms", "status?"))
        assert await hub.hear() == "status?" and hub.source == "sms" and hub.trust == "sms"
        assert hub.primary() is phone
        hub.notify("Half way there.")
        assert phone.texts[-1] == ("deliver", "Half way there.")
        # A text can't answer a question, hard or not.
        asked = asyncio.create_task(g.ask_user("reboot the NAS", "(Bash) ssh nas sudo reboot", True))
        await settle()
        assert g.answer_oldest_question("yes", by=hub.trust) is False and not asked.done()
        assert g.answer_question(g._pending_questions[0].id, "yes", by="sms") is False
        assert g.answer_question(g._pending_questions[0].id, "no", by="physical") and await asked == "no"

        # The source is the one each line arrived with, even when the channel's changes before the
        # main loop reads it (a text, then a call straight after).
        await phone.lines.put(("sms", "first"))
        await phone.lines.put(("call", "second"))
        await settle()
        assert await hub.hear() == "first" and hub.source == "sms"
        assert await hub.hear() == "second" and hub.source == "call"

        # Standby reaches every channel that has one, including ones that join later.
        hub.standby = True
        late = Push("late")
        late.standby = False
        hub.register(late)
        assert late.standby is True
        hub.standby = False

        # The screen in use gets what the interface tools draw; no screen, no drawing.
        hub.present(tab)
        assert hub.display({"type": "ui", "op": "reset"}) and tab.log[-1][0] == "frame"
        hub.unregister(tab); hub.unregister(late)
        assert hub.display({"type": "ui"}) is False

        # A keyboard closed with Ctrl+D ends the main loop's hearing.
        kb2 = Pull("kb2")
        hub.register(kb2)

        async def eof():
            raise EOFError
        kb2.hear = eof
        hub.unregister(kb2); hub.register(kb2)
        try:
            await asyncio.wait_for(hub.hear(), 1)
            raise AssertionError("EOF must end hearing")
        except EOFError:
            pass
        sender.cancel()


asyncio.run(main())
print("ALL OK")

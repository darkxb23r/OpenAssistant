"""jarvis.main and where a line came from: a text or a call (caller ID can be faked) never turns
trust mode on (nor does a voice satellite, nor do any of them resume the standing orders), and a
text never answers a permission question (phone.Channels does that, with the
code). A stand-in for Claude, so nothing reaches the API."""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
os.environ.pop("JARVIS_CODEX", None)
sys.path.insert(0, str(ROOT))
import control
import jarvis as j
import guardrails as g

queried = []


class FakeClaude:
    def __init__(self, options): pass
    async def __aenter__(self): return self
    async def __aexit__(self, *exc): pass
    async def query(self, prompt): queried.append(prompt)
    async def interrupt(self): pass

    async def receive_messages(self):
        await asyncio.Event().wait()
        yield


class FakeIO:
    """A channel whose lines say where they came from."""
    source = "web"

    def __init__(self):
        self.lines, self.said, self.asked = asyncio.Queue(), [], []

    async def __aenter__(self): return self
    async def __aexit__(self, *exc): pass
    def say(self, text): self.said.append(text)
    def ask(self, summary, detail): self.asked.append(summary)

    async def hear(self):
        self.source, text = await self.lines.get()
        return text


io = FakeIO()
j.ClaudeSDKClient = FakeClaude
j.detect_caveman = lambda *a, **k: False
class FakeServer:
    """In place of the interface's server: attaches the fake channel to the hub."""
    def __init__(self, hub): self.hub = hub
    async def __aenter__(self): self.hub.register(io); return self
    async def __aexit__(self, *exc): pass


j.web_server = lambda hub, speech, required: FakeServer(hub)
sys.argv = ["jarvis.py", "--core", "--new"]


async def settle():
    for _ in range(50):
        await asyncio.sleep(0.01)
        if io.lines.empty():
            break
    await asyncio.sleep(0.05)


async def main():
    jarvis = asyncio.create_task(j.main())
    await asyncio.sleep(0.2)

    # Trust mode: never turned on from a text, a call or a voice satellite (anyone in the room);
    # from the browser, yes. Turning it off is fine from anywhere: it only makes Jarvis ask more.
    assert set(j.REDUCE_ONLY_SOURCES) == {"sms", "call", "satellite"}
    for source in j.REDUCE_ONLY_SOURCES:
        await io.lines.put((source, "Stop asking for 30 minutes."))
        await settle()
        assert not g.trusting(), source
        assert io.said[-1] == "You can turn that on at the PC or in the browser."
    for source in j.REDUCE_ONLY_SOURCES:
        await io.lines.put(("web", "Stop asking for 30 minutes."))
        await settle()
        assert g.trusting()
        await io.lines.put((source, "Start asking again."))
        await settle()
        assert not g.trusting(), source
        assert io.said[-1] == "Okay, I'll ask again."

    # Standing orders: paused from anywhere, resumed only from the PC or a screen.
    for source in j.REDUCE_ONLY_SOURCES:
        await io.lines.put((source, "Pause your standing orders."))
        await settle()
        assert control.paused(), source
        await io.lines.put((source, "Resume your standing orders."))
        await settle()
        assert control.paused() and io.said[-1] == "You can turn that on at the PC or in the browser.", source
    await io.lines.put(("web", "Resume your standing orders."))
    await settle()
    assert not control.paused() and io.said[-1] == "Standing orders resumed."
    # Standby is fine from a satellite too (it only makes Jarvis wait for his name).
    assert j.standby_request("Jarvis, stand by.") is True
    await io.lines.put(("satellite", "Jarvis, stand by."))
    await settle()
    assert io.said[-1] == "Standing by."
    j.set_standby(False)

    # A permission question: a text's "yes" doesn't answer it; the browser's does.
    task = asyncio.create_task(g.ask_before_risky("mcp__x__y", {}, None))
    await asyncio.sleep(0.02)
    assert g._pending_questions
    await io.lines.put(("sms", "yes"))
    await settle()
    assert g._pending_questions and not task.done()
    await io.lines.put(("web", "yes"))
    await settle()
    assert type(await asyncio.wait_for(task, 2)).__name__ == "PermissionResultAllow"

    jarvis.cancel()
    try:
        await jarvis
    except (asyncio.CancelledError, SystemExit):
        pass


asyncio.run(main())

# Phone calls get the voice layer's models when it's running, else the speech service's (loaded on
# demand), never None.
class Service:
    def call_models(self): return "whisper", "kokoro"

class VoiceLike:
    _stt, _tts = "voice-whisper", "voice-kokoro"

assert j.call_models(None, Service()) == ("whisper", "kokoro")
assert j.call_models(VoiceLike(), Service()) == ("voice-whisper", "voice-kokoro")
print("ALL OK")

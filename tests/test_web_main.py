"""jarvis.main in --core mode, end to end, with a stand-in for Claude: the page asks, Jarvis's
streamed reply comes back under that question's id, and lines Jarvis handles itself still end."""
import asyncio, json, os, socket, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
os.environ.pop("JARVIS_CODEX", None)
with socket.socket() as s:
    s.bind(("127.0.0.1", 0))
    PORT = s.getsockname()[1]
os.environ["JARVIS_WEB_PORT"] = str(PORT)
sys.path.insert(0, str(ROOT))
import jarvis as j
import web
import websockets

import speech
speech.STT_AVAILABLE = speech.TTS_AVAILABLE = False   # no model downloads in a test: speech isn't tested here
from claude_agent_sdk import AssistantMessage, ResultMessage, StreamEvent, ToolUseBlock, UserMessage

asked = []


class FakeClaude:
    """Answers every query with two streamed sentences and one tool use, like the real stream."""

    def __init__(self, options):
        global claude
        self.options = options
        self._out: asyncio.Queue = asyncio.Queue()
        claude = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def query(self, prompt):
        asked.append(prompt)
        delta = lambda t: StreamEvent("u", "s", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": t}})
        for msg in (AssistantMessage([ToolUseBlock("t1", "mcp__time__get_current_time", {})], "sonnet"),
                    delta("It's five. "), delta("Anything else?"),
                    StreamEvent("u", "s", {"type": "content_block_stop"}),
                    ResultMessage("success", 1, 1, False, 1, "s")):
            await self._out.put(msg)

    async def inject(self, text):
        """A turn Claude starts itself: a background subagent's result arriving."""
        origin = {"kind": "task-notification"}
        delta = StreamEvent("u", "s", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})
        for msg in (UserMessage("<task-notification>done</task-notification>", origin=origin), delta,
                    StreamEvent("u", "s", {"type": "content_block_stop"}),
                    ResultMessage("success", 1, 1, False, 1, "s", origin=origin)):
            await self._out.put(msg)

    async def interrupt(self):
        pass

    async def receive_messages(self):
        while True:
            yield await self._out.get()


j.ClaudeSDKClient = FakeClaude
j.detect_caveman = lambda *a, **k: False
sys.argv = ["jarvis.py", "--core", "--new"]


async def main():
    jarvis = asyncio.create_task(j.main())
    for _ in range(100):
        try:
            ws = await websockets.connect(f"ws://127.0.0.1:{PORT}/", origin="http://localhost:5173")
            break
        except OSError:
            await asyncio.sleep(0.1)
    async with ws:
        frame = lambda: asyncio.wait_for(ws.recv(), 5)
        await ws.send(json.dumps({"type": "hello", "protocol": 1, "client": {"kind": "face", "name": "test"},
                                  "caps": {"display": True}}))
        assert json.loads(await frame())["type"] == "welcome"
        ready = json.loads(await frame())
        assert ready["type"] == "ready" and {"jarvis_ui", "jarvis", "tasks"} <= set(ready["servers"]), ready

        await ws.send(json.dumps({"type": "ask", "text": "what time is it", "id": "a1"}))
        got = [json.loads(await frame()) for _ in range(4)]
        assert got == [{"type": "tool", "name": "mcp__time__get_current_time", "ask": "a1"},
                       {"type": "text", "delta": "It's five. ", "ask": "a1"},
                       {"type": "text", "delta": "Anything else? ", "ask": "a1"},
                       {"type": "done", "ask": "a1"}], got
        assert len(asked) == 1 and asked[0].endswith("] what time is it")

        # Handled by Jarvis itself, no Claude turn: the page still gets its "done".
        await ws.send(json.dumps({"type": "ask", "text": "stop asking for 10 minutes", "id": "a2"}))
        reply = json.loads(await frame())
        assert reply["type"] == "text" and reply["ask"] == "a2" and "stop asking" in reply["delta"], reply
        assert json.loads(await frame()) == {"type": "done", "ask": "a2"}
        assert len(asked) == 1

        # Standby, said casually: the page is told first, then hears "Standing by." as the answer,
        # so it goes back to waiting for its name instead of listening for a follow-up.
        await ws.send(json.dumps({"type": "ask", "text": "Okay Jarvis, wait for further instructions.", "id": "s1"}))
        got = [json.loads(await frame()) for _ in range(3)]
        assert got == [{"type": "standby"}, {"type": "text", "delta": "Standing by. ", "ask": "s1"},
                       {"type": "done", "ask": "s1"}], got
        assert len(asked) == 1 and j.STANDBY_FILE.exists()
        await ws.send(json.dumps({"type": "ask", "text": "wake up", "id": "s2"}))
        got = [json.loads(await frame()) for _ in range(2)]
        assert got == [{"type": "text", "delta": "At your service. ", "ask": "s2"}, {"type": "done", "ask": "s2"}], got

        # A turn Claude starts itself (a background job finishing) reaches the tab as speech of its
        # own, ends nobody's question, and the next question works as usual.
        await claude.inject("The scan is done.")
        assert json.loads(await frame()) == {"type": "say", "text": "The scan is done."}
        await asyncio.sleep(0.1)
        assert j._io.idle.is_set()
        # One Jarvis decides isn't worth saying (QUIET) says nothing at all; the next one is said.
        await claude.inject("QUIET")
        await claude.inject("Q")       # a block that only starts like QUIET is still said
        assert json.loads(await frame()) == {"type": "say", "text": "Q"}
        await asyncio.sleep(0.1)
        assert j._io.idle.is_set()

        # The PC's quit words don't stop Jarvis from a browser tab: it's an ordinary message.
        await ws.send(json.dumps({"type": "ask", "text": "goodbye", "id": "a3"}))
        got = [json.loads(await frame()) for _ in range(4)]
        assert got[-1] == {"type": "done", "ask": "a3"} and not jarvis.done()

        # A voice satellite while the charter question waits: it can't answer it, so a "yes" is told
        # where it waits, and anything else goes on to Jarvis instead of being held as noise.
        import agency
        import guardrails as g
        code = web.current.devices.new_code("kitchen", "satellite")
        async with websockets.connect(f"ws://127.0.0.1:{PORT}/") as sat:
            await sat.send(json.dumps({"type": "hello", "protocol": 1, "pair": code,
                                       "client": {"kind": "satellite", "name": "box"}, "caps": {"display": False}}))
            assert json.loads(await asyncio.wait_for(sat.recv(), 5))["type"] == "welcome"
            assert json.loads(await asyncio.wait_for(sat.recv(), 5))["type"] == "ready"
            await sat.send(json.dumps({"type": "presence"}))
            await asyncio.sleep(0.1)
            before = len(asked)
            charter = g.open_question("adopt your charter: new standing orders homelab", "the charter", hard=True,
                                      only=agency.ANSWER_FROM, not_kinds=agency.ANSWER_NOT_FROM)
            charter.charter_hash = "abc"
            elsewhere = "There's a charter change waiting for you at the PC or in the browser."
            assert json.loads(await asyncio.wait_for(sat.recv(), 5)) == {"type": "say", "text": elsewhere}
            await sat.send(json.dumps({"type": "ask", "text": "Yes.", "id": "y1"}))
            got = [json.loads(await asyncio.wait_for(sat.recv(), 5)) for _ in range(2)]
            assert got == [{"type": "text", "delta": f"{elsewhere} ", "ask": "y1"}, {"type": "done", "ask": "y1"}], got
            assert not charter.answer.done() and len(asked) == before
            await sat.send(json.dumps({"type": "ask", "text": "lights off", "id": "y2"}))
            for _ in range(10):
                if json.loads(await asyncio.wait_for(sat.recv(), 5)) == {"type": "done", "ask": "y2"}:
                    break
            assert len(asked) == before + 1 and asked[-1].endswith("] lights off"), asked
            assert g.answer_question(charter.id, "no", by="device", kind="face")
    jarvis.cancel()
    try:
        await jarvis
    except (asyncio.CancelledError, SystemExit):
        pass


asyncio.run(main())
print("ALL OK")

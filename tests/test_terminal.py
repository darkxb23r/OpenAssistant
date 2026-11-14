"""`jarvisctl attach` (terminal.py) against a real interface server: the control token makes it a
terminal with the standing of the keyboard at the PC, it shows Jarvis's console notices, and a
socket without a browser Origin gets nowhere without that token."""
import asyncio, json, os, queue, socket, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import control
import guardrails as g
import protocol
import speech as sp
import terminal
import web as w
import websockets
from hub import Hub

sp.STT_AVAILABLE = sp.TTS_AVAILABLE = False   # never load (or download) speech models here


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --- render(): each frame as a terminal line, or nothing ---
assert terminal.render({"type": "text", "delta": "Done, sir. ", "ask": "t1"}) == "jarvis> Done, sir."
assert terminal.render({"type": "say", "text": "The backup finished."}) == "jarvis> The backup finished."
assert terminal.render({"type": "console", "text": "   [auto-approved by read-only: ls]"}) == \
    "   [auto-approved by read-only: ls]"
q = terminal.render({"type": "question", "question": {"id": "q1", "summary": "restart the NAS", "detail": "ssh nas reboot"}})
assert "Jarvis wants to restart the NAS" in q and "ssh nas reboot" in q and "Allow?" in q
assert terminal.render({"type": "question_closed", "id": "q1", "decision": "no"}) == "   [answered: no]"
assert terminal.render({"type": "tool", "name": "Bash"}) == "   [using Bash]"
assert "standing by" in terminal.render({"type": "standby"})
assert terminal.render({"type": "blade", "blade": {"kind": "markup", "title": "Disks"}}) == "   [on screen: Disks]"
for quiet in ({"type": "done", "ask": "t1"}, {"type": "ready"}, {"type": "ui"},
              {"type": "blade", "blade": {"kind": "markup", "title": "q", "question": "q1"}}):
    assert terminal.render(quiet) is None, quiet

# --- the terminal, attached: stdin and stdout replaced ---
typed: queue.Queue = queue.Queue()
shown: list[str] = []


def fake_input(prompt=""):
    try:
        line = typed.get(timeout=20)
    except queue.Empty:
        raise EOFError
    if line is EOFError:
        raise EOFError
    return line


terminal.input = fake_input
terminal.print = lambda *a, file=None, **k: shown.append(" ".join(str(x) for x in a) + (" [stderr]" if file else ""))


async def until(check, what: str, timeout: float = 10):
    for _ in range(int(timeout / 0.02)):
        if check():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}; shown: {shown}")


async def hello_without_token(port: int, first: dict) -> int:
    """A socket with no Origin (not a page) introducing itself without the control token."""
    async with websockets.connect(f"ws://127.0.0.1:{port}/") as ws:
        await ws.send(json.dumps(first))
        try:
            await asyncio.wait_for(ws.recv(), 5)
        except websockets.ConnectionClosed:
            pass
        await asyncio.wait_for(ws.wait_closed(), 5)
        return ws.close_code


async def main():
    port = free_port()
    hub = Hub()
    g._io = hub
    token = control.new_token()
    server = w.WebServer(hub, [], port=port)
    server.control = control.Control(token)
    async with hub, server:
        attached = asyncio.create_task(terminal.run(port, "ssh-laptop"))
        await until(lambda: any("Attached to Jarvis as ssh-laptop" in s for s in shown), "the welcome")
        # On the hub: a terminal, no screen, with the standing of the keyboard at the PC.
        await until(lambda: len(hub.channels) == 1, "the channel")
        ch = hub.channels[0]
        assert (ch.kind, ch.name, ch.source, ch.trust, ch.display) == \
            ("terminal", "ssh-laptop", "terminal", "physical", False), vars(ch)

        # Jarvis's console notices reach it; a face would get none of them.
        hub.console("   [auto-approved by read-only: Get-ChildItem]")
        await until(lambda: "   [auto-approved by read-only: Get-ChildItem]" in shown, "the console line")

        # Typing is a line from this terminal, heard with physical trust.
        typed.put("how are the disks?")
        assert await asyncio.wait_for(hub.hear(), 10) == "how are the disks?"
        assert hub.source == "terminal" and hub.trust == "physical"
        hub.say("All healthy, sir.")
        await until(lambda: "jarvis> All healthy, sir." in shown, "the reply")

        # Without the control token, a socket that isn't a page is refused: pairing required.
        bare = {"type": "hello", "protocol": 1, "client": {"kind": "terminal", "name": "x"}}
        assert await hello_without_token(port, bare) == protocol.CLOSE_PAIRING
        assert await hello_without_token(port, {**bare, "token": "not-the-token"}) == protocol.CLOSE_PAIRING
        # A first frame that isn't a hello: a client from before the protocol, 4002.
        assert await hello_without_token(port, {"type": "ask", "text": "hi", "id": "1"}) == protocol.CLOSE_VERSION
        assert len(hub.channels) == 1

        # "quit" detaches; Jarvis carries on.
        typed.put("quit")
        assert await asyncio.wait_for(attached, 10) == 0
        assert any("Detached" in s for s in shown)
        await until(lambda: hub.channels == [], "the channel to go")

        # A stale token (the core restarted under it): refused, said so, no traceback.
        shown.clear()
        control.TOKEN_FILE.write_text("stale", encoding="utf-8")
        assert await asyncio.wait_for(terminal.run(port, "x"), 10) == 1
        assert any("[stderr]" in s and "refused" in s for s in shown), shown
        control.TOKEN_FILE.write_text(token, encoding="utf-8")

        # The core going away while attached: the terminal says so and ends, without waiting for a line.
        shown.clear()
        attached = asyncio.create_task(terminal.run(port, "ssh-laptop"))
        await until(lambda: len(hub.channels) == 1, "the channel again")
        for client in list(server.clients):
            await client.ws.close(code=1012)
        assert await asyncio.wait_for(attached, 10) == 0
        assert any("Detached" in s or "closed" in s for s in shown), shown

    # No token file at all: nothing to attach to.
    shown.clear()
    control.TOKEN_FILE.unlink()
    assert await terminal.run(port, "x") == 1 and any("no control token" in s for s in shown)
    # Nothing listening.
    control.new_token()
    assert await terminal.run(free_port(), "x") == 1 and any("isn't answering" in s for s in shown)


asyncio.run(main())
print("ALL OK")

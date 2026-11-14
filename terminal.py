"""
`jarvisctl attach`: talk to a running Jarvis core from a terminal, on this PC or over SSH.

A protocol client (protocol.py) of kind "terminal", without a screen. It proves it's the user at
this PC with the control token (~/.jarvis/control.token, which only the user can read), so it has
the same standing as the keyboard at the PC: it can answer any question and switch trust mode.
Typing "quit" or Ctrl+D detaches; Jarvis carries on.
"""
import asyncio
import contextlib
import json
import sys
import threading

import control

PROMPT = "you> "


def render(frame: dict) -> str | None:
    """A frame from the core, as a line for the terminal (None: nothing to show)."""
    kind = frame.get("type")
    if kind in ("text", "say"):
        return f"jarvis> {(frame.get('delta') or frame.get('text') or '').strip()}"
    if kind == "console":
        return frame.get("text")
    if kind == "question":
        q = frame.get("question") or {}
        return f"\n   Jarvis wants to {q.get('summary')}\n   {q.get('detail')}\n   Allow? [y/N/always]"
    if kind == "question_closed":
        return f"   [answered: {frame.get('decision')}]"
    if kind == "tool":
        return f"   [using {frame.get('name')}]"
    if kind == "standby":
        return "   [standing by: say his name, or type, to carry on]"
    if kind == "blade" and (frame.get("blade") or {}).get("kind") == "markup" and not (frame.get("blade") or {}).get("question"):
        return f"   [on screen: {(frame.get('blade') or {}).get('title')}]"
    return None


QUIT = ("quit", "exit", "detach")


async def run(port: int, name: str) -> int:
    import websockets   # not stdlib: only attach needs it

    token = control.read_token()
    if not token:
        print("jarvisctl: no control token (~/.jarvis/control.token): is Jarvis running?", file=sys.stderr)
        return 1
    try:
        ws = await websockets.connect(f"ws://127.0.0.1:{port}/")
    except OSError as e:
        print(f"jarvisctl: Jarvis isn't answering on 127.0.0.1:{port} ({e}).", file=sys.stderr)
        return 1
    async with ws:
        await ws.send(json.dumps({"type": "hello", "protocol": 1, "token": token,
                                  "client": {"kind": "terminal", "name": name},
                                  "caps": {"display": False, "speech": "browser", "audio_in": "none",
                                           "audio_out": "none"}}))
        try:
            welcome = json.loads(await ws.recv())
        except websockets.ConnectionClosed as e:   # e.g. a stale token: the core restarted since
            reason = e.rcvd.reason if e.rcvd else ""
            print(f"jarvisctl: Jarvis refused this terminal ({reason or e}); if it just restarted, try again.",
                  file=sys.stderr)
            return 1
        if welcome.get("type") != "welcome":
            print(f"jarvisctl: Jarvis refused this terminal: {welcome}", file=sys.stderr)
            return 1
        print(f"Attached to Jarvis as {name}. Type to talk; 'quit' or Ctrl+D detaches.\n")
        asks = 0
        loop = asyncio.get_running_loop()
        lines: asyncio.Queue = asyncio.Queue()

        def read():
            """Typed lines, from a daemon thread: an input() still waiting when Jarvis goes away
            mustn't keep this process alive (asyncio.to_thread's threads would)."""
            while True:
                try:
                    line = input("")
                except (EOFError, KeyboardInterrupt):
                    line = None
                with contextlib.suppress(RuntimeError):   # the loop is gone: already detached
                    loop.call_soon_threadsafe(lines.put_nowait, line)
                if line is None or line.strip().lower() in QUIT:
                    return

        async def show():
            with contextlib.suppress(websockets.ConnectionClosed):
                async for raw in ws:
                    line = render(json.loads(raw))
                    if line:
                        print(line, flush=True)

        threading.Thread(target=read, daemon=True).start()
        shower = asyncio.create_task(show())
        try:
            while True:
                typed = asyncio.create_task(lines.get())
                await asyncio.wait({typed, shower}, return_when=asyncio.FIRST_COMPLETED)
                if not typed.done():   # the core closed the socket (stopped or restarted)
                    typed.cancel()
                    print("Jarvis closed the connection.")
                    return 0
                line = typed.result()
                if line is None or line.strip().lower() in QUIT:
                    break
                if line.strip():
                    asks += 1
                    await ws.send(json.dumps({"type": "ask", "text": line.strip(), "id": f"t{asks}"}))
        except websockets.ConnectionClosed:
            print("Jarvis closed the connection.")
            return 0
        finally:
            shower.cancel()
    print("Detached; Jarvis is still running.")
    return 0


def attach(port: int, name: str) -> int:
    try:
        return asyncio.run(run(port, name))
    except KeyboardInterrupt:
        print("\nDetached; Jarvis is still running.")
        return 0

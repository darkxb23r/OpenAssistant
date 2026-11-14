"""Model tiers (never Fable), the Codex-to-Claude fallback, and starting the Caveman proxy."""
import asyncio, os, socket, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
os.environ["JARVIS_CODEX"] = "1"           # the Codex fallback checks below need Codex on
sys.path.insert(0, str(ROOT))
import jarvis as j
import tasks as t

# --- models: only Opus, Sonnet and Haiku, ever ---
assert j.MODELS == ("opus", "sonnet", "haiku") and j.CHAT_MODEL in j.MODELS
assert all(a.model in j.MODELS for a in j.AGENTS.values()), {n: a.model for n, a in j.AGENTS.items()}
assert j.AGENTS["architect"].model == "opus" and j.AGENTS["sysadmin"].model == "haiku"
for name in ("guardrails.py", "jarvis.py", "tasks.py", "updater.py", "agency.py"):
    code = (ROOT / name).read_text(encoding="utf-8").lower()
    assert "fable" not in code.replace("never fable", ""), name
assert j.worker_options(None, "opus").model == "opus" and j.worker_options(None).model == "sonnet"
try:
    j.worker_options(None, "fable")
    raise AssertionError("fable must be refused")
except ValueError:
    pass
assert [t.Step(1, x).model for x in ["[opus] Design it", "[Haiku] list files", "Plain step", "[fable] x"]] == \
    ["opus", "haiku", "sonnet", "sonnet"]

# --- Codex out of usage: say so, stop trying for a while, and the coder uses Claude ---
fake_bin = home / "bin"
fake_bin.mkdir()
(fake_bin / "codex.cmd").write_text(
    "@echo off\r\n"
    "if exist \"%~dp0limit\" (echo {\"type\":\"error\",\"message\":\"You've hit your usage limit. Try again later.\"} & exit /b 1)\r\n"
    "echo {\"type\":\"error\",\"message\":\"something else broke\"} & exit /b 2\r\n")
# The same fake for Linux and macOS, so the test never reaches the real Codex there.
(fake_bin / "codex").write_text(
    "#!/bin/sh\n"
    "if [ -e \"$(dirname \"$0\")/limit\" ]; then echo '{\"type\":\"error\",\"message\":\"You'\\''ve hit your usage limit. Try again later.\"}'; exit 1; fi\n"
    "echo '{\"type\":\"error\",\"message\":\"something else broke\"}'; exit 2\n")
(fake_bin / "codex").chmod(0o755)
os.environ["PATH"] = f"{fake_bin}{os.pathsep}{os.environ['PATH']}"
(fake_bin / "limit").write_text("x")


async def codex_checks():
    r = await j.run_codex("write a script")
    assert r.get("is_error") and r["content"][0]["text"].startswith("CODEX_UNAVAILABLE"), r
    (fake_bin / "limit").unlink()                 # even if Codex recovers, the pause holds for a while
    r = await j.run_codex("write another")
    assert "try it again in about" in r["content"][0]["text"], r
    j._codex_paused_until = 0.0                   # once the pause is over, other failures are plain failures
    r = await j.run_codex("third")
    assert r.get("is_error") and "Codex failed (exit 2)" in r["content"][0]["text"], r


asyncio.run(codex_checks())
coder = j.AGENTS["coder"]
assert "CODEX_UNAVAILABLE" in coder.prompt and {"Write", "Edit"} <= set(coder.tools)

# --- Caveman: start the proxy when it isn't listening; leave it alone when it is ---
j.CAVEMAN_MCP = home / "caveman-mcp.exe"
j.CAVEMAN_MCP.write_text("x")
marker = home / "started.txt"
(fake_bin / "caveman.cmd").write_text(f"@echo off\r\necho %* > \"{marker}\"\r\n")
(fake_bin / "caveman").write_text(f"#!/bin/sh\necho \"$@\" > \"{marker}\"\n")
(fake_bin / "caveman").chmod(0o755)
with socket.socket() as probe:
    probe.bind(("127.0.0.1", 0))
    free_port = probe.getsockname()[1]
j.CAVEMAN_PROXY = ("127.0.0.1", free_port)
assert j.detect_caveman(start_timeout_s=3) is False and j.caveman_env() == {}
for _ in range(40):
    if marker.exists():
        break
    asyncio.run(asyncio.sleep(0.1))
assert marker.read_text().strip() == "start", "Jarvis should run `caveman start`"
marker.unlink()
with socket.socket() as listener:                 # a proxy that's already up
    listener.bind(("127.0.0.1", 0)); listener.listen()
    j.CAVEMAN_PROXY = listener.getsockname()
    assert j.detect_caveman() is True and j.caveman_env()["ANTHROPIC_BASE_URL"].endswith("/w/jarvis")
    assert "caveman" in j.caveman_servers()
asyncio.run(asyncio.sleep(0.5))
assert not marker.exists(), "a running proxy must not be started again"

# --- restarting himself: asked for, after turns that can't reach Claude, or a hung turn ---
import updater, guardrails
from types import SimpleNamespace as NS
assert "mcp__updates__restart_yourself" in guardrails.AUTO_APPROVED
result = asyncio.run(updater.restart_tool.handler({"reason": "the user asked"}))
assert updater.restart_requested and updater.restart_reason == "the user asked" and "Restarting" in result["content"][0]["text"]
updater.restart_requested, updater.restart_reason = False, ""
turn = lambda err, text="": NS(is_error=err, errors=None, result=text)
h = j.Health()
h.turn_ended(turn(True, "API Error: Connection error. (ECONNREFUSED)"))
assert h.failed_turns == 1 and not updater.restart_requested
h.turn_ended(turn(False))
assert h.failed_turns == 0                                      # one that worked resets the count
h.turn_ended(turn(True, "Request interrupted by user"))         # not a connection failure: not counted
h.turn_ended(turn(True, "connect ECONNREFUSED 127.0.0.1:8787"))
assert h.failed_turns == 1 and not updater.restart_requested
h.turn_ended(turn(True, "fetch failed"))
assert updater.restart_requested and "couldn't reach Claude" in updater.restart_reason
updater.restart_requested, updater.restart_reason = False, ""


class Idle:
    def __init__(self, busy): self.busy = busy
    def is_set(self): return not self.busy


j._io = NS(idle=Idle(True))
h.last_heard -= j.STUCK_S + 1
assert h.stuck()
guardrails._pending_questions.append(object())                  # waiting on the user is not stuck
assert not h.stuck()
guardrails._pending_questions.clear()
j._io = NS(idle=Idle(False))
assert not h.stuck()                                            # no turn running
print("ALL OK")

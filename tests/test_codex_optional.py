"""Codex is optional: off unless JARVIS_CODEX is on. Run as `test_codex_optional.py off|on`."""
import os, shutil, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

mode = sys.argv[1] if len(sys.argv) > 1 else "off"
home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
os.environ.pop("JARVIS_CODEX", None)
if mode == "on":
    # From secrets.env, the way a user turns it on.
    (home / ".jarvis").mkdir()
    (home / ".jarvis" / "secrets.env").write_text("JARVIS_CODEX=1\n", encoding="utf-8")
sys.path.insert(0, str(ROOT))
import jarvis as j

coder = j.AGENTS["coder"]
codex_tools = {"mcp__codex__codex", "mcp__codex__codex-reply"}
assert {"Write", "Edit", *j.SHELL_TOOLS} <= set(coder.tools)   # Bash, plus PowerShell on Windows
if mode == "off":
    assert j.CODEX_ON is False
    assert "codex" not in j.MCP_SERVERS
    assert not codex_tools & set(coder.tools), coder.tools
    assert "codex" not in coder.prompt.lower(), coder.prompt
    assert "codex" not in j.WORKER_PROMPT.lower().replace(".codex", "")
    assert "codex" not in j.worker_options(None).mcp_servers
    assert j.codex_problem() is None
    # The environment beats secrets.env, both ways.
    os.environ["JARVIS_CODEX"] = "yes"
    assert j.codex_wanted() is True
else:
    assert j.CODEX_ON is True
    assert "codex" in j.MCP_SERVERS and "codex" in j.worker_options(None).mcp_servers
    assert codex_tools <= set(coder.tools) and "CODEX_UNAVAILABLE" in coder.prompt
    os.environ["JARVIS_CODEX"] = "0"
    assert j.codex_wanted() is False
    # Switched on without the CLI: startup refuses, with a message saying what to do.
    os.environ["PATH"] = str(home / "empty")
    assert shutil.which("codex") is None
    assert "codex login" in j.codex_problem()
print("ALL OK")

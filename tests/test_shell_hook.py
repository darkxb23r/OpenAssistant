"""Shell commands Claude Code would approve by itself still ask when they reach a secret folder."""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import jarvis as j
import guardrails as g

shell = [m for m in j.GUARD_HOOKS["PreToolUse"] if m.matcher == "Bash|PowerShell"]
assert shell and g.guard_shell in shell[0].hooks
for options in (j.build_options(True), j.worker_options(None)):
    assert options.hooks is j.GUARD_HOOKS


async def verdict(tool, cmd):
    out = await g.guard_shell({"tool_name": tool, "tool_input": {"command": cmd}}, None, None)
    return out.get("hookSpecificOutput", {}).get("permissionDecision", "pass")


async def main():
    project = home / "ws" / "proj"
    for tool, cmd in [("Bash", f"cat {home}/.ssh/jarvis_ed25519"), ("Bash", "cat ~/.jarvis/secrets.env"),
                      ("PowerShell", "Get-Content ~/.jarvis/secrets.env"), ("Bash", "ls ~/.claude"),
                      ("Bash", "head -c 100 $HOME/.caveman/ccr.db"), ("Bash", "cat .ssh/id_ed25519"),
                      ("Bash", "shutdown -h now")]:
        assert await verdict(tool, cmd) == "ask", cmd
    for tool, cmd in [("Bash", "ls"), ("Bash", "git status"), ("PowerShell", "Get-Process"),
                      ("Bash", f"cat {project}/.claude/settings.json")]:
        assert await verdict(tool, cmd) == "pass", cmd


asyncio.run(main())
print("ALL OK")

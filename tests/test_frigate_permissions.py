"""Frigate MCP: only read tools are allowed; every other tool is denied, even in trust mode."""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]

home = tempfile.mkdtemp()
os.environ["USERPROFILE"] = home; os.environ["HOME"] = home
os.environ["JARVIS_WORKSPACE"] = os.path.join(home, "ws")
sys.path.insert(0, str(ROOT))
import jarvis as j
import guardrails as g
import mcp_launch

ALL = (Path(__file__).parent / "frigate_tools.txt").read_text().split()
assert len(ALL) == 90 and set(g.FRIGATE_AUTO) <= set(ALL)
WRITE_WORDS = ("restart", "set_", "save_", "create_", "update_", "delete_", "end_", "mark", "rename",
               "reassign", "reindex", "reprocess", "regenerate", "retain", "start_", "cancel_", "transcribe",
               "unmark", "summarize")
for n in g.FRIGATE_AUTO:
    assert n.startswith(("get_", "search_", "explore_")) and not any(w in n for w in WRITE_WORDS), n
assert "get_config_raw" not in g.FRIGATE_AUTO
for n in ALL:
    if any(w in n for w in WRITE_WORDS):
        assert n not in g.FRIGATE_AUTO and f"mcp__frigate__{n}" not in g.AUTO_APPROVED, n
for n in g.FRIGATE_AUTO:
    assert f"mcp__frigate__{n}" in g.AUTO_APPROVED
for n in ("restart_frigate", "save_config", "set_config", "delete_events", "create_export", "brand_new_tool"):
    assert f"mcp__frigate__{n}" not in g.AUTO_APPROVED


class IO:
    def ask(self, s, d): pass
    def say(self, t): pass


g._io = IO()


async def main():
    g.set_trust(60)
    for n in set(ALL) - set(g.FRIGATE_AUTO):
        r = await g.decide(f"mcp__frigate__{n}", {}, None)
        assert type(r).__name__ == "PermissionResultDeny", n
    r = await g.decide("mcp__frigate__get_events", {}, None)
    assert type(r).__name__ != "PermissionResultDeny"

asyncio.run(main())

# Launcher: missing venv is skipped, not a crash.
real = mcp_launch.SERVERS["frigate"]["requires"]
mcp_launch.SERVERS["frigate"]["requires"] = "/nonexistent/python"
assert mcp_launch.configured("frigate") is False
sys.argv = ["mcp_launch.py", "frigate"]
assert mcp_launch.main() == 1
mcp_launch.SERVERS["frigate"]["requires"] = real
assert mcp_launch.SERVERS["frigate"]["cwd"] == tempfile.gettempdir()   # /tmp on Linux, %TEMP% on Windows
if Path(real).exists():
    assert "frigate" in j.MCP_SERVERS
print("ALL OK")

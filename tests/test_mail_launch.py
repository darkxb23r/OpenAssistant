import json, os, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

project = ROOT
# Needs the mail server's own venv, installed under ~/.jarvis/mcp (see CLAUDE.md).
if not (Path.home() / ".jarvis" / "mcp" / "mailcow").exists():
    print("SKIP mail server not installed")
    sys.exit(0)
tmp = Path(tempfile.mkdtemp())
secrets = tmp / "secrets.env"
secrets.write_text("MAIL_HOST=mail.example.com\nMAIL_USER=dummy@example.com\nMAIL_PASSWORD='not-a-real-password'\n"
                   "DAV_URL=https://mail.example.com:8443/SOGo/dav/\n")
os.environ["MCP_MAILCOW_TLS_VERIFY"] = "nope"   # must be stripped by the launcher
boot = (f"import sys; sys.path.insert(0, {str(project)!r}); import mcp_launch; from pathlib import Path; "
        f"mcp_launch.SECRETS_FILE = Path({str(secrets)!r}); sys.argv = ['mcp_launch.py', 'mail']; sys.exit(mcp_launch.main())")
proc = subprocess.Popen([sys.executable, "-I", "-c", boot], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, text=True)


def rpc(msg):
    proc.stdin.write(json.dumps(msg) + "\n"); proc.stdin.flush()
    if "id" in msg:
        return json.loads(proc.stdout.readline())


rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize",
     "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}})
rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
tools = sorted(t["name"] for t in rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"])
proc.stdin.close(); proc.wait(timeout=10)
print("mail tools:", tools)

sys.path.insert(0, str(project))
home = tempfile.mkdtemp()
os.environ["USERPROFILE"] = home; os.environ["HOME"] = home; os.environ["JARVIS_WORKSPACE"] = os.path.join(home, "ws")
import jarvis as j
import guardrails as g
covered = set(g.MAIL_AUTO) | {n.removeprefix("mcp__mail__") for n in g.CONFIRM_TOOLS if n.startswith("mcp__mail__")}
assert set(tools) == covered, ("unclassified", set(tools) - covered, "unknown", covered - set(tools))

print("ALL OK")

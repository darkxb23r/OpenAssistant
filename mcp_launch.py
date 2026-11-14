#!/usr/bin/env python3
"""
Starts one of Jarvis's outside MCP servers, with its settings and secrets read from
~/.jarvis/secrets.env.

The secrets never go into Jarvis's MCP config: the SDK passes that on a command line, which any
local process can list, Jarvis's own shell commands included. This launcher reads them itself,
hands each server only the keys it needs, and passes stdin/stdout straight through.

Usage (from jarvis.py's MCP_SERVERS): python mcp_launch.py mail|calendar
"""
import os
import subprocess
import sys
from pathlib import Path

JARVIS_HOME = Path.home() / ".jarvis"
SECRETS_FILE = JARVIS_HOME / "secrets.env"
MCP_DIR = JARVIS_HOME / "mcp"
VENV_PYTHON = MCP_DIR / "mailcow" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

# For each server: its command, and its environment as {server variable: secrets.env key}.
SERVERS = {
    # mcp-mailcow 1.0.3 (reviewed), in its own venv because it needs mcp<2. User mode: one mailbox.
    "mail": {
        "command": [str(VENV_PYTHON), "-I", "-m", "mcp_mailcow", "--mode", "user"],
        "env": {"MAILCOW_HOST": "MAIL_HOST", "MAILCOW_MAIL_USER": "MAIL_USER", "MAILCOW_MAIL_PASS": "MAIL_PASSWORD"},
        # It logs every message it sends, attachments included, so the log stays in ~/.jarvis.
        "fixed": {"MCP_MAILCOW_AUDIT_LOG": str(JARVIS_HOME / "logs" / "mail-audit.log")},
    },
    # mcp-twake-dav 4.2.1 (reviewed), installed with --ignore-scripts. Jarvis's own permission
    # check confirms every write, so the server's built-in prompt (which needs MCP elicitation)
    # is switched off.
    "calendar": {
        "command": ["node", str(MCP_DIR / "twake-dav" / "node_modules" / "mcp-twake-dav" / "build" / "index.js")],
        "env": {"DAV_URL": "DAV_URL", "DAV_USERNAME": "MAIL_USER", "DAV_PASSWORD": "MAIL_PASSWORD"},
        "fixed": {"DAV_APPROVAL": "client", "LOG_LEVEL": "info"},
    },
}
# Settings that would quietly switch off certificate checks; never passed on.
UNSAFE = ("MCP_MAILCOW_TLS_VERIFY", "NODE_TLS_REJECT_UNAUTHORIZED")


def read_secrets() -> dict[str, str]:
    values = {}
    if SECRETS_FILE.exists():
        for line in SECRETS_FILE.read_text(encoding="utf-8").splitlines():
            key, sep, value = line.strip().partition("=")
            if sep and key and not key.startswith("#"):
                values[key.strip()] = value.strip().strip("'\"")
    return values


def configured(server: str) -> bool:
    """True when secrets.env has every value this server needs."""
    secrets = read_secrets()
    return all(secrets.get(key) for key in SERVERS[server]["env"].values())


def main() -> int:
    server = SERVERS.get(sys.argv[1] if len(sys.argv) > 1 else "")
    if not server:
        print(f"usage: mcp_launch.py {'|'.join(SERVERS)}", file=sys.stderr)
        return 2
    secrets = read_secrets()
    missing = [key for key in server["env"].values() if not secrets.get(key)]
    if missing:
        print(f"mcp_launch: {SECRETS_FILE} is missing {', '.join(missing)}", file=sys.stderr)
        return 1
    env = {k: v for k, v in os.environ.items() if k not in UNSAFE}
    env |= {var: secrets[key] for var, key in server["env"].items()} | server["fixed"]
    (JARVIS_HOME / "logs").mkdir(exist_ok=True)
    return subprocess.call(server["command"], env=env)


if __name__ == "__main__":
    sys.exit(main())

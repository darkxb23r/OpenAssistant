"""
Controlling a running Jarvis from this PC: what `jarvisctl` (jarvisctl.py) talks to.

The core writes a fresh random token to ~/.jarvis/control.token at every start. ~/.jarvis is a
secret folder (guardrails.SECRET_DIRS), so Jarvis's own tools can never read it; whoever can is the
user at this PC (or over SSH). Requests to /control/<command> on the interface's server must carry
it (Authorization: Bearer ...), on top of the server's usual checks (127.0.0.1 only, a loopback Host),
and must not come from a browser page (no Origin header): this is for jarvisctl, not for pages.

Standing orders (agency) can be paused with a file, ~/.jarvis/agency/paused, so `jarvisctl pause`
works even when the core is down or stuck.

Only the standard library here: jarvisctl imports it too.
"""
import hmac
import os
import secrets
from pathlib import Path

JARVIS_HOME = Path.home() / ".jarvis"
TOKEN_FILE = JARVIS_HOME / "control.token"
PAUSED_FILE = JARVIS_HOME / "agency" / "paused"


def new_token() -> str:
    """A fresh token for this run, readable only by the user where the OS supports it."""
    JARVIS_HOME.mkdir(exist_ok=True)
    token = secrets.token_urlsafe(32)
    tmp = TOKEN_FILE.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token)
    os.replace(tmp, TOKEN_FILE)
    return token


def read_token() -> str | None:
    try:
        return TOKEN_FILE.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def authorized(header: str | None, token: str) -> bool:
    return bool(header and token) and hmac.compare_digest(header.strip(), f"Bearer {token}")


def paused() -> bool:
    return PAUSED_FILE.exists()


def set_paused(on: bool) -> None:
    if on:
        PAUSED_FILE.parent.mkdir(parents=True, exist_ok=True)
        PAUSED_FILE.write_text("paused by the user\n", encoding="utf-8")
    else:
        PAUSED_FILE.unlink(missing_ok=True)


class Control:
    """The commands a running core answers, registered by jarvis.main: name -> async fn(args) -> dict."""

    def __init__(self, token: str):
        self.token = token
        self.commands: dict = {}

    def command(self, name: str):
        def register(fn):
            self.commands[name] = fn
            return fn
        return register

    async def handle(self, name: str, args: dict) -> tuple[int, dict]:
        fn = self.commands.get(name)
        if fn is None:
            return 404, {"error": f"no command {name!r}", "commands": sorted(self.commands)}
        try:
            return 200, await fn(args)
        except ValueError as e:
            return 400, {"error": str(e)}

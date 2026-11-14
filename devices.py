"""
Paired devices: which browsers, terminals and satellites may connect to the core.

Until the first device is paired, the core is in open mode, as before: any page of the interface on
this PC can connect (web.py's Origin and Host checks still apply). Once one is paired, every client
has to present a token in its hello (protocol.py), and pictures and speech need a media ticket.

- Pairing: `jarvisctl pair --name laptop` makes a one-time code (6 digits, 10 minutes, 5 tries).
  The browser gets it in the URL's #fragment (which never reaches the server or its logs), sends it
  in its hello, and is given a token it keeps. Satellites and terminals pass it with --pair.
- Tokens: 32 random bytes; only their sha256 is stored, in ~/.jarvis/devices.json (a folder
  Jarvis's own tools can't read or write, so Jarvis can't mint itself a device).
- Media tickets: <img> and <video> can't send headers, so /file, /img and /media take a short-lived
  ticket in the URL instead of the token: bound to the device, valid 12 hours, replaced at every
  hello and every `ticket` frame (the old one keeps working for 5 more minutes; tickets the
  device's other open sockets hold aren't touched). The face renews its own well before 12 hours.
"""
import hashlib
import hmac
import json
import logging
import secrets
import time
from pathlib import Path

log = logging.getLogger("jarvis.web")

JARVIS_HOME = Path.home() / ".jarvis"
DEVICES_FILE = JARVIS_HOME / "devices.json"
CODE_MINUTES, CODE_TRIES = 10, 5
TICKET_S, TICKET_GRACE_S = 12 * 3600, 5 * 60


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Devices:
    def __init__(self, path: Path = DEVICES_FILE):
        self.path = path
        self._codes: dict[str, dict] = {}      # code -> {name, kind, expires, tries}
        self._tickets: dict[str, dict] = {}    # ticket -> {device, expires}
        self._attempts = 0                      # failed pairing attempts since the last code

    # --- the file ---
    def _load(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return data if isinstance(data, list) else []
        except (OSError, ValueError):
            return []

    def _save(self, devices: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(devices, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def listed(self) -> list[dict]:
        return [{k: v for k, v in d.items() if k != "token_sha256"} for d in self._load()]

    @property
    def enforced(self) -> bool:
        """Any device paired (and not revoked): from then on every client needs a token."""
        return any(not d.get("revoked") for d in self._load())

    # --- pairing ---
    def new_code(self, name: str, kind: str = "face") -> str:
        code = f"{secrets.randbelow(10**6):06d}"
        self._codes = {code: {"name": name[:40], "kind": kind, "expires": time.time() + CODE_MINUTES * 60,
                              "tries": 0}}   # one code at a time: a new one replaces the old
        return code

    def pair(self, code: str, kind: str, name: str | None = None) -> tuple[dict, str] | None:
        """A device and its new token, or None for a wrong, used or expired code. It's called what
        `jarvisctl pair --name` said, else what the client calls itself."""
        now = time.time()
        pending = next(iter(self._codes.values()), None)
        if pending is None or pending["expires"] < now:
            self._codes.clear()
            return None
        if not hmac.compare_digest(code.encode(), next(iter(self._codes)).encode()):
            pending["tries"] += 1
            if pending["tries"] >= CODE_TRIES:   # guessing: the code is gone
                self._codes.clear()
            return None
        self._codes.clear()
        token = secrets.token_urlsafe(32)
        # Labelled with the kind that really paired; a code made for another kind says so in the log.
        if pending.get("kind") and pending["kind"] != kind:
            log.warning("a code made for a %s was used by a %s", pending["kind"], kind)
        device = {"id": f"d{secrets.token_hex(4)}", "name": (pending["name"] or name or "device")[:40], "kind": kind,
                  "token_sha256": _hash(token), "created": time.strftime("%Y-%m-%d %H:%M"), "last_seen": None,
                  "revoked": False}
        devices = self._load()
        devices.append(device)
        self._save(devices)
        return {k: v for k, v in device.items() if k != "token_sha256"}, token

    # --- tokens ---
    def check(self, token: str | None) -> dict | None:
        """The device this token belongs to (not revoked), or None."""
        if not token:
            return None
        wanted = _hash(token)
        devices = self._load()
        for d in devices:
            if not d.get("revoked") and hmac.compare_digest(d.get("token_sha256", ""), wanted):
                d["last_seen"] = time.strftime("%Y-%m-%d %H:%M")
                self._save(devices)
                return {k: v for k, v in d.items() if k != "token_sha256"}
        return None

    def revoke(self, device_id: str) -> bool:
        devices = self._load()
        found = False
        for d in devices:
            if d["id"] == device_id and not d.get("revoked"):
                d["revoked"] = found = True
        if found:
            self._save(devices)
            self._tickets = {t: v for t, v in self._tickets.items() if v["device"] != device_id}
        return found

    def reset(self) -> None:
        """Forget every device: back to open mode."""
        self._save([])
        self._tickets.clear()
        self._codes.clear()

    # --- media tickets ---
    def ticket(self, device_id: str, keep=()) -> str:
        """A new ticket for the device; its older ones expire in TICKET_GRACE_S, except those in
        `keep` (held by the device's other open sockets: a second tab mustn't cut off the first)."""
        now = time.time()
        for held, t in self._tickets.items():
            if t["device"] == device_id and held not in keep:
                t["expires"] = min(t["expires"], now + TICKET_GRACE_S)
        self._tickets = {t: v for t, v in self._tickets.items() if v["expires"] > now}
        ticket = secrets.token_urlsafe(24)
        self._tickets[ticket] = {"device": device_id, "expires": now + TICKET_S}
        return ticket

    def ticket_ok(self, ticket: str | None) -> bool:
        if not ticket:
            return False
        held = self._tickets.get(ticket)
        return bool(held) and held["expires"] > time.time()

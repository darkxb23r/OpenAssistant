"""
The interface's Settings panel: ~/.jarvis/secrets.env, edited from the browser.

Secrets are input only. For a secret (keys, passwords, tokens, the PIN, phone numbers) the page is
only ever told whether it's set: never its value, nor any part of it. It can be replaced or
cleared, never read back. Plain settings (the name, the voice, the mail host) are shown.

Only the keys below can be written, each checked before it is: one line, no control characters,
and a shape that fits (a phone number in +E.164, a PIN of digits, an https URL...). Nothing that
widens Jarvis's powers lives in secrets.env (JARVIS_CONTAINED, JARVIS_WORKSPACE and the port are
environment-only), so nothing here can either. The model settings (provider.py) choose where the
conversation is sent, so they too are only for whoever may change these. The file is rewritten atomically, its other lines
and comments kept, readable by the user only; the log names the keys changed, never the values.

Who may: a face (web.py) the user is at, trust "physical" or "device" (the same as answering a
hard question). Not satellites, texts, calls or terminals. Jarvis's own tools can't read or write
~/.jarvis, so the model can't change these. Most take effect at the next start, which the panel
offers.
"""
import ipaddress
import logging
import os
import re
from pathlib import Path

import identity
import mcp_launch
import provider

log = logging.getLogger("jarvis")

HOSTNAME = re.compile(r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(?:\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$")
PHONE = re.compile(r"^\+[1-9]\d{6,14}$")
TOKEN = re.compile(r"^[\x21-\x7e]{8,300}$")   # printable, no spaces


def _host(v):
    try:
        ipaddress.ip_address(v)
        return None
    except ValueError:
        return None if HOSTNAME.match(v) else "a host name, like mail.example.com"


def _https_url(v):
    return None if re.match(r"^https://[^\s/?#]+[^\s]*$", v) else "an https:// address"


def _pattern(rx, what):
    return lambda v: None if re.match(rx, v) else what


# key -> (group, label, secret, check, help). Order is the panel's.
FIELDS = {
    "JARVIS_NAME": ("Identity", "Name", False,
                    lambda v: None if identity.VALID_NAME.match(v) else "1-32 letters, digits, spaces or . ' -",
                    "What he's called. Empty: Jarvis."),
    "JARVIS_NAME_MEANING": ("Identity", "What the name means", False,
                            lambda v: None if len(v) <= identity.MEANING_MAX else f"{identity.MEANING_MAX} characters at most",
                            "Shown under the name, and told to him."),
    "ELEVENLABS_API_KEY": ("Voice", "ElevenLabs API key", True, _pattern(TOKEN.pattern, "an API key"),
                           "Without one he uses the local Kokoro voice."),
    "ELEVENLABS_VOICE_ID": ("Voice", "ElevenLabs voice ID", False,
                            _pattern(r"^[A-Za-z0-9]{10,40}$", "a voice ID (letters and digits)"), ""),
    "ELEVENLABS_MODEL": ("Voice", "ElevenLabs model", False,
                         _pattern(r"^[a-z0-9_]{3,60}$", "a model ID, like eleven_flash_v2_5"),
                         "eleven_flash_v2_5 is fastest."),
    "MAIL_HOST": ("Mail and calendar", "Mail server", False, _host, ""),
    "MAIL_USER": ("Mail and calendar", "Mailbox", False,
                  _pattern(r"^[^\s@]{1,64}@[^\s@]{1,253}$", "an email address"), "Jarvis's own mailbox."),
    "MAIL_PASSWORD": ("Mail and calendar", "Mailbox app password", True,
                      lambda v: None if 1 <= len(v) <= 300 else "a password", ""),
    "DAV_URL": ("Mail and calendar", "Calendar (CalDAV) address", False, _https_url, ""),
    "TWILIO_ACCOUNT_SID": ("Phone", "Twilio account SID", True, _pattern(r"^AC[0-9a-fA-F]{32}$", "AC and 32 hex digits"), ""),
    "TWILIO_AUTH_TOKEN": ("Phone", "Twilio auth token", True, _pattern(r"^[0-9a-fA-F]{32}$", "32 hex digits"), ""),
    "TWILIO_NUMBER": ("Phone", "Jarvis's phone number", True, _pattern(PHONE.pattern, "+ and the full number"), ""),
    "MY_PHONE_NUMBER": ("Phone", "Your phone number", True, _pattern(PHONE.pattern, "+ and the full number"),
                        "Only this number can text or call him."),
    "JARVIS_CALL_PIN": ("Phone", "Call PIN", True, _pattern(r"^\d{4,8}$", "4 to 8 digits"),
                        "Asked for on calls in."),
    "FRIGATE_URL": ("Cameras", "Frigate address", False,
                    _pattern(r"^https?://[^/\s?#@]+$", "an address like http://192.168.1.20:5000 (no path)"),
                    "Your Frigate NVR, for live cameras. Restart to apply."),
    "JARVIS_CODEX": ("Coding", "Codex", False, _pattern(r"^[01]$", "on or off"),
                     "The coder uses Codex (needs `codex login`)."),
    "MODEL_PROVIDER": ("Models", "Language models", False, _pattern(r"^(claude|ollama)$", "claude or ollama"),
                       "claude: your Claude Code login. ollama: local models through Ollama 0.14 or newer."),
    "OLLAMA_URL": ("Models", "Ollama address", False,
                   lambda v: None if provider.URL.match(v) else "an http:// or https:// address, like http://localhost:11434",
                   "Empty: http://localhost:11434."),
    "OLLAMA_MODEL": ("Models", "Main model", False,
                     lambda v: None if provider.MODEL_NAME.match(v) else "an Ollama model name, like qwen3-coder",
                     "Conversation and most work. Needs good tool calling, and Ollama run with a 64k+ context (OLLAMA_CONTEXT_LENGTH=65536)."),
    "OLLAMA_FAST_MODEL": ("Models", "Fast model", False,
                          lambda v: None if provider.MODEL_NAME.match(v) else "an Ollama model name",
                          "Quick jobs and the safety checks. Empty: the main model."),
    "OLLAMA_BIG_MODEL": ("Models", "Big model", False,
                         lambda v: None if provider.MODEL_NAME.match(v) else "an Ollama model name",
                         "Planning and hard problems. Empty: the main model."),
}
MAX_VALUE = identity.MEANING_MAX   # the longest any setting may be


def _file() -> Path:
    return mcp_launch.SECRETS_FILE


def listing() -> list[dict]:
    """What the panel shows. A secret's value is never included, only whether it's set."""
    values = mcp_launch.read_secrets()
    out = []
    for key, (group, label, secret, _, help_) in FIELDS.items():
        field = {"key": key, "group": group, "label": label, "secret": secret, "help": help_,
                 "set": bool(values.get(key)), "environment": key in os.environ}
        if not secret:
            field["value"] = values.get(key, "")
        out.append(field)
    return out


def check(changes: dict) -> dict[str, str]:
    """Problems with the changes, by key ({} when they can all be saved). None clears a key."""
    if not isinstance(changes, dict) or len(changes) > len(FIELDS):
        return {"": "Nothing to save."}
    problems = {}
    for key, value in changes.items():
        if key not in FIELDS:
            problems[key] = "Not a setting."
        elif value is None or value == "":   # clearing it
            continue
        elif not isinstance(value, str) or re.search(r"[\x00-\x1f\x7f]", value):
            problems[key] = "Should be one line of text."
        elif len(value) > MAX_VALUE:
            problems[key] = f"Should be {MAX_VALUE} characters at most."
        elif value.strip() != value or (value[:1] in "'\"" and value[-1:] == value[:1]):
            problems[key] = "No spaces or quotes around it."
        elif value and (why := FIELDS[key][3](value)):
            problems[key] = f"Should be {why}."
    return problems


def save(changes: dict) -> dict:
    """Write the changes (None or "" clears a key) and return {"saved": [...]}, or {"errors": {...}}
    with nothing written."""
    problems = check(changes)
    if problems:
        return {"errors": problems}
    path = _file()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    pending = {k: (v or None) for k, v in changes.items()}
    out = []
    for line in lines:
        key, sep, _ = line.strip().partition("=")
        key = key.strip()
        if sep and key in pending and not key.startswith("#"):
            value = pending.pop(key)
            if value is not None:
                out.append(f"{key}={value}")
            continue                      # cleared: the line goes
        out.append(line)
    out += [f"{k}={v}" for k, v in pending.items() if v is not None]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write("".join(line + "\n" for line in out))
    if os.name != "nt":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    log.info("settings changed from the interface: %s", ", ".join(sorted(changes)))
    return {"saved": sorted(changes)}

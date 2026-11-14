"""
What the assistant is called, and what the name means: Jarvis ("Just A Rather Very Intelligent
System") unless the user has given it another.

Settings, in the environment or ~/.jarvis/secrets.env (the environment wins), like JARVIS_CODEX:
  JARVIS_NAME           what it's called, e.g. Friday (letters, digits, spaces, . ' -; 32 at most)
  JARVIS_NAME_MEANING   what the name stands for, or why it was chosen (one line, MEANING_MAX at most)

The name goes into the system prompt, the phrases Jarvis answers to (standby, trust mode, the
browser), Whisper's hint words, the phone's greeting, and the interface (its title, the HUD brand
with the meaning under it, the wake word it listens for). "Jarvis" is still answered to, out of
habit. The PC's own --voice mode and satellites keep openWakeWord's "Hey Jarvis": there's no model
for another name.

They live in ~/.jarvis, which Jarvis's own tools can't write: the user names it, the model doesn't.
"""
import os
import re

import mcp_launch

DEFAULT_NAME = "Jarvis"
DEFAULT_MEANING = "Just A Rather Very Intelligent System"
MEANING_MAX = 750
VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 .'\-]{0,31}$")


def _setting(key: str) -> str:
    value = os.environ.get(key)
    if value is None:
        value = mcp_launch.read_secrets().get(key, "")
    return " ".join(value.split())   # one line, single spaces


def load() -> tuple[str, str]:
    """(name, meaning). An unset or unusable name is Jarvis, with Jarvis's meaning; a new name
    without a meaning has none."""
    name = _setting("JARVIS_NAME")
    if not name or not VALID_NAME.match(name) or name.lower() == DEFAULT_NAME.lower():
        return DEFAULT_NAME, _setting("JARVIS_NAME_MEANING")[:MEANING_MAX] or DEFAULT_MEANING
    return name, _setting("JARVIS_NAME_MEANING")[:MEANING_MAX]


NAME, MEANING = load()


def name_pattern() -> str:
    """A regex alternative for being called by name: the name, and Jarvis."""
    names = {NAME.lower(), DEFAULT_NAME.lower()}
    return "(?:" + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True)) + ")"


def meaning_sentence() -> str:
    """For the system prompt, after it says who it is ("" without a meaning)."""
    return f' Your name means "{MEANING}".' if MEANING else ""


def frame() -> dict:
    """For the interface's welcome frame."""
    return {"name": NAME, "meaning": MEANING}

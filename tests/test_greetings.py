"""How a phone call opens (phone.call_greeting, phone.salutation): by the time of day, now and then
by the day or by name, never the same twice running; and the prompt's curiosity when the user is vague."""
import os, random, sys, tempfile
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import phone as p

FRI = 13   # November 13, 2026 is a Friday
at = lambda hour, day=FRI: datetime(2026, 11, day, hour, 30)
assert [p.part_of_day(at(h)) for h in (4, 5, 11, 12, 16, 17, 21, 22)] == \
    ["night", "morning", "morning", "afternoon", "afternoon", "evening", "evening", "night"]
assert p.salutation(at(8)) == "Good morning, sir." and p.salutation(at(19)) == "Good evening, sir."
assert p.salutation(at(23)) == "You're up late, sir."


def all_greetings(when, n=400):
    rng = random.Random(1)
    said = [p.call_greeting(when, rng) for _ in range(n)]
    assert all(a != b for a, b in zip(said, said[1:])), "never the same twice running"
    return set(said)


morning = all_greetings(at(8))
assert "Good morning, sir." in morning and "Jarvis here." in morning and "Happy Friday, sir." in morning
assert not any("evening" in g.lower() or "afternoon" in g.lower() for g in morning)
assert "{" not in "".join(morning)                      # every placeholder filled
night = all_greetings(at(23))
assert "Burning the midnight oil, sir?" in night and not any("Friday" in g for g in night)
assert not any("Friday" in g for g in all_greetings(at(8, day=FRI - 1)))   # Thursday

import jarvis as j
prompt = j.build_options(False).system_prompt
assert "never ask them to clarify" in prompt and "Outgrown the old one" in prompt
j._io = None                                  # no channels yet
assert j.VOICE_NOTE in j.prompt_for("hi") and j.prompt_for("hi").endswith("] hi")
print("ALL OK")

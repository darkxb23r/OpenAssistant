"""identity.py: JARVIS_NAME and JARVIS_NAME_MEANING. Unset, it's Jarvis ("Just A Rather Very
Intelligent System"); set, the name reaches the system prompt, the phrases it answers to (Jarvis
still works), Whisper's hint words, the phone's greeting and the interface's welcome frame."""
import importlib, os, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
os.environ.pop("JARVIS_NAME", None); os.environ.pop("JARVIS_NAME_MEANING", None)
sys.path.insert(0, str(ROOT))
import identity as i

# Unset: Jarvis.
assert (i.NAME, i.MEANING) == ("Jarvis", "Just A Rather Very Intelligent System")
assert i.frame() == {"name": "Jarvis", "meaning": "Just A Rather Very Intelligent System"}

# From ~/.jarvis/secrets.env, one line, the environment winning; a bad name falls back to Jarvis.
(home / ".jarvis").mkdir()
secrets = home / ".jarvis" / "secrets.env"
secrets.write_text('JARVIS_NAME=Friday\nJARVIS_NAME_MEANING="Female Replacement   Intelligent\tDigital Assistant Youth"\n')
assert i.load() == ("Friday", "Female Replacement Intelligent Digital Assistant Youth")
os.environ["JARVIS_NAME"] = "Edith"
assert i.load()[0] == "Edith"
for bad in ("", "{os_name}", "a" * 40, "Ro<b>t"):
    os.environ["JARVIS_NAME"] = bad
    assert i.load()[0] == "Jarvis", bad
os.environ.pop("JARVIS_NAME")
secrets.write_text("JARVIS_NAME=Friday\n")
assert i.load() == ("Friday", "")   # a new name has no meaning until it's given one
secrets.write_text("JARVIS_NAME=Friday\nJARVIS_NAME_MEANING=" + "m" * 800 + "\n")
assert i.MEANING_MAX == 750 and i.load()[1] == "m" * 750   # up to 750 characters kept

# The rest of Jarvis, started with a name (a fresh process: the name is read at import).
secrets.write_text("JARVIS_NAME=Friday\nJARVIS_NAME_MEANING=Chosen {because} it's the day after Thursday\n")
check = r'''
import jarvis as j, speech, phone, identity
assert identity.NAME == "Friday"
prompt = j.build_options(False).system_prompt
assert prompt.startswith("You are Friday, a personal assistant")
assert 'Your name means "Chosen {because} it\'s the day after Thursday".' in prompt   # braces kept as written
assert '"Hey Friday"' in prompt
assert j.standby_request("Okay Friday, stand by") is True and j.standby_request("thanks jarvis, stand by") is True
assert j.browser_request("Friday, show the browser") is True
assert speech.Speech({}).hotwords().startswith("Friday, Jarvis")
print("named ok")
'''
out = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {str(ROOT)!r})\n" + check],
                     capture_output=True, text=True, env=dict(os.environ))
assert "named ok" in out.stdout, out.stderr[-2000:]
print("ALL OK")

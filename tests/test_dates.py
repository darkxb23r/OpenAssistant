from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder
import os, sys, tempfile
sys.path.insert(0, str(ROOT))
import voice as v

cases = {
    "It's Wed, Nov 11.": "It's Wednesday, November 11th.",
    "Today is Wednesday, November 11, 2026.": "Today is Wednesday, November 11th, 2026.",
    "Backup ran on 2026-11-11 at 5:36 PM.": "Backup ran on the 11th of November 2026 at 5:36 PM.",
    "Due 3 Dec, then Sept 22 and Mar. 1st.": "Due 3 December, then September 22nd and March 1st.",
    "Fri the 13th": "Friday the 13th",
    "On Thu Jan 2": "On Thursday January 2nd",
    "Jan and Sue were wed on Sat.": "Jan and Sue were wed on Sat.",   # names and words stay
    "November 2026 and May 5": "November 2026 and May 5th",
    "Meet at 12:30 on May 22": "Meet at 12:30 on May 22nd",
    "Version 1.2-3 of 2026-13-40": "Version 1.2-3 of 2026-13-40",
}
for text, want in cases.items():
    got = v._clean_for_speech(text, [])
    assert got == want, (text, got)

home = tempfile.mkdtemp()
os.environ["USERPROFILE"] = home; os.environ["HOME"] = home
os.environ["JARVIS_WORKSPACE"] = os.path.join(home, "ws")
import jarvis as j
import guardrails as g
print(j.spoken_now())
prompt = j.build_options(True).system_prompt
assert "seasoned butler" in prompt and 'never "Wed"' in prompt and '"next Friday"' in prompt
print("ALL OK")

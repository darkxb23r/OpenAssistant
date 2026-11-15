"""Standby: only the wake word opens the mic; switched by what the user says, kept across restarts."""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import jarvis as j
import voice as v

on = ["Hey Jarvis, standby.", "Stand by", "Go to sleep", "standby mode", "Jarvis, please stand by.",
      # said casually
      "Okay, standby.", "Wait for further instructions.", "Thanks Jarvis, that will be all.", "That's all for now, thanks.",
      "Dismissed.", "Alright, stand by for further instructions.", "Okay Jarvis, go to standby", "stand-by",
      "Thank you, that'll be all.", "You can go now", "I'm done for now", "Nothing else, thanks",
      "Await further instructions", "Hold for further orders", "Go quiet please"]
off = ["Jarvis, wake up.", "I'm back", "Stop standby", "you can listen again", "Okay, back to work"]
neither = ["Can you enhance your standbys?", "stand by me tonight", "what is standby", "wake up the server",
           "that's all I wanted, now restart plex", "wait for the backup to finish", "is that all?", "sleep 5 && reboot",
           "Okay", "Thanks", "I am done with the report, email it"]
assert all(j.standby_request(s) is True for s in on), [s for s in on if j.standby_request(s) is not True]
assert all(j.standby_request(s) is False for s in off), [s for s in off if j.standby_request(s) is not False]
assert all(j.standby_request(s) is None for s in neither), [s for s in neither if j.standby_request(s) is not None]


class IO:
    standby = False
    def say(self, text): self.said = text


j._io = IO()
j.set_standby(True)
assert j._io.standby and j.STANDBY_FILE.exists() and j._io.said == "Standing by."
j.set_standby(False)
assert not j._io.standby and not j.STANDBY_FILE.exists() and j._io.said == "At your service."


# The voice layer: after Jarvis speaks, the mic opens for a follow-up only when not on standby.
class FakeTTS:
    async def create_stream(self, text, voice, lang):
        yield text, 24000


async def finish_line(standby: bool) -> bool:
    voice = v.Voice.__new__(v.Voice)
    voice._path_names, voice._tts, voice.standby = [], FakeTTS(), standby
    voice._speech, voice._audio = asyncio.Queue(), asyncio.Queue(maxsize=4)
    voice._lines, voice._speech_generation, voice._follow_up, voice._in_flight = 0, 0, False, None
    voice._mic_free = asyncio.Event(); voice._mic_free.set()
    v.sd.play, v.sd.wait, v.sd.stop = (lambda *a: None), (lambda: None), (lambda: None)
    tasks = [asyncio.create_task(voice._synthesizer()), asyncio.create_task(voice._player())]
    voice.ask("run a command: x", "detail")
    queued = voice._speech._queue[0][0]
    while voice._lines:
        await asyncio.sleep(0.01)
    for t in tasks:
        t.cancel()
    return voice._follow_up, queued


assert asyncio.run(finish_line(False)) == (True, "May I run a command: x? Yes, no, or always.")
assert asyncio.run(finish_line(True)) == (False, "May I run a command: x? Say Hey Jarvis, then yes, no, or always.")
assert "Standby is built in" in j.build_options(True).system_prompt

# The on-screen reactor: dim ice-blue on standby, previous settings back on wake (web/src/store.ts, App.tsx).
store = (ROOT / "web/src/store.ts").read_text(encoding="utf-8")
app = (ROOT / "web/src/App.tsx").read_text(encoding="utf-8")
assert "color: '#4fd8ff', intensity: 0.3" in store and "spin: 0.4" in store
assert "standbyBackup: { ...s.ui.reactor }" in store and "reactor: { ...s.standbyBackup }" in store
watch = app.split("watchStandby(() => {", 1)[1].split("})", 1)[0]
assert "enterStandbyLook()" in watch
assert app.split("const onWake", 1)[1].split("sfx.play", 1)[0].count("leaveStandbyLook()") == 1
assert app.split("const respond", 1)[1].split("clearIdle()", 1)[0].count("leaveStandbyLook()") == 1
assert (ROOT / "web/test/standby.test.ts").exists()
print("ALL OK")

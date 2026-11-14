from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder
import asyncio, sys, time
sys.path.insert(0, str(ROOT))
import voice as v

names = {"C:\\Users\\you\\jarvis-workspace": "the workspace", "C:\\Users\\you": "home", "~": "home"}
voice = v.Voice.__new__(v.Voice)
voice._path_names = sorted((([p.lower() for p in __import__("re").split(r"[\\/]+", f) if p], n) for f, n in names.items()),
                           key=lambda e: -len(e[0]))
say = lambda t: v._clean_for_speech(t, voice._path_names)
cases = {
    "It's in C:\\Users\\you\\jarvis-workspace.": "It's in the workspace.",
    "Saved to C:\\Users\\you\\jarvis-workspace\\proj\\main.py, done.": "Saved to the workspace, proj, main.py, done.",
    "Look in C:/Users/You/Documents/Projects": "Look in home, Documents, Projects",
    "Free zone: D:\\media\\tv (2 folders)": "Free zone: D drive, media, tv (2 folders)",
    "It's at ~/notes.txt!": "It's at home, notes.txt!",
    "Share \\\\nas\\backups is up.": "Share nas, backups is up.",
    "Config is /etc/nginx/nginx.conf.": "Config is root, etc, nginx, nginx.conf.",
    "C:\\Users\\youstar\\x": "C drive, Users, youstar, x",
    "Use and/or 24/7 at https://x.com/a/b": "Use and/or 24/7 at a link",
}
for text, want in cases.items():
    got = say(text)
    assert got == want, (text, got)


# --- pipelined speaker, with fake synthesis (0.3 s per line) and playback (0.3 s per line) ---
class FakeTTS:
    async def create_stream(self, text, voice, lang):
        await asyncio.sleep(0.3)
        yield text, 24000


played = []


async def main():
    v.sd.play = lambda samples, rate: played.append((round(time.monotonic() - t0, 1), samples))
    v.sd.wait = lambda: time.sleep(0.3)
    v.sd.stop = lambda: None
    voice._tts = FakeTTS()
    voice._speech, voice._audio = asyncio.Queue(), asyncio.Queue(maxsize=4)
    voice._lines, voice._speech_generation, voice._follow_up = 0, 0, False
    voice._mic_free = asyncio.Event(); voice._mic_free.set()
    tasks = [asyncio.create_task(voice._synthesizer()), asyncio.create_task(voice._player())]
    global t0
    t0 = time.monotonic()
    for line in ["One.", "Two.", "Three in C:\\Users\\you\\jarvis-workspace."]:
        voice.say(line)
    assert voice._busy_speaking()
    while voice._lines:
        await asyncio.sleep(0.05)
    total = time.monotonic() - t0
    print("played:", played, f"total {total:.1f}s")
    assert total < 1.5, total            # serial synth+play would take 1.8 s
    assert played[2][1] == "Three in the workspace."
    assert voice._follow_up
    # interrupt: queued lines are printed as not spoken, the counter resets, nothing else plays
    played.clear(); voice._follow_up = False
    for line in ["Alpha.", "Beta.", "Gamma."]:
        voice.say(line)
    await asyncio.sleep(0.45)
    voice._interrupt()
    await asyncio.sleep(1.0)
    print("after interrupt:", played, "lines", voice._lines)
    assert voice._lines == 0 and len(played) <= 1 and not voice._follow_up
    voice.say("Back.")
    while voice._lines:
        await asyncio.sleep(0.05)
    assert played[-1][1] == "Back." and voice._follow_up
    for t in tasks:
        t.cancel()


asyncio.run(main())
print("ALL OK")

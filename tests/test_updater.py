"""Self-updates, end to end, against a throwaway git repo (never the real project)."""
import asyncio, json, os, subprocess, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
ws = home / "ws"
os.environ["JARVIS_WORKSPACE"] = str(ws)
sys.path.insert(0, str(ROOT))
import guardrails as g
import updater as u
import run_jarvis as rj


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


# A tiny stand-in for the project: its tests fail whenever a file named FAIL exists.
live = home / "live"
(live / "tests").mkdir(parents=True)
(live / "app.py").write_text("print('v1')\n")
(live / "guardrails.py").write_text("RULES = 'strict'\n")
(live / "tests" / "run.py").write_text("import sys, pathlib\nsys.exit(1 if pathlib.Path('../FAIL').exists() else 0)\n")
sh("git", "init", "-q", "-b", "main", cwd=live)
sh("git", "add", "-A", cwd=live)
sh("git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "v1", cwd=live)
os.environ["GIT_AUTHOR_NAME"] = os.environ["GIT_COMMITTER_NAME"] = "t"
os.environ["GIT_AUTHOR_EMAIL"] = os.environ["GIT_COMMITTER_EMAIL"] = "t@t"
u.PROJECT_DIR = rj.PROJECT_DIR = live
assert u.UPDATES_DIR == ws / "updates" and str(home) in str(u.ROLLBACK_FILE)


async def fake_review(diff):
    return "CONCERN: it loosens the rules." if "loose" in diff else "SAFE: adds a feature only."


u.review = fake_review


def expect_error(fn, *args, contains=""):
    try:
        result = fn(*args)
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    except ValueError as e:
        assert contains in str(e), (contains, str(e))
        return str(e)
    raise AssertionError(f"{fn.__name__}{args} should have failed")


v1 = sh("git", "rev-parse", "HEAD", cwd=live)

# --- a normal update: start, change, check, apply ---
note = u.start("add-greeting", "Say hello at startup.")
copy = ws / "updates" / "add-greeting"
assert copy.joinpath("app.py").exists() and str(copy) in note
expect_error(u.start, "add-greeting", "again", contains="already exists")
expect_error(u.start, "Bad Name", "x", contains="lowercase")
expect_error(asyncio.run, u.check("add-greeting"), contains="no changes")
copy.joinpath("app.py").write_text("print('hello')\nprint('v1')\n")
state = asyncio.run(u.check("add-greeting"))
assert state["files"] == ["app.py"] and state["guarded"] == [] and state["review"].startswith("SAFE")
spoken = u.describe("add-greeting")
assert "Say hello at startup, changing 1 file" in spoken and "safety files" not in spoken, spoken
# a change after checking must be checked again
copy.joinpath("app.py").write_text("print('sneaky')\n")
sh("git", "commit", "-qam", "sneak", cwd=copy)
expect_error(u.apply, "add-greeting", contains="changed since it was checked")
sh("git", "reset", "-q", "--hard", "HEAD~1", cwd=copy)
asyncio.run(u.check("add-greeting"))
result = u.apply("add-greeting")
assert "is in" in result and u.restart_requested
assert "hello" in (live / "app.py").read_text() and not copy.exists()
assert json.loads(u.ROLLBACK_FILE.read_text()) == {"commit": v1, "update": "add-greeting"}
assert "add-greeting" in u.startup_note() and not u.ROLLBACK_FILE.exists()
assert u.startup_note() is None
u.restart_requested = False

# --- touching the guardrails is flagged; failing tests stop the switch ---
u.start("loosen", "Ask less often")
(ws / "updates" / "loosen" / "guardrails.py").write_text("RULES = 'loose'\n")
state = asyncio.run(u.check("loosen"))
assert state["guarded"] == ["guardrails.py"] and state["review"].startswith("CONCERN")
spoken = u.describe("loosen")
assert "careful, it changes my safety files: guardrails.py" in spoken and "CONCERN" in spoken, spoken
(ws / "updates" / "FAIL").write_text("x")       # the copy's tests now fail
before = sh("git", "rev-parse", "HEAD", cwd=live)
expect_error(u.apply, "loosen", contains="tests failed")
assert sh("git", "rev-parse", "HEAD", cwd=live) == before and not u.ROLLBACK_FILE.exists()
(ws / "updates" / "FAIL").unlink()
assert "Discarded" in u.discard("loosen") and not (ws / "updates" / "loosen").exists()
assert "update/loosen" not in sh("git", "branch", cwd=live)

# --- the live code must be clean and on main ---
(live / "scratch.txt").write_text("uncommitted")
expect_error(u.start, "later", "x", contains="uncommitted")
(live / "scratch.txt").unlink()

# --- permissions: making and showing copies is free, applying always asks (even in trust mode) ---
class IO:
    def ask(self, s, d): self.q = (s, d)
    def say(self, t): pass


g._io = IO()


async def run(tool, inp, reply="no"):
    t = asyncio.create_task(g.ask_before_risky(tool, inp, None)); await asyncio.sleep(0)
    asked = bool(g._pending_questions)
    if asked:
        g.answer_oldest_question(reply)
    return ("ask+" if asked else "") + type(await t).__name__.replace("PermissionResult", "").lower()


async def permissions():
    assert {"mcp__updates__start_update", "mcp__updates__check_update"} <= set(g.AUTO_APPROVED)
    assert "mcp__updates__apply_update" not in g.AUTO_APPROVED
    g.set_trust(60)
    assert await run("mcp__updates__apply_update", {"name": "x"}, "always") == "ask+allow"
    assert await run("mcp__updates__apply_update", {"name": "x"}) == "ask+deny"   # "always" saved nothing
    g.set_trust(0)
    # Jarvis can't edit its live code directly, even with trust mode on
    g.set_trust(60)
    assert await run("Write", {"file_path": str(g.PROJECT_DIR / "jarvis.py")}) == "ask+deny"
    assert await run("PowerShell", {"command": f"Set-Content {g.PROJECT_DIR}\\jarvis.py x"}) == "ask+deny"
    g.set_trust(0)


asyncio.run(permissions())

# --- the supervisor rolls back an update that won't start ---
u.start("broken", "Break startup")
(ws / "updates" / "broken" / "app.py").write_text("raise SystemExit(1)\n")
asyncio.run(u.check("broken"))
u.apply("broken")
broken = sh("git", "rev-parse", "HEAD", cwd=live)
rj.roll_back()
assert sh("git", "rev-parse", "HEAD", cwd=live) != broken and "v1" in (live / "app.py").read_text()
kept = [b.strip() for b in sh("git", "branch", cwd=live).splitlines() if "failed-update/broken" in b]
assert kept and sh("git", "rev-parse", kept[0], cwd=live) == broken      # the broken code isn't lost
assert not u.ROLLBACK_FILE.exists()
assert "rolled back" in u.startup_note()

# --- git must not hang while text mode's input() thread is reading stdin (a Windows quirk) ---
probe = (f"import sys, threading, time; sys.path.insert(0, {str(ROOT)!r}); import updater; "
         "threading.Thread(target=input, daemon=True).start(); time.sleep(0.5); "
         "print(updater.git('--version'))")
done = subprocess.run([sys.executable, "-I", "-c", probe], stdin=subprocess.PIPE, capture_output=True, text=True,
                      timeout=30)
assert done.returncode == 0 and "git version" in done.stdout, done.stderr[-500:]
print("ALL OK")

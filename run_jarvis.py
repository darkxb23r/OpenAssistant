#!/usr/bin/env python3
"""
Runs Jarvis, restarts it after a self-update, and rolls back an update that won't start.

    python run_jarvis.py [--core | --voice | --text] [--new]      (python3 on Linux)

With no mode flag it runs the core (--core): no mic, speakers or keyboard here, reached through the
browser face, terminals, satellites and the phone. --voice talks at this PC, --text types.

jarvis.py exits with updater.RESTART_EXIT_CODE after applying an update; this starts it again.
Just before switching, updater.py saved the previous commit in ROLLBACK_FILE, and the new version
deletes that file once it's up (updater.startup_note). So if Jarvis exits with an error while the
file still exists, the update broke startup: this keeps the broken code on a failed-update/<name>
branch, resets the live code to the saved commit, and starts the old version, which explains.

Only standard-library imports here, so a broken update can't break the rollback.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

if sys.version_info < (3, 11):
    sys.exit(f"Jarvis needs Python 3.11 or newer; this is {sys.version.split()[0]}.")

PROJECT_DIR = Path(__file__).resolve().parent
JARVIS_HOME = Path.home() / ".jarvis"
ROLLBACK_FILE = JARVIS_HOME / "update-rollback.json"   # keep in step with updater.py
NOTE_FILE = JARVIS_HOME / "update-note.txt"
LOG_FILE = JARVIS_HOME / "logs" / "jarvis.log"   # jarvis.py's log; written to only between runs


def log(message: str) -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} WARNING jarvis.supervisor: {message}\n")
RESTART_EXIT_CODE = 3
# Exit codes from Ctrl+C or closing the window: the user stopped Jarvis, the update didn't fail.
# On Linux also SIGHUP (the terminal closed) and SIGTERM (logging out, or a service stop).
STOPPED_BY_USER = {-2, 130, 0xC000013A, 3221225786 - 2**32, -1, 129, -15, 143}


def git(*args: str) -> str:
    done = subprocess.run(["git", *args], cwd=PROJECT_DIR, stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if done.returncode:
        raise RuntimeError((done.stderr or done.stdout).strip())
    return done.stdout.strip()


def roll_back() -> None:
    saved = json.loads(ROLLBACK_FILE.read_text(encoding="utf-8"))
    name, commit = saved.get("update", "latest"), saved["commit"]
    kept = f"failed-update/{name}-{time.strftime('%Y%m%d-%H%M%S')}"
    git("branch", kept, "HEAD")
    git("reset", "--hard", commit)
    ROLLBACK_FILE.unlink()
    NOTE_FILE.write_text(
        f"The {name} update stopped me from starting, so I've rolled back to the version before it. "
        f"Its code is kept on the git branch {kept}, and the error is on screen above.\n", encoding="utf-8")
    print(f"\n[run_jarvis] The {name} update failed to start. Rolled back to {commit[:8]}; "
          f"the update is kept on branch {kept}.\n")
    log(f"update {name} failed to start; rolled back to {commit[:8]}, kept on {kept}")


# The core (--core, the default) runs as a service: a crash is restarted after a pause that grows
# (BACKOFF_S), unless it keeps crashing (CRASH_LIMIT within CRASH_WINDOW_S), when it stops and leaves a
# note for the next start. Keep FLAGS and is_core() in step with jarvis.py's.
FLAGS = ("--core", "--voice", "--text", "--new")
USAGE = "usage: run_jarvis.py [--core | --voice | --text] [--new]   (no mode flag: --core)"
BACKOFF_S = (5, 30, 120)
CRASH_LIMIT, CRASH_WINDOW_S = 5, 600


def no_window() -> int:
    """Windows, started without a console (pythonw, the service task): keep the child's console
    hidden, or a window flashes up for it and for everything it starts."""
    return 0x08000000 if os.name == "nt" and sys.stdout is None else 0   # CREATE_NO_WINDOW


def is_core(args: list[str]) -> bool:
    """The core unless --voice or --text asks for the PC's own mic or keyboard (--core wins)."""
    return "--core" in args or not ("--voice" in args or "--text" in args)


def main(sleep=time.sleep) -> int:
    args = sys.argv[1:]
    if unknown := [a for a in args if a not in FLAGS]:
        print(f"[run_jarvis] unknown option {unknown[0]}\n{USAGE}")
        return 2
    core = is_core(args)
    crashes: list[float] = []
    while True:
        code = subprocess.call([sys.executable, str(PROJECT_DIR / "jarvis.py"), *args], cwd=PROJECT_DIR,
                               creationflags=no_window())
        args = [a for a in args if a != "--new"]  # a restart carries on the same conversation
        if code == RESTART_EXIT_CODE:
            continue
        if code not in (0, *STOPPED_BY_USER) and ROLLBACK_FILE.exists():
            try:
                roll_back()
            except Exception as e:
                print(f"[run_jarvis] Rolling back failed: {e}. The previous commit is in {ROLLBACK_FILE}.")
                log(f"rollback failed: {e}")
                return code
            continue
        if core and code not in (0, *STOPPED_BY_USER):
            now = time.monotonic()
            crashes = [t for t in crashes if now - t < CRASH_WINDOW_S] + [now]
            if len(crashes) >= CRASH_LIMIT:
                NOTE_FILE.write_text(f"I stopped after crashing {len(crashes)} times in {CRASH_WINDOW_S // 60} minutes "
                                     f"(last exit code {code}). The log has the details.\n", encoding="utf-8")
                log(f"gave up after {len(crashes)} crashes in {CRASH_WINDOW_S}s (exit {code})")
                return code
            wait = BACKOFF_S[min(len(crashes), len(BACKOFF_S)) - 1]
            log(f"jarvis exited with {code}; restarting in {wait}s")
            print(f"[run_jarvis] Jarvis exited with {code}; restarting in {wait}s.")
            sleep(wait)
            continue
        return code


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(0)

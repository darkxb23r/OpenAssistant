"""Running the core as a service: the systemd unit and the Task Scheduler task jarvisctl writes, and
the supervisor restarting a crashed core with a growing pause, then giving up with a note."""
import os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
sys.path.insert(0, str(ROOT))
import jarvisctl
import run_jarvis

# --- the systemd user unit: the core, restarted on failure, with the PATH it was installed with ---
unit = jarvisctl.systemd_unit("/home/me/.venv/bin/python", "/home/me/.local/bin:/usr/bin")
assert "ExecStart=/home/me/.venv/bin/python " in unit and unit.rstrip().endswith("WantedBy=default.target")
assert "run_jarvis.py --core" in unit and "Restart=on-failure" in unit and "SuccessExitStatus=143" in unit
assert 'Environment="PATH=/home/me/.local/bin:/usr/bin"' in unit
assert "After=network-online.target" not in unit   # doesn't work for user units; the core retries anyway

# --- the Windows task: at log-on, as the user, never time-limited, one copy, restarted on failure ---
task = jarvisctl.task_xml(r"C:\Python312\pythonw.exe", r"PC\me & co")
for needed in ("<LogonTrigger>", "<LogonType>InteractiveToken</LogonType>", "<RunLevel>LeastPrivilege</RunLevel>",
               "<ExecutionTimeLimit>PT0S</ExecutionTimeLimit>", "<MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>",
               "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>",
               "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>", "<RestartOnFailure>",
               r"<Command>C:\Python312\pythonw.exe</Command>", "PC\\me &amp; co", "--core</Arguments>"):
    assert needed in task, needed
from xml.dom import minidom
assert minidom.parseString(task.replace('encoding="UTF-16"', 'encoding="UTF-8"')).documentElement.tagName == "Task"

# --- satellite-bundle: exactly what a satellite runs from, in its layout, and it runs from there ---
import io, subprocess
from contextlib import redirect_stdout
bundle = home / "kitchen-satellite"
with redirect_stdout(io.StringIO()) as out:
    assert jarvisctl.main(["satellite-bundle", str(bundle)]) == 0
assert "satellite/requirements.txt" in out.getvalue() and "--kind satellite" in out.getvalue()
copied = sorted(p.relative_to(bundle).as_posix() for p in bundle.rglob("*") if p.is_file())
assert copied == sorted(jarvisctl.SATELLITE_FILES), copied
for rel in jarvisctl.SATELLITE_FILES:
    assert (bundle / rel).read_bytes() == (ROOT / rel).read_bytes(), rel
done = subprocess.run([sys.executable, "-I", str(bundle / "satellite" / "satellite.py"), "--help"],
                      cwd=home, capture_output=True, text=True, timeout=60)
assert done.returncode == 0 and "--pair CODE" in done.stdout, done.stderr
done = subprocess.run([sys.executable, "-I", "-c", "import sys; sys.path[:0] = [sys.argv[1]]; import voice, protocol; "
                       "assert voice.__file__.startswith(sys.argv[1]) and protocol.PROTOCOL_VERSION == 1",
                       str(bundle)], cwd=home, capture_output=True, text=True, timeout=60)
assert done.returncode == 0, done.stderr
# Never over something already there, and never inside the project (the live tree must stay clean).
for bad in (bundle, ROOT / "satellite-bundle", ROOT):
    try:
        jarvisctl.satellite_bundle(str(bad))
        raise AssertionError(bad)
    except SystemExit as e:
        assert "isn't empty" in str(e) or "outside the Jarvis folder" in str(e), e
assert not (ROOT / "satellite-bundle").exists()
(home / "empty").mkdir()
assert jarvisctl.satellite_bundle(str(home / "empty")) == (home / "empty").resolve()

# --- the supervisor: a crashing core is restarted after 5 s, 30 s, then 2 min, and after 5 crashes in
# 10 minutes it stops and leaves a note for the next start ---
repo = Path(tempfile.mkdtemp())
counter = repo / "runs"
(repo / "jarvis.py").write_text(
    "import sys, pathlib\n"
    f"p = pathlib.Path({str(counter)!r})\n"
    "n = int(p.read_text()) + 1 if p.exists() else 1\n"
    "p.write_text(str(n))\n"
    "sys.exit(int(open(p.with_name('code')).read()))\n")
(repo / "code").write_text("75")
run_jarvis.PROJECT_DIR = repo
run_jarvis.NOTE_FILE = home / "note.txt"
run_jarvis.ROLLBACK_FILE = home / "no-rollback.json"
run_jarvis.LOG_FILE = home / "jarvis.log"
slept = []
sys.argv = ["run_jarvis.py", "--core"]
assert run_jarvis.main(sleep=slept.append) == 75
assert counter.read_text() == "5" and slept == [5, 30, 120, 120]
assert "crashing 5 times" in run_jarvis.NOTE_FILE.read_text()

# Stopped on purpose (exit 0, or a service stop's SIGTERM): no restart.
for code in ("0", "143"):
    counter.unlink(); slept.clear()
    (repo / "code").write_text(code)
    assert run_jarvis.main(sleep=slept.append) == int(code)
    assert counter.read_text() == "1" and slept == []

# Not the core (voice or text at the PC): a crash isn't restarted, as before.
counter.unlink()
(repo / "code").write_text("1")
sys.argv = ["run_jarvis.py", "--text"]
assert run_jarvis.main(sleep=slept.append) == 1 and counter.read_text() == "1" and slept == []
for flags in (["--voice"], ["--voice", "--new"]):
    counter.unlink()
    sys.argv = ["run_jarvis.py", *flags]
    assert run_jarvis.main(sleep=slept.append) == 1 and counter.read_text() == "1" and slept == []

# No flags at all is the core: a crash is restarted with the same backoff.
counter.unlink(); run_jarvis.NOTE_FILE.unlink()
(repo / "code").write_text("75")
sys.argv = ["run_jarvis.py"]
assert run_jarvis.main(sleep=slept.append) == 75 and counter.read_text() == "5" and slept == [5, 30, 120, 120]
assert run_jarvis.is_core([]) and run_jarvis.is_core(["--new"]) and run_jarvis.is_core(["--core", "--text"])
assert not run_jarvis.is_core(["--text"]) and not run_jarvis.is_core(["--voice", "--new"])

# The old names (--web, --headless) and anything else unknown are refused before Jarvis starts.
for old in ("--web", "--headless", "--bogus"):
    counter.unlink(missing_ok=True)
    sys.argv = ["run_jarvis.py", old]
    assert run_jarvis.main(sleep=slept.append) == 2 and not counter.exists()
print("ALL OK")

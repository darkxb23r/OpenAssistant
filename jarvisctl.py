#!/usr/bin/env python3
"""
Control a running Jarvis core from this PC (or over SSH: `ssh host python3 jarvisctl.py status`).

    jarvisctl.py status                 what's running, who's connected, open questions, tasks, orders
    jarvisctl.py pause | resume         standing orders off / on (works even when the core is down)
    jarvisctl.py standby on|off
    jarvisctl.py stop | restart
    jarvisctl.py attach [--name NAME]   talk to Jarvis in this terminal (needs `websockets`)
    jarvisctl.py pair [--name NAME] [--kind face|satellite]
                                        a one-time code to pair a browser (face, the default) or a
                                        voice satellite; a terminal needs none (attach)
    jarvisctl.py devices [list | revoke ID | reset]
    jarvisctl.py satellite-bundle DIR   copy what a voice satellite needs into DIR (new or empty)
    jarvisctl.py rules                  your permission rules (~/.jarvis/permissions.toml), numbered
    jarvisctl.py allow PATTERN          never ask before commands like PATTERN ("git *")
    jarvisctl.py always-ask PATTERN     always ask before commands like PATTERN ("git push *")
    jarvisctl.py trust-folder PATH      work in PATH freely, like the free zone
    jarvisctl.py ask-folder PATH        always ask before touching anything in PATH, even reading
    jarvisctl.py use-claude-settings PATH   follow that project's own .claude/settings*.json there
    jarvisctl.py remove N|VALUE         remove rule N (from `rules`), or the rule with that value
                                        (the rules commands work when the core is down; it rereads the file)
    jarvisctl.py install-service        start the core at boot (Linux, systemd) or log-on (Windows)
    jarvisctl.py uninstall-service

It talks to the core on 127.0.0.1 with the token the core writes to ~/.jarvis/control.token at
every start (control.py). Only the standard library, except `attach`.
"""
import getpass
import json
import os
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path, PurePosixPath
from xml.sax.saxutils import escape

import control
import permissions

PROJECT_DIR = Path(__file__).resolve().parent
PORT = int(os.environ.get("JARVIS_WEB_PORT") or 8790)
SERVICE_NAME = "jarvis"
UNIT_FILE = Path.home() / ".config" / "systemd" / "user" / f"{SERVICE_NAME}.service"
TASK_NAME = "Jarvis"
# What a voice satellite runs from, in this layout (satellite.py imports voice.py and protocol.py from
# its parent folder): `satellite-bundle DIR` copies exactly these.
SATELLITE_FILES = ("voice.py", "protocol.py", "satellite/satellite.py", "satellite/requirements.txt",
                   "satellite/README.md")
PAIR_KINDS = ("face", "satellite")   # what `pair --kind` makes a code for (protocol.KINDS, less terminal)


class NotRunning(Exception):
    pass


def call(command: str, args: dict | None = None) -> dict:
    """POST a command to the running core."""
    token = control.read_token()
    if not token:
        raise NotRunning("Jarvis hasn't written a control token yet (~/.jarvis/control.token): is it running?")
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/control/{command}", method="POST",
                                 data=json.dumps(args or {}).encode(),
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        if e.code == 403:
            raise NotRunning("Jarvis refused the control token: it may have restarted; try again.") from e
        raise SystemExit(f"jarvisctl: {command} failed ({e.code}): {body}") from e
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        raise NotRunning(f"Jarvis isn't answering on 127.0.0.1:{PORT} ({getattr(e, 'reason', e)}).") from e


def show_status(s: dict) -> None:
    hours, rest = divmod(s.get("uptime_s", 0), 3600)
    print(f"Jarvis is running (pid {s.get('pid')}, up {hours}h {rest // 60}m).")
    flags = [name for name, on in (("on standby", s.get("standby")), ("standing orders paused", s.get("paused")),
                                   ("a turn is running", s.get("turn_running"))) if on]
    if s.get("trust_minutes"):
        flags.append(f"not asking for {s['trust_minutes']} more minutes")
    if flags:
        print("  " + "; ".join(flags) + ".")
    print(f"  Codex {'on' if s.get('codex') else 'off'}; Caveman {'on' if s.get('caveman') else 'off'}.")
    channels = s.get("channels", [])
    print(f"  {len(channels)} channel(s):")
    for c in channels:
        idle = c.get("idle_s")
        seen = "never used" if idle is None else f"used {idle // 60}m ago" if idle >= 60 else f"used {idle}s ago"
        print(f"    {c.get('kind')}{' ' + c['name'] if c.get('name') else ''} ({c.get('source')}), {seen}")
    for q in s.get("questions", []):
        print(f"  Waiting on: may I {q['summary']}?{' (always asks)' if q.get('hard') else ''}")
    for t in s.get("tasks", []):
        print(f"  Task {t['name']}: {t['status']}")
    for o in s.get("orders", []):
        print("  " + describe_order(o))
    if s.get("charter_problems"):
        print("  The charter has a problem, so it isn't adopted: " + "; ".join(s["charter_problems"]) + ".")


ORDER_STATES = {"adopted": "adopted", "running": "adopted, running now", "waiting": "waiting for your yes",
                "changed": "a change waits for your yes (off until then)", "declined": "declined",
                "removed": "removed from the charter"}


def when(iso: str | None, today=None) -> str:
    """A time from status, short: "today 06:30", "tomorrow 06:30", or "Mon 16 Nov 06:30"."""
    if not iso:
        return ""
    at = datetime.fromisoformat(iso)
    today = today or datetime.now(at.tzinfo).date()
    day = {today: "today", today + timedelta(days=1): "tomorrow", today - timedelta(days=1): "yesterday"}
    return f"{day.get(at.date(), at.strftime('%a %d %b'))} {at:%H:%M}"


def describe_order(o: dict, today=None) -> str:
    last = (f"last ran {when(o['last_run'], today)}" + (f" ({o['last_result']})" if o.get("last_result") else "")
            if o.get("last_run") else "not run yet")
    nxt = f"next {when(o['next_run'], today)}" if o.get("next_run") else ""
    return (f"Order {o['name']} ({o['kind']}, {o['schedule']}): {ORDER_STATES.get(o['state'], o['state'])}; "
            + ", ".join(p for p in (last, nxt) if p) + ".")


# --- the service ------------------------------------------------------------------------------
def systemd_unit(python: str, path: str, project: str | None = None) -> str:
    """A systemd user service for the core. PATH is the one jarvisctl runs with: a user service
    otherwise gets a minimal PATH, and claude, codex, cloudflared and caveman wouldn't be found.
    The unit is only ever used on Linux, so its paths are POSIX ones whatever builds it."""
    project_dir = PurePosixPath(project or PROJECT_DIR.as_posix())
    run = shlex.quote(str(project_dir / "run_jarvis.py"))
    return f"""[Unit]
Description=Jarvis, the assistant core
Documentation=file://{project_dir / 'CLAUDE.md'}

[Service]
Type=simple
WorkingDirectory={project_dir}
ExecStart={shlex.quote(python)} {run} --core
Restart=on-failure
RestartSec=10
SuccessExitStatus=143
TimeoutStopSec=20
Environment=PYTHONUNBUFFERED=1
Environment="PATH={path}"

[Install]
WantedBy=default.target
"""


def task_xml(pythonw: str, user: str) -> str:
    """A Task Scheduler task for the core: at log-on, as the user (a true Windows service can't use
    the Claude login or the audio devices), never stopped for running long (the default limit is 72
    hours), not stopped on battery, one copy at a time, restarted if it fails."""
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Jarvis, the assistant core</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled><UserId>{escape(user)}</UserId></LogonTrigger></Triggers>
  <Principals>
    <Principal id="Author"><UserId>{escape(user)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <StartWhenAvailable>true</StartWhenAvailable>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(pythonw)}</Command>
      <Arguments>"{escape(str(PROJECT_DIR / 'run_jarvis.py'))}" --core</Arguments>
      <WorkingDirectory>{escape(str(PROJECT_DIR))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def pythonw_for(python: str) -> str:
    candidate = Path(python).with_name("pythonw.exe")
    return str(candidate) if candidate.exists() else python


def run(*cmd: str) -> None:
    print("  $ " + " ".join(cmd))
    subprocess.run(cmd, check=True)


def install_service() -> None:
    if os.name == "nt":
        user = f"{os.environ.get('USERDOMAIN', '')}\\{getpass.getuser()}".lstrip("\\")
        xml = PROJECT_DIR / "service" / "jarvis-task.xml"
        xml.parent.mkdir(exist_ok=True)
        xml.write_text(task_xml(pythonw_for(sys.executable), user), encoding="utf-16")
        run("schtasks", "/Create", "/TN", TASK_NAME, "/XML", str(xml), "/F")
        print(f"Jarvis will start when you log on. Start it now with: schtasks /Run /TN {TASK_NAME}")
        return
    if not shutil.which("systemctl"):
        raise SystemExit("jarvisctl: systemd isn't available here; run `python3 run_jarvis.py --core` yourself.")
    UNIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    UNIT_FILE.write_text(systemd_unit(sys.executable, os.environ.get("PATH", "")), encoding="utf-8")
    print(f"Wrote {UNIT_FILE}.")
    run("systemctl", "--user", "daemon-reload")
    run("systemctl", "--user", "enable", "--now", f"{SERVICE_NAME}.service")
    linger = subprocess.run(["loginctl", "show-user", getpass.getuser(), "-p", "Linger"],
                            capture_output=True, text=True).stdout.strip()
    if linger != "Linger=yes":
        print("To keep Jarvis running after you log out and start it at boot, run once:\n"
              f"  sudo loginctl enable-linger {getpass.getuser()}")
    print("Logs: journalctl --user -u jarvis -f   (and ~/.jarvis/logs/jarvis.log)")


def uninstall_service() -> None:
    if os.name == "nt":
        run("schtasks", "/Delete", "/TN", TASK_NAME, "/F")
        return
    subprocess.run(["systemctl", "--user", "disable", "--now", f"{SERVICE_NAME}.service"])
    UNIT_FILE.unlink(missing_ok=True)
    run("systemctl", "--user", "daemon-reload")
    print("Removed the Jarvis service. Run `python3 run_jarvis.py --core` by hand to start it.")


# --- commands ---------------------------------------------------------------------------------
def satellite_bundle(target: str) -> Path:
    """Copy SATELLITE_FILES into `target`, a new or empty folder outside the project (a folder in
    the live tree would make it dirty, and updates need it clean). Nothing is overwritten."""
    dest = Path(target).expanduser().resolve()
    if dest == PROJECT_DIR or PROJECT_DIR in dest.parents:
        raise SystemExit("jarvisctl: put the satellite bundle outside the Jarvis folder.")
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        raise SystemExit(f"jarvisctl: {dest} isn't empty; give a new or empty folder.")
    for rel in SATELLITE_FILES:
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(PROJECT_DIR / rel, out)
    return dest


# --- permission rules ---------------------------------------------------------------------------
# These edit ~/.jarvis/permissions.toml directly, so they work whether or not the core is running;
# a running core notices the change at its next decision (guardrails.refresh_rules).
RULE_COMMANDS = {"allow": "allow", "always-ask": "always_ask", "trust-folder": "trust_folder",
                 "ask-folder": "ask_folder",
                 "use-claude-settings": "claude_settings"}


def show_rules() -> None:
    try:
        rules = permissions.load()
    except ValueError as e:
        raise SystemExit(f"jarvisctl: {permissions.PERMISSIONS_FILE} has a problem: {e}") from e
    listed = permissions.numbered(rules)
    if not listed:
        print(f"No permission rules yet ({permissions.PERMISSIONS_FILE}). Add one with allow, always-ask, "
              "trust-folder or ask-folder.")
        return
    print(f"Permission rules ({permissions.PERMISSIONS_FILE}):")
    for n, kind, value in listed:
        print(f"  {n:>2}. {permissions.LABELS[kind]:<18} {value}")


def edit_rules(cmd: str, rest: list[str]) -> None:
    value = " ".join(rest).strip()
    if not value:
        raise SystemExit(f"usage: jarvisctl.py {cmd} {'N|VALUE' if cmd == 'remove' else 'PATTERN' if cmd in ('allow', 'always-ask') else 'PATH'}")
    try:
        if cmd == "remove":
            print(permissions.remove(value))
        else:
            kind = RULE_COMMANDS[cmd]
            if kind in permissions.FOLDER_KINDS:
                value = str(Path(value).expanduser().absolute())   # relative to where you are
            print(permissions.add(kind, value))
    except ValueError as e:
        raise SystemExit(f"jarvisctl: {e}") from e


def option(args: list[str], name: str, default: str | None = None) -> str | None:
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    try:
        if cmd == "status":
            show_status(call("status"))
        elif cmd in ("pause", "resume"):
            try:
                call(cmd)
            except NotRunning:   # the file is the switch: it holds even when the core is down
                control.set_paused(cmd == "pause")
            print("Standing orders paused." if cmd == "pause" else "Standing orders resumed.")
        elif cmd == "standby":
            on = (rest[:1] or ["on"])[0].lower() not in ("off", "0", "false", "no")
            call("standby", {"on": on})
            print("On standby." if on else "Off standby.")
        elif cmd in ("stop", "restart"):
            call(cmd)
            print("Stopping Jarvis." if cmd == "stop" else "Restarting Jarvis.")
        elif cmd == "pair":
            kind = option(rest, "--kind", "face")
            if kind not in PAIR_KINDS:
                raise SystemExit("usage: jarvisctl.py pair [--name NAME] [--kind face|satellite]")
            got = call("pair", {"name": option(rest, "--name", kind if kind == "satellite" else "device"),
                                "kind": kind})
            print(f"Pairing code {got['code']} for a {'browser' if kind == 'face' else 'voice satellite'}, "
                  f"valid {got['minutes']} minutes, once.")
            if kind == "face":
                print(f"  Open {got['url']} in the browser to pair.")
            else:
                print(f"  On the satellite: python satellite/satellite.py --pair {got['code']}")
        elif cmd == "devices":
            sub = (rest[:1] or ["list"])[0]
            if sub == "list":
                got = call("devices")
                if not got["devices"]:
                    print("No paired devices: any page of the interface on this PC can connect (open mode).")
                for d in got["devices"]:
                    state = "revoked" if d.get("revoked") else f"last seen {d.get('last_seen') or 'never'}"
                    print(f"  {d['id']}  {d['name']} ({d['kind']}), {state}")
            elif sub == "revoke" and len(rest) > 1:
                call("revoke", {"id": rest[1]})
                print(f"Revoked {rest[1]}.")
            elif sub == "reset":
                call("reset_devices")
                print("Forgot every paired device: back to open mode.")
            else:
                raise SystemExit("usage: jarvisctl.py devices [list | revoke ID | reset]")
        elif cmd == "attach":
            import terminal
            return terminal.attach(PORT, option(rest, "--name", getpass.getuser()))
        elif cmd == "satellite-bundle":
            if len(rest) != 1:
                raise SystemExit("usage: jarvisctl.py satellite-bundle DIR")
            dest = satellite_bundle(rest[0])
            print(f"A voice satellite is in {dest}. Copy that folder to the satellite machine, then there:")
            print("  pip install -r satellite/requirements.txt")
            print("  python satellite/satellite.py --pair CODE   (CODE from: jarvisctl.py pair --kind satellite)")
            print("satellite/README.md has the rest.")
        elif cmd == "rules":
            show_rules()
        elif cmd in RULE_COMMANDS or cmd == "remove":
            edit_rules(cmd, rest)
        elif cmd == "install-service":
            install_service()
        elif cmd == "uninstall-service":
            uninstall_service()
        else:
            raise SystemExit(f"jarvisctl: unknown command {cmd!r}. Try: jarvisctl.py help")
    except NotRunning as e:
        print(f"jarvisctl: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""
Standing orders: work Jarvis does on its own, on a schedule, from a charter the user writes.

The charter is ~/.jarvis/charter.toml (charter.example.toml in the project explains every field). It
lives in a secret folder, so Jarvis's own tools can't read or write it: only the user changes it.

- Adoption: what runs is never the file itself but its adopted copy, ~/.jarvis/agency/adopted.json.
  Each order is hashed (sha256 of canonical JSON). When the file changes, new and changed orders
  (and new [agency] limits) wait, and ONE hard question asks to adopt them; only the user at the PC
  or a paired screen ("physical"/"device" trust) can answer it, never a text. A yes grants each
  order's needs under order-<name> (guardrails' task grants); removing an order drops its grants.
- Scheduler (Agency.run): a tick every 30 s in local time; state in ~/.jarvis/agency/state.json. A
  slot missed by less than 3 hours (Jarvis was down, or another order was running) is caught up,
  one worker at a time. The kill switch (control.paused(), `jarvisctl pause`, "pause your standing
  orders") is checked every tick, and pausing stops the order that's running.
- A run is an in-memory tasks.Task named order-<name> with one step, done by tasks.do_step (a fresh
  worker, then the checker) with guardrails.current_task and guardrails.agency_run set. Inside it,
  only read-only rules, the order's grants and the free zone allow anything (no trust mode, no
  "always" rules, no safety check), and its questions are unattended: no answer within
  ask_timeout_minutes is a no, and in quiet hours the answer is no without asking.
- Budgets: max_worker_runs_per_day (worker sessions started, a retry included; a run whose quick
  check finds nothing starts none) and max_actions_per_hour (tool calls by orders' workers, counted
  by guardrails.guard_agency).
- Journal: ~/.jarvis/agency/journal.jsonl, one JSON line per event, through the jarvis.agency
  logger (guardrails.agency_note from auto_allow, ask_user and tasks._work, and journal() here).
  At summary_at a daily summary says what the orders did, decided not to do, and want permission for.
- Quiet hours: an order's report and the daily summary aren't said (or texted) then. They wait in
  ~/.jarvis/agency/held.json (so a restart keeps them) and are told together on the first tick
  after quiet hours end (Agency.tell, deliver_held). A user's own task news is never held.

What each kind of order does (backup, machines, mail, brief) is filled in by KIND_BRIEFS, KIND_ALLOW
and WATCHERS below: a kind's quick check (watchers.py) runs first, and a worker only when it finds
something; a worker's report is said to the user like task news.
"""
import asyncio
import hashlib
import json
import logging
import os
import re
import tomllib
from collections import deque
from datetime import datetime, time as clock_time, timedelta

import control
import guardrails
import tasks
import watchers
from guardrails import JARVIS_HOME

log = logging.getLogger("jarvis.agency")
CHARTER_FILE = JARVIS_HOME / "charter.toml"
AGENCY_DIR = JARVIS_HOME / "agency"
ADOPTED_FILE = AGENCY_DIR / "adopted.json"
STATE_FILE = AGENCY_DIR / "state.json"
JOURNAL_FILE = AGENCY_DIR / "journal.jsonl"
HELD_FILE = AGENCY_DIR / "held.json"   # order news held back in quiet hours (Agency.tell)
MAX_HELD = 50                          # past this the oldest is dropped (it's in the journal anyway)
JOURNAL_MAX_BYTES = 5 * 1024 * 1024   # then it moves to journal.1.jsonl and starts again
TICK_S = 30
CATCH_UP_S = 3 * 60 * 60
PREFIX = tasks.ORDER_PREFIX
ANSWER_FROM = {"physical", "device"}   # who may adopt a charter: never a text or a call
ANSWER_NOT_FROM = {"satellite"}        # ... nor a voice satellite (a paired device, but a voice alone)
MODELS = ("opus", "sonnet", "haiku")   # jarvis.MODELS (test_agency checks they match)
KINDS = ("backup", "machines", "mail", "brief")
ORDER_NAME = re.compile(r"[a-z0-9-]{1,32}")
HH_MM = re.compile(r"([01]?\d|2[0-3]):([0-5]\d)")
SCHEDULE = re.compile(r"(daily|weekdays)\s+(\S+)|every\s+(\d+)\s*(m|h)", re.IGNORECASE)
MIN_EVERY_S = 5 * 60

DEFAULTS = {"quiet_hours": "", "max_worker_runs_per_day": 24, "max_actions_per_hour": 100,
            "ask_timeout_minutes": 30, "summary_at": "21:00"}
LIMITS = {"max_worker_runs_per_day": (1, 1000), "max_actions_per_hour": (1, 10000), "ask_timeout_minutes": (1, 1440)}
ORDER_KEYS = {"name", "schedule", "kind", "check", "probes", "machines", "needs", "model", "allow",
              "paths", "max_age_hours", "min_size_mb"}
BACKUP_KEYS = ("paths", "max_age_hours", "min_size_mb")   # a backup order's only
NEEDS_KEYS = ("folders", "machines", "commands")

# What an order's worker may use ("allow"): MCP servers by name, built-in tools, "shell" for this
# OS's shell tools. Subagents are never given (agents={}, and no Agent tool).
ALLOW_SERVERS = {"machines", "time", "mail", "calendar", "codex"}
ALLOW_BUILTINS = {"Read", "Glob", "Grep", "shell", "WebSearch", "WebFetch", "Write", "Edit"}
SHELL = ["Bash", "PowerShell"] if os.name == "nt" else ["Bash"]
KIND_ALLOW = {
    "backup": ["machines", "time", "Read", "Glob", "Grep", "shell"],
    "machines": ["machines", "time", "shell"],
    "mail": ["mail", "time"],
    "brief": ["calendar", "time"],   # tasks, questions and the journal come from its quick check
}
# An order may read mail and the calendar but never change them: every mail write is disallowed.
# read_message is left out on purpose. In mcp-mailcow 1.0.3 (user_tools.py) it takes mark_read:
# true opens the folder read-write (SELECT) and fetches RFC822, which sets \Seen. A disallowed tool
# is all or nothing (no per-argument rule), and its bool(args["mark_read"]) even reads the string
# "false" as true, so the worker reads bodies with get_message_raw (always EXAMINE) instead.
# list_inbox and search_messages open the folder read-only too, and get_unread_count uses STATUS.
MAIL_READS = {"list_inbox", "list_folders", "get_unread_count", "search_messages", "get_message_raw"}
MAIL_WRITES = sorted({f"mcp__mail__{n}" for n in guardrails.MAIL_AUTO if n not in MAIL_READS}
                     | {t for t in guardrails.CONFIRM_TOOLS if t.startswith("mcp__mail__")})
CALENDAR_WRITES = sorted(t for t in guardrails.CONFIRM_TOOLS if t.startswith("mcp__calendar__"))
# The step a kind's worker is given. Its step_done summary is said to the user (tasks.announce), so
# the briefs ask for a spoken sentence or two; NOTHING_TO_TELL is journaled but not said.
NOTHING_TO_TELL = "Nothing needs you."
_SAY = ("Your step_done summary is said aloud to the user: one or two short spoken sentences, no lists or "
        f"markdown. If nothing needs the user after all, make it exactly: {NOTHING_TO_TELL}")
KIND_BRIEFS = {
    "backup": "Backup check. The quick check below found a backup that's missing, old, empty or failing. "
              "Find out why (its folder, the backup job's log, the machine's disk) and change nothing. " + _SAY
              + " Say which backup and what's wrong, like: The NAS backup hasn't run since Monday; its disk is full.",
    "machines": "Machine watch. The quick check below found a machine that can't be reached, a check that "
                "failed, or a disk nearly full. Look closer (disk, memory, services, containers, recent logs). "
                "Fix it only with what you may do without asking; otherwise change nothing and say what's needed. "
                + _SAY + " Say which machine and what's wrong, like: The NAS is down to 3 GB free; old Docker "
                "images take 40 of it.",
    "mail": "Mail triage. The new unread mail is listed below, headers only. Decide what needs the user: "
            "something to answer, pay, sign or act on today, or from a person rather than a list. Read a "
            "message only when its headers aren't enough, with get_message_raw. Never send, reply, move, flag, "
            "mark or delete anything; the mail stays unread. " + _SAY + " Name the sender and why it matters, "
            "like: Your landlord wrote about the lease renewal, and the electricity bill is due Friday.",
    "brief": "Morning brief. Read today's calendar (get_todays_schedule) and put it together with what Jarvis "
             "already knows, below, into the user's morning brief. Change nothing. Your step_done summary is said "
             "aloud as the brief: begin with Good morning, then at most five short spoken sentences, the day's "
             "first commitments and anything waiting on the user first; times in twelve-hour form, days as "
             "today or tomorrow; no lists or markdown.",
}
# Cheap checks that run before (instead of) a worker: kind -> async fn(order) -> None when all is
# well (no worker needed), else the findings to give the worker (watchers.py).
WATCHERS: dict = dict(watchers.WATCHERS)


def now() -> datetime:
    """Local time (the PC's zone). Tests replace it."""
    return datetime.now().astimezone()


# --- the journal ------------------------------------------------------------------------------------
class JournalHandler(logging.Handler):
    """Writes each jarvis.agency record that carries a journal entry as one JSON line."""

    def emit(self, record):
        entry = getattr(record, "journal", None)
        if entry is None:
            return
        try:
            AGENCY_DIR.mkdir(parents=True, exist_ok=True)
            if JOURNAL_FILE.exists() and JOURNAL_FILE.stat().st_size > JOURNAL_MAX_BYTES:
                JOURNAL_FILE.replace(JOURNAL_FILE.with_suffix(".1.jsonl"))
            line = json.dumps({"ts": now().isoformat(timespec="seconds"), **entry}, ensure_ascii=False, default=str)
            with JOURNAL_FILE.open("a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            self.handleError(record)


def install_journal() -> None:
    if not any(isinstance(h, JournalHandler) for h in guardrails.agency_log.handlers):
        guardrails.agency_log.addHandler(JournalHandler())
    guardrails.agency_log.setLevel(logging.INFO)


def journal(order: str, event: str, part: str | None = None, **fields) -> None:
    """A journal line from the scheduler itself (guardrails.agency_note is the same, from inside a run)."""
    entry = {"order": order, "event": event, **({"part": part} if part else {}), **fields}
    log.info("%s %s %s", order, event, fields, extra={"journal": entry})


def read_journal(since: str | None = None) -> list[dict]:
    if not JOURNAL_FILE.exists():
        return []
    entries = []
    for line in JOURNAL_FILE.read_text(encoding="utf-8").splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if since is None or entry.get("ts", "") > since:
            entries.append(entry)
    return entries


# --- the charter ------------------------------------------------------------------------------------
def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def parse_time(text) -> clock_time | None:
    m = HH_MM.fullmatch(str(text).strip())
    return clock_time(int(m[1]), int(m[2])) if m else None


def parse_schedule(text) -> tuple:
    """("daily"|"weekdays", time) or ("every", seconds). ValueError if it doesn't parse."""
    m = SCHEDULE.fullmatch(str(text).strip())
    if not m:
        raise ValueError(f"schedule {text!r}: write daily HH:MM, weekdays HH:MM, or every Nm / every Nh")
    if m[1]:
        at = parse_time(m[2])
        if at is None:
            raise ValueError(f"schedule {text!r}: the time must be HH:MM, 24-hour")
        return m[1].lower(), at
    seconds = int(m[3]) * (3600 if m[4].lower() == "h" else 60)
    if seconds < MIN_EVERY_S:
        raise ValueError(f"schedule {text!r}: at most every 5 minutes")
    return "every", seconds


def as_list(value) -> list:
    return [] if value is None else [value] if isinstance(value, str) else list(value)


def _strings(value) -> bool:
    return isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)


def _settings_problems(settings) -> list[str]:
    if not isinstance(settings, dict):
        return ["[agency] must be a table"]
    problems = [f"[agency] {k}: not a setting I know" for k in settings if k not in DEFAULTS]
    for key, (low, high) in LIMITS.items():
        if key in settings:
            v = settings[key]
            if isinstance(v, bool) or not isinstance(v, int) or not low <= v <= high:
                problems.append(f"[agency] {key}: a whole number from {low} to {high}")
    quiet = settings.get("quiet_hours", "")
    if quiet and not (isinstance(quiet, str) and len(parts := quiet.split("-")) == 2
                      and all(parse_time(p) for p in parts)):
        problems.append("[agency] quiet_hours: write HH:MM-HH:MM, like 22:00-07:00, or leave it out")
    summary = settings.get("summary_at", "")
    if summary and not (isinstance(summary, str) and parse_time(summary)):
        problems.append("[agency] summary_at: write HH:MM, or \"\" for no daily summary")
    return problems


def _order_problems(order, machines: set[str]) -> list[str]:
    if not isinstance(order, dict):
        return ["each [[order]] must be a table"]
    name = order.get("name")
    if not isinstance(name, str) or not ORDER_NAME.fullmatch(name):
        return [f"order {name!r}: name it with 1-32 lowercase letters, digits or dashes"]
    where = f"order {name}"
    problems = [f"{where}: {k} isn't a field I know" for k in order if k not in ORDER_KEYS]
    if order.get("kind") not in KINDS:
        problems.append(f"{where}: kind must be one of {', '.join(KINDS)}")
    try:
        parse_schedule(order.get("schedule", ""))
    except ValueError as e:
        problems.append(f"{where}: {e}")
    if order.get("model", "sonnet") not in MODELS:
        problems.append(f"{where}: model must be one of {', '.join(MODELS)}")
    allow = order.get("allow", [])
    if not _strings(allow) and allow != []:
        problems.append(f"{where}: allow must be a list of names")
    else:
        problems += [f"{where}: allow {a!r} isn't something an order can use" for a in allow
                     if a not in ALLOW_SERVERS | ALLOW_BUILTINS]
    # Watcher commands: run by Jarvis itself, so only what the read-only list passes, nowhere protected.
    check, probes = order.get("check"), order.get("probes", [])
    if check is not None and not (isinstance(check, str) and check.strip() or _strings(check)):
        problems.append(f"{where}: check must be a command or a list of commands")
    elif not _strings(probes) and probes != []:
        problems.append(f"{where}: probes must be a list of commands")
    else:
        for cmd in as_list(check) + list(probes):
            if not guardrails.is_read_only("Bash", cmd) or guardrails.location_check(cmd)[0] == "ask":
                problems.append(f"{where}: {cmd!r} isn't a read-only command I can run without asking")
    problems += _backup_problems(order, where)
    if order.get("kind") == "machines" and not order.get("machines"):
        problems.append(f"{where}: a machines order needs the machines to watch")
    used = order.get("machines", [])
    if not _strings(used) and used != []:
        problems.append(f"{where}: machines must be a list of machine names")
        used = []
    needs = order.get("needs", {})
    if not isinstance(needs, dict) or any(k not in NEEDS_KEYS or not (_strings(v) or v == []) for k, v in needs.items()):
        problems.append(f"{where}: needs takes folders, machines and commands, each a list")
    else:
        problems += [f"{where}: {p}" for p in guardrails.grant_problems(needs)]
        used = used + needs.get("machines", [])
    problems += [f"{where}: {m} isn't one of my machines (add_machine first)" for m in used
                 if m.lower() not in machines]
    return problems


def _number(value, low: float, high: float, whole: bool = False) -> bool:
    kinds = (int,) if whole else (int, float)
    return not isinstance(value, bool) and isinstance(value, kinds) and low <= value <= high


def _backup_problems(order: dict, where: str) -> list[str]:
    """paths, max_age_hours and min_size_mb: a backup order's, looked at by watchers.backup."""
    if order.get("kind") != "backup":
        return [f"{where}: {k} only goes with kind backup" for k in BACKUP_KEYS if k in order]
    problems = []
    paths = order.get("paths", [])
    if not _strings(paths) and paths != []:
        problems.append(f"{where}: paths must be a list of full paths")
        paths = []
    for path in paths:
        # Looked at with `ls` on a machine (and by Python here), so held to the check commands' rules.
        cmd = watchers.listing(path)
        absolute = path.startswith("/") or guardrails.is_absolute(path)   # a machine's paths are POSIX ones
        if (not absolute or path.startswith("~") or re.search(r"[\"'$`\n]", path)
                or not guardrails.is_read_only("Bash", cmd) or guardrails.location_check(cmd)[0] == "ask"):
            problems.append(f"{where}: {path!r} isn't a full path I can look at without asking")
    if "max_age_hours" in order and not _number(order["max_age_hours"], 1, 24 * 366, whole=True):
        problems.append(f"{where}: max_age_hours is a whole number of hours, 1 or more")
    if "min_size_mb" in order and not _number(order["min_size_mb"], 0, 10 ** 9):
        problems.append(f"{where}: min_size_mb is a number of megabytes")
    if not paths and order.get("check") is None and not order.get("probes"):
        problems.append(f"{where}: a backup order needs paths to look at, or a check command")
    return problems


def validate(data) -> tuple[dict, list[str]]:
    """The charter as {"agency": {...}, "orders": [...]} (as written) and what's wrong with it."""
    problems = [f"{k}: not a section I know" for k in data if k not in ("agency", "order")]
    settings = data.get("agency", {})
    problems += _settings_problems(settings)
    orders = data.get("order", [])
    if not isinstance(orders, list):
        orders, problems = [], problems + ["orders are written [[order]]"]
    machines = {m.lower() for m in guardrails.read_machines()}
    names = []
    for order in orders:
        problems += _order_problems(order, machines)
        if isinstance(order, dict):
            names.append(order.get("name"))
    problems += [f"order {n}: the name is used twice" for n in sorted({n for n in names if names.count(n) > 1}, key=str)]
    return {"agency": settings if isinstance(settings, dict) else {}, "orders": orders}, problems


def read_charter(path=None) -> tuple[dict | None, list[str], str]:
    """(charter, problems, hash of the file's text). No file is an empty charter."""
    path = path or CHARTER_FILE
    if not path.exists():
        return {"agency": {}, "orders": []}, [], ""
    raw = path.read_bytes()
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        return None, [f"it isn't valid TOML ({e})"], hashlib.sha256(raw).hexdigest()
    charter, problems = validate(data)
    return (None if problems else charter), problems, hashlib.sha256(raw).hexdigest()


def settings_of(agency_section: dict) -> dict:
    return DEFAULTS | agency_section


def worker_kwargs(order: dict) -> dict:
    """The extra jarvis.worker_options() arguments for an order's worker."""
    allow = order.get("allow") or KIND_ALLOW[order["kind"]]
    servers = tuple(a for a in allow if a in ALLOW_SERVERS)
    builtin = [t for a in allow if a in ALLOW_BUILTINS for t in (SHELL if a == "shell" else [a])]
    disallowed = (MAIL_WRITES if "mail" in servers else []) + (CALENDAR_WRITES if "calendar" in servers else [])
    return {"servers": servers, "builtin_tools": builtin, "disallowed": disallowed, "agents": {}, "order": True}


def make_task(order: dict, findings: str | None = None) -> tasks.Task:
    """One run of an order: an in-memory task with a single step."""
    name, kind = order["name"], order["kind"]
    parts = [KIND_BRIEFS[kind]]
    if order.get("machines"):
        parts.append("Machines: " + ", ".join(order["machines"]) + " (reach them with ssh <name> ...).")
    if checks := as_list(order.get("check")) + list(order.get("probes", [])):
        parts.append("Commands to check with: " + "; ".join(checks) + ".")
    if order.get("paths"):
        parts.append("Backups: " + ", ".join(order["paths"]) + " (the newest should be under "
                     f"{order.get('max_age_hours', watchers.MAX_AGE_HOURS)} hours old).")
    if findings:
        parts.append("What the quick check found: " + tasks.one_line(findings))
    needs = {k: list(order.get("needs", {}).get(k, [])) for k in NEEDS_KEYS}
    return tasks.Task(PREFIX + name, f"Standing order {name} ({kind}, {order['schedule']})",
                      "The step's report says what was found and anything that needs the user.",
                      status="running", steps=[tasks.Step(1, f"[{order.get('model', 'sonnet')}] " + " ".join(parts))],
                      needs=needs, in_memory=True, worker=worker_kwargs(order))


def worth_telling(report: str) -> bool:
    """An order's report is said to the user unless it's empty or NOTHING_TO_TELL."""
    plain = re.sub(r"[^a-z ]", "", report.lower()).strip()
    return bool(plain) and plain != re.sub(r"[^a-z ]", "", NOTHING_TO_TELL.lower()).strip()


def latest_slot(order: dict, at: datetime, last: datetime | None) -> datetime | None:
    """The scheduled time this order is due for (at or before `at`), or None."""
    kind, value = parse_schedule(order["schedule"])
    if kind == "every":
        start = last + timedelta(seconds=value) if last else at   # no record of a run: now
        return start if start <= at else None
    for back in range(8):
        day = at.date() - timedelta(days=back)
        slot = datetime.combine(day, value).astimezone()   # local wall-clock time
        if slot <= at and (kind == "daily" or day.weekday() < 5):
            return slot
    return None


def next_slot(order: dict, at: datetime, last: datetime | None) -> datetime:
    """When this order runs next (from `at` on; a slot already due is `at`)."""
    kind, value = parse_schedule(order["schedule"])
    if kind == "every":
        return max(last + timedelta(seconds=value), at) if last else at
    due = latest_slot(order, at, last)
    if due is not None and (last is None or due > last):
        return at
    for ahead in range(9):
        day = at.date() + timedelta(days=ahead)
        if kind == "daily" or day.weekday() < 5:
            slot = datetime.combine(day, value).astimezone()
            if slot > at:
                return slot
    return at


def in_quiet_hours(quiet: str, at: datetime) -> bool:
    if not quiet:
        return False
    start, end = (parse_time(p) for p in quiet.split("-"))
    t = at.time()
    return start <= t < end if start <= end else t >= start or t < end


# --- "Pause your standing orders" -----------------------------------------------------------------------
_ORDERS = r"(?:all\W+)?(?:of\W+)?(?:(?:your|the|my)\W+)?standing\W+orders"
PAUSE_ORDERS = re.compile(r"^\W*(?:(?:hey|ok(?:ay)?)\W+)?(?:jarvis\W+)?(?:please\W+)?(?:pause|stop|suspend|halt)\W+"
                          + _ORDERS + r"(?:\W+(?:for\W+now|please|jarvis|thanks))*\W*$", re.IGNORECASE)
RESUME_ORDERS = re.compile(r"^\W*(?:(?:hey|ok(?:ay)?)\W+)?(?:jarvis\W+)?(?:please\W+)?(?:resume|restart|unpause|start|continue)\W+"
                           + _ORDERS + r"(?:\W+(?:again|please|jarvis|thanks))*\W*$", re.IGNORECASE)


def pause_request(line: str) -> bool | None:
    """True to pause the standing orders, False to resume them, None if the line isn't about that.
    Only ever read from the user's own lines (jarvis.main), never from the model."""
    if PAUSE_ORDERS.match(line):
        return True
    if RESUME_ORDERS.match(line):
        return False
    return None


# --- the scheduler --------------------------------------------------------------------------------------
def _read_json(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(path, value) -> None:
    AGENCY_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


class Run:
    """One order being carried out: what guardrails sees through agency_run."""

    def __init__(self, order: str, agency: "Agency"):
        self.order, self.agency = order, agency
        self.ask_timeout_s = agency.settings["ask_timeout_minutes"] * 60
        self.over_budget = False

    def quiet(self) -> bool:
        return self.agency.in_quiet_hours()

    def spend_action(self, what: str) -> bool:
        return self.agency.spend_action(self, what)


class Agency:
    def __init__(self):
        install_journal()
        self.adopted = _read_json(ADOPTED_FILE, {})
        self.adopted.setdefault("orders", {})
        self.adopted.setdefault("agency", {})
        self.state = _read_json(STATE_FILE, {})
        self.state.setdefault("orders", {})
        self.current: asyncio.Task | None = None
        self.current_order: str | None = None
        self.question = None            # the open adoption question (guardrails.Question)
        self._asking: asyncio.Task | None = None
        self._charter: dict | None = None    # the last valid charter read
        self._told_problems = None      # the bad charter we've already told the user about
        self._stop_why = ""
        self._actions: deque = deque()   # times of orders' tool calls in the last hour

    # --- settings and budgets ---
    @property
    def settings(self) -> dict:
        return settings_of(self.adopted["agency"])

    def in_quiet_hours(self, at: datetime | None = None) -> bool:
        return in_quiet_hours(self.settings["quiet_hours"], at or now())

    def spend_action(self, run: Run, what: str) -> bool:
        t = now().timestamp()
        while self._actions and self._actions[0] <= t - 3600:
            self._actions.popleft()
        if len(self._actions) >= self.settings["max_actions_per_hour"]:
            if not run.over_budget:
                run.over_budget = True
                journal(run.order, "over budget", part="declined", why="the hourly action budget was used up",
                        what=what)
            return False
        self._actions.append(t)
        return True

    def _runs_today(self, at: datetime) -> int:
        runs = self.state.get("runs", {})
        return runs.get("count", 0) if runs.get("day") == at.date().isoformat() else 0

    def _count_run(self, at: datetime) -> None:
        self.state["runs"] = {"day": at.date().isoformat(), "count": self._runs_today(at) + 1}

    def start_worker(self) -> bool:
        """tasks.do_step asks this before each worker session it starts: each one, a retry too, counts
        against max_worker_runs_per_day, and none starts once that's used up."""
        at = now()
        if self._runs_today(at) >= self.settings["max_worker_runs_per_day"]:
            return False
        self._count_run(at)
        self.save()
        return True

    def save(self) -> None:
        _write_json(ADOPTED_FILE, self.adopted)
        _write_json(STATE_FILE, self.state)

    # --- news: said at once, or held through quiet hours ---
    def tell(self, text: str, at: datetime | None = None) -> bool:
        """Order news (a report, the daily summary) to the user like task news (tasks.announce),
        except in quiet hours, when it waits in HELD_FILE for deliver_held(). True if said now."""
        at = at or now()
        if not self.in_quiet_hours(at):
            tasks.announce(text)
            return True
        held = _read_json(HELD_FILE, [])
        held = (held if isinstance(held, list) else []) + [{"at": at.isoformat(timespec="seconds"), "text": text}]
        _write_json(HELD_FILE, held[-MAX_HELD:])
        log.info("standing orders: news held for quiet hours (%d waiting)", len(held[-MAX_HELD:]))
        return False

    def deliver_held(self, at: datetime | None = None) -> None:
        """Once quiet hours are over, what was held is told, all in one message, oldest first."""
        if self.in_quiet_hours(at or now()) or not HELD_FILE.exists():
            return
        held = _read_json(HELD_FILE, [])
        held = held if isinstance(held, list) else []
        texts = [h["text"] for h in held if isinstance(h, dict) and isinstance(h.get("text"), str)]
        HELD_FILE.unlink(missing_ok=True)
        if texts:
            tasks.announce(texts[0] if len(texts) == 1 else "While it was quiet: " + " ".join(texts))

    # --- adoption ---
    def runnable(self) -> list[dict]:
        """Adopted orders whose charter entry hasn't changed since (all of them while the file is unreadable)."""
        adopted = self.adopted["orders"]
        if self._charter is None:
            return [a["order"] for a in adopted.values()]
        written = {o["name"]: o for o in self._charter["orders"]}
        return [a["order"] for n, a in adopted.items() if n in written and digest(written[n]) == a["hash"]]

    def changes(self, charter: dict) -> dict:
        adopted = self.adopted["orders"]
        new = [o for o in charter["orders"] if o["name"] not in adopted]
        changed = [o for o in charter["orders"] if o["name"] in adopted and digest(o) != adopted[o["name"]]["hash"]]
        limits = digest(charter["agency"]) != self.adopted.get("agency_hash", digest({}))
        return {"new": new, "changed": changed, "limits": limits}

    def review_charter(self) -> None:
        """Read the charter; drop removed orders; ask about new or changed ones (once)."""
        charter, problems, file_hash = read_charter()
        if problems:
            if file_hash != self._told_problems:
                self._told_problems = file_hash
                log.warning("charter not adopted: %s", problems)
                journal("charter", "invalid", part="wants", why="; ".join(problems[:5]))
                tasks.say("Your standing-orders charter has a problem, so I'm keeping the orders I had: "
                          + "; ".join(problems[:3]) + ".")
            return
        self._charter = charter
        written = {o["name"] for o in charter["orders"]}
        removed = [n for n in self.adopted["orders"] if n not in written]
        for name in removed:   # taking power away needs no question
            guardrails.drop_grants(PREFIX + name)
            del self.adopted["orders"][name]
            self.state["orders"].pop(name, None)
            journal(name, "removed")
        if removed:
            self.save()
        changes = self.changes(charter)
        pending = bool(changes["new"] or changes["changed"] or changes["limits"])
        charter_hash = digest(charter)
        if self.question is not None and (not pending or self.question.charter_hash != charter_hash):
            question, self.question = self.question, None
            guardrails.close_question(question, "no", "the charter changed")
        if not pending or self.question is not None or self.adopted.get("declined") == charter_hash:
            return
        summary, detail = describe_changes(changes, charter)
        question = guardrails.open_question(summary, detail, hard=True, only=ANSWER_FROM, not_kinds=ANSWER_NOT_FROM)
        question.charter_hash = charter_hash
        self.question = question
        journal("charter", "asked", what=summary)
        self._asking = asyncio.get_running_loop().create_task(self._await_adoption(question, charter, changes))

    async def _await_adoption(self, question, charter: dict, changes: dict) -> None:
        decision = await question.answer
        if self.question is not question:   # superseded by a newer charter
            return
        self.question = None
        if decision in ("yes", "always"):
            self.adopt(charter, changes)
            tasks.say("The charter is adopted; the new standing orders start at their next scheduled time.")
        else:
            self.adopted["declined"] = question.charter_hash
            self.save()
            journal("charter", "declined", part="declined", why="you said no to the charter's changes")
            tasks.say("Understood. The new and changed standing orders stay off until the charter changes again.")

    def adopt(self, charter: dict, changes: dict) -> None:
        at = now()
        for order in changes["new"] + changes["changed"]:
            name = order["name"]
            guardrails.offer_grants(PREFIX + name, order.get("needs", {}))   # exactly what the question showed
            guardrails.accept_grants(PREFIX + name)
            self.adopted["orders"][name] = {"hash": digest(order), "order": order}
            # The first run is at its next scheduled time, not one already past.
            self.state["orders"][name] = {"last_slot": at.isoformat()}
        self.adopted["agency"] = charter["agency"]
        self.adopted["agency_hash"] = digest(charter["agency"])
        self.adopted["charter"] = digest(charter)
        self.adopted.pop("declined", None)
        self.save()
        journal("charter", "adopted", orders=[o["name"] for o in changes["new"] + changes["changed"]])

    # --- running ---
    def _last_slot(self, name: str) -> datetime | None:
        last = self.state["orders"].get(name, {}).get("last_slot")
        return datetime.fromisoformat(last) if last else None

    def due(self, at: datetime) -> list[tuple[datetime, dict]]:
        found = []
        for order in self.runnable():
            last = self._last_slot(order["name"])
            slot = latest_slot(order, at, last)
            if slot is not None and (last is None or slot > last):
                found.append((slot, order))
        return sorted(found, key=lambda pair: pair[0])

    def _mark(self, name: str, slot: datetime, result: str | None = None) -> None:
        entry = self.state["orders"].setdefault(name, {})
        entry["last_slot"] = slot.isoformat()
        if result:
            entry["last_result"] = result

    async def tick(self, at: datetime | None = None) -> None:
        at = at or now()
        self.deliver_held(at)   # news held through quiet hours, even while the orders are paused
        if control.paused():
            if self.running():
                self.stop_current("the standing orders were paused")
            return
        self.review_charter()
        if self.running() and self.current_order not in {o["name"] for o in self.runnable()}:
            self.stop_current("its order changed or was removed")
        self.maybe_summary(at)
        if self.running():
            return   # one worker at a time; whatever is due waits (and catches up) behind it
        for slot, order in self.due(at):
            name = order["name"]
            kind = parse_schedule(order["schedule"])[0]
            if kind != "every" and (at - slot).total_seconds() > CATCH_UP_S:
                self._mark(name, slot, "missed")
                journal(name, "missed", part="declined", why=f"its {slot:%H:%M} run was missed by more than 3 hours")
                continue
            if self._runs_today(at) >= self.settings["max_worker_runs_per_day"]:
                self._mark(name, slot, "over budget")
                journal(name, "over budget", part="declined", why="the daily run budget was used up")
                continue
            self._mark(name, at if kind == "every" else slot)   # its workers count as they start
            self.save()
            self.current_order = name
            self.current = asyncio.get_running_loop().create_task(self._run(order))
            break
        self.save()

    def running(self) -> bool:
        return self.current is not None and not self.current.done()

    def stop_current(self, why: str) -> None:
        if self.running():
            self._stop_why = why
            self.current.cancel()

    async def _run(self, order: dict) -> None:
        name = order["name"]
        run = Run(name, self)
        guardrails.current_task.set(PREFIX + name)   # this task's grants are the order's
        guardrails.agency_run.set(run)
        self.state["orders"].setdefault(name, {})["last_run"] = now().isoformat(timespec="seconds")
        journal(name, "started")
        result = "failed"
        try:
            findings = None
            if watcher := WATCHERS.get(order["kind"]):
                findings = await watcher(order)
                if findings is None:
                    result = "fine"
                    journal(name, "done", part="did", summary=watchers.FINE.get(order["kind"], "checked; all is well"))
                    return
            task = make_task(order, findings)
            step = task.steps[0]
            result = await tasks.do_step(task, step, on_usage_limit="skip", start_worker=self.start_worker)
            if result == "done":
                journal(name, "done", part="did", summary=step.note)
                if worth_telling(step.note):   # like task news (said, or texted), but not in quiet hours
                    self.tell(step.note)
            elif result == "skipped":
                journal(name, "skipped", part="declined", why="Claude's usage limit was reached")
            elif result == "over budget":
                journal(name, "over budget", part="declined", why="the daily run budget was used up")
            else:
                journal(name, "blocked", part="wants", what=step.note)
        except asyncio.CancelledError:
            result = "stopped"
            journal(name, "stopped", part="declined", why=self._stop_why or "Jarvis stopped")
            raise
        except Exception as e:
            log.exception("[%s] failed", name)
            journal(name, "failed", part="did", summary=f"failed: {e}")
        finally:
            self.state["orders"].setdefault(name, {})["last_result"] = result
            self.current_order, self._stop_why = None, ""
            self.save()

    # --- the daily summary ---
    def maybe_summary(self, at: datetime) -> None:
        at_time = parse_time(self.settings["summary_at"]) if self.settings["summary_at"] else None
        if at_time is None:
            return
        slot = datetime.combine(at.date(), at_time).astimezone()
        if at < slot or (at - slot).total_seconds() > CATCH_UP_S or self.state.get("summary_day") == at.date().isoformat():
            return
        text = summary(read_journal(self.state.get("summary_since")))
        self.state["summary_day"], self.state["summary_since"] = at.date().isoformat(), now().isoformat(timespec="seconds")
        self.save()
        if text:
            self.tell(text, at)

    def set_paused(self, on: bool) -> None:
        control.set_paused(on)
        if on:
            self.stop_current("the standing orders were paused")

    async def run(self) -> None:
        """The scheduler, as a task in Jarvis's loop until it's cancelled."""
        global active
        active = self
        log.info("standing orders: scheduler started")
        try:
            while True:
                try:
                    await self.tick()
                except Exception:
                    log.exception("standing orders: tick failed")
                await asyncio.sleep(TICK_S)
        finally:
            await self.stop()

    async def stop(self) -> None:
        for job in (self.current, self._asking):
            if job is not None and not job.done():
                if job is self.current:
                    self._stop_why = "Jarvis stopped"
                job.cancel()
                try:
                    await job
                except BaseException:
                    pass


active: "Agency | None" = None   # the scheduler running in this process (Agency.run)


def tell(text: str) -> None:
    """Order news from a watcher (the brief without a worker): held in quiet hours like a report."""
    run = guardrails.agency_run.get()
    agency = run.agency if run is not None else active
    if agency is not None:
        agency.tell(text)
    else:
        tasks.announce(text)


def status(agency: "Agency | None" = None) -> dict:
    """The standing orders for `jarvisctl status`: each adopted or written order, whether it's adopted or
    waiting for the user's yes, its last run and its next; and what's wrong with the charter, if anything."""
    agency = agency or active
    adopted = agency.adopted if agency else _read_json(ADOPTED_FILE, {})
    state = agency.state if agency else _read_json(STATE_FILE, {})
    charter, problems, _ = read_charter()
    written = {o["name"]: o for o in charter["orders"]} if charter else None
    at, rows = now(), []
    for name, entry in adopted.get("orders", {}).items():
        order, seen = entry["order"], state.get("orders", {}).get(name, {})
        last = datetime.fromisoformat(seen["last_slot"]) if seen.get("last_slot") else None
        if written is not None and name not in written:
            standing = "removed"
        elif written is not None and digest(written[name]) != entry["hash"]:
            standing = "changed"   # the adopted copy waits too, until the change is adopted
        else:
            standing = "running" if agency and agency.current_order == name and agency.running() else "adopted"
        rows.append({"name": name, "kind": order["kind"], "schedule": order["schedule"], "state": standing,
                     "last_run": seen.get("last_run"), "last_result": seen.get("last_result"),
                     "next_run": next_slot(order, at, last).isoformat(timespec="minutes")
                     if standing in ("adopted", "running") else None})
    declined = charter is not None and adopted.get("declined") == digest(charter)
    for name, order in (written or {}).items():
        if name not in adopted.get("orders", {}):
            rows.append({"name": name, "kind": order["kind"], "schedule": order["schedule"],
                         "state": "declined" if declined else "waiting", "last_run": None, "last_result": None,
                         "next_run": None})
    return {"orders": rows, "charter_problems": problems[:3]}


def describe_changes(changes: dict, charter: dict) -> tuple[str, str]:
    """The adoption question: a short summary to say, and the full detail to show."""
    bits = []
    if changes["new"]:
        bits.append("new standing orders " + ", ".join(f"{o['name']} ({o['kind']}, {o['schedule']})" for o in changes["new"]))
    if changes["changed"]:
        bits.append("changed " + ", ".join(o["name"] for o in changes["changed"]))
    if changes["limits"]:
        bits.append("new limits")
    grants = []
    for order in changes["new"] + changes["changed"]:
        needs = guardrails.normalized_needs(order.get("needs", {}))
        grants += needs["commands"] + needs["machines"] + needs["folders"]
    may = f"; they may use {', '.join(sorted(set(grants)))} without asking" if grants else ""
    summary = "adopt your charter: " + "; ".join(bits) + may
    detail = json.dumps({"agency": settings_of(charter["agency"]),
                         "orders": changes["new"] + changes["changed"]}, indent=1, ensure_ascii=False)
    return summary, f"Charter changes ({CHARTER_FILE}):\n{detail}"


def summary(entries: list[dict], limit: int = 6) -> str:
    """The daily summary in three parts, or "" when there's nothing to tell."""
    parts = {"did": {}, "declined": {}, "wants": {}}   # item -> how many times, first seen first
    for e in entries:
        part = e.get("part")
        if part not in parts:
            continue
        if part == "wants":
            what = e.get("what") or e.get("why") or e.get("event", "")
            what = re.sub(r", for the [\w-]+ standing order$", "", what)
            what += f" ({e['why']})" if e.get("what") and e.get("why") else ""
        else:
            what = e.get("summary") or e.get("why") or e.get("event", "")
        item = f"{e.get('order', '?')}: {tasks.one_line(what).rstrip('.')}"
        parts[part][item] = parts[part].get(item, 0) + 1
    if not any(parts.values()):
        return ""

    def said(counted: dict) -> str:
        if not counted:
            return "nothing"
        items = [item + (f" ({n} times)" if n > 1 else "") for item, n in counted.items()]
        more = f"; and {len(items) - limit} more" if len(items) > limit else ""
        return "; ".join(items[:limit]) + more

    return (f"Your standing orders since the last summary. Did: {said(parts['did'])}. "
            f"Decided not to: {said(parts['declined'])}. Wants your permission for: {said(parts['wants'])}.")

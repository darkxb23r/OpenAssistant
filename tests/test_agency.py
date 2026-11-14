"""Standing orders (agency.py): the charter, adopting it with one hard question, the scheduler, the
unattended permission rules, budgets, the journal and the daily summary (fake workers and checker)."""
import asyncio, json, os, sys, tempfile
from datetime import datetime, timedelta
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import agency as A
import control
import guardrails as g
import jarvis as j
import tasks as t
from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

said = []
t.say = said.append

# The machines Jarvis has (read_machines): nas only.
g.SSH_DIR.mkdir(exist_ok=True)
g.MACHINES_FILE.write_text("Host nas\n  HostName 10.0.0.5\n  User jarvis\n", encoding="utf-8")

CLOCK = [datetime(2026, 11, 12, 5, 0).astimezone()]
A.now = lambda: CLOCK[0]


def at(h, m=0, day=12):
    return datetime(2026, 11, day, h, m).astimezone()


# Fake workers: WORKER[name] says how each order's run goes; RAN records what ran, in which context.
WORKER, RAN, GATE = {}, [], {}


async def fake_worker(task, step, feedback):
    name = task.name.removeprefix("order-")
    run = g.agency_run.get()
    RAN.append((name, g.current_task.get(), run.order if run else None, task.in_memory, task.worker))
    if name in GATE:
        await GATE[name].wait()
    return WORKER.get(name, {"kind": "done", "summary": f"{name} looked fine", "evidence": "df -h showed 40% used"})


REJECT = [0]   # how many of the next reports the checker turns down (each then gets a retry)


async def fake_verify(step, summary, evidence):
    if REJECT[0]:
        REJECT[0] -= 1
        return False, "not convincing"
    return True, "convincing"


t.run_worker, t.verify = fake_worker, fake_verify
A.WATCHERS.clear()   # every run reaches its (fake) worker; the quick checks are test_watchers.py's


class IO:
    def __init__(self):
        self.asked = []
    def ask(self, summary, detail):
        self.asked.append(summary)
    def say(self, text): ...


io = g._io = IO()


async def no_judge(tool_name, cmd):
    raise AssertionError("a standing order must never reach the safety check")


GOOD = """
[agency]
quiet_hours = "23:00-06:00"
max_worker_runs_per_day = 5
max_actions_per_hour = 50
ask_timeout_minutes = 10
summary_at = "21:00"

[[order]]
name = "nas-backup"
kind = "backup"
schedule = "daily 06:30"
check = "ls -la /backups"
machines = ["nas"]
needs = { commands = ["docker compose pull"], machines = ["nas"] }
model = "haiku"

[[order]]
name = "mail"
kind = "mail"
schedule = "every 2h"

[[order]]
name = "brief"
kind = "brief"
schedule = "weekdays 07:00"
allow = ["calendar", "time"]
"""


def write_charter(text):
    A.CHARTER_FILE.write_text(text, encoding="utf-8")


def problems_of(text):
    write_charter(text)
    return A.read_charter()[1]


def journal(event=None, order=None):
    return [e for e in A.read_journal() if (event is None or e["event"] == event) and (order is None or e["order"] == order)]


async def settle(n=10):
    for _ in range(n):
        await asyncio.sleep(0)


async def wait_until(predicate, timeout=5):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.01)


async def drain(orders):
    """Tick until nothing more starts; the orders that ran, in order."""
    ran = []
    while True:
        before = len(RAN)
        await orders.tick()
        await settle()
        await wait_until(lambda: not orders.running())
        if len(RAN) == before:
            return ran
        ran += [r[0] for r in RAN[before:]]


def charter_question():
    return next(q for q in g._pending_questions if getattr(q, "only", None))


async def main():
    # --- the charter: a good one, and each kind of bad one ---
    assert problems_of((ROOT / "charter.example.toml").read_text(encoding="utf-8")) == []   # the example is valid
    assert problems_of(GOOD) == []
    charter = A.read_charter()[0]
    assert [o["name"] for o in charter["orders"]] == ["nas-backup", "mail", "brief"]
    for bad, why in [
        (GOOD.replace('kind = "backup"', 'kind = "shopping"'), "kind must be one of"),
        (GOOD.replace("daily 06:30", "daily 25:00"), "the time must be HH:MM"),
        (GOOD.replace("daily 06:30", "hourly"), "write daily HH:MM"),
        (GOOD.replace("every 2h", "every 1m"), "at most every 5 minutes"),
        (GOOD.replace('commands = ["docker compose pull"]', 'commands = ["python"]'), "can't be pre-approved"),
        (GOOD.replace('commands = ["docker compose pull"]', 'commands = ["rm -rf"]'), "always needs the user"),
        (GOOD.replace('"ls -la /backups"', '"rm -rf /backups"'), "isn't a read-only command"),
        (GOOD.replace('"ls -la /backups"', '"cat ~/.ssh/jarvis_ed25519"'), "isn't a read-only command"),
        (GOOD.replace('"ls -la /backups"', '"ls /backups; reboot"'), "isn't a read-only command"),
        (GOOD.replace('machines = ["nas"]\n', 'machines = ["mystery"]\n'), "isn't one of my machines"),
        (GOOD.replace('machines = ["nas"] }', 'machines = ["ghost"] }'), "isn't one of my machines"),
        (GOOD.replace('name = "mail"', 'name = "Mail Triage"'), "1-32 lowercase"),
        (GOOD.replace('name = "mail"', 'name = "' + "x" * 33 + '"'), "1-32 lowercase"),
        (GOOD.replace('name = "mail"', 'name = "brief"'), "used twice"),
        (GOOD.replace('model = "haiku"', 'model = "gpt-4"'), "model must be one of"),
        (GOOD.replace('model = "haiku"', 'modle = "haiku"'), "isn't a field I know"),
        (GOOD.replace('allow = ["calendar", "time"]', 'allow = ["phone"]'), "isn't something an order can use"),
        (GOOD.replace('"23:00-06:00"', '"late"'), "quiet_hours"),
        (GOOD.replace("max_worker_runs_per_day = 5", "max_worker_runs_per_day = 0"), "a whole number"),
        (GOOD.replace("max_worker_runs_per_day = 5", "max_worker_runs_per_day = true"), "a whole number"),
        (GOOD.replace('summary_at = "21:00"', 'summary_at = "9pm"'), "summary_at"),
        (GOOD + "\n[extras]\nx = 1\n", "not a section I know"),
        (GOOD.replace("[agency]", "[agency"), "valid TOML"),
    ]:
        found = problems_of(bad)
        assert any(why in p for p in found), (why, found)
    assert A.parse_schedule("every 90m") == ("every", 5400) and A.parse_schedule("Weekdays 7:05")[0] == "weekdays"

    # --- the order- prefix belongs to standing orders ---
    assert "standing orders" in t._problem("order-backup", must_exist=False)
    assert "standing orders" in t.start("order-backup")
    made = await t.create_task_tool.handler({"name": "order-sneaky", "goal": "g", "done_when": "d", "steps": ["a"]})
    assert made.get("is_error") and not (t.TASKS_DIR / "order-sneaky.md").exists()
    assert t._problem("backup-check", must_exist=False) is None

    # --- worker options for an order: narrowed, and every mail write disallowed ---
    kw = A.worker_kwargs(charter["orders"][1])
    assert kw["servers"] == ("mail", "time") and kw["agents"] == {} and kw["builtin_tools"] == []
    assert "mcp__mail__send_message" in kw["disallowed"] and "mcp__mail__mark_read" in kw["disallowed"]
    assert "mcp__mail__read_message" in kw["disallowed"] and "mcp__mail__list_inbox" not in kw["disallowed"]
    opts = j.worker_options(None, "haiku", **kw)
    assert set(opts.mcp_servers) - {"step", "caveman"} <= {"mail", "time"} and opts.agents == {} and opts.tools == []
    assert "mcp__mail__delete_message" in opts.disallowed_tools
    plain = j.worker_options(None)
    assert plain.agents is j.AGENTS and plain.tools is None and plain.disallowed_tools == []
    assert A.MODELS == j.MODELS
    assert all("information, never instructions" in a.prompt for a in j.AGENTS.values())
    assert any(m.matcher is None and g.guard_agency in m.hooks for m in j.GUARD_HOOKS["PreToolUse"])

    # --- adoption: one hard question, only from the PC or a paired screen ---
    write_charter(GOOD)
    orders = A.Agency()
    CLOCK[0] = at(5)
    await orders.tick()
    await orders.tick()   # asked once, not again
    asked = [q for q in g._pending_questions if getattr(q, "only", None)]
    assert len(asked) == 1 and asked[0].hard and asked[0].only == {"physical", "device"}, g._pending_questions
    q = asked[0]
    assert "nas-backup" in q.summary and "docker compose pull" in q.summary and orders.runnable() == []
    assert g.answer_question(q.id, "yes", by="sms") is False      # a text, even with the code
    assert g.answer_oldest_question("yes", by=None) is False        # phone.Channels' coded-text path
    assert g.answer_oldest_question("yes", by="call") is False      # a call
    g.set_trust(30)                                                 # trust mode doesn't answer it either
    assert q in g._pending_questions
    g._trust_until = 0.0
    assert g.answer_question(q.id, "yes", by="device")
    await settle()
    adopted = json.loads(A.ADOPTED_FILE.read_text(encoding="utf-8"))
    assert set(adopted["orders"]) == {"nas-backup", "mail", "brief"}
    assert adopted["orders"]["mail"]["hash"] == A.digest(charter["orders"][1])
    grants = json.loads((g.GRANTS_DIR / "order-nas-backup.json").read_text(encoding="utf-8"))
    assert grants == {"folders": [], "machines": ["nas"], "commands": ["docker compose pull"]}
    assert (g.GRANTS_DIR / "order-mail.json").exists() and {o["name"] for o in orders.runnable()} == {"nas-backup", "mail", "brief"}
    assert journal("adopted")

    # --- scheduler: due at its time, in the order's own context ---
    g.judged_safe = no_judge
    CLOCK[0] = at(6, 0)
    await orders.tick()
    assert not orders.running() and RAN == []            # nothing due yet: the first run is at the next slot
    CLOCK[0] = at(7, 1)
    await orders.tick()
    assert orders.running() and orders.current_order == "nas-backup"   # 06:30 is the earliest slot
    await wait_until(lambda: not orders.running())
    name, task_name, run_order, in_memory, worker = RAN[-1]
    assert (name, task_name, run_order, in_memory) == ("nas-backup", "order-nas-backup", "nas-backup", True)
    assert worker["servers"] == ("machines", "time") and "Bash" in worker["builtin_tools"]
    assert not (t.TASKS_DIR / "order-nas-backup.md").exists() and "order-nas-backup" not in t.names()
    assert journal("done", "nas-backup")[-1]["summary"] == "nas-backup looked fine"
    assert journal("started", "nas-backup")
    assert set(await drain(orders)) == {"mail", "brief"}  # the 07:00 slots, caught up one after another
    assert await drain(orders) == []                      # each slot runs once
    state = json.loads(A.STATE_FILE.read_text(encoding="utf-8"))
    assert state["orders"]["nas-backup"]["last_result"] == "done" and state["runs"]["count"] == 3

    # --- missed by more than 3 hours: skipped and journaled; under 3 hours: caught up ---
    CLOCK[0] = at(10, 1, day=13)                          # Jarvis was down at 06:30 and 07:00
    assert await drain(orders) == ["mail"]                # every-2h runs whenever it's due
    missed = journal("missed")
    assert {e["order"] for e in missed} == {"nas-backup", "brief"} and all(e["part"] == "declined" for e in missed)
    CLOCK[0] = at(9, 0, day=16)                           # a Monday: 06:30 is 2.5 h ago, 07:00 2 h ago
    assert set(await drain(orders)) == {"nas-backup", "brief", "mail"}

    # --- one worker at a time; pausing stops the one running ---
    CLOCK[0] = at(7, 5, day=17)                           # all three are due
    gate = asyncio.Event()
    for n in ("nas-backup", "brief", "mail"):
        GATE[n] = gate
    count = len(RAN)
    await orders.tick()
    await wait_until(lambda: len(RAN) == count + 1)
    first = orders.current_order
    await orders.tick()
    await orders.tick()
    await settle()
    assert len(RAN) == count + 1 and orders.current_order == first
    control.set_paused(True)
    await orders.tick()
    await wait_until(lambda: not orders.running())
    assert journal("stopped", first)[-1]["why"] == "the standing orders were paused"
    await orders.tick()
    assert not orders.running() and len(RAN) == count + 1   # nothing runs while paused
    control.set_paused(False)
    GATE.clear()
    assert set(await drain(orders)) == {"nas-backup", "brief", "mail"} - {first}

    # --- inside an order: no trust mode, no "always" rules, no safety check; grants allow ---
    run = A.Run("nas-backup", orders)
    g.agency_run.set(run); g.current_task.set("order-nas-backup")
    g._trust_until = 1e18                                  # trust mode on: must not count here
    g.approved.add("Bash: docker restart *")
    allowed = await g.decide("Bash", {"command": "docker compose pull"}, None)
    assert isinstance(allowed, PermissionResultAllow)
    assert journal("allowed", "nas-backup")[-1]["reason"] == "the adopted nas-backup standing order"
    run.ask_timeout_s = 0.05                               # an unattended question: no answer in time is a no
    CLOCK[0] = at(12, 0, day=17)
    denied = await g.decide("Bash", {"command": "docker restart plex", "description": "restart plex"}, None)
    assert isinstance(denied, PermissionResultDeny) and not g._pending_questions
    assert "standing order" in io.asked[-1] and journal("unanswered", "nas-backup")[-1]["part"] == "wants"
    run.ask_timeout_s = 60
    asking = asyncio.ensure_future(g.decide("Bash", {"command": "docker restart sonarr"}, None))
    await settle()
    q = g._pending_questions[0]
    assert q.unattended
    g.set_trust(30)                                         # trust mode never answers an order's question
    assert q in g._pending_questions
    g.answer_question(q.id, "always", by="device")         # "always" is a yes, saving no rule
    assert isinstance(await asking, PermissionResultAllow) and "Bash: docker restart sonarr" not in g.approved
    g._trust_until = 0.0
    # quiet hours: no at once, nothing asked
    CLOCK[0] = at(23, 30, day=17)
    asked_before = len(io.asked)
    denied = await g.decide("Bash", {"command": "docker restart radarr"}, None)
    assert isinstance(denied, PermissionResultDeny) and len(io.asked) == asked_before and not g._pending_questions
    assert journal("not asked", "nas-backup")[-1]["why"] == "quiet hours"

    # --- budgets: actions per hour (every tool call), runs per day ---
    orders.adopted["agency"]["max_actions_per_hour"] = 2
    orders._actions.clear()
    hook = lambda: g.guard_agency({"tool_name": "Bash", "tool_input": {}}, None, None)
    assert await hook() == {} and await hook() == {}
    assert (await hook())["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert journal("over budget", "nas-backup")[-1]["part"] == "declined"
    CLOCK[0] = at(0, 31, day=18)
    assert await hook() == {}                               # an hour later there's room again
    g.agency_run.set(None); g.current_task.set(None)
    assert await hook() == {}                               # outside an order: no opinion
    orders.adopted["agency"]["max_worker_runs_per_day"] = 1
    CLOCK[0] = at(16, 0, day=18)                            # a new day: mail (every 2h) is due, the others missed
    assert await drain(orders) == ["mail"]
    CLOCK[0] = at(18, 30, day=18)
    assert await drain(orders) == []
    assert journal("over budget", "mail")[-1]["why"] == "the daily run budget was used up"
    # The budget counts workers, not runs: a retry is a second worker.
    orders.adopted["agency"]["max_worker_runs_per_day"] = 3
    REJECT[0] = 1
    CLOCK[0] = at(20, 31, day=18)
    assert await drain(orders) == ["mail", "mail"] and orders.state["runs"]["count"] == 3
    assert journal("done", "mail")[-1]["summary"] == "mail looked fine"
    # A retry that would go over the budget isn't started; the run ends as over budget.
    orders.adopted["agency"]["max_worker_runs_per_day"] = 4
    REJECT[0] = 1
    CLOCK[0] = at(22, 40, day=18)
    assert await drain(orders) == ["mail"] and orders.state["runs"]["count"] == 4
    assert journal()[-1]["event"] == "over budget" and journal()[-1]["why"] == "the daily run budget was used up"
    assert orders.state["orders"]["mail"]["last_result"] == "over budget"
    orders.adopted["agency"]["max_worker_runs_per_day"] = 5

    # --- a usage limit skips the run instead of waiting ---
    WORKER["mail"] = {"kind": "none", "error": "Claude usage limit reached"}
    CLOCK[0] = at(9, 0, day=19)
    assert (await drain(orders))[0] == "mail"
    assert journal("skipped", "mail")[-1]["part"] == "declined"
    # ...and a blocked step is something it wants permission for
    WORKER["mail"] = {"kind": "blocked", "question": "May I file the newsletters?"}
    CLOCK[0] = at(11, 30, day=19)
    assert await drain(orders) == ["mail"]
    assert journal("blocked", "mail")[-1]["what"] == "May I file the newsletters?"

    # --- the daily summary: three parts, once, at summary_at ---
    said.clear()
    CLOCK[0] = at(20, 59, day=19)
    await orders.tick()
    assert not any("standing orders since" in s for s in said)
    CLOCK[0] = at(21, 0, day=19)
    await orders.tick()
    text = next(s for s in said if "standing orders since" in s)
    assert "Did: " in text and "Decided not to: " in text and "Wants your permission for: " in text
    assert "nas-backup looked fine" in text and "usage limit" in text and "May I file the newsletters" in text
    assert text in t.events
    said.clear()
    await orders.tick()
    assert not any("standing orders since" in s for s in said)   # once a day
    assert A.summary([]) == ""

    # --- quiet hours (23:00-06:00): order news and the summary wait, and survive a restart ---
    WORKER.pop("mail", None)
    await wait_until(lambda: not orders.running())          # the mail run the summary ticks started
    orders.adopted["agency"]["summary_at"] = "23:45"
    said.clear()
    CLOCK[0] = at(23, 30, day=20)
    assert await drain(orders) == ["mail"]                  # it runs, but its report isn't said
    assert not any("mail looked fine" in s for s in said)
    CLOCK[0] = at(23, 46, day=20)
    await orders.tick()                                      # nor is the summary
    assert said == [], said
    held = json.loads(A.HELD_FILE.read_text(encoding="utf-8"))
    assert [h["text"] for h in held][0] == "mail looked fine" and "standing orders since" in held[1]["text"]
    assert A.HELD_FILE.parent == A.AGENCY_DIR
    later = A.Agency()                                       # a restart keeps them
    later.adopted, later._charter = orders.adopted, orders._charter
    CLOCK[0] = at(5, 59, day=21)
    later.deliver_held()
    assert said == [] and A.HELD_FILE.exists()               # still quiet
    CLOCK[0] = at(6, 0, day=21)
    later.deliver_held()
    assert len(said) == 1 and said[0].startswith("While it was quiet: mail looked fine")
    assert "standing orders since" in said[0] and said[0] in t.events and not A.HELD_FILE.exists()
    later.deliver_held()
    assert len(said) == 1                                    # told once
    # A brief said without a worker (agency.tell) is held the same way; outside quiet hours it's said.
    CLOCK[0] = at(1, 0, day=21)
    run = A.Run("brief", orders)
    g.agency_run.set(run)
    A.tell("Good morning. Nothing is waiting.")
    g.agency_run.set(None)
    assert len(said) == 1 and A.HELD_FILE.exists()
    CLOCK[0] = at(7, 0, day=21)
    await orders.tick()
    assert said[-1] == "Good morning. Nothing is waiting."
    orders.adopted["agency"]["summary_at"] = "21:00"

    # --- a changed charter: the changed order waits on a new question; a no keeps it off ---
    write_charter(GOOD.replace("every 2h", "every 3h"))
    await orders.tick()
    q = charter_question()
    assert "changed mail" in q.summary
    assert {o["name"] for o in orders.runnable()} == {"nas-backup", "brief"}
    assert g.answer_question(q.id, "no", by="physical")
    await settle()
    await orders.tick()
    assert not any(getattr(q, "only", None) for q in g._pending_questions)   # not asked again
    assert "mail" not in {o["name"] for o in orders.runnable()}
    # changed again while a question is open: the old question goes, a new one asks
    write_charter(GOOD.replace("every 2h", "every 4h"))
    await orders.tick()
    first = charter_question()
    write_charter(GOOD.replace("every 2h", "every 5h"))
    await orders.tick()
    second = charter_question()
    assert first is not second and first not in g._pending_questions and "changed mail" in second.summary
    g.answer_question(second.id, "yes", by="physical")
    await settle()
    assert "mail" in {o["name"] for o in orders.runnable()}

    # --- removing an order drops its grants; a broken charter keeps what was adopted ---
    write_charter(GOOD.split("[[order]]\nname = \"mail\"")[0])   # nas-backup only
    await orders.tick()
    assert not (g.GRANTS_DIR / "order-mail.json").exists() and not (g.GRANTS_DIR / "order-brief.json").exists()
    assert (g.GRANTS_DIR / "order-nas-backup.json").exists() and journal("removed", "mail")
    assert set(json.loads(A.ADOPTED_FILE.read_text(encoding="utf-8"))["orders"]) == {"nas-backup"}
    write_charter("this is [not toml")
    said.clear()
    await orders.tick()
    await orders.tick()
    assert len([s for s in said if "charter has a problem" in s]) == 1 and [o["name"] for o in orders.runnable()] == ["nas-backup"]

    # --- "Pause your standing orders": the user's words, and the kill switch ---
    for line in ("Pause your standing orders", "Jarvis, stop all standing orders.", "please pause the standing orders for now"):
        assert A.pause_request(line) is True, line
    for line in ("Resume your standing orders", "start the standing orders again", "unpause standing orders please"):
        assert A.pause_request(line) is False, line
    for line in ("what are my standing orders?", "pause the music", "stop asking"):
        assert A.pause_request(line) is None, line
    orders.set_paused(True)
    assert control.paused()
    orders.set_paused(False)
    assert not control.paused()

    await orders.stop()
    g.decline_all_questions()


asyncio.run(main())
print("ALL OK")

"""Approving a task's plan once: what it needs is allowed for that task's workers, and only them."""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import guardrails as g
import tasks as t


async def judge_says_no(tool_name, cmd):
    return False


g.judged_safe = judge_says_no
t.say = lambda text: None


class IO:
    def __init__(self): self.reply, self.q = "no", None
    def ask(self, s, d):
        self.q = s
        asyncio.get_running_loop().call_soon(g.answer_oldest_question, self.reply)
    def say(self, text): pass


io = g._io = IO()


async def decide(tool, inp, reply="no"):
    io.reply, io.q = reply, None
    result = await g.ask_before_risky(tool, inp, None)
    return ("ask+" if io.q else "") + type(result).__name__.replace("PermissionResult", "").lower()


async def tool(fn, **args):
    r = await fn.handler(args)
    return r["content"][0]["text"], r.get("is_error", False)


async def main():
    backups = home / "backups"
    # --- needs that can't be pre-approved are refused when planning ---
    for bad in [{"commands": ["python"]}, {"commands": ["docker restart x; rm -rf /"]}, {"commands": ["shutdown"]},
                {"commands": ["rm"]}, {"commands": ["winget install"]}, {"folders": [str(home / ".ssh")]},
                {"folders": ["relative/dir"]}]:
        text, err = await tool(t.create_task_tool, name="bad", goal="g", done_when="d", steps=["s"], needs=bad)
        assert err and "can't be pre-approved" in text, (bad, text)
    text, err = await tool(t.create_task_tool, name="backup", goal="Back up the media", done_when="Backed up.",
                           steps=["Copy", "Restart the service"],
                           needs={"folders": [str(backups)], "machines": ["NAS"], "commands": ["docker restart", "Restart-Service"]})
    assert not err, text
    assert t.load("backup").needs == {"folders": [str(backups)], "machines": ["NAS"], "commands": ["docker restart", "Restart-Service"]}

    # --- starting is the approval: it always asks, shows the needs, and "always" saves nothing ---
    g.set_trust(60)
    assert await decide("mcp__tasks__start_task", {"name": "backup"}, "always") == "ask+allow"
    assert io.q.startswith("start the backup task, 2 steps. Goal: Back up the media; it may work in ") and \
        "use nas; run docker restart, restart-service without asking" in io.q, io.q
    g.set_trust(0)
    # someone edits the task file after the question: the grants are what the question showed
    task = t.load("backup")
    task.needs["commands"].append("format")
    task.save()
    g.accept_grants("backup")
    assert g._grants["backup"]["commands"] == ["docker restart", "restart-service"]

    write_backup = {"file_path": str(backups / "x.txt")}
    # --- in the conversation (no task), nothing is granted ---
    assert await decide("Write", write_backup) == "ask+deny"
    assert await decide("PowerShell", {"command": "docker restart plex"}) == "ask+deny"

    # --- inside the backup task's runner, the granted things pass without asking ---
    async def as_task(name, coro):
        g.current_task.set(name)
        return await coro

    run = lambda name, *a: asyncio.create_task(as_task(name, decide(*a)))
    assert await run("backup", "Write", write_backup) == "allow"
    assert await run("backup", "PowerShell", {"command": "docker restart plex"}) == "allow"
    assert await run("backup", "PowerShell", {"command": "Restart-Service  Spooler"}) == "allow"
    assert await run("backup", "Bash", {"command": "ssh nas docker restart sonarr"}) == "allow"   # granted machine
    assert await run("backup", "Bash", {"command": f"rm -rf {backups.as_posix()}/old"}) == "allow"  # task-only free zone
    # ...but not more than that
    assert await run("backup", "PowerShell", {"command": "docker rm plex"}) == "ask+deny"
    assert await run("backup", "PowerShell", {"command": "docker restart plex; Remove-Item C:\\x"}) == "ask+deny"
    assert await run("backup", "PowerShell", {"command": "Restart-Computer"}) == "ask+deny"         # always asks
    assert await run("backup", "Bash", {"command": "ssh pi uptime"}) == "ask+deny"                  # not granted
    assert await run("backup", "Write", {"file_path": str(home / ".ssh" / "config")}) == "ask+deny"
    # ...and only for that task
    assert await run("other", "Write", write_backup) == "ask+deny"

    # --- grants survive a restart, and end with the task ---
    g._grants.clear()
    assert await run("backup", "PowerShell", {"command": "docker restart plex"}) == "allow"   # reloaded from disk
    task = t.load("backup")
    for step in task.steps:
        step.mark = "x"
    task.status = "running"
    task.save()
    t.start = t.start   # the runner finishes a task whose steps are all done
    await t._run("backup")
    assert t.load("backup").status == "done"
    assert not (g.GRANTS_DIR / "backup.json").exists()
    assert await run("backup", "PowerShell", {"command": "docker restart plex"}) == "ask+deny"


asyncio.run(main())
print("ALL OK")

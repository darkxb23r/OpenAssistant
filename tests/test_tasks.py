"""Long tasks: task files, and the runner's step loop (with fake workers and a fake checker)."""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import guardrails as g
import tasks as t

said = []
t.say = said.append
t.USAGE_LIMIT_PAUSE_S = 0

# Fake workers: SCRIPT[(task, step)] is a list of outcomes, one per attempt.
SCRIPT, VERDICTS, ASKED = {}, {}, []


async def fake_worker(task, step, feedback):
    plan = SCRIPT.get((task.name, step.number), [{"kind": "done", "summary": f"did {step.number}", "evidence": "ok"}])
    outcome = plan.pop(0) if len(plan) > 1 else plan[0]
    if outcome.get("ask"):                       # a worker hitting a permission question
        ASKED.append(await g.ask_user("run a command: x", "detail", False))
    return dict(outcome)


async def fake_verify(step, summary, evidence):
    plan = VERDICTS.get(step.text, [(True, "fine")])
    return plan.pop(0) if len(plan) > 1 else plan[0]


t.run_worker, t.verify = fake_worker, fake_verify


class IO:
    def ask(self, s, d):
        self.q = s
        asyncio.get_running_loop().call_soon(g.answer_oldest_question, "yes")
    def say(self, text): pass


g._io = IO()


async def tool(fn, **args):
    return (await fn.handler(args))["content"][0]["text"]


async def wait_until(predicate, timeout=5):
    for _ in range(int(timeout / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def main():
    # --- plan and run a task to the end ---
    text = await tool(t.create_task_tool, name="tidy-up", goal="Tidy the workspace", done_when="Folder is tidy.",
                      steps=["List files", "Sort them\ninto folders -- by type", "Write a summary"])
    assert "3 steps" in text
    task = t.load("tidy-up")
    assert task.status == "planned" and [s.text for s in task.steps][1] == "Sort them into folders - by type"
    assert t.load("tidy-up").render() == task.render()                       # round trip
    assert "already exists" in await tool(t.create_task_tool, name="tidy-up", goal="x", done_when="x", steps=["x"])
    assert "lowercase" in await tool(t.create_task_tool, name="Tidy Up", goal="x", done_when="x", steps=["x"])
    VERDICTS["Sort them into folders - by type"] = [(False, "no listing shown"), (True, "listing shown")]
    SCRIPT[("tidy-up", 1)] = [{"kind": "done", "summary": "listed", "evidence": "ls output", "ask": True}]
    assert "background" in await tool(t.start_task_tool, name="tidy-up")
    await wait_until(lambda: t.load("tidy-up").status == "done")
    task = t.load("tidy-up")
    assert all(s.mark == "x" for s in task.steps) and task.steps[0].note == "listed"
    assert any("not accepted: no listing shown" in line for line in task.log)          # retried after the check
    assert said[-1] == "The tidy-up task is done. Shall I go over it?"      # a notice, and an offer
    # The conversation hears what it was and how to recap it, when the user is ready (jarvis.py drains this).
    assert "Folder is tidy." in t.events[-1] and "task_status for tidy-up" in t.events[-1]
    assert "only when they say they're ready" in t.events[-1]
    assert ASKED == ["yes"] and g._io.q == "run a command: x, for the tidy-up task"   # questions name the task
    assert "already done" in await tool(t.start_task_tool, name="tidy-up")

    # --- a step that needs the user, then their answer ---
    await tool(t.create_task_tool, name="setup", goal="Set things up", done_when="All set.", steps=["Pick a port", "Configure"])
    SCRIPT[("setup", 1)] = [{"kind": "blocked", "question": "Which port, 8080 or 9090?"},
                            {"kind": "done", "summary": "port 9090", "evidence": "config shows 9090"}]
    await tool(t.start_task_tool, name="setup")
    await wait_until(lambda: t.load("setup").status == "waiting")
    task = t.load("setup")
    assert task.steps[0].mark == "?" and task.steps[0].note == "Which port, 8080 or 9090?"
    assert said[-1] == "The setup task needs you: Which port, 8080 or 9090?"
    status = await tool(t.task_status_tool)
    assert "setup: waiting, 0/2 steps done" in status and "tidy-up: done, 3/3 steps done" in status
    await tool(t.answer_task_tool, name="setup", answer="Use 9090.")
    await wait_until(lambda: t.load("setup").status == "done")
    assert "(step 1): Use 9090." in t.load("setup").answers[0]

    # --- usage limits pause without using up tries; repeated failures go to the user ---
    await tool(t.create_task_tool, name="limits", goal="g", done_when="d", steps=["Busy step", "Hopeless step"])
    SCRIPT[("limits", 1)] = [{"kind": "none", "error": "Claude usage limit reached"}, {"kind": "done", "summary": "s", "evidence": "e"}]
    VERDICTS["Hopeless step"] = [(False, "still no proof")]
    await tool(t.start_task_tool, name="limits")
    await wait_until(lambda: t.load("limits").status == "waiting")
    task = t.load("limits")
    assert task.steps[0].mark == "x" and any("usage limit hit" in line for line in task.log)
    assert task.steps[1].mark == "?" and "couldn't get step 2 done" in task.steps[1].note

    # --- a worker that stops without reporting counts as a failed try ---
    await tool(t.create_task_tool, name="quiet", goal="g", done_when="d", steps=["Mumble"])
    SCRIPT[("quiet", 1)] = [{"kind": "none"}, {"kind": "done", "summary": "s", "evidence": "e"}]
    await tool(t.start_task_tool, name="quiet")
    await wait_until(lambda: t.load("quiet").status == "done")
    assert any("ended without a report" in line for line in t.load("quiet").log)

    # --- pausing, and resuming running tasks after a restart ---
    gate = asyncio.Event()

    async def slow_worker(task, step, feedback):
        await gate.wait()
        return {"kind": "done", "summary": "s", "evidence": "e"}

    t.run_worker = slow_worker
    await tool(t.create_task_tool, name="long", goal="g", done_when="d", steps=["Slow step", "Next"])
    await tool(t.start_task_tool, name="long")
    await asyncio.sleep(0.05)
    assert "Paused" in await tool(t.pause_task_tool, name="long")
    await asyncio.sleep(0.05)
    assert t.load("long").status == "paused" and "long" not in t._running
    task = t.load("long")
    task.status = "running"              # as if Jarvis stopped mid-task
    task.save()
    assert t.resume_all() == ["long"]
    gate.set()
    await wait_until(lambda: t.load("long").status == "done")


asyncio.run(main())
print("ALL OK")

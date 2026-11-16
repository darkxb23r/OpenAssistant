"""Research sessions (research.py): rounds until the deadline or nothing new, one report in the vault,
the user told once (said, texted or rung), stopping early, one at a time, resuming after a restart,
and the round's session: web search and reading only, the browser read-only and only when asked."""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import jarvis as j
import guardrails as g
import research as r
import tasks

clock = [1_000_000.0]
r.now = lambda: clock[0]
said, texted, rung = [], [], []
r.say = said.append
r.call = rung.append


async def text_ok(message):
    texted.append(message)
    return True

r.text = text_ok
script: list[dict] = []        # what each fake round finds
briefs: list[str] = []


async def fake_round(state, minutes_left):
    clock[0] += 5 * 60            # each round takes five minutes
    briefs.append(f"{minutes_left} min left; questions {state['questions']}")
    return script.pop(0) if script else {"findings": [], "questions": None, "new": False}


async def fake_report(state):
    return {"summary": f"Found {len(state['findings'])} things.", "report": "## Answer\nIt's 42."}

r.run_round, r.write_report = fake_round, fake_report


def found(text, questions):
    return {"findings": [text], "questions": questions, "new": True}


async def wait_done(name):
    for _ in range(200):
        if r.load(name)["status"] in ("done", "failed") and not r._running:
            return r.load(name)
        await asyncio.sleep(0.01)
    raise AssertionError(f"{name} never finished: {r.load(name)}")


async def main():
    # Default half an hour; rounds go on until nothing is left to find out; one report, said once.
    script[:] = [found("Batteries: LFP lasts longer.", ["cost?"]), found("Cost: about $500/kWh.", [])]
    reply = r.start("home battery options")
    assert reply.startswith("Researching for 30 minutes"), reply
    name = "home-battery-options"
    s = r.load(name)
    assert s["deadline"] - s["started"] == 30 * 60 and not s["browser"] and s["then"] == "say"
    assert r.start("something else").startswith("I'm already researching"), "one at a time"
    s = await wait_done(name)
    assert s["rounds"] == 2 and s["status"] == "done" and s["questions"] == []
    notes = r.notes_path(s).read_text(encoding="utf-8")
    assert notes.index("It's 42.") < notes.index("## Findings") and "LFP lasts longer" in notes and "tags: [research]" in notes
    assert r.notes_path(s).parent == j.knowledge.VAULT / "Research"
    assert said == ["Your research on home battery options is done. Want the recap?"], said   # no recap yet
    # The conversation gets the summary and where the report is, to recap from when the user is ready.
    assert "Found 2 things." in tasks.events[-1] and str(r.notes_path(s)) in tasks.events[-1]
    assert "only when they say they're ready" in tasks.events[-1]
    assert "30 min left" in briefs[0] and "cost?" in briefs[1]

    # The deadline ends it, however much is left to look into; no updates along the way.
    said.clear(); briefs.clear()
    script[:] = [found(f"fact {i}", [f"q{i}"]) for i in range(20)]
    r.start("solar panels", minutes=15)
    s = await wait_done("solar-panels")
    assert s["rounds"] == 3 and len(said) == 1, (s["rounds"], said)   # 15 minutes of 5-minute rounds

    # Rounds that find nothing new, three in a row: done early.
    said.clear()
    script[:] = [found("one", ["more?"])] + [{"findings": [], "questions": ["more?"], "new": False}] * 5
    r.start("obscure thing", minutes=120)
    s = await wait_done("obscure-thing")
    assert s["rounds"] == 4 and len(said) == 1

    # Nothing found at all: said plainly, no report written.
    said.clear()
    script[:] = [{"findings": [], "questions": [], "new": False}]
    r.start("nothing here", minutes=10)
    s = await wait_done("nothing-here")
    assert "couldn't find anything useful" in said[0] and s["report"] == ""

    # By text, or by a call, as asked; a text that fails is said instead.
    said.clear()
    script[:] = [found("x", [])]
    r.start("by text", then="text")
    await wait_done("by-text")
    assert len(texted) == 1 and "Text me when you want the recap" in texted[0] and not said
    script[:] = [found("x", [])]
    r.start("by call", then="call")
    await wait_done("by-call")
    assert len(rung) == 1 and "by call" in rung[0] and not said

    async def text_fails(message):
        return False
    r.text = text_fails
    script[:] = [found("x", [])]
    r.start("text fails", then="text")
    await wait_done("text-fails")
    assert len(said) == 1
    r.text = text_ok

    # Stopping early writes up what's there.
    said.clear()
    gate = asyncio.Event()

    async def slow_round(state, minutes_left):
        await gate.wait()
        return found("never", [])
    r.run_round = slow_round
    r.start("stopped early", minutes=60)
    await asyncio.sleep(0.05)
    s = r.load("stopped-early"); s["findings"] = ["half a finding"]; r.save(s)
    assert r.stop() == "Stopping; I'll write up what I have."
    s = await wait_done("stopped-early")
    assert len(said) == 1 and "Found 1 things." in tasks.events[-1]
    assert r.stop() == "No research is running."
    r.run_round = fake_round

    # After a restart: a session whose time ran out meanwhile goes straight to its report.
    said.clear()
    s = {"name": "left-running", "topic": "left running", "started": clock[0] - 3600, "deadline": clock[0] - 60,
         "browser": False, "then": "say", "status": "running", "rounds": 2, "findings": ["kept"], "questions": ["q"],
         "report": ""}
    r.save(s)
    assert r.resume_all() == ["left running"]
    s = await wait_done("left-running")
    assert s["rounds"] == 2 and len(said) == 1
    assert "left running (done, 2 rounds)" in r.status()

    # Permissions: the public web needs no question; the user's logged-in sites ask (hard), every time.
    g._io = type("IO", (), {"ask": lambda *a: None, "say": lambda *a: None, "notify": lambda *a: None})()
    allowed = await g.ask_before_risky(g.START_RESEARCH, {"topic": "x"}, None)
    assert type(allowed).__name__ == "PermissionResultAllow" and not g._pending_questions
    asking = asyncio.create_task(g.ask_before_risky(g.START_RESEARCH, {"topic": "my bank", "logged_in_sites": True}, None))
    await asyncio.sleep(0)
    q = g._pending_questions[0]
    assert q.hard and "logged into" in q.summary
    g.answer_oldest_question("no")
    assert type(await asking).__name__ == "PermissionResultDeny"
    assert {"mcp__research__research_status", "mcp__research__stop_research"} <= set(g.AUTO_APPROVED)
    assert g.START_RESEARCH not in g.AUTO_APPROVED

    # A round's session: web search and reading and its own notes; the browser only when asked, read-only.
    server = {"type": "sdk", "name": "research", "instance": None}
    opts = j.research_options(server, browser=False)
    assert opts.tools == ["WebSearch", "WebFetch"] and list(opts.mcp_servers) == ["research"]
    assert not any(t.startswith("mcp__browser__") for t in opts.allowed_tools)
    assert "mcp__research__add_findings" in opts.allowed_tools and opts.cwd == str(r.STATE_DIR)
    assert type(await opts.can_use_tool("Bash", {"command": "ls"}, None)).__name__ == "PermissionResultDeny"
    j.MCP_SERVERS.setdefault("browser", {"type": "stdio", "command": "x"})
    opts = j.research_options(server, browser=True)
    browse = [t for t in opts.allowed_tools if t.startswith("mcp__browser__")]
    assert browse and "browser" in opts.mcp_servers
    for never in ("browser_click", "browser_type", "browser_fill_form", "browser_file_upload", "browser_evaluate"):
        assert f"mcp__browser__{never}" not in browse, never


asyncio.run(main())
print("ALL OK")

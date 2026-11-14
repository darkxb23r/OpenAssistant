"""Mail and calendar tools: sends, deletes and calendar changes always ask, even in trust mode."""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = tempfile.mkdtemp()
os.environ["USERPROFILE"] = home; os.environ["HOME"] = home
os.environ["JARVIS_WORKSPACE"] = os.path.join(home, "ws")
sys.path.insert(0, str(ROOT))
import jarvis as j
import guardrails as g


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


async def main():
    g.set_trust(60)
    send = {"to": ["bob@x.com"], "subject": "Invoice", "body": "hi"}
    assert await run("mcp__mail__send_message", send, "always") == "ask+allow"
    assert g._io.q[0] == "send an email to bob@x.com, subject Invoice", g._io.q
    assert await run("mcp__mail__send_message", send) == "ask+deny"   # "always" saved nothing
    assert await run("mcp__mail__delete_message", {"uid": 4, "folder": "INBOX", "purge": True}) == "ask+deny"
    assert "permanently delete" in g._io.q[0]
    assert await run("mcp__calendar__create_event", {"title": "Dentist", "attendees": [{"email": "a@b"}]}) == "ask+deny"
    assert g._io.q[0] == "add Dentist to your calendar, sending invitations"
    assert await run("mcp__calendar__add_alarm", {"uid": "x"}) == "allow"   # trust mode covers ordinary ones
    g.set_trust(0)
    assert await run("mcp__calendar__add_alarm", {"uid": "x"}) == "ask+deny"
    assert "mcp__mail__read_message" in g.AUTO_APPROVED and "mcp__mail__send_message" not in g.AUTO_APPROVED
    assert "mail" not in j.MCP_SERVERS   # secrets.env in this temp home is empty


asyncio.run(main())
print("ALL OK")

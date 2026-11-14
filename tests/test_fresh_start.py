"""A fresh conversation (--new, `jarvisctl restart --new`) is never started without first saving the
old one's lasting facts to memory: only when there is an old one, with nothing but the memory tools,
and a failure keeps the old conversation."""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import jarvis as j
from claude_agent_sdk import ResultMessage

seen = {}


class FakeSaver:
    """Stands in for the Claude session that saves the facts."""
    fail = False

    def __init__(self, options):
        seen["options"] = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def query(self, prompt):
        seen["prompt"] = prompt

    async def receive_response(self):
        if FakeSaver.fail:
            yield ResultMessage("error_during_execution", 1, 1, True, 1, "s", result="API Error: ECONNREFUSED")
            return
        await j.remember_tool.handler({"fact": "The user's main project is ReclaimerForge."})
        yield ResultMessage("success", 1, 1, False, 1, "s", result="DONE")


j.ClaudeSDKClient = FakeSaver


async def main():
    # No old conversation: nothing to save, and no Claude session is started.
    assert await j.save_before_fresh_start() and "options" not in seen
    sessions = j.session_dir()
    sessions.mkdir(parents=True)
    (sessions / "old.jsonl").write_text("{}\n", encoding="utf-8")
    j.write_memory(["- The user likes short answers."])

    # The old conversation is reopened with only the memory tools, and the facts are saved.
    assert await j.save_before_fresh_start()
    opts = seen["options"]
    assert opts.continue_conversation and opts.tools == [] and not opts.agents
    assert list(opts.mcp_servers) == ["memory"]
    assert opts.allowed_tools == ["mcp__memory__remember", "mcp__memory__forget"]
    denied = await opts.can_use_tool("Bash", {"command": "ls"}, None)
    assert type(denied).__name__ == "PermissionResultDeny"
    assert "The user likes short answers." in seen["prompt"] and "Never passwords" in seen["prompt"]
    assert j.read_memory() == ["- The user likes short answers.", "- The user's main project is ReclaimerForge."]

    # If saving fails, the caller keeps the old conversation.
    FakeSaver.fail = True
    assert not await j.save_before_fresh_start()


asyncio.run(main())
print("ALL OK")

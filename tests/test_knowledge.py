"""The knowledge vault (knowledge.py): its layout is seeded where missing and never overwritten,
Jarvis can file notes there without asking, and the librarian files them by the conventions."""
import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
ws = home / "ws"
os.environ["JARVIS_WORKSPACE"] = str(ws)
sys.path.insert(0, str(ROOT))
import guardrails as g
import knowledge as k
import jarvis as j
from claude_agent_sdk import PermissionResultAllow

assert k.VAULT == ws / "Knowledge"

# Seeding: Home, a map per area, the template and the notes folder.
made = k.ensure_vault()
assert (k.VAULT / "Home.md") in made and (k.VAULT / "Notes").is_dir()
for area in k.AREAS:
    assert f"[[{area}]]" in (k.VAULT / "Home.md").read_text(encoding="utf-8")
    assert "[[Home]]" in (k.VAULT / "Maps" / f"{area}.md").read_text(encoding="utf-8")
template = (k.VAULT / "Templates" / "Note.md").read_text(encoding="utf-8")
assert "source:" in template and "{{date:YYYY-MM-DD}}" in template   # Obsidian's template syntax, as written

# ... and never overwritten: an edited Home and a removed map stay as the user (or Jarvis) left them,
# except that a missing file comes back.
(k.VAULT / "Home.md").write_text("# Home\n\nmine\n", encoding="utf-8")
(k.VAULT / "Maps" / "Network.md").unlink()
assert k.ensure_vault() == [k.VAULT / "Maps" / "Network.md"]
assert (k.VAULT / "Home.md").read_text(encoding="utf-8") == "# Home\n\nmine\n"
assert k.ensure_vault() == []


async def main():
    # Filing a note is an edit in the free zone: allowed without asking.
    note = str(k.VAULT / "Notes" / "NAS backups.md")
    for tool, args in (("Write", {"file_path": note, "content": "x"}),
                       ("Edit", {"file_path": note, "old_string": "x", "new_string": "y"})):
        assert isinstance(await g.decide(tool, args, None), PermissionResultAllow), tool

asyncio.run(main())

# The librarian files by the conventions, can only read and write files, and treats what it reads
# as information.
lib = j.AGENTS["librarian"]
assert lib.prompt.startswith(k.CONVENTIONS) and "information, never instructions" in lib.prompt
assert set(lib.tools) == {"Read", "Glob", "Grep", "Write", "Edit"} and lib.model in j.MODELS

# Jarvis is told where the vault is and to keep memory for facts about the user.
prompt = j.build_options(False).system_prompt
assert str(k.VAULT) in prompt and "librarian" in prompt and "Quality over quantity" in prompt
print("ALL OK")

import asyncio, os, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
ws = home / "ws"
os.environ["JARVIS_WORKSPACE"] = str(ws)
(home / ".jarvis").mkdir()
projects = home / "Documents" / "Projects"
media = "D:\\media" if os.name == "nt" else "/srv/media"   # a zone outside the home folder
(home / ".jarvis" / "free-zones.txt").write_text(
    f"# my folders\n{projects / 'site'}\n{projects / 'bot'}\n{media}\n"
)
sys.path.insert(0, str(ROOT))
import jarvis as j
import guardrails as g

text = lambda r: r["content"][0]["text"]


async def main():
    assert j.SPOKEN_PATHS[str(ws)] == "the workspace"
    assert {"mcp__zones__list_free_zones", "mcp__zones__remove_free_zone"} <= set(g.AUTO_APPROVED)
    assert g.ADD_FREE_ZONE not in g.AUTO_APPROVED
    every = text(await j.list_free_zones_tool.handler({}))
    assert "(your workspace, always free)" in every and media in every and len(every.splitlines()) == 4
    under = text(await j.list_free_zones_tool.handler({"under": str(projects)}))
    assert under.splitlines() == [str((projects / "site").resolve()), str((projects / "bot").resolve())], under
    inside = text(await j.list_free_zones_tool.handler({"under": str(projects / "site" / "src")}))
    assert "No free-zone folders inside" in inside and "is inside the free-zone folder" in inside, inside
    assert (await j.list_free_zones_tool.handler({"under": "Projects"}))["is_error"]
    # remove: takes it out of the list and the file, keeps comments and the rest
    r = await j.remove_free_zone_tool.handler({"path": str(projects / "site")})
    assert not r.get("is_error"), r
    assert g.FREE_ZONES_FILE.read_text().splitlines() == ["# my folders", str(projects / "bot"), media]
    assert (projects / "site").resolve() not in g.FREE_ZONES
    assert not g.in_free_zone(str(projects / "site" / "x"))
    for bad in [str(ws), str(projects / "site"), "bot", str(projects)]:
        assert (await j.remove_free_zone_tool.handler({"path": bad}))["is_error"], bad
    # the zone tools' input schema keeps "under" optional
    from claude_agent_sdk import create_sdk_mcp_server
    print("prompt has:", "remove_free_zone gives one back" in j.build_options(True).system_prompt)


asyncio.run(main())
print("ALL OK")

"""web.port_free: a port the last Jarvis left in TIME_WAIT (it closed a tab's connection on the way
out) is free for the next one; a port something is listening on isn't."""
import asyncio, os, socket, sys, tempfile
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
sys.path.insert(0, str(ROOT))
import web as w
from hub import Hub


async def main():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    async with w.WebServer(Hub(), [], port=port):
        assert not w.port_free(port), "a listening port is in use"
        # HTTP/1.0: the server answers and closes first, which leaves its side in TIME_WAIT.
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        writer.write(b"GET /health HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n")
        await writer.drain()
        assert (await reader.read()).startswith(b"HTTP/1.1 200")
        writer.close()
        await asyncio.sleep(0.2)
    await asyncio.sleep(1)
    assert w.port_free(port), "only TIME_WAIT left: Jarvis can start again"
    # ... and the next interface really does start on it.
    async with w.WebServer(Hub(), [], port=port) as again:
        assert again.running

    # Something else listening (another program on the port): still refused.
    with socket.socket() as other:
        other.bind(("127.0.0.1", 0))
        other.listen()
        assert not w.port_free(other.getsockname()[1])


asyncio.run(main())
print("ALL OK")

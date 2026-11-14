"""Local models through Ollama (provider.py): the settings, the startup check against a (fake) Ollama,
falling back to Claude when it can't be used, and every kind of session getting the same endpoint."""
import json, os, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]  # the project folder

home = Path(tempfile.mkdtemp())
os.environ["USERPROFILE"] = str(home); os.environ["HOME"] = str(home)
os.environ["JARVIS_WORKSPACE"] = str(home / "ws")
(home / ".jarvis").mkdir()
sys.path.insert(0, str(ROOT))
import provider as p
import settings
import mcp_launch

SECRETS = mcp_launch.SECRETS_FILE


def secrets(**values):
    SECRETS.write_text("".join(f"{k}={v}\n" for k, v in values.items()), encoding="utf-8")


# --- the settings ---
assert p.wanted({}) is None and p.wanted({"MODEL_PROVIDER": "claude"}) is None
assert p.wanted({"MODEL_PROVIDER": "ollama", "OLLAMA_MODEL": "qwen3-coder"}) == {
    "url": "http://localhost:11434", "models": {"sonnet": "qwen3-coder", "haiku": "qwen3-coder", "opus": "qwen3-coder"}}
got = p.wanted({"MODEL_PROVIDER": "Ollama", "OLLAMA_MODEL": "qwen3-coder", "OLLAMA_FAST_MODEL": "gemma3:4b",
                "OLLAMA_BIG_MODEL": "gpt-oss:120b", "OLLAMA_URL": "http://192.168.1.5:11434/"})
assert got == {"url": "http://192.168.1.5:11434",
               "models": {"sonnet": "qwen3-coder", "haiku": "gemma3:4b", "opus": "gpt-oss:120b"}}
for bad in ({"MODEL_PROVIDER": "ollama"}, {"MODEL_PROVIDER": "ollama", "OLLAMA_MODEL": "bad name"},
            {"MODEL_PROVIDER": "ollama", "OLLAMA_MODEL": "x", "OLLAMA_URL": "ftp://h"}):
    try:
        p.wanted(bad)
        raise AssertionError(bad)
    except ValueError:
        pass

# The Settings panel checks the same shapes.
assert settings.check({"MODEL_PROVIDER": "ollama", "OLLAMA_MODEL": "qwen3-coder:30b",
                       "OLLAMA_URL": "http://localhost:11434"}) == {}
assert set(settings.check({"MODEL_PROVIDER": "openai", "OLLAMA_MODEL": "a b", "OLLAMA_URL": "localhost"})) == \
    {"MODEL_PROVIDER", "OLLAMA_MODEL", "OLLAMA_URL"}


# --- a fake Ollama ---
class Ollama(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"models": [{"name": "qwen3-coder:latest"}, {"name": "gemma3:4b"}]}).encode()
        self.send_response(200 if self.path == "/api/tags" else 404)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


server = ThreadingHTTPServer(("127.0.0.1", 0), Ollama)
threading.Thread(target=server.serve_forever, daemon=True).start()
url = f"http://127.0.0.1:{server.server_address[1]}"

# Claude by default: nothing to say, nothing changed.
secrets()
assert p.start() == "" and not p.local() and p.env() == {} and "Claude" in p.describe()

# Ollama with the models pulled ("qwen3-coder" matches "qwen3-coder:latest").
secrets(MODEL_PROVIDER="ollama", OLLAMA_URL=url, OLLAMA_MODEL="qwen3-coder", OLLAMA_FAST_MODEL="gemma3:4b")
assert p.start() == "" and p.local() and "Ollama" in p.describe()
env = p.env()
assert env["ANTHROPIC_BASE_URL"] == url and env["ANTHROPIC_AUTH_TOKEN"] == "ollama"
assert env["ANTHROPIC_DEFAULT_SONNET_MODEL"] == "qwen3-coder" and env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "qwen3-coder"
assert env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] == "gemma3:4b"

# Every kind of session gets it: the conversation, workers, the safety check, verification, the update review.
import jarvis as j
import guardrails, tasks, updater
assert j.caveman_env() == env and j.detect_caveman() is False          # Caveman's proxy forwards to Anthropic
assert j.build_options(False).env["ANTHROPIC_BASE_URL"] == url
assert j.worker_options(None).env["ANTHROPIC_BASE_URL"] == url
for module in (guardrails, tasks, updater):
    assert "**provider.env()" in Path(module.__file__).read_text(encoding="utf-8"), module.__name__
assert "provider.py" in updater.GUARDED_FILES

# A model that isn't pulled, an Ollama that doesn't answer, or incomplete settings: Claude, and say why.
secrets(MODEL_PROVIDER="ollama", OLLAMA_URL=url, OLLAMA_MODEL="llama9")
assert "hasn't got llama9" in p.start() and not p.local() and p.env() == {}
server.shutdown(); server.server_close()
secrets(MODEL_PROVIDER="ollama", OLLAMA_URL=url, OLLAMA_MODEL="qwen3-coder")
assert "isn't answering" in p.start() and not p.local()
secrets(MODEL_PROVIDER="ollama")
assert "aren't complete" in p.start() and not p.local()
assert j.caveman_env() == {}
print("ALL OK")

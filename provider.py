"""
Which language models answer: Claude, through the user's Claude Code login (the default), or local
models served by Ollama (0.14 or newer speaks the Anthropic Messages API that Claude Code uses).

Everything Jarvis runs goes through Claude Code, so a local model is just a different endpoint for
it: env() points ANTHROPIC_BASE_URL at Ollama and maps the three model tiers Jarvis asks for (opus,
sonnet, haiku; jarvis.MODELS) to the user's models, so the chat, subagents, task workers, standing
orders and the checks (the safety check, task verification, the update review) all use them, and
nothing else in the code changes. Set in ~/.jarvis/secrets.env (the Settings panel), read once at
startup (start()); a change applies at the next restart.

  MODEL_PROVIDER     claude | ollama
  OLLAMA_URL         where Ollama listens (default http://localhost:11434)
  OLLAMA_MODEL       the main model (sonnet: conversation and most work)
  OLLAMA_FAST_MODEL  quick jobs and the checks (haiku); empty: the main model
  OLLAMA_BIG_MODEL   planning and hard problems (opus); empty: the main model

If Ollama doesn't answer or a model isn't pulled, start() says why and Jarvis uses Claude, so he
can still be reached to fix the setting. Stdlib only.
"""
import json
import logging
import re
import urllib.request

import mcp_launch

log = logging.getLogger("jarvis")

DEFAULT_URL = "http://localhost:11434"
MODEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
URL = re.compile(r"^https?://[^\s/?#]+(?::\d+)?/?$")

_active: dict | None = None   # what start() settled on: None is Claude


def wanted(secrets: dict | None = None) -> dict | None:
    """The Ollama settings from secrets.env, or None for Claude. ValueError if they're incomplete."""
    s = mcp_launch.read_secrets() if secrets is None else secrets
    if (s.get("MODEL_PROVIDER") or "claude").strip().lower() != "ollama":
        return None
    main = (s.get("OLLAMA_MODEL") or "").strip()
    if not main:
        raise ValueError("MODEL_PROVIDER is ollama but OLLAMA_MODEL (the model to use) isn't set")
    url = (s.get("OLLAMA_URL") or DEFAULT_URL).strip().rstrip("/")
    models = {"sonnet": main, "haiku": (s.get("OLLAMA_FAST_MODEL") or "").strip() or main,
              "opus": (s.get("OLLAMA_BIG_MODEL") or "").strip() or main}
    for name in models.values():
        if not MODEL_NAME.match(name):
            raise ValueError(f"{name!r} isn't an Ollama model name")
    if not URL.match(url):
        raise ValueError(f"OLLAMA_URL {url!r} isn't an http:// or https:// address")
    return {"url": url, "models": models}


def installed_models(url: str, timeout_s: float = 3) -> set[str]:
    """The models Ollama has pulled, with and without their ":latest" tag. OSError if it doesn't answer."""
    with urllib.request.urlopen(f"{url}/api/tags", timeout=timeout_s) as reply:
        names = {m.get("name", "") for m in json.load(reply).get("models", [])}
    return names | {n.removesuffix(":latest") for n in names}


def start() -> str:
    """Settle which models answer, once at startup. Returns a line to tell the user (or "")."""
    global _active
    _active = None
    try:
        cfg = wanted()
    except ValueError as e:
        log.warning("model settings: %s", e)
        return f"My local model settings aren't complete ({e}), so I'm using Claude for now."
    if cfg is None:
        return ""
    try:
        have = installed_models(cfg["url"])
    except (OSError, ValueError) as e:
        log.warning("Ollama at %s didn't answer: %s", cfg["url"], e)
        return f"Ollama isn't answering at {cfg['url']}, so I'm using Claude for now."
    if missing := sorted({m for m in cfg["models"].values() if m not in have}):
        log.warning("Ollama hasn't got %s", ", ".join(missing))
        return f"Ollama hasn't got {', '.join(missing)} yet (ollama pull it), so I'm using Claude for now."
    _active = cfg
    log.info("models: Ollama at %s, %s", cfg["url"], cfg["models"])
    return ""


def local() -> bool:
    return _active is not None


def describe() -> str:
    if not _active:
        return "Claude (your Claude Code login)"
    names = sorted(set(_active["models"].values()))
    return f"Ollama at {_active['url']} ({', '.join(names)})"


def env() -> dict:
    """What Claude Code needs to use the local models ({} for Claude)."""
    if not _active:
        return {}
    m = _active["models"]
    return {
        "ANTHROPIC_BASE_URL": _active["url"],
        "ANTHROPIC_AUTH_TOKEN": "ollama",    # Ollama wants one and ignores it
        "ANTHROPIC_API_KEY": "",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": m["sonnet"],
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": m["haiku"],
        "ANTHROPIC_DEFAULT_OPUS_MODEL": m["opus"],
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",   # nothing else goes to Anthropic
    }

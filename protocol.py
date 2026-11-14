"""
The client protocol, version 1: how a client (the browser face, a terminal, a voice satellite)
introduces itself to the core over the WebSocket.

The first frame a client sends is `hello`; the core answers `welcome` (or closes the socket):

  hello   {"type": "hello", "protocol": 1,
           "client": {"kind": "face" | "terminal" | "satellite", "name": str, "version": str},
           "caps": {"display": bool, "gestures": bool, "speech": "server" | "browser",
                    "audio_in": "segments" | "none", "audio_out": "server" | "browser" | "none"},
           "token": str?,                  a paired device's token, or the control token (terminals)
           "pair": str?}                   a one-time pairing code from `jarvisctl pair`
  welcome {"type": "welcome", "protocol": 1, "client_id": str, "auth": "ok",
           "device": {"id", "name"}?, "token": str? (just paired: keep it), "media_ticket": str?,
           "state": {"standby": bool, "questions": [...], "screen": str,
                     "assistant": {"name": str, "meaning": str}?}}   (what it's called: identity.py)

A token is only ever sent in this first frame, never in a URL, so it doesn't end up in logs. A
first frame that isn't `hello` (a client from before this protocol) is refused like any other
version: 4002. Close codes: 4001 revoked or unknown device, 4002 protocol version not supported,
4003 pairing required.

After the handshake, the frames are the ones web.py documents (ask, interrupt, answer, presence /
ready, text, tool, done, say, standby, question, question_closed, blade, ui, console).

Version 1 is FROZEN: the face, terminal.py and satellite/satellite.py all speak it, and
tests/test_protocol.py pins the frame shapes. Don't rename, remove or change the meaning of a
field or frame. Adding an optional field that older clients can ignore (and that the core never
needs from them) is fine within v1. Anything else is version 2: bump PROTOCOL_VERSION only once
the core can serve both (parse_hello accepting 1 and 2, answering each client in its own
version), keep v1 working until every client has moved, and document v2's frames here.
"""
PROTOCOL_VERSION = 1
KINDS = ("face", "terminal", "satellite")

CLOSE_REVOKED = 4001
CLOSE_VERSION = 4002
CLOSE_PAIRING = 4003


class Refused(Exception):
    """A hello the core won't accept; code is the WebSocket close code to use."""

    def __init__(self, code: int, reason: str):
        super().__init__(reason)
        self.code, self.reason = code, reason


def parse_hello(frame: dict) -> dict:
    """The hello's fields, checked and with defaults filled in. Refused if it can't be served."""
    if frame.get("type") != "hello":
        raise Refused(CLOSE_VERSION, f"say hello first (protocol {PROTOCOL_VERSION})")
    try:
        version = int(frame.get("protocol", 0))
    except (TypeError, ValueError):
        version = 0
    if version != PROTOCOL_VERSION:
        raise Refused(CLOSE_VERSION, f"protocol {frame.get('protocol')!r} isn't supported; this is {PROTOCOL_VERSION}")
    client = frame.get("client") if isinstance(frame.get("client"), dict) else {}
    caps = frame.get("caps") if isinstance(frame.get("caps"), dict) else {}
    kind = client.get("kind") if client.get("kind") in KINDS else "face"
    return {
        "kind": kind,
        "name": str(client.get("name") or kind)[:40],
        "version": str(client.get("version") or "")[:20],
        "display": bool(caps.get("display", kind == "face")),
        "gestures": bool(caps.get("gestures", False)),
        "token": frame.get("token") if isinstance(frame.get("token"), str) else None,
        "pair": frame.get("pair") if isinstance(frame.get("pair"), str) else None,
    }


def welcome(client_id: str, device: dict | None, token: str | None, ticket: str | None, state: dict) -> dict:
    frame = {"type": "welcome", "protocol": PROTOCOL_VERSION, "client_id": client_id, "auth": "ok", "state": state}
    if device:
        frame["device"] = {"id": device["id"], "name": device["name"]}
    if token:
        frame["token"] = token
    if ticket:
        frame["media_ticket"] = ticket
    return frame

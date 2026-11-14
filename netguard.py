"""
Fetching remote media for the browser interface, without becoming a way into the LAN.

The page shows images and video that a model picked while reading the open web. Jarvis fetches
them (web.py's /img and /media), so hotlink-blocked thumbnails still render and the browser
never tells a stranger's server this machine's address. But Jarvis, unlike a browser tab, can
reach the router's admin page, the NAS, and cloud metadata endpoints, so this is a guard first
and a fetch second (SSRF):

- only absolute http(s) URLs, and no hostnames that only exist on a LAN (.local, .internal...);
- the name is resolved once, refused if ANY address is not on the public internet, and the
  connection goes to the address that was checked, so DNS can't change its answer in between
  (rebinding);
- every redirect hop is checked again, at most MAX_REDIRECTS of them;
- no proxy from the environment is used, so nothing routes around the check.

Ported from adewaskar/jarvis (bridge/net.mjs, MIT).
"""
import asyncio
import ipaddress
import logging
import re
import socket
from urllib.parse import urljoin, urlsplit

import httpcore

log = logging.getLogger("jarvis.web")

MAX_REDIRECTS = 4
# An ordinary desktop browser: image hosts serve placeholders to anything that looks automated.
# No Referer and no cookies, so nothing about the user goes upstream.
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/125.0.0.0 Safari/537.36")
LAN_NAME = re.compile(r"(^|\.)(localhost|local|internal|intranet|lan|home\.arpa)$", re.IGNORECASE)


class Refused(Exception):
    """A request that won't be made, with the HTTP status to answer the browser with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def blocked_address(address: str) -> bool:
    """Is this resolved address one Jarvis must not connect to? Anything that isn't public:
    loopback, private ranges, link-local and cloud metadata (169.254.169.254), carrier-grade
    NAT and tailnets (100.64/10), multicast, reserved. IPv4 written as IPv6 is judged as IPv4."""
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        for embedded in (ip.ipv4_mapped, ip.sixtofour, ip.teredo and ip.teredo[1]):
            if embedded:
                return blocked_address(str(embedded))
    return not ip.is_global or ip.is_multicast


def vet(url: str) -> str:
    """The URL, if it may be fetched at all; Refused otherwise. Checked before any DNS lookup."""
    try:
        parts = urlsplit(str(url or "").strip())
        host = parts.hostname or ""
        parts.port   # raises on a bad port
    except ValueError:
        raise Refused(400, "an absolute http(s) URL is needed")
    if parts.scheme not in ("http", "https") or not host:
        raise Refused(400, "an absolute http(s) URL is needed")
    if LAN_NAME.search(host.rstrip(".")):
        raise Refused(403, "that host is on a local network")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and blocked_address(str(literal)):
        raise Refused(403, "that address is on a local network")
    return parts.geturl()


class GuardedBackend(httpcore.AsyncNetworkBackend):
    """Resolves each host once, refuses private answers, and connects to the address it checked.
    TLS still verifies the certificate against the hostname (httpcore passes it separately)."""

    def __init__(self, resolve=None):
        self._inner = httpcore.AnyIOBackend()
        self._resolve = resolve or self._getaddrinfo

    @staticmethod
    async def _getaddrinfo(host: str, port: int) -> list[str]:
        infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
        return [info[4][0] for info in infos]

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        name = host.decode() if isinstance(host, bytes) else host
        try:
            addresses = await self._resolve(name, port)
        except OSError:
            raise Refused(502, f"couldn't resolve {name}")
        if not addresses or any(blocked_address(a) for a in addresses):
            raise Refused(403, f"{name} resolves to a local network address")
        return await self._inner.connect_tcp(addresses[0], port, timeout=timeout, local_address=local_address,
                                             socket_options=socket_options)

    async def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise Refused(403, "no local sockets")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


_pool: httpcore.AsyncConnectionPool | None = None


def pool() -> httpcore.AsyncConnectionPool:
    global _pool
    if _pool is None:
        _pool = httpcore.AsyncConnectionPool(network_backend=GuardedBackend(), max_connections=16)
    return _pool


async def open_remote(url: str, headers: dict, timeout_s: float):
    """GET url, following checked redirects. Returns (response, final_url) once the headers are
    in; the caller streams the body and must `await response.aclose()`."""
    url = vet(url)
    sent = [(b"user-agent", USER_AGENT.encode()), (b"accept-encoding", b"identity")]
    sent += [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    timeouts = {"connect": timeout_s, "read": timeout_s, "write": timeout_s, "pool": timeout_s}
    for hop in range(MAX_REDIRECTS + 1):
        parts = urlsplit(url)
        host = parts.hostname.encode("idna")   # the name only: credentials in the URL are never sent
        host = (b"[" + host + b"]" if b":" in host else host) + (f":{parts.port}".encode() if parts.port else b"")
        request = httpcore.Request("GET", url, headers=[(b"host", host)] + sent,
                                   extensions={"timeout": timeouts})
        try:
            response = await asyncio.wait_for(pool().handle_async_request(request), timeout_s)
        except Refused:
            raise
        except asyncio.TimeoutError:
            raise Refused(504, "the host didn't answer in time")
        except Exception as e:
            log.info("couldn't fetch %s: %r", url, e)
            raise Refused(502, "the host couldn't be reached")
        location = header(response, "location")
        if 300 <= response.status < 400 and location:
            await response.aclose()
            if hop == MAX_REDIRECTS:
                raise Refused(502, "too many redirects")
            url = vet(urljoin(url, location))   # every hop is checked again
            continue
        return response, url
    raise Refused(502, "too many redirects")


def header(response, name: str) -> str:
    wanted = name.lower().encode()
    for key, value in response.headers:
        if key.lower() == wanted:
            return value.decode("latin-1")
    return ""


# --- the browser's proxy ----------------------------------------------------------------------------

class FilteringProxy:
    """The HTTP proxy Jarvis's Playwright browser has to go through (browser_launch.py): the same
    rule as the fetches above, for everything the browser opens. Each host is resolved here,
    refused (403) if any of its addresses isn't public (loopback, the LAN, link-local and cloud
    metadata, CGNAT and tailnets), and the connection goes to the address that was checked, so DNS
    can't change its answer in between. CONNECT carries https and wss; plain http is forwarded
    one request per connection. Redirects are new requests, so each is checked again.

    Listens on 127.0.0.1 only. Anything local can use it, but it reaches only the public internet,
    which anything local can reach anyway."""

    MAX_HEAD = 64 * 1024
    TIMEOUT_S = 30

    def __init__(self, resolve=None):
        self._resolve = resolve or GuardedBackend._getaddrinfo
        self._server: asyncio.base_events.Server | None = None

    async def start(self, port: int = 0) -> int:
        self._server = await asyncio.start_server(self._client, "127.0.0.1", port)
        return self._server.sockets[0].getsockname()[1]

    async def close(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def vetted(self, host: str, port: int) -> str:
        """The address to connect to for host:port, or Refused."""
        host = host.strip("[]").rstrip(".")
        if not host or not 0 < port < 65536:
            raise Refused(400, "a host and port are needed")
        if LAN_NAME.search(host):
            raise Refused(403, f"{host} is on a local network")
        try:
            addresses = await self._resolve(host, port)
        except OSError:
            raise Refused(502, f"couldn't resolve {host}")
        if not addresses or any(blocked_address(a) for a in addresses):
            raise Refused(403, f"{host} is on a local network")
        return addresses[0]

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), self.TIMEOUT_S)
            if len(head) > self.MAX_HEAD:
                raise Refused(431, "request head too large")
            request_line, *lines = head.decode("latin-1").split("\r\n")
            method, target, version = (request_line.split(" ") + ["", "", ""])[:3]
            if method == "CONNECT":
                host, _, port = target.rpartition(":")
                port = int(port) if port.isdigit() else 0
            else:
                parts = urlsplit(target)
                if parts.scheme != "http" or not parts.hostname:
                    raise Refused(400, "only absolute http URLs and CONNECT are proxied")
                host, port = parts.hostname, parts.port or 80
            address = await self.vetted(host, port)
            up_reader, upstream = await asyncio.wait_for(asyncio.open_connection(address, port), self.TIMEOUT_S)
            if method == "CONNECT":
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            else:
                parts = urlsplit(target)
                path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                kept = [l for l in lines if l and not l.lower().startswith(("proxy-", "connection:", "keep-alive:"))]
                upstream.write(f"{method} {path} {version}\r\n".encode("latin-1")
                               + "".join(f"{l}\r\n" for l in kept).encode("latin-1")
                               + b"Connection: close\r\n\r\n")
            await asyncio.gather(_pipe(reader, upstream), _pipe(up_reader, writer))
        except Refused as e:
            log.info("browser proxy refused %s", e)
            reason = str(e).encode("latin-1", "replace")
            writer.write(b"HTTP/1.1 %d Refused\r\nContent-Type: text/plain\r\nContent-Length: %d\r\n"
                         b"Connection: close\r\n\r\n%s" % (e.status, len(reason), reason))
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, OSError, ValueError):
            pass
        finally:
            for w in (writer, upstream):
                if w is not None:
                    try:
                        await w.drain()
                    except OSError:
                        pass
                    w.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except OSError:
        pass
    finally:
        if writer.can_write_eof():
            try:
                writer.write_eof()
            except OSError:
                pass

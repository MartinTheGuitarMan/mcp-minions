"""Fetch one public web page for summarize_url. This is an SSRF-shaped surface, so the guards live here, in code:

- http and https only, default ports only, no credentials in the URL.
- The host is resolved; every address must be global unicast (no loopback, private, link-local, multicast,
  reserved or metadata address, IPv4-mapped IPv6 and 6to4/NAT64/Teredo-embedded IPv4 included). `.local`,
  `.internal` and similar names are refused before any lookup.
- The checked address is pinned: the socket connects to that IP, never to a second lookup.
- Every redirect target goes through the same checks. At most MAX_REDIRECTS.
- Timeout (total), size cap, text/html and text/plain only, no compression.
- No cookies, no auth headers, nothing but a fixed minimal header set is sent.
"""
from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from .errors import BadInput, RefusedAddress

MAX_REDIRECTS = 3
MAX_BYTES = 5_000_000      # raw download; streamed and stopped at the cap. The text is cut much shorter later
TIMEOUT_S = 15.0           # total, over all redirects
ALLOWED_TYPES = ("text/html", "application/xhtml+xml", "text/plain")
DEFAULT_PORTS = {"http": 80, "https": 443}
BAD_SUFFIXES = (".local", ".internal", ".localhost", ".localdomain", ".lan", ".home", ".corp", ".intranet",
                ".home.arpa", ".test", ".invalid", ".example")
METADATA = {ipaddress.ip_address(a) for a in (
    "169.254.169.254", "169.254.170.2", "100.100.100.200", "192.0.0.192", "fd00:ec2::254")}
HEADERS = {"User-Agent": "mcp-minions-summarize-url/1", "Accept": "text/html,text/plain;q=0.9",
           "Accept-Encoding": "identity", "Connection": "close"}


# ---------- address checks ----------
def _embedded_v4(ip: ipaddress.IPv6Address):
    """The IPv4 address hidden in an IPv6 one (mapped, 6to4, NAT64, Teredo), or None."""
    if ip.ipv4_mapped:
        return ip.ipv4_mapped
    if ip.sixtofour:
        return ip.sixtofour
    if ip in ipaddress.ip_network("64:ff9b::/96"):
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    if ip.teredo:
        return ip.teredo[1]
    return None


def address_problem(ip) -> str | None:
    """Why this address may not be fetched, or None when it is a public unicast address."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = _embedded_v4(ip)
        if inner is not None:
            return address_problem(inner)
    if ip in METADATA:
        return "cloud metadata address"
    if ip.is_loopback:
        return "loopback address"
    if ip.is_multicast:
        return "multicast address"
    if ip.is_link_local:
        return "link-local address"
    if ip.is_private:
        return "private address"
    if ip.is_unspecified or ip.is_reserved:
        return "reserved address"
    if not ip.is_global:
        return "non-public address"
    return None


def check_host_name(host: str) -> None:
    h = host.lower().rstrip(".")
    if not h:
        raise BadInput("url has no host")
    try:
        ipaddress.ip_address(h.strip("[]"))
        return   # an IP literal: checked after "resolution"
    except ValueError:
        pass
    if "." not in h or h == "localhost" or h.endswith(BAD_SUFFIXES):
        raise RefusedAddress(f"host name {host!r} is local or internal")


def _resolve(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as e:
        raise BadInput(f"cannot resolve {host!r}: {e}") from e
    return list(dict.fromkeys(i[4][0] for i in infos))


def check_url(url: str) -> tuple[str, str, int, str]:
    """Validate one URL and return (scheme, host, port, request target). Raises BadInput or RefusedAddress."""
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError as e:
        raise BadInput(f"not a valid url: {e}") from e
    scheme = parts.scheme.lower()
    if scheme not in DEFAULT_PORTS:
        raise BadInput("only http and https urls are allowed")
    if parts.username is not None or parts.password is not None:
        raise BadInput("urls with credentials are not allowed")
    if port is not None and port != DEFAULT_PORTS[scheme]:
        raise BadInput(f"only the default port is allowed ({DEFAULT_PORTS[scheme]} for {scheme})")
    host = parts.hostname or ""
    check_host_name(host)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return scheme, host, DEFAULT_PORTS[scheme], target


def pick_address(host: str, port: int) -> str:
    """Resolve host and return one address to pin. Every address it resolves to must be public."""
    addrs = _resolve(host.strip("[]"), port)
    if not addrs:
        raise BadInput(f"cannot resolve {host!r}")
    for a in addrs:
        try:
            ip = ipaddress.ip_address(a.split("%")[0])
        except ValueError as e:
            raise RefusedAddress(f"{host!r} resolved to an unreadable address") from e
        why = address_problem(ip)
        if why:
            raise RefusedAddress(f"{host!r} resolves to a {why}")
    return addrs[0]


# ---------- one request on a pinned address ----------
def _request(scheme: str, host: str, ip: str, port: int, target: str, deadline: float):
    """GET target from `ip` (the pinned address), sending Host/SNI for `host`. Returns (status, headers, body)."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise BadInput("fetch timed out")
    sock = None
    try:
        sock = socket.create_connection((ip, port), timeout=left)
        if scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host.strip("[]"))
        conn = http.client.HTTPConnection(host, port, timeout=left)
        conn.sock = sock   # already connected to the pinned address: http.client will not look anything up
        conn.request("GET", target, headers={**HEADERS, "Host": host if port == DEFAULT_PORTS[scheme] else f"{host}:{port}"})
        resp = conn.getresponse()
        headers = {k.lower(): v for k, v in resp.getheaders()}
        body = b""
        if resp.status not in (301, 302, 303, 307, 308):
            ctype = headers.get("content-type", "").split(";")[0].strip().lower()
            if ctype not in ALLOWED_TYPES:
                raise BadInput(f"content type {ctype or 'unknown'!r} is not text or html")
            if headers.get("content-encoding", "identity").lower() != "identity":
                raise BadInput("compressed responses are not accepted")
            chunks, n = [], 0
            while True:
                if time.monotonic() > deadline:
                    raise BadInput("fetch timed out")
                chunk = resp.read(16384)
                if not chunk:
                    break
                n += len(chunk)
                if n > MAX_BYTES:
                    raise BadInput(f"page is larger than {MAX_BYTES} bytes")
                chunks.append(chunk)
            body = b"".join(chunks)
        return resp.status, headers, body
    except (OSError, http.client.HTTPException) as e:   # timeouts and ssl errors are OSError
        raise BadInput(f"fetch failed: {type(e).__name__}: {e}") from e
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass


def fetch(url: str, timeout: float = TIMEOUT_S) -> tuple[str, str, str]:
    """Fetch a public page. Returns (final_url, content_type, text). Every hop is checked and pinned."""
    deadline = time.monotonic() + timeout
    current = url
    for hop in range(MAX_REDIRECTS + 1):
        scheme, host, port, target = check_url(current)
        ip = pick_address(host, port)
        status, headers, body = _request(scheme, host, ip, port, target, deadline)
        if status in (301, 302, 303, 307, 308):
            loc = headers.get("location")
            if not loc:
                raise BadInput("redirect without a location")
            if hop == MAX_REDIRECTS:
                raise BadInput(f"more than {MAX_REDIRECTS} redirects")
            current = urljoin(current, loc)
            continue
        if status != 200:
            raise BadInput(f"the server answered HTTP {status}")
        ctype = headers.get("content-type", "")
        return current, ctype, decode(body, ctype)
    raise BadInput(f"more than {MAX_REDIRECTS} redirects")   # unreachable, keeps the type checker calm


# ---------- text ----------
def decode(body: bytes, content_type: str) -> str:
    m = re.search(r"charset=([\w.-]+)", content_type, re.I)
    if not m:
        m = re.search(rb"<meta[^>]+charset=[\"']?([\w.-]+)", body[:2048], re.I)
    enc = m.group(1) if m else "utf-8"
    enc = enc.decode("ascii", "ignore") if isinstance(enc, bytes) else enc
    try:
        return body.decode(enc, errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class _Text(HTMLParser):
    SKIP = {"script", "style", "noscript", "template", "svg", "head", "iframe", "object"}
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "header",
             "footer", "ul", "ol", "table", "blockquote", "pre", "hr", "main", "nav", "aside"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.skip = 0
        self.title: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self.in_title = True
        if tag in self.SKIP and tag != "head":
            self.skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in self.SKIP and tag != "head" and self.skip:
            self.skip -= 1
        if tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        elif not self.skip:
            self.out.append(data)


def html_to_text(html: str) -> str:
    p = _Text()
    p.feed(html)
    p.close()
    lines = [re.sub(r"[ \t\r\f\v]+", " ", ln).strip() for ln in "".join(p.out).split("\n")]
    body = "\n".join(ln for ln in lines if ln)
    title = re.sub(r"\s+", " ", "".join(p.title)).strip()
    return f"{title}\n{body}".strip() if title else body


def page_text(content_type: str, raw: str) -> str:
    if content_type.split(";")[0].strip().lower() == "text/plain":
        return raw.strip()
    return html_to_text(raw)

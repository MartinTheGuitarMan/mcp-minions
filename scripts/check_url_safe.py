"""Check a URL against the fetch layer's address rules without touching the network.

Plain code, no model, so it lives here, not in the MCP tool list. It reuses `fetch.check_url` (scheme, credentials,
default port, host name rules) and `fetch.address_problem` (the address classes `summarize_url` refuses), and adds one
thing: IP literals, including the old numeric spellings (`127.1`, `0x7f.0.0.1`, `2130706433`, `017700000001`), are judged by
their real address.

    uv run python scripts/check_url_safe.py https://example.org/page http://127.1/ ...
    from check_url_safe import check          # -> {"url", "ok", "reason", "resolved": False, "note"}

It opens no socket, resolves no name and fetches nothing, so it cannot widen what a minion can read. `ok: true` does not
mean "safe to fetch": a host *name* can still resolve to a private address, which only the fetch layer finds out, when it
resolves, checks every address and pins one for the connection. `ok: false` is final: the fetch layer would refuse it too.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from minions import fetch  # noqa: E402
from minions.errors import BadInput, RefusedAddress  # noqa: E402

_NUMERIC_HOST = re.compile(r"[0-9a-fx.]+", re.I)
NAME_NOTE = "host name not resolved here; the fetch layer resolves it, checks every address and pins one"


def _literal(host: str):
    """The IP address a host spells, or None when it is a name. `inet_aton` only parses text (no lookup); it is what
    makes `127.1` and `0x7f.0.0.1` mean 127.0.0.1 to a resolver."""
    h = host.strip("[]").split("%")[0]
    try:
        return ipaddress.ip_address(h)
    except ValueError:
        pass
    if _NUMERIC_HOST.fullmatch(h):
        try:
            return ipaddress.IPv4Address(socket.inet_aton(h))
        except OSError:
            return None
    return None


def check(url: str) -> dict:
    out = {"url": url, "ok": False, "reason": None, "resolved": False, "note": None}
    if not isinstance(url, str) or not url.strip():
        out["reason"] = "url must be a non-empty string"
        return out
    try:
        fetch.check_url(url)
    except (BadInput, RefusedAddress) as e:
        out["reason"] = str(e)
        return out
    host = urlsplit(url.strip()).hostname or ""
    ip = _literal(host)
    if ip is not None:
        why = fetch.address_problem(ip)
        if why:
            out["reason"] = f"{host!r} is a {why}"
            return out
        out["note"] = "public address literal"
    else:
        out["note"] = NAME_NOTE
    out["ok"] = True
    return out


def main(argv: list[str] | None = None) -> int:
    urls = sys.argv[1:] if argv is None else argv
    if not urls or urls in (["-h"], ["--help"]):
        print(__doc__)
        return 0 if urls else 2
    bad = 0
    for u in urls:
        r = check(u)
        bad += not r["ok"]
        print(f"{'OK     ' if r['ok'] else 'REFUSED'} {u}  {r['note'] if r['ok'] else r['reason']}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

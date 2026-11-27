"""Is a Screen Sharing connection with a paired Mac live right now?

Used only at click time by `focus --machine` (peer_cli), never by
`snapshot`, `peer-sync` or the hook. One local `nettop` call (unprivileged,
and unlike `netstat`/`lsof` it sees root's screensharingd sockets), plus a
resolver lookup when the paired host is a name; both bounded.

A connection counts when it is Established with exactly one end on port 5900
and the other end's address is one the paired host resolves to:

- `"outgoing"`: this Mac is viewing the peer (remote port 5900);
- `"incoming"`: the peer is viewing this Mac (local port 5900).

Not detected (reads as no connection): Screen Sharing reached through another
address than the paired host resolves to, relayed or tunnelled VNC, a port
other than 5900, a name that does not resolve in time. Any VNC client's
connection counts (no process name is matched), so another viewer's
connection to the peer reads as `"outgoing"` too.
"""

import ipaddress

from . import diag

VNC_PORT = 5900
TIMEOUT_SECS = 2
NETTOP_ARGV = ["nettop", "-L", "1", "-n", "-x", "-m", "tcp", "-J", "interface,state"]


def _normalize(text):
    """The canonical text of an IP address (zone dropped, IPv4-mapped IPv6
    unwrapped), or None when `text` is not one."""
    if not isinstance(text, str):
        return None
    text = text.strip()
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    text = text.split("%", 1)[0]
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return str(addr)


def peer_addresses(host, run) -> set:
    """The addresses `host` stands for: an IP literal as is (no lookup),
    else what `dscacheutil` resolves it to. Any failure → empty set."""
    literal = _normalize(host)
    if literal is not None:
        return {literal}
    if not isinstance(host, str) or not host or host.startswith("-"):
        return set()
    from . import cli
    result = cli._run_bounded(run, ["dscacheutil", "-q", "host", "-a", "name", host],
                              timeout=TIMEOUT_SECS)
    if isinstance(result, cli._TimedOut) or cli._returncode(result) != 0:
        return set()
    found = set()
    for line in (result.stdout or "").splitlines():
        label, _, value = line.partition(":")
        if label.strip() in ("ip_address", "ipv6_address"):
            addr = _normalize(value)
            if addr is not None:
                found.add(addr)
    return found


def _split_end(proto, end):
    """(address, port) of one connection end, or None (`*`, malformed)."""
    sep = ":" if proto == "tcp4" else "."
    addr, found, port = end.rpartition(sep)
    if not found or not port.isdigit():
        return None
    addr = _normalize(addr)
    if addr is None:
        return None
    return addr, int(port)


def parse_links(nettop_text, addresses):
    """`"outgoing"`, `"incoming"` or None for the rows of
    `nettop -L 1 -n -x -m tcp -J interface,state` (see the module docstring).
    `"outgoing"` wins when both are present. Never raises."""
    if not isinstance(nettop_text, str) or not addresses:
        return None
    wanted = {a for a in (_normalize(x) for x in addresses) if a is not None}
    incoming = False
    state_col = -2  # `…,state,`: the trailing comma leaves an empty last field
    for line in nettop_text.splitlines():
        fields = line.split(",")
        if line.startswith(",") and "state" in (f.strip() for f in fields):
            state_col = [f.strip() for f in fields].index("state")
            continue
        if len(fields) < 3 or len(fields) <= state_col \
                or fields[state_col].strip() != "Established":
            continue
        proto, _, ends = fields[0].partition(" ")
        if proto not in ("tcp4", "tcp6"):
            continue
        local, sep, remote = ends.partition("<->")
        if not sep:
            continue
        local, remote = _split_end(proto, local), _split_end(proto, remote)
        if local is None or remote is None or remote[0] not in wanted:
            continue
        if remote[1] == VNC_PORT and local[1] != VNC_PORT:
            return "outgoing"
        if local[1] == VNC_PORT and remote[1] != VNC_PORT:
            incoming = True
    return "incoming" if incoming else None


def link_to(peer, run):
    """The live Screen Sharing link with `peer` (a paired-machine dict), or
    None when there is none or it can't be told. Never raises."""
    try:
        addresses = peer_addresses(peer.get("host"), run)
        if not addresses:
            diag.debug("screen_link", "no_addresses", peer=peer.get("id"))
            return None
        from . import cli
        result = cli._run_bounded(run, list(NETTOP_ARGV), timeout=TIMEOUT_SECS)
        if isinstance(result, cli._TimedOut) or cli._returncode(result) != 0:
            diag.debug("screen_link", "nettop_failed", peer=peer.get("id"))
            return None
        text = result.stdout or ""
        if text.strip() and not text.startswith(","):
            diag.debug("screen_link", "nettop_no_header", peer=peer.get("id"))
        link = parse_links(text, addresses)
        diag.debug("screen_link", "checked", peer=peer.get("id"), link=link)
        return link
    except Exception:  # noqa: BLE001 - detection must never fail a click
        return None

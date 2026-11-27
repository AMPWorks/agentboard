"""The addresses other Macs could reach this one at, for its pairing code.

`candidates()` lists them in default-preference order: a name or address
from an optional VPN CLI (`_VPN_PROBES`), IPv4 addresses on tunnel
(`utun*`) interfaces, the Bonjour `.local` name, and IPv4 addresses on
`en*` interfaces (this network only). `automatic()` picks the default one.

Detection runs only local commands (`scutil`, `ifconfig`, a VPN CLI's
status call to its own daemon), each bounded, and never raises. It is used
by `peers list`/`set-self` and the export's advertised address, never by
`snapshot`. Nothing here is specific to one setup: a VPN is one entry in
`_VPN_PROBES`, and everything works without one through the interface scan
and the address the owner types.
"""

import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess

from . import peers

PROBE_TIMEOUT_SECS = 1
IFCONFIG = "/sbin/ifconfig"
_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_IFACE_RE = re.compile(r"\A([A-Za-z0-9]+):\s")
_INET_RE = re.compile(r"\A\s+inet\s+(\d{1,3}(?:\.\d{1,3}){3})(?:\s|\Z)")


def parse_tailscale_status(text) -> list:
    """(address, label) pairs from `tailscale status --json`: the MagicDNS
    name (when MagicDNS is on), then the first IPv4 address, both only while
    the backend is running. Anything malformed yields []."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError, RecursionError):
        return []
    if not isinstance(data, dict) or data.get("BackendState") != "Running":
        return []
    me = data.get("Self")
    if not isinstance(me, dict):
        return []
    out = []
    tailnet = data.get("CurrentTailnet")
    name = me.get("DNSName")
    if isinstance(tailnet, dict) and tailnet.get("MagicDNSEnabled") is True and isinstance(name, str):
        name = name.rstrip(".")
        if peers.valid_host(name):
            out.append((name, "VPN name"))
    ips = me.get("TailscaleIPs")
    if isinstance(ips, list):
        for ip in ips:
            if _ipv4(ip) is not None:
                out.append((ip, "VPN address"))
                break
    return out


# (executable names looked up on PATH, app-bundle paths, argv after the
# executable, parser of its stdout into (address, label) pairs). Another
# VPN is one more entry.
_VPN_PROBES = (
    (("tailscale",), ("/Applications/Tailscale.app/Contents/MacOS/Tailscale",),
     ("status", "--json"), parse_tailscale_status),
)


def _ipv4(value):
    if not isinstance(value, str):
        return None
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None
    if addr.version != 4 or addr.is_loopback or addr.is_link_local or addr.is_unspecified \
            or addr.is_multicast:
        return None
    return addr


def parse_ifconfig(text) -> list:
    """Candidates from `ifconfig` output: IPv4 on `utun*` as `vpn` (those
    in 100.64.0.0/10 first), then IPv4 on `en*` as `lan`. Loopback,
    link-local and every IPv6 address are skipped."""
    tunnels, lans, iface = [], [], None
    for line in (text or "").splitlines():
        match = _IFACE_RE.match(line)
        if match:
            iface = match.group(1)
            continue
        match = _INET_RE.match(line)
        if not match or iface is None:
            continue
        addr = _ipv4(match.group(1))
        if addr is None:
            continue
        if iface.startswith("utun"):
            tunnels.append((addr not in _CGNAT, {
                "address": str(addr), "kind": "vpn", "label": f"VPN address ({iface})",
                "via": "interface", "cgnat": addr in _CGNAT}))
        elif iface.startswith("en"):
            lans.append({"address": str(addr), "kind": "lan",
                         "label": f"This network only ({iface})", "via": "interface"})
    tunnels.sort(key=lambda pair: pair[0])
    return [entry for _rank, entry in tunnels] + lans


def local_name(run=subprocess.run) -> str:
    """The Bonjour `.local` name, else the node name, else `localhost`."""
    local = peers._scutil(run, "LocalHostName")
    if local and peers.valid_host(local + ".local"):
        return local + ".local"
    node = platform.node() or "localhost"
    return node if peers.valid_host(node) else "localhost"


def _is_executable(path) -> bool:
    return os.path.isfile(path) and os.access(path, os.X_OK)


def _probe(run, which, is_exe):
    out = []
    for names, bundle_paths, argv_tail, parser in _VPN_PROBES:
        exe = next((found for name in names if (found := which(name))), None)
        if exe is None:
            exe = next((path for path in bundle_paths if is_exe(path)), None)
        if exe is None:
            continue
        result = peers._run(run, [exe, *argv_tail], timeout=PROBE_TIMEOUT_SECS)
        if result is None or getattr(result, "returncode", 1) != 0:
            continue
        try:
            pairs = parser(result.stdout or "")
        except Exception:  # a parser must never break the listing
            pairs = []
        out.extend({"address": address, "kind": "vpn", "label": label, "via": "probe"}
                   for address, label in pairs)
    return out


def candidates(run=subprocess.run, which=None, is_exe=None) -> list:
    """Every address offered for this Mac, in default-preference order,
    each {address, kind, label, via}, deduplicated and valid_host-checked."""
    found = _probe(run, which or shutil.which, is_exe or _is_executable)
    result = peers._run(run, [IFCONFIG], timeout=PROBE_TIMEOUT_SECS)
    scanned = []
    if result is not None and getattr(result, "returncode", 1) == 0:
        scanned = parse_ifconfig(result.stdout or "")
    lans = [entry for entry in scanned if entry["kind"] == "lan"]
    found.extend(entry for entry in scanned if entry["kind"] == "vpn")
    local = local_name(run)
    if local != "localhost":
        found.append({"address": local, "kind": "local", "label": "Local name (same network only)",
                      "via": "local"})
    found.extend(lans)
    out, seen = [], set()
    for entry in found:
        address = entry["address"]
        if address in seen or not peers.valid_host(address):
            continue
        seen.add(address)
        out.append({k: entry[k] for k in ("address", "kind", "label", "via")})
        if entry.get("cgnat"):
            out[-1]["cgnat"] = True
    return out


def automatic(entries) -> dict:
    """The default address: a VPN CLI's, else a tunnel address in
    100.64.0.0/10, else the `.local` name, else `localhost`. Other tunnel
    addresses and LAN addresses are offered but never chosen."""
    for test in (lambda e: e["kind"] == "vpn" and e.get("via") == "probe",
                 lambda e: e["kind"] == "vpn" and e.get("cgnat"),
                 lambda e: e["kind"] == "local"):
        entry = next((e for e in entries if test(e)), None)
        if entry is not None:
            return {"address": entry["address"], "kind": entry["kind"]}
    return {"address": "localhost", "kind": "local"}

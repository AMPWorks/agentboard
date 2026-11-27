"""The addresses offered for this Mac's pairing code (peer_address)."""

import json
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import peer_address  # noqa: E402

TS_EXE = "/usr/local/bin/tailscale"

IFCONFIG = """\
lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
\tinet6 fe80::1%lo0 prefixlen 64 scopeid 0x1
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet6 fe80::1c2b:3a4d:5e6f:7081%en0 prefixlen 64 secured scopeid 0xe
\tinet 192.168.1.20 netmask 0xffffff00 broadcast 192.168.1.255
\tinet6 2001:db8::5 prefixlen 64 autoconf temporary
en5: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tinet 169.254.10.10 netmask 0xffff0000 broadcast 169.254.255.255
utun0: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1380
\tinet6 fe80::abcd%utun0 prefixlen 64 scopeid 0x10
utun3: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1400
\tinet 10.8.0.2 --> 10.8.0.1 netmask 0xffffff00
utun4: flags=8051<UP,POINTOPOINT,RUNNING,MULTICAST> mtu 1280
\tinet 100.101.102.103 --> 100.101.102.103 netmask 0xffffffff
"""


def ts_status(state="Running", magic=True, dns="mac.example-net.ts.net.",
              ips=("100.101.102.103", "fd7a:115c:a1e0::1")):
    return json.dumps({
        "BackendState": state,
        "Self": {"DNSName": dns, "TailscaleIPs": list(ips)},
        "CurrentTailnet": {"MagicDNSEnabled": magic},
    })


class FakeRun:
    """Records argv; answers scutil, ifconfig and the VPN probe."""

    def __init__(self, ifconfig=IFCONFIG, status=None, probe_exc=None, local="Laptop"):
        self.calls = []
        self.ifconfig, self.status, self.probe_exc, self.local = ifconfig, status, probe_exc, local

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0].endswith("scutil"):
            if self.local is None:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return subprocess.CompletedProcess(argv, 0, self.local + "\n", "")
        if argv[0] == peer_address.IFCONFIG:
            return subprocess.CompletedProcess(argv, 0, self.ifconfig, "")
        if self.probe_exc is not None:
            raise self.probe_exc
        if self.status is None:
            return subprocess.CompletedProcess(argv, 1, "", "not running")
        return subprocess.CompletedProcess(argv, 0, self.status, "")

    def probes(self):
        return [c for c in self.calls if not c[0].endswith("scutil") and c[0] != peer_address.IFCONFIG]


def which_ts(name):
    return TS_EXE if name == "tailscale" else None


def no_bundle(path):
    return False


class TailscaleStatusTests(unittest.TestCase):
    def test_running_with_magic_dns_gives_name_then_ip(self):
        self.assertEqual(peer_address.parse_tailscale_status(ts_status()),
                         [("mac.example-net.ts.net", "VPN name"), ("100.101.102.103", "VPN address")])

    def test_magic_dns_off_gives_only_the_ip(self):
        self.assertEqual(peer_address.parse_tailscale_status(ts_status(magic=False)),
                         [("100.101.102.103", "VPN address")])

    def test_ipv6_first_still_picks_the_ipv4(self):
        out = peer_address.parse_tailscale_status(ts_status(ips=("fd7a::1", "100.64.0.9")))
        self.assertEqual(out[-1], ("100.64.0.9", "VPN address"))

    def test_bad_input_gives_nothing(self):
        for text in (ts_status(state="Stopped"), ts_status()[:30], "[1, 2]", "",
                     json.dumps({"BackendState": "Running", "Self": []}),
                     ts_status(dns="-oProxyCommand=x.", ips=("not-an-ip",)),
                     ts_status(dns=5, ips="100.64.0.1")):
            with self.subTest(text=text[:40]):
                self.assertEqual(peer_address.parse_tailscale_status(text), [])


class IfconfigTests(unittest.TestCase):
    def test_tunnels_first_cgnat_leading_then_lan(self):
        out = peer_address.parse_ifconfig(IFCONFIG)
        self.assertEqual([(e["address"], e["kind"]) for e in out],
                         [("100.101.102.103", "vpn"), ("10.8.0.2", "vpn"), ("192.168.1.20", "lan")])
        self.assertEqual(out[0]["label"], "VPN address (utun4)")
        self.assertEqual(out[2]["label"], "This network only (en0)")

    def test_loopback_link_local_and_ipv6_are_skipped(self):
        text = "\n".join(e["address"] for e in peer_address.parse_ifconfig(IFCONFIG))
        for skipped in ("127.0.0.1", "169.254", "fe80", "2001:db8", "::1"):
            self.assertNotIn(skipped, text)

    def test_garbage_gives_nothing(self):
        self.assertEqual(peer_address.parse_ifconfig(""), [])
        self.assertEqual(peer_address.parse_ifconfig(None), [])
        self.assertEqual(peer_address.parse_ifconfig("\tinet 1.2.3.4 netmask 0\n"), [])


class CandidateTests(unittest.TestCase):
    def test_no_cli_found_runs_no_probe(self):
        run = FakeRun(status=ts_status())
        out = peer_address.candidates(run, which=lambda name: None, is_exe=no_bundle)
        self.assertEqual(run.probes(), [])
        self.assertEqual([e["kind"] for e in out], ["vpn", "vpn", "local", "lan"])

    def test_app_bundle_path_is_probed_when_not_on_path(self):
        run = FakeRun(status=ts_status())
        bundle = peer_address._VPN_PROBES[0][1][0]
        peer_address.candidates(run, which=lambda name: None, is_exe=lambda p: p == bundle)
        self.assertEqual(run.probes(), [[bundle, "status", "--json"]])

    def test_probe_results_lead_and_duplicates_collapse(self):
        run = FakeRun(status=ts_status())
        out = peer_address.candidates(run, which=which_ts, is_exe=no_bundle)
        self.assertEqual([e["address"] for e in out],
                         ["mac.example-net.ts.net", "100.101.102.103", "10.8.0.2", "Laptop.local",
                          "192.168.1.20"])
        self.assertEqual(out[1]["label"], "VPN address")  # the probe's, not the scan's
        self.assertEqual(run.probes(), [[TS_EXE, "status", "--json"]])
        self.assertEqual(peer_address.automatic(out),
                         {"address": "mac.example-net.ts.net", "kind": "vpn"})

    def test_probe_failures_still_return_the_local_entry(self):
        for exc in (subprocess.TimeoutExpired(TS_EXE, 1), OSError("gone")):
            with self.subTest(exc=type(exc).__name__):
                run = FakeRun(ifconfig="", probe_exc=exc)
                out = peer_address.candidates(run, which=which_ts, is_exe=no_bundle)
                self.assertEqual(out, [{"address": "Laptop.local", "kind": "local",
                                        "label": "Local name (same network only)", "via": "local"}])

    def test_probe_is_bounded_to_one_second(self):
        seen = []

        def run(argv, **kwargs):
            seen.append(kwargs.get("timeout"))
            return subprocess.CompletedProcess(argv, 1, "", "")

        peer_address.candidates(run, which=which_ts, is_exe=no_bundle)
        self.assertEqual(seen[:2], [1, 1])  # the probe, then ifconfig

    def test_every_candidate_is_a_valid_host(self):
        from agentboard import peers
        out = peer_address.candidates(FakeRun(status=ts_status()), which=which_ts, is_exe=no_bundle)
        self.assertTrue(all(peers.valid_host(e["address"]) for e in out))


class AutomaticTests(unittest.TestCase):
    def entry(self, address, kind, via, **extra):
        return {"address": address, "kind": kind, "label": "x", "via": via, **extra}

    def test_order_vpn_probe_then_cgnat_tunnel_then_local_then_localhost(self):
        probe = self.entry("mac.vpn", "vpn", "probe")
        tunnel = self.entry("100.64.1.1", "vpn", "interface", cgnat=True)
        other = self.entry("10.8.0.2", "vpn", "interface")
        local = self.entry("Laptop.local", "local", "local")
        lan = self.entry("192.168.1.20", "lan", "interface")
        self.assertEqual(peer_address.automatic([lan, local, tunnel, probe])["address"], "mac.vpn")
        self.assertEqual(peer_address.automatic([lan, local, other, tunnel])["address"], "100.64.1.1")
        self.assertEqual(peer_address.automatic([other, lan, local])["address"], "Laptop.local")
        self.assertEqual(peer_address.automatic([lan, other]),
                         {"address": "localhost", "kind": "local"})

    def test_a_non_cgnat_tunnel_is_listed_but_not_chosen(self):
        ifconfig = "utun3: flags=8051<UP> mtu 1400\n\tinet 10.8.0.2 --> 10.8.0.1 netmask 0xffffff00\n"
        out = peer_address.candidates(FakeRun(ifconfig=ifconfig), which=lambda n: None, is_exe=no_bundle)
        self.assertIn("10.8.0.2", [e["address"] for e in out])
        self.assertEqual(peer_address.automatic(out), {"address": "Laptop.local", "kind": "local"})


if __name__ == "__main__":
    unittest.main()

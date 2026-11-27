"""Screen Sharing connection detection (screen_link). All addresses are
documentation or VPN-range examples; no command really runs."""

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import screen_link
from tests.helpers import AgentBoardTestCase

PEER = "100.64.0.7"

NETTOP = """,interface,state,
launchd.1,,,
tcp4 *:5900<->*:*,,Listen,
tcp6 *.5900<->*.*,,Listen,
apsd.608,,,
tcp4 192.168.1.134:50195<->17.57.147.4:5223,en0,Established,
"""


class ParseLinksTests(AgentBoardTestCase):
    def test_outgoing_tcp4_to_a_vpn_address(self):
        text = NETTOP + ("Screen Sharing.901,,,\n"
                         f"tcp4 100.99.75.93:51234<->{PEER}:5900,utun4,Established,\n")
        self.assertEqual(screen_link.parse_links(text, {PEER}), "outgoing")

    def test_incoming_on_local_port_5900(self):
        text = NETTOP + ("screensharingd.77,,,\n"
                         f"tcp4 100.99.75.93:5900<->{PEER}:60001,utun4,Established,\n")
        self.assertEqual(screen_link.parse_links(text, {PEER}), "incoming")

    def test_outgoing_wins_over_incoming(self):
        text = NETTOP + (f"tcp4 10.0.0.2:5900<->{PEER}:60001,en0,Established,\n"
                         f"tcp4 10.0.0.2:51234<->{PEER}:5900,en0,Established,\n")
        self.assertEqual(screen_link.parse_links(text, {PEER}), "outgoing")

    def test_tcp6_with_zone(self):
        text = NETTOP + "tcp6 fe80::2%en0.51234<->fe80::9%en0.5900,en0,Established,\n"
        self.assertEqual(screen_link.parse_links(text, {"fe80::9"}), "outgoing")
        text = NETTOP + "tcp6 fd7a:115c::1.5900<->fd7a:115c::9.60001,utun0,Established,\n"
        self.assertEqual(screen_link.parse_links(text, {"fd7a:115c::9"}), "incoming")

    def test_ipv4_mapped_ipv6_matches_the_ipv4_address(self):
        text = NETTOP + f"tcp6 ::ffff:10.0.0.2.51234<->::ffff:{PEER}.5900,en0,Established,\n"
        self.assertEqual(screen_link.parse_links(text, {PEER}), "outgoing")

    def test_listen_other_hosts_and_other_states_are_ignored(self):
        text = NETTOP + ("tcp4 10.0.0.2:51234<->192.0.2.50:5900,en0,Established,\n"
                         f"tcp4 10.0.0.2:51235<->{PEER}:5900,en0,TimeWait,\n"
                         f"tcp4 10.0.0.2:51236<->{PEER}:22,en0,Established,\n"
                         f"tcp4 10.0.0.2:5900<->{PEER}:5900,en0,Established,\n")
        self.assertIsNone(screen_link.parse_links(text, {PEER}))
        self.assertIsNone(screen_link.parse_links(NETTOP, {PEER}))

    def test_state_column_is_read_from_the_header(self):
        row = f"tcp4 10.0.0.2:51234<->{PEER}:5900,utun4,Established"
        self.assertEqual(screen_link.parse_links(",interface,state\n" + row + "\n", {PEER}),
                         "outgoing")
        self.assertEqual(screen_link.parse_links(
            ",interface,state,extra,\n" + row + ",x,\n", {PEER}), "outgoing")
        # No header: the position `nettop -J interface,state` prints today.
        self.assertEqual(screen_link.parse_links(row + ",\n", {PEER}), "outgoing")

    def test_garbage_is_none(self):
        for text in ("", "nonsense", "tcp4 x<->y,,Established,", "tcp4 a:b<->c:d,e,Established,",
                     "tcp9 1.2.3.4:1<->1.2.3.4:5900,,Established,", None, b"bytes"):
            self.assertIsNone(screen_link.parse_links(text, {PEER}))
        self.assertIsNone(screen_link.parse_links(
            f"tcp4 1.1.1.1:1<->{PEER}:5900,,Established,", set()))


class PeerAddressesTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        self.calls = []

    def runner(self, rc=0, stdout="", timeout=False):
        def run(argv, **kwargs):
            self.calls.append((argv, kwargs.get("timeout")))
            if timeout:
                raise subprocess.TimeoutExpired(argv, kwargs.get("timeout"))
            return subprocess.CompletedProcess(argv, rc, stdout, "")
        return run

    def test_ip_literals_run_no_command(self):
        run = self.runner()
        self.assertEqual(screen_link.peer_addresses(PEER, run), {PEER})
        self.assertEqual(screen_link.peer_addresses("[fd7a::9]", run), {"fd7a::9"})
        self.assertEqual(screen_link.peer_addresses("fe80::9%en0", run), {"fe80::9"})
        self.assertEqual(self.calls, [])

    def test_name_is_resolved_with_dscacheutil(self):
        out = ("name: mini.example\nipv6_address: fd7a::9\n\n"
               "name: mini.example\nip_address: 100.64.0.7\n\n")
        got = screen_link.peer_addresses("mini.example", self.runner(stdout=out))
        self.assertEqual(got, {"fd7a::9", PEER})
        self.assertEqual(self.calls, [(["dscacheutil", "-q", "host", "-a", "name", "mini.example"],
                                       screen_link.TIMEOUT_SECS)])

    def test_failures_are_empty(self):
        self.assertEqual(screen_link.peer_addresses("mini.example", self.runner(timeout=True)), set())
        self.assertEqual(screen_link.peer_addresses("mini.example", self.runner(rc=1)), set())
        self.assertEqual(screen_link.peer_addresses("mini.example", self.runner(stdout="")), set())
        self.assertEqual(screen_link.peer_addresses("-oops", self.runner()), set())
        self.assertEqual(screen_link.peer_addresses(None, self.runner()), set())

    def test_link_to_runs_nettop_once_bounded(self):
        out = f"tcp4 10.0.0.2:51234<->{PEER}:5900,en0,Established,\n"
        self.assertEqual(screen_link.link_to({"host": PEER}, self.runner(stdout=out)), "outgoing")
        self.assertEqual(self.calls, [(screen_link.NETTOP_ARGV, screen_link.TIMEOUT_SECS)])

    def test_link_to_failures_are_none(self):
        self.assertIsNone(screen_link.link_to({"host": PEER}, self.runner(rc=1)))
        self.assertIsNone(screen_link.link_to({"host": PEER}, self.runner(timeout=True)))
        self.calls.clear()
        self.assertIsNone(screen_link.link_to({"host": "mini.example"}, self.runner(stdout="")))
        self.assertEqual(len(self.calls), 1)  # nothing resolved: no nettop

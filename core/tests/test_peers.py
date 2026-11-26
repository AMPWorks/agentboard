"""Peer sync: identity, config, pairing, authorized_keys, export, gate, sync, merge.

Every test runs under AgentBoardTestCase's temp HOME and AGENTBOARD_HOME,
both with a space in the path, so `~/.ssh/authorized_keys` here is the fake
home's, never the real one.
"""

import base64
import json
import os
import stat
import subprocess
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import peers  # noqa: E402
from tests.helpers import AgentBoardTestCase as _BaseTestCase  # noqa: E402


class AgentBoardTestCase(_BaseTestCase):
    # Every peer test runs with a space in HOME and AGENTBOARD_HOME, as the
    # default data directory has one (the ssh known_hosts split went unseen
    # while the tests used paths without one).
    home_with_space = True

PEER_ID = "11111111-2222-4333-8444-555555555555"
OTHER_ID = "66666666-7777-4888-9999-aaaaaaaaaaaa"


def fake_key(seed: int) -> str:
    blob = b"\x00\x00\x00\x0bssh-ed25519\x00\x00\x00\x20" + bytes([seed]) * 32
    return "ssh-ed25519 " + base64.b64encode(blob).decode()


def make_code(**overrides) -> str:
    payload = {
        "v": 1, "id": PEER_ID, "name": "Mini", "user": "someone", "host": "mini.local",
        "key": fake_key(1), "host_key": fake_key(2),
    }
    payload.update(overrides)
    payload = {k: v for k, v in payload.items() if v is not None}
    raw = json.dumps(payload).encode()
    return peers.CODE_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def fake_ssh_files():
    """Stand-ins for AgentBoard's key and pinned known_hosts, which ssh_argv
    requires (it refuses to run ssh without them)."""
    for path in (peers.key_path(), peers.known_hosts_path()):
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("stand-in\n")


def no_scutil(argv, **kwargs):
    if argv and argv[0].endswith("scutil"):
        return subprocess.CompletedProcess(argv, 1, "", "")
    return subprocess.run(argv, **kwargs)


class IdentityTests(AgentBoardTestCase):
    def test_machine_id_is_created_once_and_stable(self):
        self.assertIsNone(peers.read_machine())
        first = peers.ensure_machine(run=no_scutil)
        self.assertTrue(peers.valid_id(first["id"]))
        self.assertEqual(peers.ensure_machine(run=no_scutil), first)
        self.assertEqual(peers.read_machine(), first)
        renamed = peers.set_machine_name("Laptop", run=no_scutil)
        self.assertEqual(renamed["id"], first["id"])
        self.assertEqual(peers.read_machine()["name"], "Laptop")
        with self.assertRaises(peers.PeerError):
            peers.set_machine_name("bad\nname")

    def test_config_writer_keeps_unrelated_keys(self):
        path = self.agentboard_home / "config.json"
        path.write_text(json.dumps({"providers": [{"name": "x", "command": ["y"]}], "log_level": "debug"}))
        peers.set_share("titles")
        data = json.loads(path.read_text())
        self.assertEqual(data["providers"], [{"name": "x", "command": ["y"]}])
        self.assertEqual(data["log_level"], "debug")
        self.assertEqual(data["peer_share"], "titles")
        self.assertEqual(peers.share_level(data), "titles")

    def test_config_writer_refuses_malformed_json(self):
        path = self.agentboard_home / "config.json"
        path.write_text("{not json")
        with self.assertRaises(peers.MalformedConfigError):
            peers.set_share("state")
        self.assertEqual(path.read_text(), "{not json")
        path.write_text("[1, 2]")
        with self.assertRaises(peers.MalformedConfigError):
            peers.set_share("state")

    def test_key_files_are_0600(self):
        key = peers.ensure_keypair(run=no_scutil)
        self.assertTrue(peers.valid_key(key))
        for path in (peers.key_path(), peers.key_path().with_suffix(".pub")):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(peers.keys_dir().stat().st_mode), 0o700)
        self.assertEqual(peers.ensure_keypair(run=no_scutil), key)

    def test_defaults_for_bad_values(self):
        self.assertEqual(peers.share_level({"peer_share": "everything"}), "full")
        self.assertEqual(peers.stale_days({"peer_stale_days": -1}), 7)
        self.assertEqual(peers.stale_days({"peer_stale_days": True}), 7)
        self.assertEqual(peers.stale_days({"peer_stale_days": 2}), 2)


class PairingCodeTests(AgentBoardTestCase):
    def test_code_round_trips(self):
        host_key = self.tmp_path / "host.pub"
        host_key.write_text(fake_key(9) + " root@mac\n")
        built = peers.build_code(run=no_scutil, host_key_path=host_key)
        parsed = peers.parse_code(built["code"])
        self.assertEqual(parsed["id"], peers.read_machine()["id"])
        self.assertEqual(parsed["host_key"], fake_key(9))
        self.assertEqual(parsed["fingerprint"], built["fingerprint"])
        # Pasted with line breaks and spaces, it still parses.
        code = built["code"]
        self.assertEqual(peers.parse_code(code[:20] + "\n  " + code[20:])["id"], parsed["id"])

    def test_bad_codes_are_rejected(self):
        good = make_code()
        self.assertEqual(peers.parse_code(good)["id"], PEER_ID)
        bad = [
            good[:-10],                                   # truncated
            peers.CODE_PREFIX + "A" * 5000,                # oversized
            "hello",                                       # not a code
            make_code(v=2),                                # wrong version
            make_code(name=5),                             # wrong type
            make_code(host="-oProxyCommand=evil"),         # option-looking host
            make_code(user="a b"),
            make_code(key="ssh-rsa AAAA"),
            make_code(id="not-a-uuid"),
            make_code(extra="x"),
            make_code(host_key=None),                      # missing field
        ]
        for code in bad:
            with self.subTest(code=code[:40]):
                with self.assertRaises(peers.PeerError):
                    peers.parse_code(code)

    def test_fingerprint_is_short_and_stable(self):
        fp = peers.parse_code(make_code())["fingerprint"]
        self.assertRegex(fp, r"\A[0-9A-F]{4}(-[0-9A-F]{4}){5}\Z")
        self.assertNotEqual(fp, peers.parse_code(make_code(key=fake_key(3)))["fingerprint"])


class AuthorizedKeysTests(AgentBoardTestCase):
    def ak(self) -> Path:
        return self.fake_home / ".ssh" / "authorized_keys"

    def test_add_is_idempotent_and_creates_with_safe_modes(self):
        peers.authorize(PEER_ID, fake_key(1), True, bin_path="/opt/ab/bin/agentboard")
        first = self.ak().read_bytes()
        peers.authorize(PEER_ID, fake_key(1), True, bin_path="/opt/ab/bin/agentboard")
        self.assertEqual(self.ak().read_bytes(), first)
        self.assertEqual(first.count(b"\n"), 1)
        self.assertEqual(stat.S_IMODE(self.ak().stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.ak().parent.stat().st_mode), 0o700)
        line = first.decode()
        self.assertTrue(line.startswith('restrict,pty,command="/opt/ab/bin/agentboard peer-gate '
                                        + PEER_ID + '" ssh-ed25519 '))
        self.assertTrue(line.rstrip().endswith(" agentboard-peer:" + PEER_ID))

    def test_replace_keeps_other_lines_byte_identical(self):
        self.ak().parent.mkdir(mode=0o700)
        others = b"ssh-ed25519 AAAAother me@laptop\r\n# comment\nssh-rsa AAAAB3 x"
        self.ak().write_bytes(others)
        os.chmod(self.ak(), 0o640)
        peers.authorize(PEER_ID, fake_key(1), True, bin_path="/x/agentboard")
        peers.authorize(PEER_ID, fake_key(1), False, bin_path="/x/agentboard")
        data = self.ak().read_bytes()
        self.assertTrue(data.startswith(others + b"\n"))
        marked = [l for l in data.splitlines() if b"agentboard-peer:" in l]
        self.assertEqual(len(marked), 1)
        self.assertTrue(marked[0].startswith(b'restrict,command='))
        self.assertNotIn(b"pty", marked[0])
        self.assertEqual(stat.S_IMODE(self.ak().stat().st_mode), 0o640)

    def test_remove_deletes_only_the_marked_line(self):
        self.ak().parent.mkdir(mode=0o700)
        self.ak().write_bytes(b"keep-1\n")
        peers.authorize(PEER_ID, fake_key(1), True, bin_path="/x/agentboard")
        peers.authorize(OTHER_ID, fake_key(2), True, bin_path="/x/agentboard")
        with open(self.ak(), "ab") as fh:
            fh.write(b"keep-2 agentboard-peer:not-an-id\n")
        self.assertEqual(peers.authorized_ids(), {PEER_ID, OTHER_ID})
        self.assertEqual(peers.deauthorize(PEER_ID), 1)
        self.assertEqual(peers.authorized_ids(), {OTHER_ID})
        self.assertEqual(peers.deauthorize(None, dry_run=True), 1)
        self.assertEqual(peers.authorized_ids(), {OTHER_ID})
        self.assertEqual(peers.deauthorize(None), 1)
        self.assertEqual(self.ak().read_bytes(), b"keep-1\nkeep-2 agentboard-peer:not-an-id\n")

    def test_path_with_a_quote_is_refused(self):
        with self.assertRaises(peers.PeerError):
            peers.authorized_line(PEER_ID, fake_key(1), True, bin_path='/a"b/agentboard')

    def test_add_peer_pins_host_key_and_writes_config(self):
        peer = peers.add_peer(make_code(), bin_path="/x/agentboard")
        self.assertEqual(peer["name"], "Mini")
        config = peers.load_config()
        self.assertEqual([p["id"] for p in peers.configured_peers(config)], [PEER_ID])
        known = peers.known_hosts_path().read_text()
        self.assertEqual(known, f"agentboard-{PEER_ID} {fake_key(2)}\n")
        self.assertEqual(peers.authorized_ids(), {PEER_ID})
        # Adding again replaces, never duplicates.
        peers.add_peer(make_code(name="Mini 2"), bin_path="/x/agentboard")
        self.assertEqual(len(peers.load_config()["peers"]), 1)
        self.assertEqual(peers.load_config()["peers"][0]["name"], "Mini 2")
        peers.remove_peer(PEER_ID)
        self.assertEqual(peers.load_config()["peers"], [])
        self.assertEqual(peers.known_hosts_path().read_text(), "")
        self.assertEqual(peers.authorized_ids(), set())

    def test_own_code_is_refused(self):
        machine = peers.ensure_machine(run=no_scutil)
        with self.assertRaises(peers.PeerError):
            peers.add_peer(make_code(id=machine["id"]), bin_path="/x/agentboard")

    def test_remote_login_probe(self):
        def refused(*a, **k):
            raise ConnectionRefusedError()

        def timeout(*a, **k):
            raise TimeoutError()

        self.assertEqual(peers.remote_login_status(refused), "off")
        self.assertEqual(peers.remote_login_status(timeout), "unknown")
        self.assertEqual(peers.remote_login_status(lambda *a, **k: mock.Mock()), "on")


def insert_session(conn, tool="claude", session_id="s1", **overrides):
    row = {
        "tool": tool, "session_id": session_id, "cwd": "/tmp/proj", "branch": "main",
        "title": None, "first_prompt": "secret prompt", "last_prompt": "secret last",
        "last_assistant": "secret reply", "started_at": "2026-06-15T10:00:00.000000Z",
        "updated_at": "2026-06-15T10:00:00.000000Z", "last_event": "Stop",
        "last_event_at": "2026-06-15T10:00:00.000000Z", "waiting_reason": None,
        "pid": None, "iterm_session_id": None, "term_program": None, "tty": None,
        "transcript_path": None, "origin": "hook", "interactive": 1, "ended_at": None,
    }
    row.update(overrides)
    cols = ",".join(row.keys())
    conn.execute(f"INSERT INTO sessions({cols}) VALUES ({','.join('?' for _ in row)})",
                 list(row.values()))


NOW = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)


class ExportTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        peers.ensure_machine(run=no_scutil)
        insert_session(self.conn, session_id="s1")
        from agentboard import notes
        notes.record_note(self.conn, "claude", "s1", "update", "secret note", "cli",
                          "2026-06-15T10:00:00.000000Z")

    def export(self, level, allow_open=True):
        return peers.build_export(self.conn, level, allow_open=allow_open, now=NOW)

    def test_full_has_text(self):
        data = self.export("full")
        self.assertEqual(data["v"], peers.PROTOCOL_VERSION)
        self.assertEqual(data["machine"]["id"], peers.read_machine()["id"])
        [entry] = data["sessions"]
        self.assertEqual(entry["first_prompt"], "secret prompt")
        self.assertEqual(entry["cwd"], "/tmp/proj")
        self.assertEqual([n["text"] for n in entry["notes"]], ["secret note"])
        self.assertEqual(entry["remote_open"], "resume")
        self.assertNotIn("resume_command", entry)
        self.assertNotIn("usage", data)

    def test_lower_levels_drop_text(self):
        for level in ("titles", "state"):
            with self.subTest(level=level):
                data = self.export(level)
                text = json.dumps(data)
                for secret in ("secret prompt", "secret last", "secret reply", "secret note",
                               "/tmp/proj"):
                    self.assertNotIn(secret, text)
                [entry] = data["sessions"]
                self.assertNotIn("notes", entry)
                self.assertNotIn("cwd", entry)
        titles = self.export("titles")["sessions"][0]
        self.assertEqual((titles["notes_count"], titles["branch"]), (1, "main"))
        state = self.export("state")["sessions"][0]
        self.assertEqual(state["title"], "proj")
        self.assertNotIn("branch", state)
        self.assertEqual(set(state) - set(peers._STATE_FIELDS), {"title", "triage", "remote_open"})

    def test_each_level_has_exactly_its_fields(self):
        extras = {"full": {"triage", "remote_open", "links", "notes", "denials"},
                  "titles": {"triage", "remote_open", "links"},
                  "state": {"triage", "remote_open", "title"}}
        fields = {"full": peers._FULL_FIELDS, "titles": peers._TITLE_FIELDS,
                  "state": peers._STATE_FIELDS}
        for level in peers.SHARE_LEVELS:
            entry = self.export(level)["sessions"][0]
            self.assertEqual(set(entry), set(fields[level]) | extras[level])

    def test_opening_not_allowed_gives_no_remote_open(self):
        self.assertIsNone(self.export("full", allow_open=False)["sessions"][0]["remote_open"])

    def test_remote_open_kinds(self):
        self.assertEqual(peers.remote_open_for({"alive": True, "tmux": True}, True), "tmux")
        self.assertEqual(peers.remote_open_for({"alive": True, "tmux": False}, True), "screen")
        self.assertEqual(peers.remote_open_for({"alive": False, "resume_command": "x"}, True), "resume")
        self.assertIsNone(peers.remote_open_for({"alive": False}, True))

    def test_export_never_contains_a_peer_session(self):
        snapshot = {"sessions": [
            {"tool": "claude", "session_id": "mine", "state": "running", "machine": None},
            {"tool": "claude", "session_id": "theirs", "state": "running",
             "machine": {"id": PEER_ID, "name": "Mini"}},
        ]}
        data = peers.build_export(self.conn, "full", snapshot=snapshot, now=NOW)
        self.assertEqual([s["session_id"] for s in data["sessions"]], ["mine"])
        # And the export validates on the receiving side.
        self.assertEqual(len(peers.validate_line(json.dumps(self.export("full")))["sessions"]), 1)

    def test_export_keeps_live_and_newest_within_the_cap(self):
        def entry(sid, updated, **kw):
            return {"tool": "claude", "session_id": sid, "state": "done", "alive": False,
                    "updated_at": updated, "machine": None, **kw}
        snapshot = {"sessions": [
            entry("old", "2026-06-01T00:00:00.000000Z"),
            entry("live", "2026-05-01T00:00:00.000000Z", alive=True, state="running"),
            entry("new", "2026-06-10T00:00:00.000000Z"),
            entry("waits", "2026-05-02T00:00:00.000000Z", state="needs_you"),
        ]}
        with mock.patch.object(peers, "MAX_SESSIONS", 3):
            data = peers.build_export(self.conn, "full", snapshot=snapshot, now=NOW)
            self.assertEqual([s["session_id"] for s in data["sessions"]], ["live", "new", "waits"])
            # A line over MAX_LINE_BYTES drops the lowest ranked until it fits.
            line = peers.encode_export(data)
            limit = len(line.encode()) - 1
            with mock.patch.object(peers, "MAX_LINE_BYTES", limit):
                trimmed = peers.encode_export(data)
                self.assertLessEqual(len(trimmed.encode()), limit)
                kept = [s["session_id"] for s in peers.validate_line(trimmed)["sessions"]]
                self.assertEqual(kept, ["live", "waits"])


class GateTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        peers.ensure_machine(run=no_scutil)
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        insert_session(self.conn, session_id="s1")
        from agentboard import peer_gate
        self.gate = peer_gate
        for target in ("subprocess.run", "subprocess.Popen", "os.execv"):
            patcher = mock.patch(target, side_effect=AssertionError(f"unexpected {target}"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def refused(self, command, peer_id=PEER_ID):
        with mock.patch("sys.stderr"):
            rc = self.gate.run(peer_id, command=command, launch=self.fail_exec)
        self.assertNotEqual(rc, 0, command)

    def fail_exec(self, *args):
        raise AssertionError("exec'd")

    def test_bad_commands_are_refused_without_a_subprocess(self):
        for command in (
            None, "", "shell", "serve now", "mark claude s1", "mark claude s1 done 3",
            "mark claude s1 nuke", "attach claude s1 extra", "attach claude s1; rm -rf ~",
            "attach claude $(id)", "attach claude `id`", "attach claude s1|cat",
            "attach  claude s1", "attach bash s1", "attach claude ../../etc",
            "resume claude unknown-session", "mark claude unknown-session done",
            "x" * 300,
        ):
            with self.subTest(command=command):
                self.refused(command)

    def test_unpaired_peer_is_refused(self):
        self.refused("serve", peer_id=OTHER_ID)
        self.refused("serve", peer_id="not-an-id")

    def test_attach_without_permission_is_refused(self):
        peers.set_peer(PEER_ID, allow_open=False, bin_path="/x/agentboard")
        for verb in ("attach", "resume", "focus"):
            with self.subTest(verb=verb):
                self.refused(f"{verb} claude s1")

    def test_resume_of_a_live_session_is_refused(self):
        live = {"alive": True, "tmux": False}
        with mock.patch("sys.stderr"):
            rc = self.gate.run(PEER_ID, command="resume claude s1", launch=self.fail_exec,
                               entry_for=lambda t, s: live)
        self.assertEqual(rc, self.gate.EXIT_REFUSED)

    def test_resume_execs_the_login_shell(self):
        calls = []
        rc = self.gate.run(PEER_ID, command="resume claude s1",
                           launch=lambda peer_id, argv: calls.append(argv),
                           entry_for=lambda t, s: {"alive": False, "resume_command": "x"})
        self.assertEqual(rc, 0)
        [argv] = calls
        # Interactive too, so a PATH set only in ~/.zshrc finds claude.
        self.assertEqual(argv[1:4], ["-l", "-i", "-c"])
        self.assertEqual(argv[4], "cd /tmp/proj && claude --resume s1")

    def test_mark_done_changes_triage(self):
        self.assertEqual(self.gate.run(PEER_ID, command="mark claude s1 done"), 0)
        row = self.conn.execute("SELECT status FROM triage WHERE session_id='s1'").fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(self.gate.run(PEER_ID, command="mark claude s1 snooze 2.5"), 0)
        row = self.conn.execute("SELECT status FROM triage WHERE session_id='s1'").fetchone()
        self.assertEqual(row["status"], "snoozed")

    def test_parse_command(self):
        self.assertEqual(self.gate.parse_command("serve"), ("serve", {}))
        verb, parsed = self.gate.parse_command("mark codex abc-1 snooze 24")
        self.assertEqual((verb, parsed["hours"]), ("mark", 24.0))

    def test_serve_emits_on_change_and_on_heartbeat(self):
        import io
        out = io.StringIO()
        clock = {"t": 0.0}
        steps = []

        def wait():
            steps.append(len(out.getvalue().splitlines()))
            n = len(steps)
            if n == 1:
                # A hook event lands: the session gets a new prompt.
                self.conn.execute("UPDATE sessions SET last_prompt='next', "
                                  "updated_at='2026-06-15T11:00:00.000000Z' WHERE session_id='s1'")
            elif n == 2:
                clock["t"] += 1  # nothing changed: no line
            elif n == 3:
                clock["t"] += peers.HEARTBEAT_SECS
            return n >= 4

        peer = peers.find_peer(peers.load_config(), PEER_ID)
        rc = self.gate.serve(peer, out=out, clock=lambda: clock["t"], wait=wait)
        self.assertEqual(rc, 0)
        lines = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual(len(lines), 3, steps)
        self.assertEqual(steps, [1, 2, 2, 3])
        self.assertEqual(lines[0]["sessions"][0]["last_prompt"], "secret last")
        self.assertEqual(lines[1]["sessions"][0]["last_prompt"], "next")
        self.assertEqual(lines[2]["sessions"], lines[1]["sessions"])

    def test_serve_follows_the_peer_config(self):
        import io
        out = io.StringIO()
        steps = []

        def wait():
            steps.append(1)
            if len(steps) == 1:
                peers.set_peer(PEER_ID, allow_open=False, bin_path="/x/agentboard")
            elif len(steps) == 2:
                peers.remove_peer(PEER_ID)
            return len(steps) > 5

        peer = peers.find_peer(peers.load_config(), PEER_ID)
        rc = self.gate.serve(peer, out=out, wait=wait)
        self.assertEqual(rc, 0)
        lines = [json.loads(l) for l in out.getvalue().splitlines()]
        # Opening turned off reaches the next line; removal ends the stream.
        self.assertEqual([l["sessions"][0]["remote_open"] for l in lines], ["resume", None])
        self.assertEqual(len(steps), 2)

    def test_serve_stops_on_a_closed_pipe(self):
        class Broken:
            def write(self, _):
                raise BrokenPipeError()

            def flush(self):
                pass

        peer = peers.find_peer(peers.load_config(), PEER_ID)
        self.assertEqual(self.gate.serve(peer, out=Broken(), wait=lambda: False), 0)


class FakeProc:
    def __init__(self, data=b"", stderr=b"", rc=255):
        import io
        self.stdout = io.BytesIO(data)
        self.stderr = io.BytesIO(stderr)
        self.rc = rc
        self.terminated = False

    def poll(self):
        return self.rc if self.stdout.tell() == len(self.stdout.getvalue()) else None

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return self.rc

    def kill(self):
        pass


def export_line(sessions=None, machine_id=PEER_ID, **overrides):
    data = {"v": 1, "machine": {"id": machine_id, "name": "Mini"},
            "generated_at": "2026-06-15T11:59:59.000000Z", "share": "full",
            "sessions": sessions if sessions is not None else [
                {"tool": "claude", "session_id": "r1", "state": "running", "title": "Remote work",
                 "updated_at": "2026-06-15T11:59:00.000000Z", "alive": True, "tmux": True,
                 "remote_open": "tmux"},
            ]}
    data.update(overrides)
    return json.dumps(data).encode()


class SyncTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        from agentboard import peer_sync
        self.sync_mod = peer_sync
        self.local = peers.ensure_machine(run=no_scutil)
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        fake_ssh_files()
        self.clock = {"t": 0.0}
        self.spawned = []

    def make_sync(self, procs):
        def spawner(peer, argv):
            self.spawned.append(self.clock["t"])
            return procs.pop(0) if procs else FakeProc()
        return self.sync_mod.PeerSync(
            spawner=spawner, clock=lambda: self.clock["t"],
            wall=lambda: NOW, ssh_argv=lambda peer: ["ssh", peer["host"]], min_line_secs=0)

    def settle(self, sync):
        for link in sync.links.values():
            for thread in (link.reader, link.handler):
                if thread is not None:
                    thread.join(timeout=5)

    def test_bad_lines_are_dropped_and_good_ones_cached(self):
        too_many = export_line(sessions=[{"tool": "claude", "session_id": f"s{i}", "state": "done"}
                                         for i in range(peers.MAX_SESSIONS + 1)])
        wrong_type = export_line(sessions=[{"tool": "claude", "session_id": "x", "state": "done",
                                            "alive": "yes"}])
        oversized = b'{"v":1,"pad":"' + b"x" * (peers.MAX_LINE_BYTES + 10) + b'"}'
        lines = [b"not json", too_many, wrong_type, oversized, export_line(machine_id=OTHER_ID),
                 export_line(v=2), export_line()]
        sync = self.make_sync([FakeProc(b"\n".join(lines) + b"\n")])
        sync.tick()
        self.settle(sync)
        cache = peers.read_cache(PEER_ID)
        self.assertEqual(cache["status"], "online")
        self.assertEqual([s["session_id"] for s in cache["sessions"]], ["r1"])
        self.assertEqual(cache["received_at"], peers.now_iso(NOW))
        self.assertEqual(cache["clock_skew_secs"], -1.0)
        self.assertTrue(peers.stamp_path().exists())
        self.assertEqual(sync.links[PEER_ID].lines, 1)

    def test_rejected_lines_record_an_error(self):
        too_many = export_line(sessions=[{"tool": "claude", "session_id": f"s{i}", "state": "done"}
                                         for i in range(peers.MAX_SESSIONS + 1)])
        sync = self.make_sync([FakeProc(export_line() + b"\n" + too_many + b"\n")])
        sync.tick()
        self.settle(sync)
        cache = peers.read_cache(PEER_ID)
        self.assertEqual(cache["status"], "online")
        self.assertEqual(cache["error"], f"its board is too large to sync ({peers.MAX_SESSIONS + 1} "
                                         "sessions); update both Macs")
        # last_seen_at stays at the last good line, so the peer goes offline
        # (its cached sessions stale) while Settings shows the reason.
        self.assertEqual(cache["last_seen_at"], peers.now_iso(NOW))
        self.assertEqual(peers.peer_status(cache, NOW + timedelta(minutes=5))[0], "offline")
        [listed] = peers.remote_entries(peers.load_config(), NOW + timedelta(minutes=5))[1]
        self.assertEqual(listed["error"], cache["error"])
        sync2 = self.make_sync([FakeProc(b'{"v":1,"pad":"' + b"x" * (peers.MAX_LINE_BYTES + 10) + b'"}\n')])
        sync2.tick()
        self.settle(sync2)
        self.assertIn("too large to sync", peers.read_cache(PEER_ID)["error"])

    def test_a_stopped_link_writes_no_cache(self):
        sync = self.make_sync([])
        sync.reload(force=True)
        link = sync.links[PEER_ID]
        peers.remove_peer(PEER_ID)
        sync._stop(link)
        sync.handle_line(link, export_line())
        self.assertIsNone(peers.read_cache(PEER_ID))

    def test_incompatible_version_sets_status(self):
        sync = self.make_sync([FakeProc(export_line(v=99) + b"\n")])
        sync.tick()
        self.settle(sync)
        self.clock["t"] = 1
        sync.tick()
        self.assertEqual(peers.read_cache(PEER_ID)["status"], "incompatible")

    def test_dying_child_is_retried_with_backoff(self):
        sync = self.make_sync([FakeProc(stderr=b"ssh: connect to host mini.local port 22: Connection refused")
                               for _ in range(10)])
        for t in range(0, 130):
            self.clock["t"] = float(t)
            sync.tick()
            self.settle(sync)
            sync.tick()  # reaps the child that just exited
        self.assertEqual(self.spawned[:6], [0.0, 2.0, 6.0, 14.0, 30.0, 62.0])
        self.assertEqual(self.spawned[6], 122.0)  # capped at 60 s
        cache = peers.read_cache(PEER_ID)
        self.assertEqual(cache["status"], "offline")
        self.assertEqual(cache["error"], "Remote Login is off there")

    def test_backoff_resets_after_a_good_connection(self):
        sync = self.make_sync([FakeProc(), FakeProc(), FakeProc(export_line() + b"\n"), FakeProc()])
        for t in range(0, 20):
            self.clock["t"] = float(t)
            sync.tick()
            self.settle(sync)
            sync.tick()
        # 0 fails, 2 fails, 6 delivers a line then closes, retried 2 s later.
        self.assertEqual(self.spawned[:4], [0.0, 2.0, 6.0, 8.0])

    def test_removed_peer_is_stopped(self):
        proc = FakeProc()
        proc.poll = lambda: None
        sync = self.make_sync([proc])
        sync.tick()
        peers.remove_peer(PEER_ID)
        sync.tick()
        self.assertTrue(proc.terminated)
        self.assertEqual(sync.links, {})

    def test_second_instance_exits_on_the_lock(self):
        fd = self.sync_mod._acquire_lock(self.agentboard_home)
        self.assertIsNotNone(fd)
        try:
            self.assertEqual(self.sync_mod.run(spawner=lambda p, a: FakeProc()),
                             self.sync_mod.EXIT_ALREADY_RUNNING)
        finally:
            os.close(fd)

    def test_exits_when_orphaned_or_without_peers(self):
        with mock.patch("signal.signal"):
            self.assertEqual(self.sync_mod.run(spawner=lambda p, a: FakeProc(),
                                               parent_alive=lambda: False, sleep=lambda s: None), 0)
            peers.remove_peer(PEER_ID)
            self.assertEqual(self.sync_mod.run(spawner=lambda p, a: FakeProc(),
                                               parent_alive=lambda: True, sleep=lambda s: None), 0)

    def test_two_home_loopback(self):
        # The "peer" is a second data dir whose gate serves over a pipe, no ssh.
        other_home = self.tmp_path / "mini-home"
        other_ab = self.tmp_path / "mini-ab"
        other_home.mkdir()
        other_ab.mkdir()
        (other_ab / "machine.json").write_text(json.dumps({"id": PEER_ID, "name": "Mini"}))
        (other_ab / "config.json").write_text(json.dumps({"peers": [{
            "id": self.local["id"], "name": "Laptop", "host": "laptop.local", "user": "someone",
            "allow_open": True, "public_key": fake_key(5), "host_key": fake_key(6)}]}))
        from agentboard import db
        conn = db.connect(other_ab / "agentboard.db")
        insert_session(conn, session_id="mini-1", title="On the mini")
        conn.close()
        env = stub_env(self)
        env.update(HOME=str(other_home), AGENTBOARD_HOME=str(other_ab), SSH_ORIGINAL_COMMAND="serve")

        def spawner(peer, argv):
            return subprocess.Popen([str(SHIM), "peer-gate", self.local["id"]], env=env,
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE)

        with mock.patch("signal.signal"):
            rc = self.sync_mod.run(once=True, spawner=spawner, parent_alive=lambda: True)
        self.assertEqual(rc, 0)
        cache = peers.read_cache(PEER_ID)
        self.assertEqual(cache["status"], "online")
        self.assertEqual([s["title"] for s in cache["sessions"]], ["On the mini"])
        self.assertEqual(cache["machine"]["name"], "Mini")


class MergeTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        from agentboard import cli
        self.cli = cli
        peers.ensure_machine(run=no_scutil)
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        insert_session(self.conn, session_id="r1", updated_at="2026-06-15T11:00:00.000000Z")

    def cache(self, seen=NOW - timedelta(seconds=5), received=None, **extra):
        data = peers.validate_line(export_line(sessions=[
            {"tool": "claude", "session_id": "r1", "state": "running", "title": "Remote work",
             "updated_at": "2026-06-15T11:59:00.000000Z", "alive": True, "tmux": True,
             "remote_open": "tmux", "notes_count": 2},
        ]))
        cache = {"peer_id": PEER_ID, "status": "online", "error": None,
                 "received_at": peers.now_iso(received or seen), "last_seen_at": peers.now_iso(seen),
                 **data, **extra}
        peers.write_cache(PEER_ID, cache)

    def snapshot(self, now=NOW):
        return self.cli.build_snapshot(self.conn, now=now)

    def test_remote_entry_shape_and_duplicate_ids(self):
        self.cache()
        snap = self.snapshot()
        keys = sorted(e["key"] for e in snap["sessions"])
        self.assertEqual(keys, ["claude:r1", f"claude:r1@{PEER_ID}"])
        remote = next(e for e in snap["sessions"] if e["machine"])
        local = next(e for e in snap["sessions"] if not e["machine"])
        self.assertEqual(remote["machine"], {"id": PEER_ID, "name": "Mini"})
        self.assertEqual((remote["stale"], remote["remote_open"], remote["can_focus"]),
                         (False, "tmux", True))
        self.assertIsNone(remote["resume_command"])
        self.assertEqual((local["machine"], local["stale"], local["remote_open"]), (None, False, None))
        self.assertEqual(snap["machine"]["id"], peers.read_machine()["id"])
        [peer] = snap["peers"]
        self.assertEqual((peer["id"], peer["status"], peer["session_count"]), (PEER_ID, "online", 1))
        self.assertEqual(snap["counts"]["running"], 1)
        self.assertEqual(snap["counts"]["notes"], 2)

    def test_stale_after_45_seconds_and_left_out_of_counts(self):
        self.cache(seen=NOW - timedelta(seconds=46))
        snap = self.snapshot()
        remote = next(e for e in snap["sessions"] if e["machine"])
        self.assertTrue(remote["stale"])
        self.assertEqual(snap["peers"][0]["status"], "offline")
        self.assertEqual(snap["peers"][0]["session_count"], 1)
        self.assertEqual(snap["counts"]["running"], 0)
        self.assertEqual(snap["counts"]["notes"], 0)

    def test_old_cache_is_dropped(self):
        self.cache(seen=NOW - timedelta(days=8))
        snap = self.snapshot()
        self.assertEqual([e["key"] for e in snap["sessions"]], ["claude:r1"])
        self.assertEqual(snap["peers"][0]["status"], "expired")
        peers.update_config(lambda d: d.__setitem__("peer_stale_days", 10))
        self.assertEqual(len(self.snapshot()["sessions"]), 2)

    def test_corrupt_cache_is_skipped(self):
        peers.peers_dir().mkdir(exist_ok=True)
        peers.cache_path(PEER_ID).write_text("{oops")
        snap = self.snapshot()
        self.assertEqual([e["key"] for e in snap["sessions"]], ["claude:r1"])
        self.assertEqual(snap["peers"][0]["status"], "never")

    def test_removed_peers_cache_is_ignored(self):
        self.cache()
        peers.update_config(lambda d: d.__setitem__("peers", []))
        snap = self.snapshot()
        self.assertEqual([e["key"] for e in snap["sessions"]], ["claude:r1"])
        self.assertEqual(snap["peers"], [])

    def test_cache_write_updates_the_stamp(self):
        self.cache()
        first = peers.stamp_path().read_text()
        peers.write_cache(PEER_ID, peers.read_cache(PEER_ID), now=NOW + timedelta(seconds=1))
        self.assertNotEqual(peers.stamp_path().read_text(), first)

    def test_export_leaves_out_peer_sessions(self):
        self.cache()
        data = peers.build_export(self.conn, "full", now=NOW)
        self.assertEqual([s["session_id"] for s in data["sessions"]], ["r1"])
        self.assertEqual(len(data["sessions"]), 1)

    def test_snapshot_opens_no_socket_and_spawns_nothing(self):
        self.cache()
        boom = AssertionError("network or process in snapshot")
        with mock.patch("subprocess.run", side_effect=boom), \
                mock.patch("subprocess.Popen", side_effect=boom), \
                mock.patch("socket.socket", side_effect=boom), \
                mock.patch("socket.create_connection", side_effect=boom):
            snap = self.snapshot()
        self.assertEqual(len(snap["sessions"]), 2)


class RemoteActionTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        from agentboard import cli, peer_cli
        self.cli, self.peer_cli = cli, peer_cli
        peers.ensure_machine(run=no_scutil)
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        fake_ssh_files()
        self.calls = []

    def write_cache(self, remote_open, seen=None):
        seen = seen or datetime.now(timezone.utc)
        sessions = [{"tool": "claude", "session_id": "r1", "state": "running",
                     "remote_open": remote_open}]
        peers.write_cache(PEER_ID, {
            "peer_id": PEER_ID, "status": "online", "received_at": peers.now_iso(seen),
            "last_seen_at": peers.now_iso(seen), "sessions": sessions})

    def run_fake(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    def args(self, **kw):
        import argparse
        base = dict(tool="claude", session_id="r1", machine=PEER_ID, print=False)
        base.update(kw)
        return argparse.Namespace(**base)

    def test_tmux_opens_a_terminal_on_peer_open(self):
        self.write_cache("tmux")
        rc = self.cli.cmd_focus(self.args(), run_osascript=self.run_fake)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls[0], ["open", "-Ra", "iTerm"])
        script_call = self.calls[1]
        self.assertEqual(script_call[:3], ["osascript", "-e", self.cli._ITERM_NEW_WINDOW_APPLESCRIPT])
        self.assertTrue(script_call[3].endswith(f"peer-open --machine {PEER_ID} --verb attach claude r1"))

    def test_resume_uses_resume_verb_and_terminal_without_iterm(self):
        self.write_cache("resume")

        def run(argv, **kwargs):
            self.calls.append(argv)
            rc = 1 if argv[:2] == ["open", "-Ra"] else 0
            return subprocess.CompletedProcess(argv, rc, "", "")

        rc = self.cli.cmd_resume(self.args(), run_osascript=run)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls[1][2], self.cli._TERMINAL_RESUME_APPLESCRIPT)
        self.assertIn("--verb resume claude r1", self.calls[1][3])

    def test_screen_falls_back_to_vnc(self):
        self.write_cache("screen")
        rc = self.cli.cmd_focus(self.args(), run_osascript=self.run_fake)
        self.assertEqual(rc, 0)
        ssh, vnc = self.calls
        self.assertEqual(ssh[0], "/usr/bin/ssh")
        self.assertEqual(ssh[-1], "focus claude r1")
        self.assertEqual(vnc, ["open", "vnc://someone@mini.local"])

    def test_offline_or_unknown_peer_fails_fast(self):
        self.write_cache("tmux", seen=datetime.now(timezone.utc) - timedelta(minutes=5))
        with mock.patch("sys.stderr"):
            self.assertEqual(self.cli.cmd_focus(self.args(), run_osascript=self.run_fake),
                             peers.EXIT_PEER_UNAVAILABLE)
            self.assertEqual(self.cli.cmd_focus(self.args(machine=OTHER_ID), run_osascript=self.run_fake),
                             peers.EXIT_PEER_UNAVAILABLE)
        self.assertEqual(self.calls, [])

    def test_not_allowed_fails(self):
        self.write_cache(None)
        with mock.patch("sys.stderr"):
            self.assertEqual(self.cli.cmd_focus(self.args(), run_osascript=self.run_fake), 1)

    def test_mark_builds_a_bounded_ssh_call(self):
        self.write_cache("tmux")
        seen = {}

        def run(argv, **kwargs):
            seen.update(kwargs)
            self.calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        rc = self.cli.cmd_mark(self.args(action="snooze", hours=2.0, note=None), run=run)
        self.assertEqual(rc, 0)
        [argv] = self.calls
        self.assertEqual(argv[-1], "mark claude r1 snooze 2")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn(f"HostKeyAlias=agentboard-{PEER_ID}", argv)
        self.assertIn(f'UserKnownHostsFile="{peers.known_hosts_path()}"', argv)
        self.assertEqual(argv[argv.index("--") + 1], "mini.local")
        self.assertEqual(seen.get("timeout"), self.peer_cli.REMOTE_CALL_TIMEOUT_SECS)

    def test_mark_hours_are_formatted_as_the_gate_checks(self):
        self.write_cache("tmux")
        rc = self.cli.cmd_mark(self.args(action="snooze", hours=0.3333, note=None), run=self.run_fake)
        self.assertEqual(rc, 0)
        self.assertEqual(self.calls[-1][-1], "mark claude r1 snooze 0.333")
        from agentboard import peer_gate
        self.assertEqual(peer_gate.parse_command(self.calls[-1][-1])[1]["hours"], 0.333)
        self.calls.clear()
        with mock.patch("sys.stderr"):
            for hours in (1234567.0, -1.0):
                rc = self.cli.cmd_mark(self.args(action="snooze", hours=hours, note=None),
                                       run=self.run_fake)
                self.assertEqual(rc, 1)
        self.assertEqual(self.calls, [])

    def test_peer_open_execs_ssh_with_a_tty(self):
        import argparse
        calls = []
        rc = self.peer_cli.cmd_peer_open(
            argparse.Namespace(machine=PEER_ID, verb="attach", tool="claude", session_id="r1"),
            execv=lambda path, argv: calls.append(argv))
        self.assertEqual(rc, 0)
        self.assertEqual(calls[0][1], "-tt")
        self.assertEqual(calls[0][-1], "attach claude r1")


class RemoteControlTests(AgentBoardTestCase):
    URL = "https://claude.ai/code/session_01KVjE5EzNMnQ54BKG8zNp3J"

    def test_url_comes_from_the_registry_for_a_live_session(self):
        from agentboard import cli
        entry = {"bridgeSessionId": "session_01KVjE5EzNMnQ54BKG8zNp3J"}
        self.assertEqual(cli._remote_control_url(entry, True), self.URL)
        self.assertIsNone(cli._remote_control_url(entry, False))
        self.assertIsNone(cli._remote_control_url({"bridgeSessionId": "../evil"}, True))
        self.assertIsNone(cli._remote_control_url(None, True))

    def test_snapshot_reads_it_from_the_registry(self):
        from agentboard import cli
        pid = os.getpid()
        registry = self.fake_home / ".claude" / "sessions"
        registry.mkdir()
        (registry / f"{pid}.json").write_text(json.dumps({
            "sessionId": "s1", "pid": pid, "status": "busy", "kind": "interactive",
            "bridgeSessionId": "session_01KVjE5EzNMnQ54BKG8zNp3J",
            "updatedAt": 1781600000000}))
        insert_session(self.conn, session_id="s1", pid=pid, last_event="UserPromptSubmit")
        entry = cli.build_snapshot(self.conn, include_all=True)["sessions"][0]
        self.assertTrue(entry["alive"])
        self.assertEqual(entry["remote_control_url"], self.URL)

    def test_export_prefers_remote_control_over_screen_at_full_only(self):
        entry = {"alive": True, "tmux": False, "remote_control_url": self.URL}
        self.assertEqual(peers.remote_open_for(entry, True, "full"), "remote-control")
        self.assertEqual(peers.remote_open_for(entry, True, "titles"), "screen")
        self.assertEqual(peers.remote_open_for(dict(entry, tmux=True), True, "full"), "tmux")
        exported = peers.export_entry(dict(entry, tool="claude", session_id="x", state="running"),
                                      "titles", True)
        self.assertNotIn("remote_control_url", exported)

    def test_received_urls_are_checked(self):
        def session(url):
            return {"tool": "claude", "session_id": "r1", "state": "running", "alive": True,
                    "remote_open": "remote-control", "remote_control_url": url}
        good = peers.validate_line(export_line(sessions=[session(self.URL)]))["sessions"][0]
        self.assertEqual((good["remote_open"], good["remote_control_url"]), ("remote-control", self.URL))
        for bad in ("https://evil.example/code/session_01KVjE5EzNMnQ54BKG8zNp3J",
                    "file:///etc/passwd", "https://claude.ai/code/session_x;rm"):
            got = peers.validate_line(export_line(sessions=[session(bad)]))["sessions"][0]
            self.assertEqual((got["remote_open"], got["remote_control_url"]), ("screen", None))

    def test_click_opens_the_url(self):
        import argparse
        from agentboard import cli
        peers.ensure_machine(run=no_scutil)
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        now = datetime.now(timezone.utc)
        peers.write_cache(PEER_ID, {
            "peer_id": PEER_ID, "status": "online", "received_at": peers.now_iso(now),
            "last_seen_at": peers.now_iso(now), "sessions": [
                {"tool": "claude", "session_id": "r1", "state": "running",
                 "remote_open": "remote-control", "remote_control_url": self.URL}]})
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, "", "")

        args = argparse.Namespace(tool="claude", session_id="r1", machine=PEER_ID, print=False)
        self.assertEqual(cli.cmd_focus(args, run_osascript=run), 0)
        self.assertEqual(calls, [["open", self.URL]])


SHIM = Path(__file__).resolve().parents[1] / "bin" / "agentboard"


def stub_env(test) -> dict:
    bindir = test.tmp_path / "stub-bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "python3"
    stub.write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
    stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env.get('PATH', '')}"
    env["HOME"] = str(test.fake_home)
    env["AGENTBOARD_HOME"] = str(test.agentboard_home)
    env.pop("AGENTBOARD_CORE_DIR", None)
    return env


class PeersCliTests(AgentBoardTestCase):
    def run_shim(self, *args, env=None):
        return subprocess.run([str(SHIM), *args], env=env or stub_env(self),
                              capture_output=True, text=True, timeout=60)

    def test_add_list_set_remove_through_the_shim(self):
        result = self.run_shim("peers", "add", "--code", make_code(), "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        preview = json.loads(result.stdout)
        self.assertEqual((preview["name"], preview["own"], preview["known"]), ("Mini", False, False))
        self.assertFalse((self.agentboard_home / "config.json").exists())

        result = self.run_shim("peers", "add", "--code", make_code())
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["id"], PEER_ID)
        self.assertEqual(peers.authorized_ids(), {PEER_ID})

        result = self.run_shim("peers", "set", "--id", PEER_ID, "--host", "mini.vpn",
                               "--allow-open", "off")
        self.assertEqual(result.returncode, 0, result.stderr)
        peer = peers.find_peer(peers.load_config(), PEER_ID)
        self.assertEqual((peer["host"], peer["allow_open"]), ("mini.vpn", False))
        line = (self.fake_home / ".ssh" / "authorized_keys").read_text()
        self.assertNotIn("pty", line)

        result = self.run_shim("peers", "set-self", "--name", "Laptop", "--share", "titles")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["share"], "titles")

        result = self.run_shim("peers", "list", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        listing = json.loads(result.stdout)
        self.assertEqual(listing["machine"]["name"], "Laptop")
        self.assertEqual(listing["share"], "titles")
        self.assertIn(listing["remote_login"], ("on", "off", "unknown"))
        [item] = listing["peers"]
        self.assertEqual((item["id"], item["status"], item["authorized"]), (PEER_ID, "never", True))

        result = self.run_shim("peers", "remove", "--id", PEER_ID)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(peers.authorized_ids(), set())
        self.assertEqual(json.loads(self.run_shim("peers", "list", "--json").stdout)["peers"], [])

    def test_bad_code_and_unknown_peer_fail_cleanly(self):
        result = self.run_shim("peers", "add", "--code", "nonsense")
        self.assertEqual(result.returncode, 1)
        self.assertIn("not an AgentBoard pairing code", result.stderr)
        result = self.run_shim("peers", "set", "--id", PEER_ID, "--name", "x")
        self.assertEqual(result.returncode, 1)
        result = self.run_shim("peers", "remove")
        self.assertEqual(result.returncode, 2)


class SecurityReviewTests(AgentBoardTestCase):
    """Follow-ups from the security re-review of the peer-sync PR."""

    def ak(self) -> Path:
        return self.fake_home / ".ssh" / "authorized_keys"

    # (a) trailing newlines never pass a pattern
    def test_a_field_ending_in_a_newline_is_rejected(self):
        for field, value in (("id", PEER_ID), ("key", fake_key(1)), ("host_key", fake_key(2)),
                             ("host", "mini.local"), ("user", "someone")):
            with self.subTest(field=field):
                with self.assertRaises(peers.PeerError):
                    peers.parse_code(make_code(**{field: value + "\n"}))

    def test_add_then_remove_leaves_authorized_keys_byte_identical(self):
        self.ak().parent.mkdir(mode=0o700)
        before = b"ssh-ed25519 AAAAother me@laptop\n# comment\n"
        self.ak().write_bytes(before)
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        self.assertEqual(self.ak().read_bytes().count(b"\n"), 3)
        peers.remove_peer(PEER_ID)
        self.assertEqual(self.ak().read_bytes(), before)

    def test_gate_and_sender_patterns_refuse_a_trailing_newline(self):
        from agentboard import peer_gate
        for command in ("serve\n", "mark claude s1 done\n", "mark claude s1 snooze 3\n"):
            with self.subTest(command=command):
                with self.assertRaises(peer_gate.Refused):
                    peer_gate.parse_command(command)
        self.assertIsNone(peer_gate._HOURS_RE.match("3\n"))
        self.assertIsNone(peers._REMOTE_CONTROL_URL_RE.match(
            "https://claude.ai/code/session_01KVjE5EzNMnQ54BKG8zNp3J\n"))

    def test_authorized_line_refuses_a_quote_or_control_character_in_the_path(self):
        for path in ("/a'b/agentboard", "/a\tb/agentboard", "/a\nb/agentboard", "/a\\b/x"):
            with self.subTest(path=path):
                with self.assertRaises(peers.PeerError):
                    peers.authorized_line(PEER_ID, fake_key(1), True, bin_path=path)
        line = peers.authorized_line(PEER_ID, fake_key(1), True, bin_path="/a b/agentboard")
        self.assertIn("command=\"'/a b/agentboard' peer-gate ", line)

    # (b) ssh ignores the user's ssh_config and forwards nothing
    def test_ssh_argv_reads_no_config_and_forwards_nothing(self):
        from agentboard import peer_cli
        peer = {"id": PEER_ID, "user": "someone", "host": "mini.local"}
        fake_ssh_files()
        argv = peer_cli.ssh_argv(peer, "serve")
        self.assertEqual(argv[argv.index("-F") + 1], "none")
        self.assertLess(argv.index("-F"), argv.index("--"))
        options = {argv[i + 1] for i, a in enumerate(argv) if a == "-o"}
        for option in ("ForwardAgent=no", "ForwardX11=no", "IdentitiesOnly=yes",
                       "IdentityAgent=none", "VerifyHostKeyDNS=no"):
            self.assertIn(option, options)

    # (c) received-line hardening
    def test_lone_surrogates_are_replaced(self):
        line = export_line(sessions=[{"tool": "claude", "session_id": "r1", "state": "running",
                                      "title": "a\ud800b", "notes": [{"text": "x\udfffy"}]}])
        self.assertIn(b"\\ud800", line)
        [session] = peers.validate_line(line)["sessions"]
        self.assertEqual(session["title"], "a?b")
        self.assertEqual(session["notes"][0]["text"], "x?y")
        json.dumps(session).encode("utf-8")  # no surrogate left to fail encoding

    def test_session_ids_starting_with_a_dash_or_ending_in_a_newline_are_rejected(self):
        for bad in ("-h", "--print", "s1\n"):
            with self.subTest(session_id=bad):
                with self.assertRaises(peers.LineRejected):
                    peers.validate_line(export_line(sessions=[
                        {"tool": "claude", "session_id": bad, "state": "done"}]))
        from agentboard import peer_gate
        with self.assertRaises(peer_gate.Refused):
            peer_gate.parse_command("mark claude -h done")

    def test_deeply_nested_line_is_rejected_not_raised(self):
        with self.assertRaises(peers.LineRejected) as caught:
            peers.validate_line(b"[" * 200000)
        self.assertEqual(str(caught.exception), "not json")

    def test_duplicate_sessions_in_one_line_keep_the_first(self):
        line = export_line(sessions=[
            {"tool": "claude", "session_id": "r1", "state": "running", "title": "first"},
            {"tool": "claude", "session_id": "r1", "state": "done", "title": "second"},
            {"tool": "codex", "session_id": "r1", "state": "done"},
        ])
        sessions = peers.validate_line(line)["sessions"]
        self.assertEqual([(s["tool"], s.get("title")) for s in sessions],
                         [("claude", "first"), ("codex", None)])

    def test_export_leaves_out_a_session_id_the_receiver_would_refuse(self):
        peers.ensure_machine(run=no_scutil)
        snapshot = {"sessions": [{"tool": "claude", "session_id": "-odd", "state": "done"},
                                 {"tool": "claude", "session_id": "ok1", "state": "done"}]}
        export = peers.build_export(None, "state", snapshot=snapshot)
        self.assertEqual([s["session_id"] for s in export["sessions"]], ["ok1"])
        peers.validate_line(peers.encode_export(export))

    # fingerprint covers every field
    def test_fingerprint_covers_every_field_and_is_96_bits(self):
        base = peers.parse_code(make_code())["fingerprint"]
        self.assertEqual(len(base.replace("-", "")) * 4, 96)
        for field, value in (("id", OTHER_ID), ("key", fake_key(7)), ("host_key", fake_key(8)),
                             ("host", "evil.local"), ("user", "other"), ("name", "Other")):
            with self.subTest(field=field):
                self.assertNotEqual(peers.parse_code(make_code(**{field: value}))["fingerprint"], base)

    def test_listed_fingerprint_is_the_codes_after_a_host_edit(self):
        added = peers.add_peer(make_code(), bin_path="/x/agentboard")
        peers.set_peer(PEER_ID, host="mini.vpn")
        self.assertEqual(peers.find_peer(peers.load_config(), PEER_ID)["fingerprint"],
                         added["fingerprint"])

    def test_a_lone_surrogate_in_a_timestamp_is_rejected(self):
        bad = "2026-01-01\ud80012:00:00"
        self.assertIsNone(peers.parse_ts(bad))
        for field in ("started_at", "updated_at", "ended_at", "turn_finished_at", "context_at"):
            with self.subTest(field=field):
                with self.assertRaises(peers.LineRejected):
                    peers.validate_line(export_line(sessions=[
                        {"tool": "claude", "session_id": "r1", "state": "done", field: bad}]))
        with self.assertRaises(peers.LineRejected):
            peers.validate_line(export_line(sessions=[
                {"tool": "claude", "session_id": "r1", "state": "snoozed",
                 "triage": {"state": "snoozed", "until": bad}}]))
        with self.assertRaises(peers.LineRejected):
            peers.validate_line(export_line(generated_at=bad))

    def test_add_reports_an_unwritable_authorized_keys_and_lists_nothing(self):
        peers.ensure_machine(run=no_scutil)
        self.ak().parent.mkdir(mode=0o700)
        self.ak().mkdir()  # a directory where the file should be
        with self.assertRaises(peers.PeerError) as caught:
            peers.add_peer(make_code(), bin_path="/x/agentboard")
        self.assertIn("authorized_keys", str(caught.exception))
        self.assertIsNone(peers.find_peer(peers.load_config(), PEER_ID))

    def test_config_rewrite_holds_a_lock_on_the_data_directory(self):
        import fcntl
        import threading
        fd = os.open(self.agentboard_home, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        done = threading.Event()
        worker = threading.Thread(target=lambda: (peers.set_share("titles"), done.set()))
        worker.start()
        try:
            self.assertFalse(done.wait(0.3))
        finally:
            os.close(fd)
        worker.join(timeout=5)
        self.assertTrue(done.is_set())
        self.assertEqual(peers.load_config()["peer_share"], "titles")

    def test_concurrent_config_changes_are_all_kept(self):
        import threading
        names = [f"k{i}" for i in range(20)]
        threads = [threading.Thread(target=peers.update_config,
                                    args=(lambda data, n=n: data.__setitem__(n, True),))
                   for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertTrue(all(peers.load_config().get(n) for n in names))

    # (e) a symlinked authorized_keys stays a symlink
    def test_symlinked_authorized_keys_is_written_through(self):
        self.ak().parent.mkdir(mode=0o700)
        target = self.tmp_path / "dotfiles" / "authorized_keys"
        target.parent.mkdir()
        target.write_bytes(b"keep\n")
        os.symlink(target, self.ak())
        peers.authorize(PEER_ID, fake_key(1), True, bin_path="/x/agentboard")
        self.assertTrue(self.ak().is_symlink())
        self.assertIn(b"agentboard-peer:" + PEER_ID.encode(), target.read_bytes())
        peers.deauthorize(PEER_ID)
        self.assertTrue(self.ak().is_symlink())
        self.assertEqual(target.read_bytes(), b"keep\n")

    def test_rewrite_holds_a_lock_on_the_ssh_directory(self):
        import fcntl
        import threading
        self.ak().parent.mkdir(mode=0o700)
        fd = os.open(self.ak().parent, os.O_RDONLY)
        fcntl.flock(fd, fcntl.LOCK_EX)
        done = threading.Event()
        worker = threading.Thread(target=lambda: (
            peers.authorize(PEER_ID, fake_key(1), True, bin_path="/x/agentboard"), done.set()))
        worker.start()
        try:
            self.assertFalse(done.wait(0.3))  # waits for our lock
        finally:
            os.close(fd)
        worker.join(timeout=5)
        self.assertTrue(done.is_set())

    # minor hardening
    def test_allow_open_is_on_only_when_true_or_missing(self):
        base = {"id": PEER_ID, "host": "h", "user": "u"}
        self.assertTrue(peers._clean_peer(base)["allow_open"])
        self.assertTrue(peers._clean_peer(dict(base, allow_open=True))["allow_open"])
        for value in ("off", 0, 1, "true", None):
            with self.subTest(value=value):
                self.assertFalse(peers._clean_peer(dict(base, allow_open=value))["allow_open"])

    def test_link_identity_changes_with_the_keys(self):
        from agentboard import peer_sync
        peer = {"id": PEER_ID, "host": "h", "user": "u", "allow_open": True,
                "public_key": fake_key(1), "host_key": fake_key(2)}
        self.assertNotEqual(peer_sync.Link(peer).identity(),
                            peer_sync.Link(dict(peer, public_key=fake_key(3))).identity())

    def test_receiver_handles_at_most_one_line_a_second_and_only_the_newest(self):
        from agentboard import peer_sync
        peers.ensure_machine(run=no_scutil)
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        sleeps, handled = [], []
        box = peer_sync._Latest()

        def line_sleep(secs):
            sleeps.append(secs)
            box.put(export_line(sessions=[], generated_at="2026-06-15T11:59:59.000004Z"))

        sync = peer_sync.PeerSync(spawner=lambda p, a: None, wall=lambda: NOW,
                                  ssh_argv=lambda p: ["ssh"], line_sleep=line_sleep)
        sync.reload(force=True)
        link = sync.links[PEER_ID]
        real_handle = sync.handle_line

        def handle(link_, raw, generation=None):
            handled.append(json.loads(raw)["generated_at"][-2:])
            if len(handled) == 1:  # a burst arrives while the first is handled
                for n in (2, 3):
                    box.put(export_line(generated_at=f"2026-06-15T11:59:59.00000{n}Z"))
            else:
                box.close()
            return real_handle(link_, raw, generation)

        sync.handle_line = handle
        box.put(export_line(generated_at="2026-06-15T11:59:59.000001Z"))
        sync._handle(link, box, link.generation)
        # 1 handled; 2 replaced by 3; 3 replaced by 4 during the one-second wait.
        self.assertEqual(handled, ["1Z", "4Z"])
        self.assertEqual(len(sleeps), 1)
        self.assertTrue(0.5 < sleeps[0] <= 1.0, sleeps)
        self.assertEqual(peers.read_cache(PEER_ID)["sessions"], [])

    def test_rejected_and_oversized_lines_are_paced_too(self):
        from agentboard import peer_sync
        peers.ensure_machine(run=no_scutil)
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        sleeps = []
        sync = peer_sync.PeerSync(spawner=lambda p, a: None, wall=lambda: NOW,
                                  ssh_argv=lambda p: ["ssh"], line_sleep=sleeps.append)
        sync.reload(force=True)
        link = sync.links[PEER_ID]
        box = peer_sync._Latest(keep_all=True)
        for item in (peer_sync.TOO_LARGE, peer_sync.TOO_LARGE, b"not json", peer_sync.TOO_LARGE):
            box.put(item)
        box.close()
        sync._handle(link, box, link.generation)
        self.assertEqual(len(sleeps), 3)

    def test_reader_keeps_only_the_newest_line_of_a_backlog(self):
        from agentboard import peer_sync
        box = peer_sync._Latest()
        oversized = b"x" * (peers.MAX_LINE_BYTES + 10)
        proc = FakeProc(b"\n".join([export_line()] * 50 + [oversized, b"last"]) + b"\n")
        sync = peer_sync.PeerSync(spawner=lambda p, a: None, wall=lambda: NOW, ssh_argv=lambda p: [])
        sync._read(peer_sync.Link({"id": PEER_ID}), proc, box)
        self.assertEqual(box.take(), b"last")
        self.assertIsNone(box.take())
        self.assertEqual(box.replaced, 51)

    def test_a_superseded_connection_writes_nothing(self):
        from agentboard import peer_sync
        peers.ensure_machine(run=no_scutil)
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        sync = peer_sync.PeerSync(spawner=lambda p, a: None, wall=lambda: NOW,
                                  ssh_argv=lambda p: ["ssh"], min_line_secs=0)
        sync.reload(force=True)
        link = sync.links[PEER_ID]
        old = link.generation
        link.generation += 1  # what _reap and _start do
        sync.handle_line(link, export_line(), old)
        self.assertIsNone(peers.read_cache(PEER_ID))
        box = peer_sync._Latest(keep_all=True)
        box.put(export_line())
        box.close()
        sync._handle(link, box, old)
        self.assertIsNone(peers.read_cache(PEER_ID))
        sync.handle_line(link, export_line(), link.generation)
        self.assertEqual(peers.read_cache(PEER_ID)["status"], "online")

    def test_serve_wait_does_not_spin_on_a_flooded_stdin(self):
        from agentboard import peer_gate
        r, w = os.pipe()
        self.addCleanup(os.close, r)
        self.addCleanup(os.close, w)
        os.write(w, b"x" * 10000)
        slept = []
        with open(r, "rb", closefd=False) as stdin:
            self.assertFalse(peer_gate._stdin_closed(stdin, 1.0, sleep=slept.append))
        self.assertEqual(len(slept), 1)
        self.assertGreater(slept[0], 0.5)

    # (d) an open attach or resume ends when the peer goes
    def _gate_setup(self):
        peers.ensure_machine(run=no_scutil)
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        from agentboard import peer_gate
        return peer_gate

    def test_open_session_ends_when_the_peer_is_removed(self):
        gate = self._gate_setup()
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        checks = []

        def allowed():
            checks.append(1)
            if len(checks) == 2:
                peers.remove_peer(PEER_ID)
            return gate.open_allowed(PEER_ID)

        rc = gate.supervise(PEER_ID, ["unused"], allowed=allowed, spawn=lambda argv: child,
                            poll_secs=0.05)
        self.assertEqual(rc, gate.EXIT_REFUSED)
        self.assertIsNotNone(child.poll())
        self.assertEqual(len(checks), 2)

    def test_open_session_ends_when_opening_is_turned_off(self):
        gate = self._gate_setup()
        self.assertTrue(gate.open_allowed(PEER_ID))
        # The real path without a terminal: own session, signals to all of it.
        script = "import subprocess, time; subprocess.Popen(['sleep', '60']); time.sleep(60)"
        flipped = []

        def allowed():
            flipped.append(1)
            if len(flipped) == 2:  # the first call is the check before starting
                peers.set_peer(PEER_ID, allow_open=False, bin_path="/x/agentboard")
            return gate.open_allowed(PEER_ID)

        started = []
        real_popen = subprocess.Popen

        def spawn_recording(argv, **kwargs):
            if argv[0] == "/bin/ps":  # the session walk
                return real_popen(argv, **kwargs)
            self.assertEqual(kwargs, {"start_new_session": True})
            proc = real_popen(argv, **kwargs)
            started.append(proc)
            return proc

        with mock.patch.object(gate.subprocess, "Popen", side_effect=spawn_recording):
            rc = gate.supervise(PEER_ID, [sys.executable, "-c", script], allowed=allowed,
                                poll_secs=0.2)
        self.assertEqual(rc, gate.EXIT_REFUSED)
        [proc] = started
        self.assertIsNotNone(proc.poll())
        self.assertEqual(gate._session_members(proc.pid), set())  # the grandchild went too

    def test_ending_a_session_kills_group_members_that_ignore_sighup(self):
        gate = self._gate_setup()
        # The child starts a member that ignores SIGHUP and SIGTERM (nohup and
        # worse), then the child itself goes on SIGHUP.
        script = ("import signal, subprocess, sys, time\n"
                  "subprocess.Popen([sys.executable, '-c', 'import signal, time; "
                  "signal.signal(signal.SIGHUP, signal.SIG_IGN); "
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'])\n"
                  "time.sleep(0.3)\nprint('ready', flush=True)\ntime.sleep(60)\n")
        child = subprocess.Popen([sys.executable, "-c", script], start_new_session=True,
                                 stdout=subprocess.PIPE)
        self.addCleanup(lambda: (child.stdout.close(), _killpg_quietly(child.pid)))
        self.assertEqual(child.stdout.readline().strip(), b"ready")
        self.assertTrue(gate._end_child(child, True, wait_secs=0.5))
        self.assertIsNotNone(child.poll())
        self.assertEqual(gate._session_members(child.pid), set())

    def test_open_is_checked_before_the_child_starts(self):
        gate = self._gate_setup()
        spawned = []
        rc = gate.supervise(PEER_ID, ["unused"], allowed=lambda: False,
                            spawn=lambda argv: spawned.append(argv))
        self.assertEqual(rc, gate.EXIT_REFUSED)
        self.assertEqual(spawned, [])

    def test_pairing_again_with_new_keys_ends_what_the_old_key_opened(self):
        gate = self._gate_setup()
        old_key = peers.find_peer(peers.load_config(), PEER_ID)["public_key"]
        self.assertTrue(gate.open_allowed(PEER_ID, public_key=old_key))
        peers.add_peer(make_code(key=fake_key(9)), bin_path="/x/agentboard")
        self.assertTrue(gate.open_allowed(PEER_ID))
        self.assertFalse(gate.open_allowed(PEER_ID, public_key=old_key))
        # The default launch run() picks passes the key the session started with.
        seen = {}
        with mock.patch.object(gate, "supervise",
                               side_effect=lambda pid, argv, **kw: seen.update(kw) or 0):
            gate._default_launch(PEER_ID, ["x"], public_key=old_key)
        self.assertFalse(seen["allowed"]())

    def test_serve_ends_when_the_peer_is_paired_again_with_new_keys(self):
        import io
        from agentboard import peer_gate
        self._gate_setup()
        insert_session(self.conn, session_id="s1")
        peer = peers.find_peer(peers.load_config(), PEER_ID)
        steps = []

        def wait():
            steps.append(1)
            if len(steps) == 1:
                peers.add_peer(make_code(key=fake_key(9)), bin_path="/x/agentboard")
            return len(steps) > 5

        out = io.StringIO()
        self.assertEqual(peer_gate.serve(peer, out=out, wait=wait), 0)
        self.assertEqual(len(out.getvalue().splitlines()), 1)
        self.assertEqual(len(steps), 1)

    def test_open_session_returns_the_childs_exit_code(self):
        gate = self._gate_setup()
        rc = gate.supervise(PEER_ID, [sys.executable, "-c", "raise SystemExit(7)"],
                            allowed=lambda: True, poll_secs=0.05)
        self.assertEqual(rc, 7)

    def test_attach_runs_tmux_under_the_supervisor(self):
        gate = self._gate_setup()
        insert_session(self.conn, session_id="t1", tmux_socket="/tmp/sock", tmux_pane="%1")
        launched = []
        from agentboard import cli
        with mock.patch.object(cli, "_tmux_binary", return_value="/usr/bin/tmux"), \
                mock.patch.object(cli, "_run_tmux", return_value=object()), \
                mock.patch.object(cli, "_parse_tmux_display", return_value=("$1", "main", "@1")):
            rc = gate.run(PEER_ID, command="attach claude t1",
                          launch=lambda peer_id, argv: launched.append((peer_id, argv)) or 0,
                          entry_for=lambda t, s: {"alive": True, "tmux": True})
        self.assertEqual(rc, 0)
        # -u: no LANG/LC_* arrives over ssh -F none, so tmux is told UTF-8.
        self.assertEqual(launched, [(PEER_ID, ["/usr/bin/tmux", "-u", "-S", "/tmp/sock",
                                               "attach-session", "-t", "$1"])])


class SshPathTests(AgentBoardTestCase):
    """ssh_argv with the data directory's space in it, as on a default install
    (~/Library/Application Support/AgentBoard), checked by ssh itself."""

    PEER = {"id": PEER_ID, "user": "someone", "host": "mini.local"}

    def setUp(self):
        super().setUp()
        self.assertIn(" ", str(self.agentboard_home))
        from agentboard import peer_cli
        self.peer_cli = peer_cli

    def ssh_g(self, argv):
        """ssh -G: the effective options for argv, no connection made."""
        if not os.access("/usr/bin/ssh", os.X_OK):
            self.skipTest("no /usr/bin/ssh")
        argv = list(argv)
        argv.insert(1, "-G")
        result = subprocess.run(argv[:-1], capture_output=True, text=True, timeout=10,
                                env={"HOME": str(self.fake_home), "PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)
        options = {}
        for line in result.stdout.splitlines():
            name, _, value = line.partition(" ")
            options.setdefault(name, []).append(value)
        return options

    def test_paths_reach_ssh_whole_and_only_agentboards_key_is_offered(self):
        fake_ssh_files()
        # Personal keys ssh would fall back to if AgentBoard's went missing.
        (self.fake_home / ".ssh").mkdir()
        (self.fake_home / ".ssh" / "id_ed25519").write_text("personal\n")
        argv = self.peer_cli.ssh_argv(self.PEER, "serve")
        options = self.ssh_g(argv)
        self.assertEqual(options["userknownhostsfile"], [str(peers.known_hosts_path())])
        self.assertEqual(options["identityfile"], [str(peers.key_path())])
        # The key going away after the argv was built still offers no other.
        peers.key_path().unlink()
        self.assertEqual(self.ssh_g(argv)["identityfile"], [str(peers.key_path())])

    def test_a_percent_is_doubled_and_a_quote_refused(self):
        self.assertEqual(self.peer_cli._ssh_path_option("UserKnownHostsFile", "/a b/%h"),
                         'UserKnownHostsFile="/a b/%%h"')
        for bad in ('/a"b', "/a\nb"):
            with self.subTest(path=bad):
                with self.assertRaises(self.peer_cli.PeerUnavailable):
                    self.peer_cli._ssh_path_option("IdentityFile", bad)

    def test_a_missing_key_or_known_hosts_refuses_to_run_ssh(self):
        fake_ssh_files()
        for path in (peers.key_path(), peers.known_hosts_path()):
            with self.subTest(missing=path.name):
                path.unlink()
                with self.assertRaises(self.peer_cli.PeerUnavailable):
                    self.peer_cli.ssh_argv(self.PEER, "serve")
                fake_ssh_files()

    def test_sync_and_mark_report_a_missing_key_without_running_ssh(self):
        import argparse
        from agentboard import cli, peer_sync
        peers.ensure_machine(run=no_scutil)
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        spawned = []
        sync = peer_sync.PeerSync(spawner=lambda p, a: spawned.append(a), wall=lambda: NOW)
        sync.tick()
        self.assertEqual(spawned, [])
        self.assertIn("ssh key is missing", peers.read_cache(PEER_ID)["error"])
        now = datetime.now(timezone.utc)
        peers.write_cache(PEER_ID, {"peer_id": PEER_ID, "status": "online",
                                    "received_at": peers.now_iso(now), "last_seen_at": peers.now_iso(now),
                                    "sessions": [{"tool": "claude", "session_id": "r1",
                                                  "state": "running", "remote_open": "tmux"}]})
        ran = []
        args = argparse.Namespace(tool="claude", session_id="r1", machine=PEER_ID,
                                  action="done", hours=None, note=None, print=False)
        self.assertEqual(cli.cmd_mark(args, run=lambda argv, **kw: ran.append(argv)),
                         peers.EXIT_PEER_UNAVAILABLE)
        self.assertEqual(cli.cmd_focus(args, run_osascript=lambda argv, **kw: ran.append(argv)),
                         peers.EXIT_PEER_UNAVAILABLE)
        self.assertEqual(ran, [])

    def test_a_real_loopback_sshd_accepts_the_pinned_key_under_a_spaced_path(self):
        """A throwaway sshd on a free loopback port, with its own host key and
        an AuthorizedKeysFile inside the temp HOME; skipped when sshd can't
        run here. The forced command is a stub gate in a folder with a space."""
        import socket
        import time
        sshd = "/usr/sbin/sshd"
        if not (os.access(sshd, os.X_OK) and os.access("/usr/bin/ssh", os.X_OK)):
            self.skipTest("no sshd/ssh")
        work = self.tmp_path / "sshd work"
        work.mkdir()
        host_key = work / "host key"
        subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)],
                       check=True, timeout=20)
        stub = work / "gate bin" / "agentboard"
        stub.parent.mkdir()
        stub.write_text('#!/bin/sh\necho "gate-ok $1 $2 $SSH_ORIGINAL_COMMAND home=$HOME"\n')
        stub.chmod(0o755)
        peers.ensure_machine(run=no_scutil)
        host_pub = " ".join(host_key.with_suffix(".pub").read_text().split()[:2])
        peers.add_peer(make_code(host_key=host_pub, host="127.0.0.1", user=_login()),
                       bin_path=str(stub))
        # This Mac's key stands in for the peer's: authorize it under the peer's id.
        peers.authorize(PEER_ID, peers.ensure_keypair(), False, bin_path=str(stub))
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = work / "sshd_config"
        config.write_text(
            f"Port {port}\nListenAddress 127.0.0.1\nHostKey \"{host_key}\"\n"
            f"AuthorizedKeysFile \"{peers.authorized_keys_path()}\"\nPidFile \"{work / 'pid'}\"\n"
            "UsePAM no\nStrictModes no\nPasswordAuthentication no\n"
            "KbdInteractiveAuthentication no\nPermitUserEnvironment no\n"
            # The session gets the temp HOME, so the login shell reads the
            # fake home's (absent) startup files, never the developer's.
            f"SetEnv \"HOME={self.fake_home}\" \"ZDOTDIR={self.fake_home}\"\n")
        server = subprocess.Popen([sshd, "-f", str(config), "-D", "-e"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (server.poll() is None and server.kill(), server.wait()))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if server.poll() is not None:
                self.skipTest("sshd would not start here")
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
                break
            except OSError:
                time.sleep(0.05)
        else:
            self.skipTest("sshd did not listen")
        peer = peers.find_peer(peers.load_config(), PEER_ID)
        argv = self.peer_cli.ssh_argv(peer, "serve")
        argv[1:1] = ["-p", str(port)]
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20,
                                env={"HOME": str(self.fake_home), "PATH": "/usr/bin:/bin"})
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"gate-ok peer-gate {PEER_ID} serve home={self.fake_home}\n")


class ThirdPassTests(AgentBoardTestCase):
    """Follow-ups from the third security review of the peer-sync PR."""

    NEW_ID = "77777777-8888-4999-aaaa-bbbbbbbbbbbb"

    def ak(self) -> Path:
        return self.fake_home / ".ssh" / "authorized_keys"

    def marked(self):
        return [l for l in self.ak().read_bytes().splitlines() if b"agentboard-peer:" in l]

    def test_same_key_under_a_new_id_replaces_the_old_entry(self):
        peers.ensure_machine(run=no_scutil)
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        peers.write_cache(PEER_ID, {"peer_id": PEER_ID, "status": "online", "sessions": []})
        # The same Mac (same key) comes back with a new id.
        peers.add_peer(make_code(id=self.NEW_ID), bin_path="/x/agentboard")
        config = peers.load_config()
        self.assertEqual([p["id"] for p in peers.configured_peers(config)], [self.NEW_ID])
        [line] = self.marked()
        self.assertTrue(line.endswith(("agentboard-peer:" + self.NEW_ID).encode()))
        self.assertFalse(peers.cache_path(PEER_ID).exists())
        # Turning opening off on the new entry now takes effect: no line
        # with that key keeps a pty.
        peers.set_peer(self.NEW_ID, allow_open=False, bin_path="/x/agentboard")
        [line] = self.marked()
        self.assertTrue(line.startswith(b"restrict,command="))
        self.assertEqual(peers.authorized_ids(), {self.NEW_ID})

    def test_authorize_drops_another_ids_line_with_the_same_key(self):
        peers.authorize(PEER_ID, fake_key(1), True, bin_path="/x/agentboard")
        peers.authorize(OTHER_ID, fake_key(3), True, bin_path="/x/agentboard")
        peers.authorize(self.NEW_ID, fake_key(1), False, bin_path="/x/agentboard")
        self.assertEqual(peers.authorized_ids(), {OTHER_ID, self.NEW_ID})
        self.assertEqual(sum(fake_key(1).encode() in l for l in self.marked()), 1)

    def test_a_failed_config_write_takes_the_new_line_back_out(self):
        peers.ensure_machine(run=no_scutil)
        self.ak().parent.mkdir(mode=0o700)
        self.ak().write_bytes(b"ssh-ed25519 AAAAother me@laptop\n")
        before = self.ak().read_bytes()
        peers.config_file().write_text("{not json")
        with self.assertRaises(peers.MalformedConfigError):
            peers.add_peer(make_code(), bin_path="/x/agentboard")
        self.assertEqual(self.ak().read_bytes(), before)
        peers.config_file().unlink()
        with mock.patch.object(peers, "update_config", side_effect=PermissionError(13, "denied")):
            with self.assertRaises(peers.PeerError):
                peers.add_peer(make_code(), bin_path="/x/agentboard")
        self.assertEqual(self.ak().read_bytes(), before)

    def test_a_failed_re_pair_puts_the_previous_line_back(self):
        peers.ensure_machine(run=no_scutil)
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        peers.set_peer(PEER_ID, allow_open=False, bin_path="/x/agentboard")
        before = self.ak().read_bytes()
        with mock.patch.object(peers, "update_config", side_effect=OSError(30, "read-only")):
            with self.assertRaises(peers.PeerError):
                peers.add_peer(make_code(), bin_path="/x/agentboard")
        self.assertEqual(sorted(self.ak().read_bytes().splitlines()), sorted(before.splitlines()))
        self.assertTrue(self.marked()[0].startswith(b"restrict,command="))

    def test_remove_all_does_not_create_ssh_dir(self):
        ssh_dir = self.fake_home / ".ssh"
        self.assertFalse(ssh_dir.exists())
        self.assertEqual(peers.remove_peer(None), 0)
        self.assertEqual(peers.deauthorize(None, dry_run=True), 0)
        self.assertFalse(ssh_dir.exists())

    def test_a_never_reached_mac_reads_never_not_expired(self):
        peers.ensure_machine(run=no_scutil)
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        # What peer-sync writes on the first failed connection.
        peers.write_cache(PEER_ID, {"peer_id": PEER_ID, "status": "offline",
                                    "error": "this Mac is not authorized there"})
        _entries, [peer] = peers.remote_entries(peers.load_config(), datetime.now(timezone.utc))
        self.assertEqual(peer["status"], "never")
        self.assertEqual(peer["error"], "this Mac is not authorized there")

    def test_ssh_path_spellings_ssh_rewrites_are_refused(self):
        from agentboard import peer_cli
        for bad in ("/a\\\\b", "/a\\b", "/x/${HOME}/y"):
            with self.subTest(path=bad):
                with self.assertRaises(peer_cli.PeerUnavailable):
                    peer_cli._ssh_path_option("UserKnownHostsFile", bad)
        self.assertEqual(peer_cli._ssh_path_option("IdentityFile", "/a b/$HOME/~"),
                         'IdentityFile="/a b/$HOME/~"')


class _OuterTerminal:
    """A pty standing in for the ssh session's terminal; a thread drains
    what the gate writes to it."""

    def __init__(self, test):
        import pty
        import threading
        self.master, self.slave = pty.openpty()
        self.output = bytearray()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()
        test.addCleanup(self.close)

    def _drain(self):
        import select
        while not self._stop.is_set():
            ready, _, _ = select.select([self.master], [], [], 0.05)
            if ready:
                try:
                    data = os.read(self.master, 4096)
                except OSError:
                    return
                if not data:
                    return
                self.output += data

    def type(self, data):
        os.write(self.master, data)

    def close(self):
        self._stop.set()
        self._thread.join(2)
        for fd in (self.master, self.slave):
            try:
                os.close(fd)
            except OSError:
                pass


def _ignoring_grandchild(pid_file, how):
    """Python source for a child that starts a grandchild ignoring SIGHUP and
    SIGTERM, outside the child's process group (`how`: process_group=0, as
    job control does, or start_new_session=True, as setsid does), writes
    the grandchild's pid to `pid_file`, then sleeps."""
    grandchild = ("import signal, time; signal.signal(signal.SIGHUP, signal.SIG_IGN); "
                  "signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)")
    return (f"import subprocess, sys, time\n"
            f"p = subprocess.Popen([sys.executable, '-c', {grandchild!r}], {how})\n"
            f"open({str(pid_file)!r}, 'w').write(str(p.pid))\n"
            f"time.sleep(60)\n")


class FourthPassTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        peers.ensure_machine(run=no_scutil)
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        from agentboard import peer_gate
        self.gate = peer_gate
        # forkpty warns in a threaded process; the gate has no threads, only
        # these tests do (the terminal's drain thread).
        import warnings
        self.enterContext(warnings.catch_warnings())
        warnings.filterwarnings("ignore", "This process .* is multi-threaded",
                                DeprecationWarning)
        self.pid_file = Path(self.agentboard_home) / "grandchild pid"

    def _grandchild(self):
        pid = int(self.pid_file.read_text())
        self.addCleanup(lambda: _kill_quietly(pid))
        return pid

    def _gone(self, pid, secs=5.0):
        import time
        deadline = time.monotonic() + secs
        while time.monotonic() < deadline:
            table = self.gate._process_table()
            if table is not None and pid not in table:
                return True
            time.sleep(0.05)
        return False

    def _flip_when_started(self, change):
        """allowed(): True until the grandchild's pid is written, then
        `change()` once and the real check."""
        state = {"changed": False}

        def allowed():
            if not state["changed"] and self.pid_file.exists() and self.pid_file.read_text():
                state["changed"] = True
                change()
            return self.gate.open_allowed(PEER_ID)
        return allowed

    def test_a_job_control_group_on_the_pty_ends_when_opening_goes_off(self):
        term = _OuterTerminal(self)
        script = _ignoring_grandchild(self.pid_file, "process_group=0")
        allowed = self._flip_when_started(
            lambda: peers.set_peer(PEER_ID, allow_open=False, bin_path="/x/agentboard"))
        rc = self.gate.supervise(PEER_ID, [sys.executable, "-c", script], allowed=allowed,
                                 poll_secs=0.1, tty_fd=term.slave, out_fd=term.slave)
        self.assertEqual(rc, self.gate.EXIT_REFUSED)
        self.assertTrue(self._gone(self._grandchild()))

    def test_bash_job_control_resume_ends_when_the_peer_is_removed(self):
        import shlex
        term = _OuterTerminal(self)
        # bash -i gives the shell job control: the command, not exec'd (a
        # second command follows), runs in a process group of its own.
        script = _ignoring_grandchild(self.pid_file, "process_group=0")
        command = f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}; echo after"
        allowed = self._flip_when_started(lambda: peers.remove_peer(PEER_ID))
        env = {"PATH": "/usr/bin:/bin", "HOME": os.environ["HOME"], "TERM": "dumb",
               "AGENTBOARD_HOME": os.environ["AGENTBOARD_HOME"]}
        with mock.patch.dict(os.environ, env, clear=True):
            rc = self.gate.supervise(
                PEER_ID, ["/bin/bash", "--noprofile", "--norc", "-i", "-c", command],
                allowed=allowed, poll_secs=0.1, tty_fd=term.slave, out_fd=term.slave)
        self.assertEqual(rc, self.gate.EXIT_REFUSED)
        self.assertTrue(self._gone(self._grandchild()))

    def test_a_setsid_descendant_without_a_terminal_ends_too(self):
        script = _ignoring_grandchild(self.pid_file, "start_new_session=True")
        allowed = self._flip_when_started(
            lambda: peers.set_peer(PEER_ID, allow_open=False, bin_path="/x/agentboard"))
        rc = self.gate.supervise(PEER_ID, [sys.executable, "-c", script], allowed=allowed,
                                 poll_secs=0.1)
        self.assertEqual(rc, self.gate.EXIT_REFUSED)
        self.assertTrue(self._gone(self._grandchild()))

    def test_what_a_child_leaves_behind_ends_when_it_exits(self):
        term = _OuterTerminal(self)
        script = _ignoring_grandchild(self.pid_file, "process_group=0").replace(
            "time.sleep(60)\n", "raise SystemExit(3)\n")
        rc = self.gate.supervise(PEER_ID, [sys.executable, "-c", script],
                                 allowed=lambda: True, poll_secs=0.1,
                                 tty_fd=term.slave, out_fd=term.slave)
        self.assertEqual(rc, 3)
        self.assertTrue(self._gone(self._grandchild()))

    def test_a_suspended_child_is_continued_not_left_frozen(self):
        import time
        term = _OuterTerminal(self)
        script = ("import os, signal\nos.kill(os.getpid(), signal.SIGTSTP)\n"
                  "print('resumed', flush=True)\nraise SystemExit(5)\n")
        started = time.monotonic()
        rc = self.gate.supervise(PEER_ID, [sys.executable, "-c", script],
                                 allowed=lambda: True, poll_secs=0.1,
                                 tty_fd=term.slave, out_fd=term.slave)
        self.assertEqual(rc, 5)
        self.assertLess(time.monotonic() - started, 5)
        time.sleep(0.2)
        self.assertIn(b"resumed", bytes(term.output))

    def test_the_relay_carries_keys_in_and_output_out(self):
        import time
        term = _OuterTerminal(self)
        script = "import sys\nline = sys.stdin.readline()\nprint('got:' + line.strip().upper())\n"
        term.type(b"hello\r")
        rc = self.gate.supervise(PEER_ID, [sys.executable, "-c", script],
                                 allowed=lambda: True, poll_secs=0.1,
                                 tty_fd=term.slave, out_fd=term.slave)
        self.assertEqual(rc, 0)
        time.sleep(0.2)
        self.assertIn(b"got:HELLO", bytes(term.output))


class PairingNitTests(AgentBoardTestCase):
    """Fourth pass: a copied identity, what a re-pair keeps, a removed Mac's cache."""

    def setUp(self):
        super().setUp()
        self.local = peers.ensure_machine(run=no_scutil)
        self.host_key_file = Path(self.agentboard_home) / "host key.pub"

    def _with_host_key(self, key):
        self.host_key_file.write_text(key + " root@host\n")
        return self.host_key_file

    def _dry_run(self, code):
        import contextlib
        import io
        import argparse
        from agentboard import peer_cli
        out = io.StringIO()
        args = argparse.Namespace(peers_command="add", code=code, dry_run=True,
                                  host=None, name=None, allow_open=None)
        with contextlib.redirect_stdout(out):
            self.assertEqual(peer_cli.cmd_peers(args), 0)
        return json.loads(out.getvalue())

    def test_a_code_with_this_macs_id_and_another_host_key_is_a_copied_identity(self):
        code = make_code(id=self.local["id"], host_key=fake_key(2))
        with self.assertRaises(peers.PeerError) as caught:
            peers.add_peer(code, bin_path="/x/agentboard",
                           host_key_path=self._with_host_key(fake_key(3)))
        message = str(caught.exception)
        self.assertIn("copied from this one", message)
        self.assertIn("Migration Assistant", message)
        self.assertIn("machine.json", message)
        self.assertEqual(peers.authorized_ids(), set())
        # This Mac's own code (same host key) keeps its own message.
        with self.assertRaises(peers.PeerError) as caught:
            peers.add_peer(code, bin_path="/x/agentboard",
                           host_key_path=self._with_host_key(fake_key(2)))
        self.assertIn("own pairing code", str(caught.exception))

    def test_the_dry_run_names_a_copied_identity(self):
        code = make_code(id=self.local["id"], host_key=fake_key(2))
        with mock.patch.object(peers, "read_host_key", return_value=fake_key(3)):
            preview = self._dry_run(code)
        self.assertEqual((preview["own"], preview["copied_identity"]), (False, True))
        self.assertEqual(preview["error"], peers.COPIED_IDENTITY_ERROR)
        with mock.patch.object(peers, "read_host_key", return_value=fake_key(2)):
            preview = self._dry_run(code)
        self.assertEqual((preview["own"], preview["copied_identity"]), (True, False))

    def test_pairing_again_keeps_allow_open_and_host(self):
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        peers.set_peer(PEER_ID, allow_open=False, host="mini.vpn.example", bin_path="/x/agentboard")
        preview = self._dry_run(make_code())
        self.assertEqual(preview["replaces"], {"id": PEER_ID, "name": "Mini",
                                               "host": "mini.vpn.example", "allow_open": False})
        peer = peers.add_peer(make_code(), bin_path="/x/agentboard")
        self.assertEqual((peer["allow_open"], peer["host"]), (False, "mini.vpn.example"))
        line = [l for l in peers.authorized_keys_path().read_text().splitlines() if PEER_ID in l]
        self.assertEqual(len(line), 1)
        self.assertNotIn("pty", line[0])
        # An explicit switch still wins.
        self.assertTrue(peers.add_peer(make_code(), allow_open=True, bin_path="/x/agentboard")["allow_open"])

    def test_a_key_paired_under_another_id_is_named_and_keeps_its_switch(self):
        peers.add_peer(make_code(id=OTHER_ID, name="Old Mini"), bin_path="/x/agentboard")
        peers.set_peer(OTHER_ID, allow_open=False, bin_path="/x/agentboard")
        preview = self._dry_run(make_code())
        self.assertTrue(preview["known"])
        self.assertEqual((preview["replaces"]["id"], preview["replaces"]["name"],
                          preview["replaces"]["allow_open"]), (OTHER_ID, "Old Mini", False))
        peer = peers.add_peer(make_code(), bin_path="/x/agentboard")
        self.assertFalse(peer["allow_open"])
        self.assertEqual([p["id"] for p in peers.configured_peers(peers.load_config())], [PEER_ID])

    def test_a_new_mac_starts_with_opening_allowed(self):
        preview = self._dry_run(make_code())
        self.assertEqual((preview["known"], preview["replaces"], preview["error"]), (False, None, None))
        self.assertTrue(peers.add_peer(make_code(), bin_path="/x/agentboard")["allow_open"])

    def _sync(self):
        from agentboard import peer_sync
        sync = peer_sync.PeerSync(spawner=lambda p, a: None, wall=lambda: NOW,
                                  ssh_argv=lambda p: ["ssh"], min_line_secs=0)
        sync.reload(force=True)
        return sync

    def test_a_removed_macs_cache_is_not_written_again(self):
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        sync = self._sync()
        link = sync.links[PEER_ID]
        peers.remove_peer(PEER_ID)
        # A line handled before the sync's next reload writes nothing.
        sync.handle_line(link, export_line())
        self.assertIsNone(peers.read_cache(PEER_ID))
        self.assertFalse(peers.cache_path(PEER_ID).exists())

    def test_reload_deletes_a_removed_macs_cache(self):
        peers.add_peer(make_code(), bin_path="/x/agentboard")
        sync = self._sync()
        sync.handle_line(sync.links[PEER_ID], export_line())
        self.assertTrue(peers.cache_path(PEER_ID).exists())
        peers.remove_peer(PEER_ID)
        peers.write_cache(PEER_ID, {"peer_id": PEER_ID, "sessions": []})  # a late write
        sync.reload(force=True)
        self.assertFalse(peers.cache_path(PEER_ID).exists())
        self.assertNotIn(PEER_ID, sync.links)

    def test_a_replaced_ids_cache_is_not_written_again(self):
        peers.add_peer(make_code(id=OTHER_ID), bin_path="/x/agentboard")
        sync = self._sync()
        link = sync.links[OTHER_ID]
        peers.add_peer(make_code(), bin_path="/x/agentboard")  # same key, new id
        sync.handle_line(link, export_line(machine_id=OTHER_ID))
        self.assertFalse(peers.cache_path(OTHER_ID).exists())


def _killpg_quietly(pgid):
    import signal
    try:
        os.killpg(pgid, signal.SIGKILL)
    except OSError:
        pass


def _kill_quietly(pid):
    import signal
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def _login():
    import pwd
    return pwd.getpwuid(os.getuid()).pw_name


if __name__ == "__main__":
    unittest.main()

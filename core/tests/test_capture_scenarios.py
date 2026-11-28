"""scripts/capture_scenarios.py: the popover screenshot fixture generator.

The app decodes snapshots leniently, so a fixture missing a key would only
show as a blank row in a screenshot; these checks catch it without the app.
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "capture_scenarios.py"
SAMPLE = REPO / "app" / "Fixtures" / "snapshot.json"


def load_module():
    spec = importlib.util.spec_from_file_location("capture_scenarios", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CaptureScenariosTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = load_module()
        cls.sample = json.loads(SAMPLE.read_text())
        cls.tmp = tempfile.TemporaryDirectory()
        cls.out = Path(cls.tmp.name) / "fixtures"
        rc = cls.mod.main([str(SAMPLE), str(cls.out)])
        assert rc == 0

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def fixtures(self):
        return {name: snap for name, snap, *_ in self.mod.scenarios(self.sample)}

    def test_every_fixture_parses_and_sessions_keep_the_sample_keys(self):
        # The keys every sample session has (the sample's own rows differ
        # in the optional ones).
        sample_keys = set.intersection(*(set(s) for s in self.sample["sessions"]))
        self.assertIn("state", sample_keys)
        names = self.fixtures()
        self.assertIn("banner", names)
        for name in names:
            text = (self.out / (name + ".json")).read_text()
            if name == "banner":
                with self.assertRaises(json.JSONDecodeError):
                    json.loads(text)
                continue
            snap = json.loads(text)
            for key in ("generated_at", "counts", "sessions"):
                self.assertIn(key, snap, name)
            for session in snap["sessions"]:
                missing = sample_keys - set(session)
                self.assertFalse(missing, f"{name}: {session['key']} lacks {sorted(missing)}")

    def test_only_fixture_projects_titles_and_branches_appear(self):
        projects = set(self.mod.PROJECTS) | {s["project"] for s in self.sample["sessions"]}
        titles = set(self.mod.TITLES) | {s["title"] for s in self.sample["sessions"]}
        branches = set(self.mod.BRANCHES) | {s.get("branch") for s in self.sample["sessions"]}
        # The long-text scenario's own values, built from the module's LONG.
        long_project = "metrics-pipeline-with-a-long-project-name"
        long_branch = "fix/very-long-branch-name-for-the-rollup-overcount-regression-test"
        for name, snap in self.fixtures().items():
            if isinstance(snap, str):
                continue
            for session in snap["sessions"]:
                self.assertIn(session["project"], projects | {long_project}, name)
                self.assertIn(session["title"], titles | {self.mod.LONG}, name)
                self.assertIn(session.get("branch"), branches | {long_branch}, name)
                self.assertTrue(session["cwd"].startswith("/Users/dev/projects/")
                                or session["cwd"] in {s["cwd"] for s in self.sample["sessions"]}, name)

    def test_every_scenario_line_has_five_tab_separated_fields(self):
        lines = (self.out / "scenarios.tsv").read_text().splitlines()
        self.assertEqual(len(lines), 2 * len(self.fixtures()))
        for line in lines:
            fields = line.split("\t")
            self.assertEqual(len(fields), 5, line)
            name, path, settings, backdrop, _query = fields
            self.assertTrue(name.endswith(("-light", "-dark")), line)
            self.assertTrue(Path(path).is_file(), line)
            self.assertIn("appearance=string:", settings)
            # Never empty: the shell's tab-splitting read collapses empty
            # middle fields.
            self.assertIn(backdrop, ("none", "white"), line)

    def test_only_the_bright_backdrop_scenario_has_a_white_backdrop(self):
        rows = [line.split("\t") for line in (self.out / "scenarios.tsv").read_text().splitlines()]
        white = sorted(row[0] for row in rows if row[3] == "white")
        self.assertEqual(white, ["bright-backdrop-dark", "bright-backdrop-light"])
        snap = self.fixtures()["bright-backdrop"]
        self.assertTrue(any(s["stale"] for s in snap["sessions"]))
        self.assertTrue(snap["usage"])
        self.assertTrue(any(len(s["cwd"]) > 60 for s in snap["sessions"]))

    def test_the_polish_scenarios_show_what_they_are_named_for(self):
        fixtures = self.fixtures()
        mixed = fixtures["updates-always-mixed"]["sessions"]
        kinds = [n["kind"] for s in mixed for n in s["notes"]]
        self.assertEqual(sorted(kinds), ["error", "needs_you", "update"])
        self.assertTrue(any(s["state"] == "needs_you" and not s["notes"] for s in mixed))
        settings = {n: d for n, _snap, d, *_ in self.mod.scenarios(self.sample)}
        self.assertIn("updatesPanelMode=string:alwaysShown", settings["updates-always-mixed"])
        self.assertIn("updatesPanelMode=string:alwaysShown", settings["two-notes-one-project"])
        two = fixtures["two-notes-one-project"]["sessions"]
        self.assertEqual(len({s["project"] for s in two}), 1)
        self.assertEqual(len({s["title"] for s in two}), 2)
        self.assertTrue(all(len(s["notes"]) == 1 for s in two))
        rows = fixtures["row-pr-no-model"]["sessions"]
        self.assertTrue(all(any(link["kind"] == "pr" for link in s["links"]) for s in rows))
        self.assertEqual(sum(1 for s in rows if s.get("model") is None), 2)
        self.assertEqual(sum(1 for s in rows if s.get("model")), 2)

    def test_the_machines_scenario_has_live_and_stale_remote_rows(self):
        snap = self.fixtures()["machines"]
        remote = [s for s in snap["sessions"] if s["machine"]]
        self.assertTrue(any(s["stale"] for s in remote))
        self.assertTrue(any(not s["stale"] and s["remote_open"] == "tmux" for s in remote))
        self.assertTrue(all(s["key"].endswith("@" + s["machine"]["id"]) for s in remote))
        self.assertEqual({p["status"] for p in snap["peers"]}, {"online", "offline"})
        # Other scenarios are this Mac's own rows only.
        self.assertTrue(all(s["machine"] is None for s in self.fixtures()["five-sessions"]["sessions"]))

    def test_the_machines_board_groups_by_machine(self):
        snap = self.fixtures()["machines"]
        by_state = {}
        for s in snap["sessions"]:
            by_state.setdefault(s["state"], set()).add(s["machine"]["id"] if s["machine"] else "local")
        # A section with this Mac's and another Mac's sessions, and two other Macs.
        self.assertTrue(any("local" in ids and len(ids) > 1 for ids in by_state.values()))
        self.assertGreaterEqual(len({s["machine"]["id"] for s in snap["sessions"] if s["machine"]}), 2)
        settings = {n: (d, q) for n, _snap, d, q, _b in self.mod.scenarios(self.sample)}
        self.assertNotIn("groupPopoverByMachine=bool:false", settings["machines"][0])
        self.assertIn("groupPopoverByMachine=bool:false", settings["machines-flat"][0])
        self.assertEqual(settings["machines-filtered"][1], "machine:mini")
        self.assertIn("popoverWidth=float:320", settings["machines-narrow"][0])
        fixtures = self.fixtures()
        for name in ("machines-flat", "machines-filtered", "machines-narrow"):
            self.assertEqual(fixtures[name], snap)

    def test_only_filters_by_name(self):
        out = Path(self.tmp.name) / "only"
        self.assertEqual(self.mod.main([str(SAMPLE), str(out), "--only", "^empty$"]), 0)
        lines = (out / "scenarios.tsv").read_text().splitlines()
        self.assertEqual([line.split("\t")[0] for line in lines], ["empty-light", "empty-dark"])


if __name__ == "__main__":
    unittest.main()

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
        return {name: snap for name, snap, _, _ in self.mod.scenarios(self.sample)}

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

    def test_every_scenario_line_has_four_tab_separated_fields(self):
        lines = (self.out / "scenarios.tsv").read_text().splitlines()
        self.assertEqual(len(lines), 2 * len(self.fixtures()))
        for line in lines:
            fields = line.split("\t")
            self.assertEqual(len(fields), 4, line)
            name, path, settings, _query = fields
            self.assertTrue(name.endswith(("-light", "-dark")), line)
            self.assertTrue(Path(path).is_file(), line)
            self.assertIn("appearance=string:", settings)

    def test_only_filters_by_name(self):
        out = Path(self.tmp.name) / "only"
        self.assertEqual(self.mod.main([str(SAMPLE), str(out), "--only", "^empty$"]), 0)
        lines = (out / "scenarios.tsv").read_text().splitlines()
        self.assertEqual([line.split("\t")[0] for line in lines], ["empty-light", "empty-dark"])


if __name__ == "__main__":
    unittest.main()

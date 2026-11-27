"""The popover backdrop stays applied where the fix put it.

`PopoverBackdropTests` covers only the pure opacity rule; dropping a
`.popoverBackdrop()` call (say in a rebase) would leave every test green and
bring back washed-out text over a bright window. This reads the Swift
sources as text (comments and strings blanked) and checks each call site.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import swift_source  # noqa: E402

SITES = (
    "Views/MenuBarView.swift",
    "Views/DashboardView.swift",
    "Help/HelpView.swift",
)


class PopoverBackdropSiteTests(unittest.TestCase):
    def test_each_site_applies_the_backdrop(self):
        for rel in SITES:
            with self.subTest(file=rel):
                source = (swift_source.APP_SOURCES / rel).read_text(encoding="utf-8")
                self.assertIn(".popoverBackdrop()", swift_source.blank(source))

    def test_a_commented_out_call_does_not_count(self):
        self.assertNotIn(".popoverBackdrop()", swift_source.blank("// .popoverBackdrop()\n"))


if __name__ == "__main__":
    unittest.main()

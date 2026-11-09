"""Keeps the app's plain-words reason tables in step with the hook.

`SessionDisplay.swift` rewrites raw `waiting_reason` values the hook stores
(a needs-you notification type with no message, the AskUserQuestion /
ExitPlanMode reasons, a StopFailure's "<error>: ..." prefix) into
sentences. The hook owns the first two tables; nothing else fails when one
side gains a value, so this test does.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import hook  # noqa: E402
from tests import swift_source  # noqa: E402

SESSION_DISPLAY = swift_source.APP_SOURCES / "Support" / "SessionDisplay.swift"

# Claude Code's StopFailure `error` values (its hooks reference), plus
# `overloaded`, which the hook tests feed through hook.failure_reason. The
# hook passes the value through, so this list lives here, not in hook.py.
STOP_FAILURE_ERRORS = {
    "rate_limit", "authentication_failed", "billing_error", "invalid_request",
    "server_error", "max_output_tokens", "overloaded", "unknown",
}


def swift_table(source: str, name: str) -> set:
    """The string keys of `static let <name>: [String: String] = [...]`."""
    match = re.search(r"static let %s: \[String: String\] = \[" % re.escape(name), source)
    if not match:
        raise AssertionError(f"{name} not found in {SESSION_DISPLAY}")
    end = swift_source.skip_balanced(source, match.end() - 1)
    return set(re.findall(r'"([^"\\]+)"\s*:', source[match.end():end]))


class SessionDisplayReasonTablesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SESSION_DISPLAY.read_text()

    def test_notification_types_stored_as_reasons_have_words(self):
        # _extract_waiting_reason stores the type itself only for relevant,
        # non-resume types without a fallback sentence.
        stored = (hook.RELEVANT_NOTIFICATION_TYPES - hook.RESUME_NOTIFICATION_TYPES
                  - set(hook.NOTIFICATION_FALLBACK_REASONS))
        self.assertTrue(stored)
        missing = stored - swift_table(self.source, "notificationReasons")
        self.assertFalse(missing, f"add to SessionDisplay.notificationReasons: {sorted(missing)}")

    def test_needs_you_tool_reasons_have_words(self):
        missing = set(hook.NEEDS_YOU_TOOL_REASONS.values()) - swift_table(self.source, "toolReasons")
        self.assertFalse(missing, f"add to SessionDisplay.toolReasons: {sorted(missing)}")

    def test_stop_failure_errors_have_words(self):
        missing = STOP_FAILURE_ERRORS - swift_table(self.source, "failureReasons")
        self.assertFalse(missing, f"add to SessionDisplay.failureReasons: {sorted(missing)}")

    def test_table_scan_reads_keys_not_values(self):
        sample = 'static let t: [String: String] = [\n    "a_b": "words: here",\n    "c": "d",\n]'
        self.assertEqual(swift_table(sample, "t"), {"a_b", "c"})


if __name__ == "__main__":
    unittest.main()

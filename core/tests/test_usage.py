import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import diag, usage
from tests.helpers import AgentBoardTestCase

# 2026-10-24T11:34:03Z, 2026-10-24T13:30:00Z, 2026-10-27T17:00:00Z
CACHED_AT = 1792841643
FIVE_HOUR_RESET = 1792848600
SEVEN_DAY_RESET = 1793120400


class ReadClaudeRateLimitsTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.fake_home / ".claude" / "rate-limits.json"

    def write(self, value):
        self.path.write_text(value if isinstance(value, str) else json.dumps(value))

    def read(self):
        return usage.read_claude_rate_limits(self.path)

    def test_valid_file_converts_epochs_to_iso(self):
        self.write({
            "cached_at": CACHED_AT,
            "five_hour": {"used_percentage": 57, "resets_at": FIVE_HOUR_RESET},
            "seven_day": {"used_percentage": 70, "resets_at": SEVEN_DAY_RESET},
        })
        self.assertEqual(self.read(), {
            "source": "claude",
            "label": "Claude",
            "updated_at": "2026-10-24T11:34:03Z",
            "origin": "~/.claude/rate-limits.json",
            "windows": [
                {"label": "5h", "used_percentage": 57, "resets_at": "2026-10-24T13:30:00Z"},
                {"label": "7d", "used_percentage": 70, "resets_at": "2026-10-27T17:00:00Z"},
            ],
        })

    def test_windows_keep_the_file_order_and_unknown_keys_are_labelled_by_rule(self):
        self.write({
            "seven_day_opus": {"used_percentage": 12},
            "five_hour": {"used_percentage": 57},
            "spend_limit": {"used_percentage": 40},
        })
        self.assertEqual([w["label"] for w in self.read()["windows"]],
                         ["7d opus", "5h", "spend limit"])

    def test_missing_file(self):
        self.assertIsNone(self.read())

    def test_malformed_files_return_none(self):
        for content in ("", "{\"five_hour\": {\"used_perc", "[1, 2]", "null", "57",
                        json.dumps({"cached_at": CACHED_AT}),
                        json.dumps({"five_hour": {"used_percentage": "57"}}),
                        json.dumps({"five_hour": {"used_percentage": True}}),
                        json.dumps({"five_hour": [57], "seven_day": 70})):
            with self.subTest(content=content):
                self.write(content)
                self.assertIsNone(self.read())

    def test_deeply_nested_json_returns_none(self):
        # Under the size cap, but json.loads raises RecursionError on it.
        self.write("[" * 20000 + "]" * 20000)
        self.assertIsNone(self.read())

    def test_unicode_digit_and_huge_digit_keys_are_labelled_as_words(self):
        # int() raises ValueError on "\u00b2" (isdigit() is true) and on
        # digit strings longer than the int conversion limit.
        long_digits = "9" * 5000
        self.write({"\u00b2_hour": {"used_percentage": 1},
                    f"{long_digits}_day": {"used_percentage": 2}})
        self.assertEqual([w["label"] for w in self.read()["windows"]],
                         ["\u00b2 hour", f"{long_digits} day"[:usage.MAX_LABEL_CHARS]])

    def test_out_of_range_times_are_dropped(self):
        self.write({"cached_at": "9999-12-31T23:59:59-05:00",
                    "five_hour": {"used_percentage": 57,
                                  "resets_at": "0001-01-01T00:00:00+05:00"}})
        self.assertEqual(self.read()["updated_at"], None)
        self.assertEqual(self.read()["windows"], [{"label": "5h", "used_percentage": 57}])

    def test_directory_in_place_of_the_file(self):
        self.path.mkdir()
        self.assertIsNone(self.read())

    def test_string_percentage_window_is_dropped_but_others_kept(self):
        self.write({
            "five_hour": {"used_percentage": "57", "resets_at": FIVE_HOUR_RESET},
            "seven_day": {"used_percentage": 70},
        })
        self.assertEqual([w["label"] for w in self.read()["windows"]], ["7d"])

    def test_spend_limit_window_is_copied_generically(self):
        self.write({
            "five_hour": {"used_percentage": 10, "resets_at": FIVE_HOUR_RESET},
            "spend_limit": {"used_percentage": 62.84, "used_usd": 314.2, "limit_usd": 500,
                            "period": "monthly", "resets_at": SEVEN_DAY_RESET},
        })
        spend = self.read()["windows"][1]
        self.assertEqual(spend, {
            "label": "spend limit", "used_percentage": 62.8, "used_usd": 314.2, "limit_usd": 500,
            "period": "monthly", "resets_at": "2026-10-27T17:00:00Z",
        })

    def test_missing_or_bad_resets_at_is_omitted(self):
        self.write({
            "five_hour": {"used_percentage": 57},
            "seven_day": {"used_percentage": 70, "resets_at": "tomorrow"},
        })
        windows = self.read()["windows"]
        self.assertNotIn("resets_at", windows[0])
        self.assertNotIn("resets_at", windows[1])

    def test_missing_cached_at_is_null(self):
        self.write({"five_hour": {"used_percentage": 57}})
        self.assertIsNone(self.read()["updated_at"])

    def test_percentages_are_floored_at_zero_and_rounded(self):
        # Above 100 is kept: the status line's spend_limit "runs from 0 to
        # 100, or above 100 once you exceed the limit".
        self.write({
            "a": {"used_percentage": 140}, "b": {"used_percentage": -3},
            "c": {"used_percentage": 33.333}, "spend_limit": {"used_percentage": 130.04},
        })
        windows = self.read()["windows"]
        self.assertEqual([w["used_percentage"] for w in windows], [140.0, 0.0, 33.3, 130.0])

    def test_an_int_too_large_for_a_float_is_not_a_number(self):
        self.path.write_text(
            '{"five_hour": {"used_percentage": 1%s}, "seven_day": {"used_percentage": 7}}'
            % ("0" * 400))
        usage = self.read()
        self.assertEqual([w["label"] for w in usage["windows"]], ["7d"])

    def test_tmp_sibling_is_ignored(self):
        (self.path.parent / "rate-limits.json.tmp.4242").write_text(
            json.dumps({"five_hour": {"used_percentage": 99}}))
        self.assertIsNone(self.read())
        self.write({"five_hour": {"used_percentage": 12}})
        self.assertEqual(self.read()["windows"][0]["used_percentage"], 12)

    def test_oversized_file_is_not_parsed(self):
        self.write({"five_hour": {"used_percentage": 12}, "pad": "x" * (usage.MAX_BYTES + 1)})
        self.assertIsNone(self.read())

    def test_never_logs_above_debug(self):
        with mock.patch.object(diag, "log") as log:
            for content in ("", "[]", "{bad"):
                self.write(content)
                self.read()
        levels = {call.args[0] for call in log.call_args_list}
        self.assertLessEqual(levels, {"debug"})
        with mock.patch.object(diag, "info") as info, mock.patch.object(diag, "warn") as warn:
            self.write("{bad")
            self.read()
            self.path.unlink()
            self.read()
        info.assert_not_called()
        warn.assert_not_called()



class LabelTests(AgentBoardTestCase):
    def test_label_from_key(self):
        cases = {
            "five_hour": "5h", "seven_day": "7d", "seven_day_opus": "7d opus",
            "30_minute": "30m", "one_week": "1w", "two_months": "2mo",
            "spend_limit": "spend limit", "weekly": "weekly", "five": "five",
            "five-hour": "5h",
        }
        for key, label in cases.items():
            with self.subTest(key=key):
                self.assertEqual(usage.label_from_key(key), label)

    def test_label_from_minutes(self):
        cases = {300: "5h", 299: "5h", 10080: "7d", 10079: "7d", 1440: "1d", 60: "1h",
                 45: "45m", 90: "90m", 0: None, -5: None, None: None, "300": None,
                 True: None}
        for minutes, label in cases.items():
            with self.subTest(minutes=minutes):
                self.assertEqual(usage.label_from_minutes(minutes), label)


class DropFileTests(AgentBoardTestCase):
    def setUp(self):
        super().setUp()
        self.dir = usage.usage_dir(self.agentboard_home)
        self.dir.mkdir(parents=True)
        self.claude = self.fake_home / ".claude" / "rate-limits.json"
        self.now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)

    def drop(self, name, value):
        path = self.dir / name
        path.write_text(value if isinstance(value, str) else json.dumps(value))
        return path

    def sources(self):
        return usage.read_usage_sources(self.agentboard_home, self.claude, now=self.now)

    def test_no_sources_is_an_empty_list(self):
        self.dir.rmdir()
        self.assertEqual(self.sources(), [])

    def test_generic_file_shape(self):
        self.drop("acme.json", {
            "label": "Acme agent",
            "updated_at": "2026-10-26T11:00:00Z",
            "windows": [
                {"label": "daily", "used_percentage": 20, "resets_at": "2026-10-27T00:00:00+00:00"},
                {"label": "monthly spend", "used_percentage": 63.04, "used_usd": 314,
                 "limit_usd": 500, "period": "monthly", "resets_at": 1793491200},
                {"label": "", "used_percentage": 5},
                {"used_percentage": 5},
                {"label": "bad", "used_percentage": "5"},
                "not a window",
            ],
        })
        self.assertEqual(self.sources(), [{
            "source": "acme",
            "label": "Acme agent",
            "updated_at": "2026-10-26T11:00:00Z",
            "origin": str(self.dir / "acme.json"),
            "windows": [
                {"label": "daily", "used_percentage": 20.0, "resets_at": "2026-10-27T00:00:00Z"},
                {"label": "monthly spend", "used_percentage": 63.0, "used_usd": 314,
                 "limit_usd": 500, "period": "monthly", "resets_at": "2026-11-01T00:00:00Z"},
            ],
        }])

    def test_label_defaults_to_the_file_name_and_updated_at_to_its_mtime(self):
        path = self.drop("tool-x.json", {"windows": [{"label": "5h", "used_percentage": 1}]})
        stamp = self.now.timestamp() - 600
        os.utime(path, (stamp, stamp))
        (source,) = self.sources()
        self.assertEqual((source["source"], source["label"], source["updated_at"]),
                         ("tool-x", "tool-x", "2026-10-26T11:50:00Z"))

    def test_malformed_drop_files_are_skipped_silently(self):
        for name, content in (("a.json", "{bad"), ("b.json", "[]"), ("c.json", {"windows": {}}),
                              ("d.json", {"windows": []}), ("e.txt", {"windows": [
                                  {"label": "x", "used_percentage": 1}]})):
            self.drop(name, content)
        (self.dir / "f.json").mkdir()
        with mock.patch.object(diag, "info") as info, mock.patch.object(diag, "warn") as warn:
            self.assertEqual(self.sources(), [])
        info.assert_not_called()
        warn.assert_not_called()

    def test_out_of_range_offset_times_never_raise(self):
        # Valid ISO strings that overflow once converted to UTC.
        path = self.drop("edge.json", {
            "updated_at": "9999-12-31T23:59:59-05:00",
            "windows": [{"label": "5h", "used_percentage": 3,
                         "resets_at": "9999-12-31T23:59:59-05:00"},
                        {"label": "7d", "used_percentage": 4,
                         "resets_at": "0001-01-01T00:00:00+05:00"}]})
        stamp = self.now.timestamp() - 60
        os.utime(path, (stamp, stamp))
        self.drop("ok.json", {"updated_at": "2026-10-26T11:00:00Z",
                              "windows": [{"label": "5h", "used_percentage": 1}]})
        sources = self.sources()
        self.assertEqual([s["source"] for s in sources], ["edge", "ok"])
        # The bad times are dropped: updated_at falls back to the mtime.
        self.assertEqual(sources[0]["updated_at"], "2026-10-26T11:59:00Z")
        self.assertEqual(sources[0]["windows"], [{"label": "5h", "used_percentage": 3.0},
                                                 {"label": "7d", "used_percentage": 4.0}])
        self.assertIsNone(usage._parse_time("9999-12-31T23:59:59-05:00"))
        self.assertEqual(usage._parse_time("2026-10-26T12:00:00+02:00"),
                         datetime(2026, 10, 26, 10, 0, tzinfo=timezone.utc))

    def test_deeply_nested_drop_file_is_skipped_and_others_kept(self):
        self.drop("deep.json", "[" * 20000 + "]" * 20000)
        self.drop("ok.json", {"updated_at": "2026-10-26T11:00:00Z",
                              "windows": [{"label": "5h", "used_percentage": 1}]})
        with mock.patch.object(diag, "info") as info, mock.patch.object(diag, "warn") as warn:
            self.assertEqual([s["source"] for s in self.sources()], ["ok"])
        info.assert_not_called()
        warn.assert_not_called()

    def test_claude_first_then_files_by_name_and_freshest_wins_per_source(self):
        self.claude.write_text(json.dumps({"cached_at": self.now.timestamp() - 60,
                                           "five_hour": {"used_percentage": 57}}))
        self.drop("zeta.json", {"updated_at": "2026-10-26T11:00:00Z",
                                "windows": [{"label": "5h", "used_percentage": 1}]})
        self.drop("codex.json", {"label": "Codex", "updated_at": "2026-10-26T11:00:00Z",
                                 "windows": [{"label": "5h", "used_percentage": 2}]})
        # Same source name as the built-in reader: the older one loses.
        self.drop("claude.json", {"updated_at": "2026-10-26T10:00:00Z",
                                  "windows": [{"label": "5h", "used_percentage": 99}]})
        sources = self.sources()
        self.assertEqual([s["source"] for s in sources], ["claude", "codex", "zeta"])
        self.assertEqual(sources[0]["windows"][0]["used_percentage"], 57)
        self.drop("claude.json", {"updated_at": "2026-10-26T11:59:30Z",
                                  "windows": [{"label": "5h", "used_percentage": 99}]})
        self.assertEqual(self.sources()[0]["windows"][0]["used_percentage"], 99)

    def test_sources_older_than_max_age_are_left_out(self):
        self.drop("old.json", {"updated_at": "2026-10-18T11:59:00Z",
                               "windows": [{"label": "5h", "used_percentage": 1}]})
        self.drop("new.json", {"updated_at": "2026-10-19T12:01:00Z",
                               "windows": [{"label": "5h", "used_percentage": 1}]})
        self.assertEqual([s["source"] for s in self.sources()], ["new"])

    def test_source_and_window_counts_are_capped(self):
        for i in range(usage.MAX_SOURCES + 3):
            self.drop(f"s{i:02}.json", {"windows": [
                {"label": f"w{j}", "used_percentage": j} for j in range(usage.MAX_WINDOWS + 2)]})
        sources = self.sources()
        self.assertEqual(len(sources), usage.MAX_SOURCES)
        self.assertEqual(len(sources[0]["windows"]), usage.MAX_WINDOWS)

    def test_record_source_writes_atomically_and_keeps_the_fresher_file(self):
        data = {"label": "Codex", "updated_at": "2026-10-26T11:00:00Z",
                "windows": [{"label": "5h", "used_percentage": 10}]}
        self.assertTrue(usage.record_source(self.agentboard_home, "codex", data))
        older = dict(data, updated_at="2026-10-26T10:00:00Z",
                     windows=[{"label": "5h", "used_percentage": 99}])
        self.assertFalse(usage.record_source(self.agentboard_home, "codex", older))
        newer = dict(data, updated_at="2026-10-26T11:30:00Z",
                     windows=[{"label": "5h", "used_percentage": 20}])
        self.assertTrue(usage.record_source(self.agentboard_home, "codex", newer))
        (source,) = self.sources()
        self.assertEqual(source["windows"][0]["used_percentage"], 20)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["codex.json"])

    def test_record_source_creates_the_directory(self):
        self.dir.rmdir()
        self.assertTrue(usage.record_source(self.agentboard_home, "codex", {
            "updated_at": "2026-10-26T11:00:00Z",
            "windows": [{"label": "5h", "used_percentage": 10}]}))
        self.assertTrue((self.dir / "codex.json").is_file())

    def test_uninstall_purges_the_usage_directory(self):
        text = (Path(__file__).resolve().parents[2] / "scripts" / "uninstall.sh").read_text()
        start = text.index("AGENTBOARD_DATA_DIRS=(")
        listed = text[start:text.index(")", start)].split("(", 1)[1].split()
        self.assertIn(usage.USAGE_DIR_NAME, listed)
        self.assertIn(f'_purge_known_glob "$dir/{usage.USAGE_DIR_NAME}" "*.json"', text)


class CodexSourceTests(AgentBoardTestCase):
    # Shaped like codex-rs protocol RateLimitSnapshot / RateLimitWindow.
    def test_primary_and_secondary_windows(self):
        source = usage.codex_source({
            "limit_id": "codex", "limit_name": None,
            "primary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": 1793491200},
            "secondary": {"used_percent": 40.0, "window_minutes": 10080, "resets_at": None},
            "credits": None, "plan_type": "plus",
        }, "2026-10-26T11:00:00.123Z")
        self.assertEqual(source, {
            "label": "Codex",
            "updated_at": "2026-10-26T11:00:00Z",
            "windows": [
                {"label": "5h", "used_percentage": 12.5, "resets_at": "2026-11-01T00:00:00Z"},
                {"label": "7d", "used_percentage": 40.0},
            ],
        })

    def test_older_resets_in_seconds_is_relative_to_the_event(self):
        source = usage.codex_source({
            "primary": {"used_percent": 3, "window_minutes": 299, "resets_in_seconds": 3600},
            "secondary": None,
        }, "2026-10-26T11:00:00Z")
        self.assertEqual(source["windows"], [
            {"label": "5h", "used_percentage": 3.0, "resets_at": "2026-10-26T12:00:00Z"}])

    def test_window_without_minutes_is_named_by_position(self):
        source = usage.codex_source({"primary": {"used_percent": 3}}, "2026-10-26T11:00:00Z")
        self.assertEqual(source["windows"][0]["label"], "primary")

    def test_no_usable_snapshot(self):
        for value in (None, [], {}, {"primary": None, "secondary": None},
                      {"primary": {"used_percent": "3"}},
                      {"limit_id": "codex_other", "primary": {"used_percent": 3}}):
            with self.subTest(value=value):
                self.assertIsNone(usage.codex_source(value, "2026-10-26T11:00:00Z"))
        for stamp in (None, "", "yesterday", 1793491200):
            with self.subTest(stamp=stamp):
                self.assertIsNone(usage.codex_source(
                    {"primary": {"used_percent": 3}}, stamp))

if __name__ == "__main__":
    import unittest

    unittest.main()

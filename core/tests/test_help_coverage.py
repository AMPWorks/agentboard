"""Keeps the in-app guide, docs/REFERENCE.md and the code in step.

- Every config.json key core reads is registered in `config_keys.py`, and
  every registered key (and sub-field) is still read somewhere in core.
- Every registered key is in the in-app guide and docs/REFERENCE.md's
  Configuration section.
- Every CLI subcommand and long option is in the in-app guide and
  docs/REFERENCE.md's CLI reference.
- Every UserDefaults key the app reads or writes is a Settings key
  (`SettingsKeys.all`) or carries a `// not-a-setting: <reason>` comment.
"""

import argparse
import ast
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import cli, config_keys  # noqa: E402
from tests import swift_source  # noqa: E402
from tests.helpers import AgentBoardTestCase  # noqa: E402

CORE = Path(__file__).resolve().parents[1] / "agentboard"
HELP_CONTENT = swift_source.APP_SOURCES / "Help" / "HelpContent.swift"
REFERENCE = swift_source.REPO / "docs" / "REFERENCE.md"
SETTINGS_KEYS_SWIFT = swift_source.APP_SOURCES / "Support" / "SettingsKeys.swift"

# Keys the scan finds today; a broken scan that finds nothing fails here.
KNOWN_READ_KEYS = {
    "providers", "ticket_patterns", "automation_logins", "note_markers",
    "note_unattended_prefixes", "log_level", "retitle_command",
    "retitle_min_prompts", "retitle_max_per_run",
}

# Long options deliberately left out of the guide.
INTERNAL_FLAGS = {
    "--bin-path",  # install-hooks: install.sh --dry-run's preview path
}

# -- config.json key scan --------------------------------------------------------

_CONFIG_NAMES = {"config", "cfg"}


def _string(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(call):
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def scan_config_keys(source: str) -> set:
    """config.json keys `source` reads: `config.get("k")` / `cfg.get("k")`,
    `data.get("k")` only inside a function whose body mentions the literal
    "config.json", `"k" in config` / `not in config`, `config["k"]`,
    `load_config(...)["k"]`, and a string-literal key passed right after
    `config` to a helper (`_int_setting(config, "k", ...)`)."""
    tree = ast.parse(source)
    config_file_scopes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(_string(inner) == "config.json" for inner in ast.walk(node)):
                config_file_scopes.update(id(inner) for inner in ast.walk(node))

    keys = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if (isinstance(func, ast.Attribute) and func.attr == "get"
                    and isinstance(func.value, ast.Name) and node.args):
                key = _string(node.args[0])
                receiver = func.value.id
                if key and (receiver in _CONFIG_NAMES
                            or (receiver == "data" and id(node) in config_file_scopes)):
                    keys.add(key)
            if (len(node.args) >= 2 and isinstance(node.args[0], ast.Name)
                    and node.args[0].id in _CONFIG_NAMES):
                key = _string(node.args[1])
                if key:
                    keys.add(key)
        elif isinstance(node, ast.Compare):
            if (len(node.ops) == 1 and isinstance(node.ops[0], (ast.In, ast.NotIn))
                    and isinstance(node.comparators[0], ast.Name)
                    and node.comparators[0].id in _CONFIG_NAMES):
                key = _string(node.left)
                if key:
                    keys.add(key)
        elif isinstance(node, ast.Subscript):
            value = node.value
            if ((isinstance(value, ast.Name) and value.id in _CONFIG_NAMES)
                    or (isinstance(value, ast.Call) and _call_name(value) == "load_config")):
                key = _string(node.slice)
                if key:
                    keys.add(key)
    return keys


def core_modules():
    return sorted(path for path in CORE.glob("*.py") if path.name != "config_keys.py")


def code_string_literals(source: str) -> set:
    """String constants used as values in `source`; docstrings and other
    bare string statements are not uses."""
    tree = ast.parse(source)
    prose = {id(node.value) for node in ast.walk(tree) if isinstance(node, ast.Expr)}
    return {value for node in ast.walk(tree)
            if id(node) not in prose and (value := _string(node)) is not None}


def core_string_literals() -> set:
    literals = set()
    for path in core_modules():
        literals |= code_string_literals(path.read_text())
    return literals


def reference_section(title: str) -> str:
    text = REFERENCE.read_text()
    start = text.find(f"\n## {title}\n")
    if start == -1:
        return ""
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


class ConfigKeyScanSnippetTests(unittest.TestCase):
    def test_config_get_is_a_read(self):
        self.assertEqual(scan_config_keys('def f(config):\n    return config.get("a")\n'), {"a"})
        self.assertEqual(scan_config_keys('def f(cfg):\n    return cfg.get("b", 1)\n'), {"b"})

    def test_data_get_outside_a_config_json_reader_is_not_a_read(self):
        source = 'def ingest(path):\n    data = load(path)\n    return data.get("session")\n'
        self.assertEqual(scan_config_keys(source), set())

    def test_data_get_inside_a_config_json_reader_is_a_read(self):
        source = (
            'def level(home):\n'
            '    data = json.load(open(home / "config.json"))\n'
            '    return data.get("log_level")\n'
        )
        self.assertEqual(scan_config_keys(source), {"log_level"})

    def test_helper_taking_config_and_key_is_a_read(self):
        source = 'def f(config):\n    return _int_setting(config, "retitle_min_prompts", 5, 1)\n'
        self.assertEqual(scan_config_keys(source), {"retitle_min_prompts"})

    def test_membership_and_subscripts_are_reads(self):
        source = (
            'def f(config, home):\n'
            '    if "x" not in config:\n'
            '        return config["y"]\n'
            '    return load_config(home)["z"]\n'
        )
        self.assertEqual(scan_config_keys(source), {"x", "y", "z"})

    def test_other_receivers_are_not_reads(self):
        self.assertEqual(scan_config_keys('def f(row):\n    return row.get("url")\n'), set())

    def test_docstrings_are_not_code_literals(self):
        source = (
            '"""regex"""\n'
            'def f(entry):\n'
            '    """url"""\n'
            '    "command"\n'
            '    return entry.get("name")\n'
        )
        self.assertEqual(code_string_literals(source), {"name"})


class ConfigKeyRegistryTests(unittest.TestCase):
    def test_registry_has_no_duplicates(self):
        names = config_keys.CONFIG_KEY_NAMES
        self.assertEqual(len(names), len(set(names)))

    def test_every_key_core_reads_is_registered(self):
        found = {}
        for path in core_modules():
            for key in scan_config_keys(path.read_text()):
                found.setdefault(key, path.name)
        self.assertLessEqual(KNOWN_READ_KEYS, set(found),
                             "the config-key scan missed keys it found before")
        unregistered = {key: name for key, name in found.items()
                        if key not in config_keys.CONFIG_KEY_NAMES}
        self.assertEqual(unregistered, {},
                         "config.json keys read by core but missing from config_keys.CONFIG_KEYS")

    def test_every_registered_key_and_field_is_still_read(self):
        # Top-level keys must still be found by the read scan itself; a
        # sub-field has no scan, so it must still appear as a code literal.
        found = set()
        for path in core_modules():
            found |= scan_config_keys(path.read_text())
        stale = [entry.key for entry in config_keys.CONFIG_KEYS if entry.key not in found]
        literals = core_string_literals()
        stale += [f"{entry.key}[].{field}" for entry in config_keys.CONFIG_KEYS
                  for field in entry.fields if field not in literals]
        self.assertEqual(stale, [], "registered config keys no core module mentions any more")

    def test_every_registered_key_is_in_the_in_app_guide(self):
        content = HELP_CONTENT.read_text()
        missing = [entry.key for entry in config_keys.CONFIG_KEYS if f"`{entry.key}`" not in content]
        missing += [f"{entry.key}[].{field}" for entry in config_keys.CONFIG_KEYS
                    for field in entry.fields if f"`{field}`" not in content]
        self.assertEqual(missing, [], f"config keys missing from {HELP_CONTENT.name}")

    def test_every_registered_key_is_in_reference_configuration(self):
        section = reference_section("Configuration (config.json)")
        self.assertTrue(section, "docs/REFERENCE.md has no '## Configuration (config.json)' section")
        missing = [entry.key for entry in config_keys.CONFIG_KEYS if f"`{entry.key}`" not in section]
        missing += [f"{entry.key}[].{field}" for entry in config_keys.CONFIG_KEYS
                    for field in entry.fields if f"`{field}`" not in section]
        self.assertEqual(missing, [], "config keys missing from docs/REFERENCE.md's Configuration section")


# -- CLI coverage ------------------------------------------------------------------

def _subcommands(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def missing_cli_mentions(content: str, *, skip=()) -> list:
    """`agentboard <subcommand>` and every long option (except --help and
    INTERNAL_FLAGS) that `content` does not mention."""
    subcommands = _subcommands(cli.build_parser())
    missing = []
    for name, subparser in sorted(subcommands.items()):
        if name in skip:
            continue
        if not re.search(r"agentboard " + re.escape(name) + r"(?![\w-])", content):
            missing.append(f"agentboard {name}")
        for action in subparser._actions:
            for option in action.option_strings:
                if not option.startswith("--") or option == "--help" or option in INTERNAL_FLAGS:
                    continue
                if not re.search(re.escape(option) + r"(?![\w-])", content):
                    missing.append(f"agentboard {name} {option}")
    return missing


class CliCoverageTests(AgentBoardTestCase):
    def test_every_subcommand_and_long_option_is_in_the_in_app_guide(self):
        self.assertGreater(len(_subcommands(cli.build_parser())), 15)
        missing = missing_cli_mentions(HELP_CONTENT.read_text(), skip={"hook"})
        self.assertEqual(missing, [], f"CLI commands or options missing from {HELP_CONTENT.name}")

    def test_every_subcommand_and_long_option_is_in_the_reference(self):
        section = reference_section("CLI reference")
        self.assertTrue(section, "docs/REFERENCE.md has no '## CLI reference' section")
        self.assertEqual(missing_cli_mentions(section), [],
                         "CLI commands or options missing from docs/REFERENCE.md's CLI reference")

    def test_a_longer_subcommand_name_is_not_a_mention(self):
        # `agentboard notes ...` must not count as mentioning `agentboard note`.
        self.assertIn("agentboard note", missing_cli_mentions("agentboard notes --all"))
        self.assertNotIn("agentboard note", missing_cli_mentions("agentboard note --kind"))

    def test_internal_flags_still_exist(self):
        options = {option for subparser in _subcommands(cli.build_parser()).values()
                   for action in subparser._actions for option in action.option_strings}
        self.assertLessEqual(INTERNAL_FLAGS, options)


# -- UserDefaults keys -------------------------------------------------------------

_APP_STORAGE_RE = re.compile(r"@AppStorage\(\s*([^,)\s]+)")
_FOR_KEY_RE = re.compile(r"\bforKey:\s*([^,)\s]+)")
_TYPE_MEMBER_RE = re.compile(r"^[A-Z]\w*\.[a-z]\w*$")
NOT_A_SETTING = "// not-a-setting:"


def settings_keys_all(source: str) -> set:
    """The `Type.member` entries of `static let all` in SettingsKeys.swift."""
    match = re.search(r"static let all\b[^=]*=\s*\[(.*?)\]", source, re.S)
    if not match:
        return set()
    return set(re.findall(r"\b[A-Z]\w*\.[a-z]\w*\b", match.group(1)))


def scan_defaults_keys(source: str, settings: set) -> list:
    """Problems with the UserDefaults keys in one Swift file: every
    `@AppStorage(K)` and `forKey: K` must name a `Type.member` listed in
    SettingsKeys.all (`Self.k` resolves to the enclosing type), or its line
    must carry `// not-a-setting: <reason>`. Codable keys (`.id`) are
    skipped."""
    blanked = swift_source.blank(source)
    problems = []
    for pattern in (_APP_STORAGE_RE, _FOR_KEY_RE):
        for match in pattern.finditer(source):
            # A match inside a comment or string literal does not count.
            if blanked[match.start()] == " ":
                continue
            argument = match.group(1)
            if argument.startswith("."):
                continue
            line = swift_source.line_of(source, match.start())
            if NOT_A_SETTING in line and line.split(NOT_A_SETTING, 1)[1].strip():
                continue
            if argument.startswith("Self."):
                owner = swift_source.enclosing_type(blanked, match.start())
                argument = f"{owner}.{argument[5:]}" if owner else argument
            if _TYPE_MEMBER_RE.match(argument) and argument in settings:
                continue
            problems.append(f"{argument} (line: {line.strip()})")
    return problems


class DefaultsKeySnippetTests(unittest.TestCase):
    SETTINGS = {"AgentBoardCLI.useFixtureDefaultsKey", "SettingsKeys.appearance"}

    def test_self_resolves_to_the_enclosing_type(self):
        source = (
            "enum AgentBoardCLI {\n"
            "    static let useFixtureDefaultsKey = \"agentboardUseFixture\"\n"
            "    func f() {\n"
            "        if defaults.bool(forKey: Self.useFixtureDefaultsKey) { }\n"
            "    }\n"
            "}\n"
        )
        self.assertEqual(scan_defaults_keys(source, self.SETTINGS), [])
        self.assertEqual(len(scan_defaults_keys(source, {"Other.useFixtureDefaultsKey"})), 1)

    def test_codable_keys_are_skipped(self):
        source = "struct N { init(from d: Decoder) { id = try c.decode(Int.self, forKey: .id) } }\n"
        self.assertEqual(scan_defaults_keys(source, set()), [])

    def test_marked_literal_passes_and_unmarked_literal_fails(self):
        marked = 'let dark = d.string(forKey: "AppleInterfaceStyle") // not-a-setting: system key\n'
        self.assertEqual(scan_defaults_keys(marked, set()), [])
        unmarked = 'let dark = d.string(forKey: "AppleInterfaceStyle")\n'
        self.assertEqual(len(scan_defaults_keys(unmarked, set())), 1)
        self.assertEqual(len(scan_defaults_keys('@AppStorage("appearance") var a = ""\n', set())), 1)

    def test_listed_constant_passes(self):
        source = "@AppStorage(SettingsKeys.appearance) private var appearance = \"\"\n"
        self.assertEqual(scan_defaults_keys(source, self.SETTINGS), [])
        source = "UserDefaults.standard.set(1, forKey: SettingsKeys.appearance)\n"
        self.assertEqual(scan_defaults_keys(source, self.SETTINGS), [])

    def test_matches_in_comments_are_ignored(self):
        source = "/// Use @AppStorage(\"x\") or forKey: \"y\" here.\nlet a = 1\n"
        self.assertEqual(scan_defaults_keys(source, set()), [])


class DefaultsKeyTests(unittest.TestCase):
    def test_every_defaults_key_in_the_app_is_a_settings_key(self):
        settings = settings_keys_all(SETTINGS_KEYS_SWIFT.read_text())
        self.assertGreaterEqual(len(settings), 9)
        problems = []
        for path in swift_source.swift_files(swift_source.APP_SOURCES):
            for problem in scan_defaults_keys(path.read_text(), settings):
                problems.append(f"{path.relative_to(swift_source.REPO)}: {problem}")
        self.assertEqual(problems, [], "UserDefaults keys that are not in SettingsKeys.all "
                                       "and not marked // not-a-setting:")


if __name__ == "__main__":
    unittest.main()

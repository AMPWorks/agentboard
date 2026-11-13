"""The app tests never use a real UserDefaults suite.

`UserDefaults(suiteName:)` leaves a plist in ~/Library/Preferences on every
run, even after `removePersistentDomain`, and `UserDefaults.standard` is the
user's own app domain. Tests use `TestDefaults` (an in-memory
`MemoryDefaults`, app/Tests/AgentBoardTests/TestDefaults.swift) instead.
A text scan of app/Tests (comments and strings blanked), stdlib only;
`scripts/swift-test.sh` also fails a run that leaves such a plist behind.

An app API whose defaults parameter defaults to `.standard` (found in
app/Sources) must be called from a test with that argument, and not as
`.standard`, or the test reaches the runner's real domain.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import swift_source  # noqa: E402

APP_TESTS = swift_source.REPO / "app" / "Tests"
FORBIDDEN_RE = re.compile(
    r"(?<![\w.])UserDefaults\s*(?:\(|\.\s*init\s*\(|\.\s*standard\b)"
    r"|\.\s*standard\s*\.\s*(?:set|removeObject|removePersistentDomain|setPersistentDomain)\b"
    r"|\bremovePersistentDomain\b|\bsetPersistentDomain\b"
)


# `[label] name: UserDefaults = .standard` in a parameter list.
_STANDARD_PARAM_RE = re.compile(
    r"\b([A-Za-z_]\w*)(?:\s+[A-Za-z_]\w*)?\s*:\s*UserDefaults\s*=\s*\.\s*standard\b")
_CALLABLE_RE = re.compile(r"\bfunc\s+([A-Za-z_]\w*)\s*[(<]|\binit\s*[(<]")


def standard_default_apis(source: str) -> set[tuple[str, str]]:
    """(callee, argument label) for every func/init in `source` whose
    UserDefaults parameter defaults to `.standard`; an init's callee is its
    type's name."""
    blanked = swift_source.blank(source)
    apis = set()
    for param in _STANDARD_PARAM_RE.finditer(blanked):
        decl = None
        for decl in _CALLABLE_RE.finditer(blanked, 0, param.start()):
            pass
        if decl is None:
            continue
        callee = decl.group(1) or swift_source.enclosing_type(blanked, decl.start())
        if callee:
            apis.add((callee, param.group(1)))
    return apis


def _source_apis() -> set[tuple[str, str]]:
    apis = set()
    for path in swift_source.swift_files(swift_source.APP_SOURCES):
        apis |= standard_default_apis(path.read_text())
    return apis


def violations(source: str, apis: set[tuple[str, str]] = frozenset()) -> list[str]:
    """The lines of `source` that reach a real defaults domain: a forbidden
    spelling, or a call to one of `apis` that omits its defaults argument
    (so it falls back to `.standard`) or passes `.standard`."""
    blanked = swift_source.blank(source)
    offsets = [m.start() for m in FORBIDDEN_RE.finditer(blanked)]
    for callee, label in apis:
        call_re = re.compile(rf"(?<!func )(?<![\w])(?:{re.escape(callee)})\s*(?=\()")
        for call in call_re.finditer(blanked):
            args = blanked[call.end():swift_source.skip_balanced(blanked, call.end())]
            given = re.search(rf"\b{re.escape(label)}\s*:\s*(\.\s*standard\b)?", args)
            if given is None or given.group(1):
                offsets.append(call.start())
    return [swift_source.line_of(source, offset).strip() for offset in sorted(offsets)]


class ScannerTests(unittest.TestCase):
    def test_flags_real_suites_and_the_standard_domain(self):
        for line in ('let d = UserDefaults(suiteName: "x")!',
                     'let d = try #require(UserDefaults(suiteName: suite))',
                     'let d = UserDefaults.init(suiteName: "x")',
                     'let d = UserDefaults.standard',
                     'let d = UserDefaults()',
                     'defaults.removePersistentDomain(forName: suite)'):
            with self.subTest(line=line):
                self.assertEqual(violations(line + "\n"), [line])

    def test_flags_calls_that_fall_back_to_or_pass_standard(self):
        apis = {("resetSize", "in"), ("SoundPlayer", "defaults")}
        for line in ('PopoverSizing.resetSize()',
                     'PopoverSizing.resetSize(in: .standard)',
                     'let p = SoundPlayer(backend: b, resolve: { _ in nil })',
                     'let p = SoundPlayer(defaults: .standard)'):
            with self.subTest(line=line):
                self.assertEqual(violations(line + "\n", apis), [line])
        fine = ('PopoverSizing.resetSize(in: defaults)\n'
                'let p = SoundPlayer(\n    backend: b, defaults: suite.defaults\n)\n'
                'func resetSize() {}\n'
                '#expect(UpdatesFontSize.parse("x") == .standard)\n')
        self.assertEqual(violations(fine, apis), [])

    def test_finds_standard_default_apis_in_sources(self):
        source = ('enum PopoverSizing {\n'
                  '    static func resetSize(in defaults: UserDefaults = .standard) {}\n'
                  '}\n'
                  'final class SoundPlayer {\n'
                  '    init(\n        backend: B? = nil,\n        defaults: UserDefaults = .standard\n    ) {}\n'
                  '    func other(_ d: UserDefaults) {}\n'
                  '}\n')
        self.assertEqual(standard_default_apis(source),
                         {("resetSize", "in"), ("SoundPlayer", "defaults")})
        apis = _source_apis()
        for api in (("resetSize", "in"), ("systemIsDark", "defaults"), ("SoundPlayer", "defaults")):
            with self.subTest(api=api):
                self.assertIn(api, apis)

    def test_ignores_memory_defaults_comments_and_strings(self):
        source = ('let d = TestDefaults().defaults\n'
                  'let m = MemoryDefaults(suiteName: "agentboard-tests-1")!\n'
                  '/// never `UserDefaults(suiteName:)` directly\n'
                  'let s = "UserDefaults.standard"\n'
                  'func f(_ d: UserDefaults) {}\n')
        self.assertEqual(violations(source), [])


class AppTestsUseMemoryDefaultsTests(unittest.TestCase):
    def test_no_app_test_uses_a_real_defaults_domain(self):
        files = sorted(APP_TESTS.rglob("*.swift"))
        self.assertTrue(files, f"no Swift files under {APP_TESTS}")
        apis = _source_apis()
        found = [f"{path.relative_to(swift_source.REPO)}: {line}"
                 for path in files for line in violations(path.read_text(), apis)]
        self.assertEqual(found, [], "use TestDefaults().defaults (in memory) instead")

    def test_the_shared_helper_exists_once(self):
        defs = [path.name for path in APP_TESTS.rglob("*.swift")
                if re.search(r"^final class MemoryDefaults\b", path.read_text(), re.M)]
        self.assertEqual(defs, ["TestDefaults.swift"])


if __name__ == "__main__":
    unittest.main()

"""Every Settings control is in the search catalog, and every catalog item
is on screen.

`app/Sources/AgentBoard/Support/SettingsCatalog.swift` holds each Settings
control's label and tooltip, and the Settings search matches against it,
so a control that bypasses the catalog would be unsearchable and an item
no view renders would be a dead search result. A heuristic text scan
(stdlib only, `tests/swift_source.py`):

(a) every `static let <name> = SettingsItem(` is anchored as
    `.settingsAnchor(SettingsCatalog.<name>` somewhere under `Views/`, and
    the sound rows as `.settingsAnchor(SettingsCatalog.sound(` (a control
    that only takes its label from the catalog would leave its search
    result nothing to scroll to or highlight);
(b) in the Settings views, every control constructor (the tooltip lint's
    `CONTROLS`) names `SettingsCatalog.`, or an `item.`/`event.` label, in
    its arguments or its modifier chain, or carries
    `// not-searchable: <reason>` on its line. Per-peer rows and the Add
    machine sheet in MachinesSettingsView.swift are out of scope (their
    labels name a peer; the catalog's `pairedMachines` stands for them), so
    that file is scanned inside `MachinesSettingsSection` only;
(c) every row named as another item's `fallback: "x"` is anchored as
    `.settingsAnchor(SettingsCatalog.x, standingIn: ...)`, so it lights
    up while that item's own row is not on screen.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import swift_source  # noqa: E402
from tests.test_app_tooltips import CONTROLS  # noqa: E402

VIEWS = swift_source.APP_SOURCES / "Views"
CATALOG = swift_source.APP_SOURCES / "Support" / "SettingsCatalog.swift"
# File name -> the types scanned in it (None: the whole file).
SETTINGS_VIEWS = {
    "SettingsView.swift": None,
    "SettingsPane.swift": None,
    "SettingsGeneralPane.swift": None,
    "SettingsMenuBarPane.swift": None,
    "SettingsNotificationsPane.swift": None,
    "SettingsAdvancedPane.swift": None,
    "SoundSettingsRow.swift": None,
    "MachinesSettingsView.swift": {"MachinesSettingsSection"},
}
NOT_SEARCHABLE = "// not-searchable:"
CATALOGUED_RE = re.compile(r"\bSettingsCatalog\.|(?<![\w.])(?:item|event)\.")
# Settings controls the real-source scan inspected when this was written;
# fewer means the scan broke (or controls were removed: lower it then).
MIN_SETTINGS_CONTROLS = 41

_ITEM_RE = re.compile(r"\bstatic\s+let\s+([A-Za-z_]\w*)\s*=\s*SettingsItem\s*\(")
_CONTROL_RE = re.compile(r"(?<![\w.])(?:SwiftUI\.)?(" + "|".join(CONTROLS) + r")\s*(?=[({])")


def catalog_items(source: str) -> list:
    """The names of the `static let x = SettingsItem(` declarations."""
    return _ITEM_RE.findall(swift_source.blank(source))


def unrendered(items: list, view_sources: list) -> list:
    """The items no view source anchors as `.settingsAnchor(SettingsCatalog.<name>`
    (`sound(` stands for the per-event sound items)."""
    text = "\n".join(swift_source.blank(source) for source in view_sources)
    # A word boundary only after a name that ends in a word character
    # (not after `sound(`).
    return [name for name in items
            if not re.search(rf"\.settingsAnchor\(\s*SettingsCatalog\.{re.escape(name)}"
                             + (r"\b" if name[-1:].isalnum() or name.endswith("_") else ""), text)]


_FALLBACK_RE = re.compile(r'\bfallback\s*:\s*"(\w+)"')


def _fallbacks(catalog_source: str) -> list:
    """The ids named as `fallback: "x"` outside comments."""
    code = "\n".join(line for line in catalog_source.splitlines()
                     if not line.lstrip().startswith("//"))
    return _FALLBACK_RE.findall(code)


def fallbacks_not_standing_in(catalog_source: str, view_sources: list) -> list:
    """The `fallback: "x"` ids whose row is not anchored as
    `.settingsAnchor(SettingsCatalog.x, standingIn: ...)`, so it would not
    light up for the item it stands in for."""
    fallbacks = _fallbacks(catalog_source)
    text = "\n".join(swift_source.blank(source) for source in view_sources)
    return [name for name in dict.fromkeys(fallbacks)
            if not re.search(rf"\.settingsAnchor\(\s*SettingsCatalog\.{re.escape(name)}\s*,\s*standingIn\s*:", text)]


def _args_and_chain(blanked: str, index: int) -> str:
    """The argument list and modifier chain of the constructor whose name
    ends at `index`, without its trailing closures (their contents are
    other views)."""
    probe = swift_source.skip_space(blanked, index)
    args = ""
    if probe < len(blanked) and blanked[probe] == "(":
        end = swift_source.skip_balanced(blanked, probe)
        args = blanked[probe:end]
        index = end
    # Skip the trailing closures the way modifier_chain does, then keep
    # only the chain after them.
    while True:
        probe = swift_source.skip_space(blanked, index)
        if probe < len(blanked) and blanked[probe] == "{":
            index = swift_source.skip_balanced(blanked, probe)
            continue
        match = re.compile(r"[A-Za-z_]\w*").match(blanked, probe)
        if match:
            after = swift_source.skip_space(blanked, match.end())
            if after < len(blanked) and blanked[after] == ":":
                brace = swift_source.skip_space(blanked, after + 1)
                if brace < len(blanked) and blanked[brace] == "{":
                    index = swift_source.skip_balanced(blanked, brace)
                    continue
        break
    _, chain_end = swift_source.modifier_chain(blanked, index)
    return args + blanked[index:chain_end]


def uncatalogued(source: str, types=None) -> tuple:
    """(controls inspected, [(line number, control, line text)] of those
    that neither use the catalog nor carry `// not-searchable: <reason>`).
    `types` limits the scan to controls inside those types."""
    blanked = swift_source.blank(source)
    inspected = 0
    missing = []
    for match in _CONTROL_RE.finditer(blanked):
        if types is not None and swift_source.enclosing_type(blanked, match.start()) not in types:
            continue
        inspected += 1
        line = swift_source.line_of(source, match.start())
        if NOT_SEARCHABLE in line and line.split(NOT_SEARCHABLE, 1)[1].strip():
            continue
        if CATALOGUED_RE.search(_args_and_chain(blanked, match.end())):
            continue
        number = source.count("\n", 0, match.start()) + 1
        missing.append((number, match.group(1), line.strip()))
    return inspected, missing


class ScannerSnippetTests(unittest.TestCase):
    def test_catalog_items_are_found(self):
        source = (
            "enum SettingsCatalog {\n"
            '    static let a = SettingsItem(id: "a", tab: .general, section: "S", label: "A", help: "h")\n'
            "    static let b =\n        SettingsItem(id: \"b\")\n"
            "    static let items: [SettingsItem] = [a, b]\n"
            '    // static let c = SettingsItem(id: "c")\n'
            "}\n"
        )
        self.assertEqual(catalog_items(source), ["a", "b"])

    def test_a_sound_anchor_with_any_argument_passes(self):
        for call in ("sound(event)", "sound(.denied)", "sound( event)"):
            source = f"Picker(...).settingsAnchor(SettingsCatalog.{call})\n"
            self.assertEqual(unrendered(["sound("], [source]), [], call)

    def test_a_fallback_row_must_stand_in(self):
        catalog = ('static let a = SettingsItem(id: "a", fallback: "b")\n'
                   '// fallback: "c" in a comment is ignored\n')
        standing = "Toggle(...).settingsAnchor(SettingsCatalog.b, standingIn: !on)\n"
        plain = "Toggle(...).settingsAnchor(SettingsCatalog.b)\n"
        self.assertEqual(fallbacks_not_standing_in(catalog, [standing]), [])
        self.assertEqual(fallbacks_not_standing_in(catalog, [plain]), ["b"])

    def test_an_item_no_view_anchors_is_reported(self):
        views = [
            "Toggle(SettingsCatalog.a.label, isOn: $a)\n    .settingsAnchor(SettingsCatalog.a)\n"
            "Toggle(SettingsCatalog.b.label, isOn: $b)\n    .help(SettingsCatalog.b.help)\n"
            "Button(SettingsCatalog.c.label) { }\n    .settingsAnchor(SettingsCatalog.c, standingIn: x)\n"
            "// .settingsAnchor(SettingsCatalog.d)\n"
        ]
        self.assertEqual(unrendered(["a", "b", "c", "d", "ab"], views), ["b", "d", "ab"])

    def test_a_control_labelled_from_the_catalog_passes(self):
        source = (
            "Toggle(SettingsCatalog.flash.label, isOn: $flash)\n"
            "    .help(SettingsCatalog.flash.help)\n"
        )
        self.assertEqual(uncatalogued(source), (1, []))

    def test_a_catalogued_tooltip_alone_passes(self):
        source = 'Button(copied ? "Copied" : label) { copy() }\n    .help(SettingsCatalog.copy.help)\n'
        self.assertEqual(uncatalogued(source), (1, []))

    def test_item_and_event_labels_pass(self):
        source = (
            "Picker(event.label, selection: $s) { Text(\"x\") }\n    .help(Self.pickerHelp(event: event))\n"
            "Button(item.label) { go(item) }\n    .help(\"Go\")\n"
        )
        self.assertEqual(uncatalogued(source), (2, []))

    def test_a_literal_label_is_flagged(self):
        source = 'Toggle("Verbose logging", isOn: $v)\n    .help("Log more")\n'
        inspected, missing = uncatalogued(source)
        self.assertEqual(inspected, 1)
        self.assertEqual([(m[0], m[1]) for m in missing], [(1, "Toggle")])

    def test_catalog_use_inside_a_trailing_closure_does_not_count(self):
        source = (
            'Picker("Mode", selection: $m) {\n'
            "    Text(SettingsCatalog.mode.label)\n"
            "}\n"
            '.help("Pick")\n'
        )
        self.assertEqual(len(uncatalogued(source)[1]), 1)

    def test_not_searchable_needs_a_reason(self):
        marked = 'Button("Cancel") { close() } // not-searchable: dismisses the dialog\n'
        self.assertEqual(uncatalogued(marked), (1, []))
        empty = 'Button("Cancel") { close() } // not-searchable:\n'
        self.assertEqual(len(uncatalogued(empty)[1]), 1)

    def test_types_limit_the_scan(self):
        source = (
            "struct MachinesSettingsSection: View {\n"
            "    var body: some View { Toggle(SettingsCatalog.a.label, isOn: $a).help(\"x\") }\n"
            "}\n"
            "private struct PeerRow: View {\n"
            '    var body: some View { Button("Remove") { }.help("Remove it") }\n'
            "}\n"
        )
        self.assertEqual(uncatalogued(source, {"MachinesSettingsSection"}), (1, []))
        self.assertEqual(len(uncatalogued(source)[1]), 1)


class SettingsCatalogTests(unittest.TestCase):
    def test_every_catalog_item_is_rendered(self):
        items = catalog_items(CATALOG.read_text())
        self.assertGreaterEqual(len(items), 30, "the catalog scan found too few items")
        views = [path.read_text() for path in swift_source.swift_files(VIEWS)]
        self.assertEqual(unrendered(items + ["sound("], views), [],
                         "catalog items no view anchors as .settingsAnchor(SettingsCatalog.<name>")

    def test_every_fallback_row_stands_in(self):
        views = [path.read_text() for path in swift_source.swift_files(VIEWS)]
        fallbacks = _fallbacks(CATALOG.read_text())
        self.assertGreaterEqual(len(fallbacks), 3, "the fallback scan found too few fallbacks")
        self.assertEqual(fallbacks_not_standing_in(CATALOG.read_text(), views), [],
                         "fallback rows without .settingsAnchor(SettingsCatalog.<name>, standingIn:")

    def test_every_settings_control_is_catalogued(self):
        inspected = 0
        problems = []
        for name, types in SETTINGS_VIEWS.items():
            path = VIEWS / name
            count, missing = uncatalogued(path.read_text(), types)
            inspected += count
            problems += [f"Views/{name}:{number}: {control} not in SettingsCatalog: {line}"
                         for number, control, line in missing]
        self.assertGreaterEqual(inspected, MIN_SETTINGS_CONTROLS,
                                "the scan found fewer Settings controls than before")
        self.assertEqual(problems, [], "Settings controls outside the catalog (label them from "
                                       "SettingsCatalog or add // not-searchable: <reason>)")


if __name__ == "__main__":
    unittest.main()

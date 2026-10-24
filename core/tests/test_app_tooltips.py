"""Every control in the app's views carries a tooltip.

A heuristic lint over the Swift sources in `app/Sources/AgentBoard/Views`
and `app/Sources/AgentBoard/Help`, read as text (stdlib only): it finds each
`Button`, `Toggle`, `Picker`, `Menu`, `TextField` and `SecureField`
constructor (bare or `SwiftUI.`-qualified) and each `SwiftUI.Link` (the app
declares its own model `struct Link`, so a bare `Link(` is that model and a
link control has to be written `SwiftUI.Link(`), skips its arguments and trailing closures by counting brackets
(after blanking comments and string literals), and checks that the modifier
chain that follows contains `.help(`. It is not a Swift parser: a tooltip
set on an enclosing view, or through a helper the chain calls, is not seen.
A deliberate exception carries `// no-tooltip: <reason>` on the
constructor's line.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests import swift_source  # noqa: E402

DIRECTORIES = (
    swift_source.APP_SOURCES / "Views",
    swift_source.APP_SOURCES / "Help",
)
CONTROLS = ("Button", "Toggle", "Picker", "Menu", "TextField", "SecureField")
# Controls only when written `SwiftUI.<name>`: the bare name is an app type.
QUALIFIED_CONTROLS = ("Link",)
NO_TOOLTIP = "// no-tooltip:"
# Constructors the real-source run inspected when this was written; fewer
# means the scan broke (or controls were removed: lower it then).
MIN_CONSTRUCTORS = 53

_CONSTRUCTOR_RE = re.compile(
    r"(?<![\w.])(?:SwiftUI\.)?("
    + "|".join(CONTROLS)
    + "|"
    + "|".join(rf"(?<=SwiftUI\.){name}" for name in QUALIFIED_CONTROLS)
    + r")\s*(?=[({])"
)


def untooltipped(source: str) -> tuple:
    """(constructors inspected, [(line number, control, line text)] of
    those whose modifier chain has no `.help(` and no `// no-tooltip:`)."""
    blanked = swift_source.blank(source)
    inspected = 0
    missing = []
    for match in _CONSTRUCTOR_RE.finditer(blanked):
        inspected += 1
        line = swift_source.line_of(source, match.start())
        if NO_TOOLTIP in line and line.split(NO_TOOLTIP, 1)[1].strip():
            continue
        modifiers, _ = swift_source.modifier_chain(blanked, match.end())
        if "help" in modifiers:
            continue
        number = source.count("\n", 0, match.start()) + 1
        missing.append((number, match.group(1), line.strip()))
    return inspected, missing


class TooltipScannerSnippetTests(unittest.TestCase):
    def test_button_without_help_is_flagged(self):
        inspected, missing = untooltipped('Button("x") {}\n')
        self.assertEqual(inspected, 1)
        self.assertEqual([m[1] for m in missing], ["Button"])

    def test_multiline_button_with_label_and_help_passes(self):
        source = (
            "Button {\n"
            "    run()\n"
            "} label: {\n"
            "    Image(systemName: \"x\")\n"
            "}\n"
            ".buttonStyle(.plain)\n"
            ".help(\"Do the thing\")\n"
        )
        self.assertEqual(untooltipped(source), (1, []))

    def test_no_tooltip_comment_exempts_the_constructor(self):
        source = 'Button("x") { run() } // no-tooltip: menu item\n'
        self.assertEqual(untooltipped(source), (1, []))

    def test_an_empty_no_tooltip_reason_does_not_count(self):
        source = 'Button("x") { run() } // no-tooltip:\n'
        self.assertEqual(len(untooltipped(source)[1]), 1)

    def test_toggle_with_help_after_other_modifiers_passes(self):
        source = (
            'Toggle("On", isOn: $on)\n'
            "    .disabled(!enabled)\n"
            "    .onChange(of: on) { _, value in apply(value) }\n"
            '    .help("Turn it on")\n'
        )
        self.assertEqual(untooltipped(source), (1, []))

    def test_help_on_a_later_sibling_does_not_count(self):
        source = (
            'Button("a") { }\n'
            'Text("b").help("not the button")\n'
        )
        self.assertEqual(len(untooltipped(source)[1]), 1)

    def test_names_in_strings_comments_and_other_identifiers_are_ignored(self):
        source = (
            '// Button("not real") {}\n'
            'Text("Press the Button(\\"x\\") now")\n'
            "SessionContextMenu(session: s)\n"
            "PRLinkButtons(session: s)\n"
            "func color(for link: Link) -> Color { .red }\n"
            "x.Button(1)\n"
        )
        self.assertEqual(untooltipped(source), (0, []))

    def test_swiftui_link_is_a_control_and_the_model_link_is_not(self):
        source = (
            'SwiftUI.Link("Open", destination: url)\n'
            'SwiftUI.Link("Docs", destination: url).help("Open the docs")\n'
            'let link = Link(kind: "pr", url: u)\n'
            'SwiftUI.Button("x") { }\n'
        )
        inspected, missing = untooltipped(source)
        self.assertEqual(inspected, 3)
        self.assertEqual([(m[0], m[1]) for m in missing], [(1, "Link"), (4, "Button")])

    def test_menu_and_picker_with_trailing_content(self):
        source = (
            "Menu {\n"
            '    Button("Quit") { quit() }\n'
            '        .help("Quit the app")\n'
            "} label: {\n"
            '    Image(systemName: "gearshape")\n'
            "}\n"
            ".menuStyle(.borderlessButton)\n"
            '.help("More")\n'
            'Picker("P", selection: $p) { ForEach(xs) { Text($0) } }\n'
        )
        inspected, missing = untooltipped(source)
        self.assertEqual(inspected, 3)
        self.assertEqual([m[1] for m in missing], ["Picker"])


class AppTooltipTests(unittest.TestCase):
    def test_every_control_has_a_tooltip(self):
        inspected = 0
        problems = []
        for path in swift_source.swift_files(*DIRECTORIES):
            count, missing = untooltipped(path.read_text())
            inspected += count
            relative = path.relative_to(swift_source.REPO)
            problems += [f"{relative}:{number}: {control} without .help: {line}"
                         for number, control, line in missing]
        self.assertGreaterEqual(inspected, MIN_CONSTRUCTORS,
                                "the tooltip scan found fewer controls than before")
        self.assertEqual(problems, [], "controls without a tooltip (add .help(\"what it does\") "
                                       "or // no-tooltip: <reason>)")


if __name__ == "__main__":
    unittest.main()

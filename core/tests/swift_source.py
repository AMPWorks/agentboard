"""Stdlib-only helpers for tests that read the app's Swift sources as text.

These are heuristics, not a Swift parser: comments and string literals are
blanked out (keeping every offset and newline in place) so brackets and
keywords inside them are never counted, and brackets are then matched by
counting. They are good enough for the shapes the app's SwiftUI code uses;
a test that relies on them also runs them on inline snippets.
"""

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
APP_SOURCES = REPO / "app" / "Sources" / "AgentBoard"

# Comments, multi-line strings, then single-line strings (escapes kept
# inside). Interpolations with nested quotes are not handled.
_BLANK_RE = re.compile(
    r'//[^\n]*'
    r'|/\*[\s\S]*?\*/'
    r'|"""[\s\S]*?"""'
    r'|"(?:\\.|[^"\\\n])*"'
)


def blank(source: str) -> str:
    """`source` with comments and string literals replaced by spaces
    (newlines kept), so offsets and line numbers still line up."""
    def spaces(match):
        return re.sub(r"[^\n]", " ", match.group(0))
    return _BLANK_RE.sub(spaces, source)


def line_of(source: str, offset: int) -> str:
    """The full original line containing `offset`."""
    start = source.rfind("\n", 0, offset) + 1
    end = source.find("\n", offset)
    return source[start:] if end == -1 else source[start:end]


def skip_balanced(text: str, index: int) -> int:
    """`text[index]` is an opening ( [ or {; the index just past its match
    (or len(text) if it never closes)."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    opener = text[index]
    closer = pairs[opener]
    depth = 0
    for position in range(index, len(text)):
        char = text[position]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return position + 1
    return len(text)


def skip_space(text: str, index: int) -> int:
    while index < len(text) and text[index].isspace():
        index += 1
    return index


_IDENT_RE = re.compile(r"[A-Za-z_]\w*")


def modifier_chain(text: str, index: int) -> tuple:
    """Starting just past a view constructor's name in blanked `text`: skip
    its argument list and trailing closures (`{ … }`, `label: { … }`), then
    collect the `.modifier(…)` names chained after it. Returns
    (modifier names, end index)."""
    index = skip_space(text, index)
    if index < len(text) and text[index] == "(":
        index = skip_balanced(text, index)
    # Trailing closures, the first unlabeled, later ones `label: { … }`.
    while True:
        probe = skip_space(text, index)
        if probe < len(text) and text[probe] == "{":
            index = skip_balanced(text, probe)
            continue
        match = _IDENT_RE.match(text, probe)
        if match:
            after = skip_space(text, match.end())
            if after < len(text) and text[after] == ":":
                brace = skip_space(text, after + 1)
                if brace < len(text) and text[brace] == "{":
                    index = skip_balanced(text, brace)
                    continue
        break
    names = []
    while True:
        probe = skip_space(text, index)
        if probe >= len(text) or text[probe] != ".":
            break
        match = _IDENT_RE.match(text, probe + 1)
        if not match:
            break
        names.append(match.group(0))
        index = match.end()
        probe = skip_space(text, index)
        if probe < len(text) and text[probe] == "(":
            index = skip_balanced(text, probe)
        while True:
            probe = skip_space(text, index)
            if probe < len(text) and text[probe] == "{":
                index = skip_balanced(text, probe)
            else:
                break
    return names, index


_DECL_RE = re.compile(r"\b(?:enum|struct|class|extension|actor)\s+([A-Za-z_]\w*)")
_NOT_TYPE_NAMES = {"var", "let", "func", "subscript", "init", "case"}


def enclosing_type(blanked: str, offset: int):
    """The name of the innermost `enum`/`struct`/`class`/`extension`/`actor`
    whose body contains `offset` in blanked source, or None."""
    decls = {}
    for match in _DECL_RE.finditer(blanked, 0, offset):
        if match.group(1) not in _NOT_TYPE_NAMES:
            decls[match.start()] = (match.end(), match.group(1))
    stack = []
    pending = None
    position = 0
    while position < offset:
        if position in decls:
            end, name = decls[position]
            pending = name
            position = end
            continue
        char = blanked[position]
        if char == "{":
            stack.append(pending)
            pending = None
        elif char == "}":
            if stack:
                stack.pop()
        position += 1
    for name in reversed(stack):
        if name:
            return name
    return None


def swift_files(*directories: Path):
    for directory in directories:
        yield from sorted(directory.rglob("*.swift"))

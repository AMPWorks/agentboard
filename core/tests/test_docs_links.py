"""The repo's markdown links, anchors and images stay valid.

Stdlib only. Over README.md, SPEC.md, CLAUDE.md, LOG.md and `docs/**/*.md`:

- every relative link, `<img src>` and `<source srcset>` names a file that
  exists, and a `#anchor` into a markdown file names one of its headings
  (GitHub's slug rules: lowercase, punctuation dropped, spaces to hyphens,
  `-1`, `-2` ... for repeats);
- every image under `docs/images/` is referenced from one of those files,
  is a PNG of at most 1 MB, and has its light/dark twin
  (`<name>-light.png` with `<name>-dark.png`).

A README deep link into docs/REFERENCE.md breaks silently when a heading is
renamed; this test is what notices. `PENDING_IMAGES` names screenshots the
README references before their commit lands; the commit that adds them
empties it.
"""

import re
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
IMAGES = REPO / "docs" / "images"
MAX_IMAGE_BYTES = 1024 * 1024
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_FENCE_RE = re.compile(r"^ *```.*?^ *```", re.S | re.M)
_CODE_SPAN_RE = re.compile(r"`[^`\n]*`")
_HEADING_RE = re.compile(r"#{1,6} (.+?)\s*#*\s*$")
_LINK_RE = re.compile(
    r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)"
    r"|<img[^>]*\bsrc=\"([^\"]+)\""
    r"|<source[^>]*\bsrcset=\"([^\"]+)\""
)
_TWIN_RE = re.compile(r"^(.+)-(light|dark)\.png$")

# Screenshots the README already references but whose commit has not landed
# yet. A missing file listed here is not a broken link; once a listed file
# exists, test_pending_images_are_still_missing fails until it is removed
# from this set, so the exception cannot outlive the images; and once no
# markdown file references a listed path, test_pending_images_are_still_referenced
# fails, so an entry cannot outlive its <picture> either.
PENDING_IMAGES = frozenset()


def markdown_files(repo: Path = REPO) -> list:
    files = [repo / "README.md", repo / "SPEC.md", repo / "CLAUDE.md", repo / "LOG.md"]
    files += sorted((repo / "docs").rglob("*.md"))
    return [path for path in files if path.exists()]


def slug(heading: str) -> str:
    """GitHub's anchor for a heading's text (before de-duplication)."""
    return re.sub(r"[^\w\- ]", "", heading.strip().lower()).replace(" ", "-")


def anchors(text: str) -> set:
    """Every heading anchor in a markdown text, outside code fences."""
    found, seen, fence = set(), {}, False
    for line in text.splitlines():
        if line.lstrip(" ").startswith("```"):
            fence = not fence
            continue
        match = None if fence else _HEADING_RE.match(line)
        if not match:
            continue
        base = slug(match.group(1))
        count = seen.get(base, 0)
        seen[base] = count + 1
        found.add(base if count == 0 else f"{base}-{count}")
    return found


def link_targets(text: str) -> list:
    """Link, image and srcset targets outside code fences and code spans."""
    text = _CODE_SPAN_RE.sub("", _FENCE_RE.sub("", text))
    return [next(group for group in match.groups() if group) for match in _LINK_RE.finditer(text)]


def broken_links(files: list, repo: Path = REPO, pending=frozenset()) -> list:
    """Problems with the links in `files`; a missing file whose repo-relative
    path is in `pending` is not one."""
    problems, anchor_cache = [], {}
    for path in files:
        for target in link_targets(path.read_text()):
            if re.match(r"[a-z][a-z0-9+.-]*:", target):  # https:, mailto:, ...
                continue
            file_part, _, anchor = target.partition("#")
            dest = (path.parent / file_part).resolve() if file_part else path
            where = path.relative_to(repo)
            if not dest.exists():
                if _repo_relative(dest, repo) not in pending:
                    problems.append(f"{where}: missing file {target}")
            elif anchor and dest.suffix == ".md":
                if dest not in anchor_cache:
                    anchor_cache[dest] = anchors(dest.read_text())
                if anchor not in anchor_cache[dest]:
                    problems.append(f"{where}: missing anchor {target}")
    return problems


def _repo_relative(path: Path, repo: Path) -> str:
    try:
        return path.relative_to(repo.resolve()).as_posix()
    except ValueError:
        return str(path)


def image_problems(images: Path, referenced: set) -> list:
    if not images.is_dir():
        return []
    problems = []
    names = {path.name for path in images.iterdir() if path.is_file()}
    for path in sorted(images.iterdir()):
        if not path.is_file():
            continue
        name = path.name
        if path.resolve() not in referenced:
            problems.append(f"{name}: not referenced from any markdown file")
        if path.suffix != ".png" or path.read_bytes()[:8] != PNG_SIGNATURE:
            problems.append(f"{name}: not a PNG")
        if path.stat().st_size > MAX_IMAGE_BYTES:
            problems.append(f"{name}: {path.stat().st_size} bytes, over 1 MB")
        twin = _TWIN_RE.match(name)
        if not twin:
            problems.append(f"{name}: not named <name>-light.png or <name>-dark.png")
        else:
            other = "dark" if twin.group(2) == "light" else "light"
            if f"{twin.group(1)}-{other}.png" not in names:
                problems.append(f"{name}: no {twin.group(1)}-{other}.png twin")
    return problems


def referenced_files(files: list) -> set:
    refs = set()
    for path in files:
        for target in link_targets(path.read_text()):
            file_part = target.partition("#")[0]
            if file_part and not re.match(r"[a-z][a-z0-9+.-]*:", target):
                refs.add((path.parent / file_part).resolve())
    return refs


class SnippetTests(unittest.TestCase):
    def test_slugs_follow_github(self):
        self.assertEqual(slug("Links & integrations"), "links--integrations")
        self.assertEqual(slug("Configuration (config.json)"), "configuration-configjson")
        self.assertEqual(slug("Install / uninstall"), "install--uninstall")
        self.assertEqual(slug("Updates (retained notes)"), "updates-retained-notes")

    def test_repeated_headings_get_numbered_anchors(self):
        self.assertEqual(anchors("# A\n## Notes\n### Notes\n"), {"a", "notes", "notes-1"})

    def test_headings_and_links_in_code_are_ignored(self):
        text = "# Real\n```\n# not a heading\n[x](missing.md)\n```\nSee `[y](gone.md)`.\n"
        self.assertEqual(anchors(text), {"real"})
        self.assertEqual(link_targets(text), [])

    def test_pending_files_are_not_broken_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp).resolve()
            doc = repo / "README.md"
            doc.write_text('<img src="docs/images/a-light.png" alt="A">\n[b](docs/b.md)\n')
            self.assertEqual(broken_links([doc], repo, pending={"docs/images/a-light.png"}),
                             ["README.md: missing file docs/b.md"])
            self.assertEqual(len(broken_links([doc], repo)), 2)

    def test_picture_sources_and_images_are_targets(self):
        text = ('<picture>\n  <source media="(prefers-color-scheme: dark)" srcset="a-dark.png">\n'
                '  <img src="a-light.png" alt="A">\n</picture>\n[ref](docs/R.md#x "title")\n')
        self.assertEqual(sorted(link_targets(text)), ["a-dark.png", "a-light.png", "docs/R.md#x"])


class ImageRuleTests(unittest.TestCase):
    def test_image_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            images = Path(tmp)
            png = PNG_SIGNATURE + b"\0" * 16
            (images / "ok-light.png").write_bytes(png)
            (images / "ok-dark.png").write_bytes(png)
            (images / "lonely-light.png").write_bytes(png)
            (images / "fake-light.png").write_bytes(b"GIF89a")
            (images / "fake-dark.png").write_bytes(png)
            (images / "big-light.png").write_bytes(png + b"\0" * MAX_IMAGE_BYTES)
            (images / "big-dark.png").write_bytes(png)
            used = {(images / name).resolve() for name in
                    ("ok-light.png", "ok-dark.png", "lonely-light.png", "fake-light.png",
                     "fake-dark.png", "big-light.png")}
            problems = image_problems(images, used)
        self.assertEqual(problems, [
            "big-dark.png: not referenced from any markdown file",
            f"big-light.png: {len(png) + MAX_IMAGE_BYTES} bytes, over 1 MB",
            "fake-light.png: not a PNG",
            "lonely-light.png: no lonely-dark.png twin",
        ])


class RepoDocsTests(unittest.TestCase):
    def test_the_docs_exist(self):
        names = {path.relative_to(REPO).as_posix() for path in markdown_files()}
        self.assertLessEqual({"README.md", "docs/REFERENCE.md", "docs/DEVELOPMENT.md", "LOG.md"}, names)

    def test_no_broken_links_anchors_or_images(self):
        self.assertEqual(broken_links(markdown_files(), pending=PENDING_IMAGES), [])

    def test_pending_images_are_still_missing(self):
        landed = sorted(path for path in PENDING_IMAGES if (REPO / path).exists())
        self.assertEqual(landed, [], "these images exist now: remove them from PENDING_IMAGES")

    def test_pending_images_are_still_referenced(self):
        referenced = referenced_files(markdown_files())
        stale = sorted(path for path in PENDING_IMAGES if (REPO / path).resolve() not in referenced)
        self.assertEqual(stale, [], "no markdown file references these: remove them from PENDING_IMAGES")

    def test_every_image_is_used_small_and_has_its_twin(self):
        self.assertEqual(image_problems(IMAGES, referenced_files(markdown_files())), [])


if __name__ == "__main__":
    unittest.main()

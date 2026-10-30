# Developing AgentBoard

For contributors: how the repository is laid out and how to run the tests. What AgentBoard does,
in full detail, is in [REFERENCE.md](REFERENCE.md); [README.md](../README.md) is the overview for
users.

- [Components](#components)
- [Testing](#testing)
- [Development log and agent guide](#development-log-and-agent-guide)

## Components

```
agentboard/
  core/                     Python 3 (stdlib only, >=3.11) — this is what most of
                             docs/REFERENCE.md documents.
    agentboard/             the package: db, hook, reconcile, backfill, links, cli
    bin/agentboard          executable CLI entry point
    tests/                  unittest suite — python3 -m unittest discover core/tests
  app/                      SwiftUI menu bar app (macOS 14+), built by scripts/build-app.sh
  scripts/                  install.sh, uninstall.sh, build-app.sh, swift-test.sh
```

## Testing

```
python3 -m unittest discover core/tests
```

The suite needs Python `>= 3.11` too; if your `python3` is older, name one explicitly
(`python3.12 -m unittest discover core/tests`) -- under an old interpreter the test package
refuses to import with a message saying so, rather than failing every test the same way.

Every test runs behind a temporary `AGENTBOARD_HOME` and `HOME` (see `core/tests/helpers.py`) —
none of them ever read or write the real `~/Library/Application Support/AgentBoard`,
`~/.claude/settings.json`, or `~/.codex/hooks.json`. Tests that run `core/bin/agentboard` or the
scripts as a subprocess also put a `python3` stub on `PATH` that execs the suite's own
interpreter, so the shim's `#!/usr/bin/env python3` can't drift to a different one.

The app has a Swift Testing target (`app/Tests/AgentBoardTests`; Swift Testing because the
Command Line Tools ship no XCTest). Run it with:

```
scripts/swift-test.sh                       # quiet: one summary line, or the failures + log path
scripts/swift-test.sh --verbose             # stream everything (or AGENTBOARD_VERBOSE=1)
scripts/swift-test.sh -- --filter HelpTests # anything after -- goes to swift test
```

The script runs `swift test` in `app/` and adds only what this toolchain needs, deciding by what
is on disk:

- **Testing.framework.** With only the Command Line Tools, SwiftPM leaves their
  Testing.framework off the search paths and plain `swift test` silently runs zero tests. When
  `$(xcode-select -p)/Library/Developer/Frameworks/Testing.framework` exists, the script passes
  that directory with `-Xswiftc -F` and `-Xlinker -rpath`. With Xcode selected the directory
  doesn't exist and nothing is added.
- **The TestingMacros plugin.** Some Command Line Tools releases keep it in
  `usr/lib/swift/host/plugins/testing/`, which the compiler doesn't search. When the plugin is
  there and not directly in `host/plugins/`, the script adds `-Xswiftc -plugin-path` for it,
  derived from the location of `xcrun --find swiftc`.
- **The macOS SDK.** The same SwiftUI-macro check as `build-app.sh` (see
  [Building the app](REFERENCE.md#building-the-app)): when the default SDK can't expand
  `@State`/`@AppStorage` with this toolchain, it builds against the newest older SDK that can,
  and prints one `note:` line on stderr.

A run that passes but executes no Swift Testing test counts as a failure, so a missing framework
(or a `--filter` that matches nothing) can't pass silently; only `-- --list-tests` (or `-l`)
skips that check. The full output always goes to `app/build/swift-test.log` (gitignored).

Without the script, on a Command Line Tools-only machine, the equivalent is:

```
cd app
F=/Library/Developer/CommandLineTools/Library/Developer/Frameworks
swift test -Xswiftc -F -Xswiftc $F -Xlinker -rpath -Xlinker $F
```

plus `-Xswiftc -plugin-path -Xswiftc /Library/Developer/CommandLineTools/usr/lib/swift/host/plugins/testing`
where the plugin sits in that subdirectory, and `SDKROOT=<an older SDK>` where the default SDK
can't expand SwiftUI macros.

`app/Fixtures/` holds snapshots for running the app without the CLI:
`AGENTBOARD_FIXTURE_PATH=app/Fixtures/snapshot-needs-you.json app/.build/release/AgentBoard`
(one session needing you), `snapshot-quiet.json` (only done/snoozed), or `snapshot.json` (a bit of
everything; also what `AGENTBOARD_USE_FIXTURE=1` loads from the app bundle).

### Regenerating the screenshots

The README's images live in `docs/images/`: `<name>-light.png` and `<name>-dark.png` for
`popover`, `dashboard`, `session-row`, `settings` and `menu-bar-icon`. Each light/dark pair has
the same pixel size, the long side is at most 1600 px, and each file stays under 1 MB (aim for
400 KB). The README shows each pair with a `<picture>` element, the dark file as a
`prefers-color-scheme: dark` source and the light one as the fallback `<img>`, whose `width` is
half the pixel width so it stays sharp on Retina screens. `core/tests/test_docs_links.py` checks
that every image is referenced, is a PNG of at most 1 MB and has its twin.

Capture them from the fixture, never from a real board, so no real project, path, account or
token can appear:

1. Build the app (`scripts/build-app.sh --no-install`) and quit the installed AgentBoard (gear
   menu → Quit AgentBoard): both would register the ⌃⌥⌘A hotkey and show a menu bar icon.
2. The fixture's timestamps are fixed, so rows would read "2w ago". Write a copy shifted to now:

   ```
   python3.12 -I -c '
   import datetime as dt, json, re, sys, pathlib
   src, out = map(pathlib.Path, sys.argv[1:3]); text = src.read_text()
   p = lambda s: dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
   delta = dt.datetime.now(dt.UTC).replace(tzinfo=None, microsecond=0) - p(json.loads(text)["generated_at"][:19])
   out.write_text(re.sub(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z",
       lambda m: (p(m.group(1)) + delta).strftime("%Y-%m-%dT%H:%M:%S") + (m.group(2) or "") + "Z", text))
   ' app/Fixtures/snapshot.json "$TMPDIR/snapshot-now.json"
   ```

3. Note the defaults domains (`defaults domains | tr ',' '\n' | sed 's/^ //' | sort`), then launch
   the bare binary against an empty data directory:
   `AGENTBOARD_HOME=$(mktemp -d) AGENTBOARD_FIXTURE_PATH="$TMPDIR/snapshot-now.json" app/.build/release/AgentBoard`.
   It has no bundle id, so its settings go to a defaults domain of its own, and fixture mode posts
   no notifications and runs no actions.
4. In its Settings leave Appearance on **System**, turn on General → **Show closed and merged
   PRs** (so a row shows colored PR buttons), and check that the CLI & Data fields are empty.
5. Take two passes, one with System Settings → Appearance on **Light** and one on **Dark**. In
   each, capture the popover, the Settings window and the dashboard (⌃⌥⌘A, with the "Fix missing
   counter switch case" row selected) with ⌘⇧4, then Space, then a click on the window, plus a
   generous ⌘⇧4 rectangle around the menu bar icon.
6. Crop the menu bar icon to the status item alone and the session row out of the dashboard
   capture (`sips --cropOffset`), downscale with `sips -Z 1600` or less, and name the files
   `<name>-light.png` / `<name>-dark.png`.
7. Look at every image, and OCR it (Vision's `VNRecognizeTextRequest`): every project, branch and
   title must come from the fixture (widget-service, billing-api, ...), with no other menu bar
   item, clock, window title, avatar or wallpaper.
8. Quit the fixture app, delete only the new defaults domain it created (never
   `io.github.agentboard` or any domain that existed before), remove the temporary data
   directory, restore your appearance setting and relaunch AgentBoard
   (`open ~/Applications/AgentBoard.app`).

## Development log and agent guide

- [`LOG.md`](../LOG.md) is the development log: a timestamped narrative of the decisions,
  milestones and trade-offs behind AgentBoard, oldest first. Every task appends its entry in
  the same PR as the work.
- [`CLAUDE.md`](../CLAUDE.md) is the guide for agents (and people) changing the code: commands,
  test isolation rules, architecture and conventions.
- [`REFERENCE.md`](REFERENCE.md) documents behavior: hooks, state rules, the snapshot fields,
  `config.json`, the CLI and diagnostics. A behavior change updates it.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

AgentBoard is a local-only macOS menu bar app plus a Python CLI that shows the state of every
Claude Code and Codex CLI session on the Mac. The two halves share one sqlite database and one
JSON contract:

- `core/` is stdlib-only Python (>= 3.11). Hooks write events; `agentboard snapshot --json`
  derives each session's state.
- `app/` is a SwiftPM/SwiftUI menu bar app (macOS 14+, Swift 6 language mode, no dependencies).
  It never opens the database; it shells out to the CLI for everything.
- `scripts/` holds `install.sh`, `uninstall.sh`, `build-app.sh`, `swift-test.sh`. docs/REFERENCE.md documents their
  behavior in depth; SPEC.md is the original design contract (snapshot JSON shape, state rules).

## Commands

Python >= 3.11 is required (tomllib, `X | None` annotations evaluated at import). If `python3`
on PATH is older, substitute an explicit interpreter such as `python3.12` below.

```
python3 -m unittest discover core/tests                        # full suite, ~30s
python3 -m unittest core.tests.test_reconcile                   # one module
python3 -m unittest core.tests.test_cli.SnapshotTests.test_name # one test
core/bin/agentboard snapshot                                    # CLI from the checkout
AGENTBOARD_HOME=/tmp/ab core/bin/agentboard backfill --days 7   # against a throwaway data dir

scripts/swift-test.sh                                           # app tests (Swift Testing), quiet; -v for full output
scripts/build-app.sh --no-install                               # compile + assemble app/build/AgentBoard.app (also leaves app/.build/release/AgentBoard)
AGENTBOARD_FIXTURE_PATH=app/Fixtures/snapshot.json app/.build/release/AgentBoard  # app on the sample fixture, no CLI
for f in scripts/*.sh; do bash -n "$f"; done                    # syntax-check every script (bash -n a b checks only a)
scripts/install.sh --dry-run                                    # preview against the real ~/.claude and ~/.codex
```

Use the wrappers, not bare `swift build`/`swift test`: with only the Command Line Tools, plain
`swift test` runs zero tests, and some releases need the Testing plugin path or an older SDK.
Both are quiet (logs in `app/build/`; `--verbose`/`AGENTBOARD_VERBOSE=1` for everything);
`AGENTBOARD_SDKROOT=default|<sdk dir>` skips or forces the SDK fallback. Details:
docs/DEVELOPMENT.md#testing, docs/REFERENCE.md#building-the-app. The app tests live in
`app/Tests/AgentBoardTests` and use Swift Testing, not XCTest.

There is no linter configured. `scripts/install.sh` runs the full install (core copy, app build,
hooks, backfill) and relaunches the app; `--no-app` skips the Swift build.

## Test isolation rules

`core/tests/helpers.py` (`AgentBoardTestCase`) points `HOME` and `AGENTBOARD_HOME` at temp dirs
and scrubs session env vars. A test that runs the shim (`core/bin/agentboard`) or a script as a
subprocess must pass an env whose `HOME` (and normally `AGENTBOARD_HOME`) is a temp dir, plus a
`python3` stub on PATH that execs `sys.executable` (see `_python3_stub_env` in
`test_install_script.py` and `test_cli.py`); the shim's `#!/usr/bin/env python3` otherwise
resolves to whatever the machine's PATH has first. Nothing in the suite may touch
`~/Library/Application Support/AgentBoard`, `~/.claude`, or `~/.codex`.

The shell-script tests extract bash functions by name with `sed -n '/^name() {/,/^}/p'`, so keep
that exact definition shape (`name() {` on its own line, closing `}` in column 0) in the scripts.

## Architecture

**Data flow.** Claude/Codex run `agentboard hook --tool X` on every lifecycle event. `hook.py`
appends to `events` and upserts `sessions`. `reconcile.compute_state` turns a session row plus its
triage and links into exactly one of `done`, `snoozed`, `needs_you`, `interrupted`, `review`,
`running`, `unconcluded` (checked in that priority order). `cli.build_snapshot` emits the JSON that
`app/.../Models/Snapshot.swift` decodes. `backfill.py` fills the same tables from transcripts on
disk and never overwrites hook-written fields, except `todos_open` and known-bad legacy values.

**The hook contract** (`hook.py`, `core/bin/agentboard`): print nothing to stdout, always exit 0,
log failures to `$AGENTBOARD_HOME/hook-errors.log`. The shim's `hook` subcommand takes a fast path
that skips argparse and fails soft on Python < 3.11 before importing the package. Only
`git branch --show-current` and a bounded `ps` walk ever shell out. Subagent-tagged events
(`agent_id` set) never change a session's `last_event`.

**Installed layout vs. checkout.** Hooks never point at the repo. `install.sh` stages `core/` into
`~/.local/share/agentboard/core.d/<version>/`, atomically repoints the `core` symlink, pins the
shim's shebang to the validated interpreter, and bakes a non-default `AGENTBOARD_HOME` into it.
`build-app.sh` bundles another copy at `AgentBoard.app/Contents/Resources/core`. An edit under
`core/` reaches hooks and the app only after `scripts/install.sh` runs again. `cli._bin_path()`
deliberately does not resolve symlinks: the hook command string registered in
`settings.json`/`hooks.json` must stay byte-identical across reinstalls.

**App.** `SnapshotStore` (MainActor) owns polling (3s while a popover/window is visible, 15s
otherwise), a directory watcher on the dir reported by `agentboard paths --json`, and background
`backfill`/`refresh-links`. `AgentBoardCLI` locates the CLI as bundled copy, then PATH, then the
`agentboardPath` default, and has a fixture mode (`AGENTBOARD_USE_FIXTURE=1` or
`AGENTBOARD_FIXTURE_PATH`). Views call the store, never the CLI.

**Integrations are generic only** (`links.py`): `agentboard link`/`status`, the `inbox/` drop
directory, provider commands from `config.json`, and `gh pr list`. Nothing team- or host-specific
may be hard-coded. `snapshot` must never touch the network; only `refresh-links` may.

## Conventions

- Schema changes: add a numbered entry to `_MIGRATIONS` in `db.py` and bump `SCHEMA_VERSION`.
  Never edit an existing migration.
- A new hook event name goes in `CLAUDE_EVENTS`/`CODEX_EVENTS` in `cli.py` (what `install-hooks`
  registers) and must be classified in `hook.py` (`ACTIVITY_EVENTS` etc.) and `reconcile.py`. It
  stays synchronous unless added to `hook.ASYNC_EVENTS` for its tool; an event that any query
  orders or picks by `events.id` (`ORDER BY id`, `MAX(id)`, `id < ?`) must stay synchronous.
  Today those readers are `links.last_parent_prompt_at`/`_times`, `newest_parent_prompts`,
  `last_parent_stop_times`, `background_wait_before_newest_prompt`, `mid_turn_checker`, the
  replay in `notes.rebuild` and `titles.build_context`. Async hooks are not killed at `timeout`
  by Claude Code, so every blocking step in the hook must stay self-bounded. An event's `ts` is
  the hook's start time (`_HOOK_STARTED` in the shim), and `hook._is_stale` ignores a late async
  commit for state.
- Any new file AgentBoard writes directly under `$AGENTBOARD_HOME` must be listed in
  `AGENTBOARD_DATA_FILES`/`AGENTBOARD_DATA_DIRS` in `uninstall.sh`, or `--purge-data` refuses
  (`agentboard.log` and `agentboard.log.1` are already there).
- Diagnostics go through `core/agentboard/diag.py` (`diag.info("component", "event", key=...)`),
  which writes `$AGENTBOARD_HOME/agentboard.log`, caps it at 2 MB with one rotation to `.1`, and
  never raises. Never add a bare `print` for diagnostics: a CLI error that goes to stderr uses
  `cli._report()`, which prints and logs the same text. `snapshot`/`paths` must stay quiet at
  `info` (debug lines only). `hook.py` imports `diag` lazily inside `_diag()`, never at module
  top. App-side logging uses `OSLog` (subsystem `io.github.agentboard`) with `privacy: .public`.
- `install.sh`, `uninstall.sh`, `build-app.sh` and `swift-test.sh` deliberately duplicate their
  helper functions (`normalize_path`, `safe_remove_in_store`, `find_python3`, ...; the SDK
  helpers only in the last two, kept identical by a test) so each works standalone. Change every
  copy together; the tests extract them by name from each script.
- A new `config.json` key goes in `core/agentboard/config_keys.py`, docs/REFERENCE.md's
  "Configuration (config.json)" table and `app/Sources/AgentBoard/Help/HelpContent.swift`; a new
  CLI subcommand or option goes in HelpContent's CLI reference and docs/REFERENCE.md; a new
  Settings key goes in `SettingsKeys.all` and the Settings help topic.
  `core/tests/test_help_coverage.py` enforces all three.
- Every new control (`Button`, `Toggle`, `Picker`, `Menu`, `TextField`, `SecureField`, `SwiftUI.Link`)
  gets a `.help("what it does")` tooltip, or `// no-tooltip: <reason>` on its line;
  `core/tests/test_app_tooltips.py` enforces it.
- Snapshot JSON: adding fields is safe (the Swift model decodes leniently and defaults missing
  ones). Removing or renaming a field breaks the app.
- Docs: README.md is the user overview, the reference is docs/REFERENCE.md and testing is
  docs/DEVELOPMENT.md. Each task appends a `LOG.md` entry in its PR (format at the top of LOG.md).

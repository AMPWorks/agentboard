# AgentBoard

A local-only macOS menu bar dashboard showing the state of every Claude Code and Codex CLI
session on this Mac: what needs you, what has output to review, what is running, and what
older work is still unconcluded.

Everything lives on your Mac. There is no server, no account, and no telemetry.

## Install / uninstall

All setup is done by the scripts in `scripts/`; nothing here should be hand-edited.

```
scripts/install.sh [--no-hooks] [--no-app] [--dry-run] [--prefix DIR] [--enable-codex-hooks]
scripts/uninstall.sh [--no-hooks] [--no-app] [--purge-data] [--dry-run] [--prefix DIR]
```

`install.sh` checks prerequisites (`python3` >= 3.11, and Xcode Command Line Tools / `swift`
unless `--no-app`), builds and installs the menu bar app via `scripts/build-app.sh`, installs
hooks for whichever of Claude Code / Codex are present on this machine, and imports the last 30
days of session history. It is idempotent — re-run it any time to pick up an upgrade, including
after installing Claude Code or Codex *for the first time*: both are detected via `~/.claude` /
`~/.codex` or `claude`/`codex` on `PATH`, so a tool that didn't exist yet at install time gets no
hooks until you re-run `install.sh`. Pass `--dry-run` first to see exactly what would change
(including a diff of `~/.claude/settings.json` and `~/.codex/hooks.json`) before it touches
anything.

Xcode Command Line Tools / `swift` are optional, not a hard requirement: if either is missing,
`install.sh` prints a warning, skips the app build and install step on its own (you don't need to
pass `--no-app` yourself), and still exits 0 — hooks and backfill are installed normally, you just
get no menu bar app. Install Xcode Command Line Tools and re-run `install.sh` later to add it.

`install.sh` looks for an interpreter in this order: `AGENTBOARD_PYTHON3` (an explicit path, if
set), then `python3`, then `python3.14`, `python3.13`, `python3.12`, `python3.11` on `PATH`, taking
the first that is `>= 3.11` -- so a Mac whose bare `python3` is an older system or Homebrew
build still installs fine as long as a newer versioned one is present (`brew install
python@3.12`). Whichever it picks is the one install.sh (and `build-app.sh`) pin into
the installed/bundled `bin/agentboard` copies' shebang line, by absolute path. This matters
because `/usr/bin/env python3` — what the repo checkout itself uses — re-resolves `PATH` every
time it runs, so a hook launched by Claude/Codex, or the app launched at login, can land on
whatever `python3` is first on *that* process's `PATH` (commonly macOS's own `/usr/bin/python3`,
which is too old). Pinning the exact interpreter at install time avoids that; if a hook ever is
invoked under an interpreter older than 3.11 anyway, it fails soft — it logs to
`hook-errors.log` and exits 0 rather than crashing. The hook *command string* registered in
`settings.json`/`hooks.json` never changes because of this, so Codex users never need to re-trust
hooks over an interpreter change alone.

If AgentBoard's app is already running from the same location `install.sh` is about to install
to, `install.sh` quits it only after the new build succeeds (a failed build leaves the running
app alone) and relaunches the new one once installed. Every `AgentBoard` process on the machine
is checked (not just the first one `pgrep` happens to return) and matched by its actual running
executable path against the target bundle; only a match is sent `SIGTERM` directly (never
`osascript ... quit app "AgentBoard"`, which addresses by name and would just as happily quit a
copy running from somewhere else), with up to 5 seconds to exit before a `SIGKILL` fallback. An
app running from somewhere else (a different `--prefix`) is left untouched. `--prefix` itself is
normalized before any of this (made absolute, trailing slash stripped, symlinks resolved) so a
relative path, a trailing slash, or a symlinked ancestor all compare equal — `uninstall.sh --prefix
DIR` applies the exact same quit logic before removing the bundle.

Codex additionally requires `[features]` / `hooks = true` in `~/.codex/config.toml` before it
will act on `~/.codex/hooks.json` at all — `install.sh` never edits that file on its own (it's
your config, same as the rest of `config.toml`), but it reads it and prints a prominent warning
with the exact snippet to add whenever the flag isn't set. Pass `--enable-codex-hooks` to have
`install.sh` set it for you: this backs up `config.toml` first, then makes the smallest possible
textual edit (add `hooks = true` under an existing `[features]` section, or append a new one) —
it never rewrites the file through a TOML parser/serializer, so every other byte, including
comments, is preserved. Either way, the first time hooks are registered (and on every later
`install.sh` re-run while they're still present) you'll see a reminder to run `/hooks` inside
Codex to trust them — Codex won't act on a hook it hasn't been told to trust, same as Claude Code.

`uninstall.sh` removes whatever `install.sh` added, and documents what it restores versus leaves
in place:

- Only the hook entries it registered itself are removed from `settings.json`/`hooks.json` — any
  other hooks sharing the same event, including ones in the exact same matcher group, are left
  alone. If install-hooks' change was the *only* change ever made to the file (nothing else has
  touched it since), the file is restored byte-for-byte from the backup `install-hooks` made,
  rather than re-serialized — so hand formatting, key order, and any non-ASCII content come back
  exactly as they were. If `install.sh` was the one
  that created the file (there was none before), and removing AgentBoard's entries leaves nothing
  else in it, `uninstall.sh` deletes the file instead of leaving behind an empty `{"hooks": {}}`
  that never existed before; that bookkeeping lives in a small install manifest under
  `$AGENTBOARD_HOME`.
- The installed app, the `core` symlink, and its entire version store (`core.d/`) are removed
  (see the core copy layout below) — but only if `core` still points into a store `uninstall.sh`
  recognizes as its own; otherwise it refuses and leaves both alone (see below).
- `--enable-codex-hooks`' edit to `~/.codex/config.toml` (`[features]` / `hooks = true`) is **not**
  reversed by `uninstall.sh` — only its own backup file (`config.toml.agentboard-bak-<timestamp>`)
  is left beside it. That flag is a Codex-wide config choice, not something `uninstall.sh` should
  silently undo; restore the backup yourself if you want it reverted.
- The data directory is left in place unless you pass `--purge-data`.

Both scripts resolve all paths relative to the repository they live in — there's nothing to edit
for a different checkout location or a different Mac.

Hooks never run against this repo checkout directly. `install.sh` copies `core/` into a stable
location (default `~/.local/share/agentboard/core`, overridable via `AGENTBOARD_CORE_DIR`) and
that copy is what hooks, `install-hooks`, and `backfill` actually run from — so a code edit here
only takes effect once you re-run `scripts/install.sh`. This is separate from `$AGENTBOARD_HOME`
(the data dir, see below); the default avoids the space in "Application Support" because hook
commands run through a shell.

That installed `core` path is a **symlink**, not a plain directory. Every version it has ever
pointed at lives in one dedicated sibling directory, `core.d/` (i.e. `<INSTALLED_CORE_DIR>.d` —
`~/.local/share/agentboard/core.d` by default), never loose next to `core` itself: each version is
`core.d/<UTC timestamp>-<short id>/`, and `core.d/` itself carries a marker file
(`.agentboard-core-store`) identifying it as AgentBoard's own. Scoping versions to a store named
after the exact link they belong to, rather than a shared parent directory, is what lets two
installs coexist safely — a custom `AGENTBOARD_CORE_DIR` right next to the default one, or two
machines' worth of installs under a shared parent — since each install's versions can never be
confused with (or accidentally deleted by) another's.

Re-running `install.sh` stages a fresh version inside `core.d/`, then atomically repoints `core`
at it (`mv -fh`, which replaces the symlink itself without ever following it, so `core` is never
briefly missing or half-written), and only then prunes every other version out of `core.d/`. That
prune step (also run by an EXIT trap on any failure or signal) always re-reads where `core`
*currently* points before deleting anything, rather than trusting a value computed earlier in the
run — so even a `SIGTERM`/`SIGHUP` landing right as the swap completes can't make it delete the
version `core` now points at. A hook firing mid-upgrade — on a busy machine, or a re-run that
races a live session — always sees either the complete old version or the complete new one, never
a partial one; the hook *command string* registered in `settings.json`/`hooks.json` is unaffected
either way, since it always names the stable `core` symlink path, never a specific version
directory.

Before touching an existing `core`, `install.sh` checks what it already is: missing (a fresh
install), or a symlink into its own marked `core.d/` (an ordinary upgrade). Anything else — a real
directory (including a source checkout's own `core/`), a symlink `install.sh` doesn't recognize
(such as a developer's own symlink to a live checkout), or an unmarked or symlinked `core.d/` —
makes it refuse outright, printing why and changing nothing. `uninstall.sh` applies the identical
check before removing anything: it only ever removes `core` and `core.d/` together, and only when
`core` points into its own marked store.

Installs from before this versioning scheme (anything with a real directory, rather than a
symlink, at `core`) aren't migrated automatically — move the existing one aside once, e.g.:

```
mv ~/.local/share/agentboard/core ~/.local/share/agentboard/core.old
```

and re-run `install.sh` (or set `AGENTBOARD_CORE_DIR` to a new location instead).

## Components

```
agentboard/
  core/                     Python 3 (stdlib only, >=3.11) — this is what the rest of this
                             README documents.
    agentboard/             the package: db, hook, reconcile, backfill, links, cli
    bin/agentboard          executable CLI entry point
    tests/                  unittest suite — python3 -m unittest discover core/tests
  app/                      SwiftUI menu bar app (macOS 14+), built by scripts/build-app.sh
  scripts/                  install.sh, uninstall.sh, build-app.sh
```

## Data

Everything AgentBoard knows lives in one sqlite database:

```
~/Library/Application Support/AgentBoard/agentboard.db
```

Override the whole data directory with the `AGENTBOARD_HOME` environment variable (tests always
do this, pointed at a throwaway temp directory — nothing in this project ever touches the real
path above during a test run).

The schema (see `core/agentboard/db.py`) has four tables:

- `events` — an append-only log of every hook event ever received, per `(tool, session_id)`.
- `sessions` — one row per session: cwd, branch, title, prompts, last activity, liveness
  bookkeeping (pid, terminal info), and a free-form `status_text`/`status_until` set via
  `agentboard status`.
- `triage` — the user's own done/snoozed/open decision for a session.
- `links` — PRs, tickets, or arbitrary URLs associated with a session, from whatever source
  reported them (see Links below).

Migrations are tracked with `PRAGMA user_version`; `db.connect()` applies any pending ones.

Next to the database live `agentboard.log` and `agentboard.log.1`, the diagnostic log (see
Diagnostics below), and `hook-errors.log`, the hook's own failure log.

## Hooks

`agentboard hook --tool claude|codex` is registered for every lifecycle event in both tools. It
reads one JSON payload from stdin, records it, and **always exits 0 and prints nothing to
stdout** — Claude Code folds a hook's stdout into the model's context for `SessionStart` /
`UserPromptSubmit`, so any output there would leak into the conversation. Every exception is
caught and logged to `$AGENTBOARD_HOME/hook-errors.log` (capped at ~1 MB) instead of surfacing.

It targets well under 50 ms per call. The only things that ever shell out are:

- `git -C <cwd> branch --show-current`, and only on `SessionStart` / `UserPromptSubmit`, or the
  first time a session's branch is still unknown.
- A short, targeted `ps` walk (at most 3 hops) to find the actual `claude`/`codex` process above
  the hook's immediate parent, recorded once per session the same way the branch is.

Install/remove the hooks with:

```
agentboard install-hooks [--claude] [--codex] [--dry-run]
agentboard uninstall-hooks [--claude] [--codex] [--dry-run]
agentboard enable-codex-hooks [--dry-run]
```

Both `install-hooks`/`uninstall-hooks` back up the target file first
(`<file>.agentboard-bak-<timestamp>`), append (or remove) one hook group per event, and are
idempotent — re-running detects the existing AgentBoard entry and leaves everything else (other
hooks, other settings keys, their ordering) untouched. If a target file isn't valid JSON,
install/uninstall prints an error naming that file, skips just that tool, and still processes the
other tool and the backfill step — the overall run exits non-zero at the end so the problem isn't
silently missed. `enable-codex-hooks` is the opt-in that sets Codex's `[features]` / `hooks = true`
in `~/.codex/config.toml` (see Install/uninstall above); it's normally invoked via
`install.sh --enable-codex-hooks` rather than directly.

## State

`agentboard snapshot [--json] [--all]` is the one thing the Swift app (and anything else) needs
to poll. It never touches the network and runs in well under 500 ms even with a few thousand
sessions. Each session is assigned exactly one `state`, in priority order:

`done` → `snoozed` → `needs_you` → `interrupted` → `review` → `running` → `unconcluded`

(see `core/agentboard/reconcile.py` for the exact rules — permission requests and failures mean
`needs_you`; live activity means `running`; a dead process or stale activity means `interrupted`;
a finished turn less than 24h old means `review`, older or never-concluded means `unconcluded`).
By default the snapshot excludes sessions marked `interactive: false` (non-interactive `claude -p`
/ SDK runs, Codex `exec` runs) and `done` sessions older than 7 days; `--all` includes everything.

`agentboard paths --json` reports the resolved data/db/core directories (`{"home": ..., "db":
..., "core": ...}`), honoring `AGENTBOARD_HOME` exactly the way the rest of the CLI does. The app
calls this once at launch so its directory watcher watches the same place the CLI actually
reads/writes, falling back to `AGENTBOARD_HOME`, then the same default above, only if the call
fails.

Mark a session's own triage state directly:

```
agentboard mark <tool> <session_id> done|open|snooze [--hours N] [--note TEXT]
```

A new prompt in a session that was marked `done` reopens it automatically.

A session can also reach `done` on its own, surfaced as `auto_done: true` with a `done_reason`
("pr merged", "link done", or "clean exit" — a session that exited cleanly, with its last turn
actually finished, no open todos/tasks, and no open PR) in the snapshot; `done_reason: "manual"`
and `auto_done: false` means a human marked it done. `agentboard mark <tool> <id> open` after a
clean exit rejects the auto-done call for that session until it genuinely resumes (a new prompt
clears the exit marker and makes it live again).

## Menu bar icon

The menu bar icon takes the color of the most urgent session, so you can tell whether an agent
is waiting on you without opening the popover. One color for the whole board, first match wins:

| Board contains (first match)                | Icon                                   |
|---------------------------------------------|----------------------------------------|
| any `needs_you`                             | red, flashing until you have seen it   |
| any `interrupted`                           | orange                                 |
| any `review` whose process is still alive   | teal                                   |
| any `running`                               | green                                  |
| only `unconcluded` / closed `review`        | amber                                  |
| nothing, or only `done` / `snoozed`         | the plain monochrome (template) glyph  |

These are the same colors as the row dots and the dashboard legend. The counts next to the icon
are unchanged: needs_you + interrupted, then a dimmer review count.

Flashing: when a session *enters* `needs_you` the glyph alternates at 1 Hz between solid red and
a dimmed red outline. It stops when you open the popover, when the dashboard window becomes key,
or when the session leaves `needs_you`; the icon then stays solid red while anything still needs
you. Acknowledgement is per session, so a second session (or the same one needing you again
later) flashes again. Sessions already waiting when AgentBoard launches flash once. With
System Settings → Accessibility → Display → Reduce motion on, the icon does not flash; it shows
a static red glyph with an exclamation badge until you have seen it.

Settings → Menu bar icon:

- **Color the menu bar icon by session state** (default on). Off restores the monochrome icon.
- **Flash when a session needs you** (default on).
- **Stop flashing**: *When I open AgentBoard* (default), *After 30 seconds* / *After 2 minutes*
  (also stops on its own after that long), or *Never (until handled)*, which ignores opening
  AgentBoard and stops only when every flashing session has left `needs_you`.

The colors are resolved for the system's light/dark setting, which is what the menu bar follows,
not AgentBoard's own Appearance setting. The icon reacts within the 15 s background poll when
nothing is open (usually sooner, via the data directory watcher).

## Links & integrations

AgentBoard has no built-in knowledge of any specific ticket system, team, or host — it's meant
to work unmodified on anyone's Mac. Everything environment-specific comes in through one of
these generic mechanisms, all handled in `core/agentboard/links.py`:

1. **`agentboard link` / `agentboard status`** — any script or skill reports a link, or sets a
   free-form current-activity label, for the session it's running in:

   ```
   agentboard link --kind pr|ticket|url --ref REF [--url URL] [--state OPEN|MERGED|CLOSED|DONE] [--title T] [--session TOOL:ID]
   agentboard status --text "phase/label" [--ttl MIN] [--session TOOL:ID]
   ```

   If `--session` is omitted, the session is resolved in order: a session-identifying
   environment variable the calling tool set (`CLAUDE_CODE_SESSION_ID`, `CODEX_SESSION_ID` /
   `CODEX_THREAD_ID`), then the caller's ancestor process tree matched against a recorded
   session pid, then the most recently active session whose cwd matches `$PWD`. If none of that
   resolves, the command prints a warning to stderr and still exits 0 — a missing or confused
   AgentBoard should never break the caller.

2. **Drop directory** — for tools that can't exec the CLI, drop a file in
   `$AGENTBOARD_HOME/inbox/*.json` shaped like
   `{"session": "tool:id" | null, "cwd": "...", "links": [...], "status": "..."}`. It's ingested
   (using the same session resolution) and deleted the next time `agentboard snapshot` runs.

3. **Provider commands** — list external integrations in `$AGENTBOARD_HOME/config.json`:

   ```json
   {"providers": [{"name": "...", "command": ["..."], "interval_minutes": 10}]}
   ```

   `agentboard refresh-links` runs each one (30s timeout), passing the open sessions as JSON on
   stdin, and reads back `[{"session": "tool:id", "links": [...]}]` from stdout. This is how a
   team-specific integration plugs in without any code in AgentBoard itself; see
   `examples/providers/` for a fully generic example (reads git trailers).

4. **Built-in generic detectors**, both optional and silently skipped if unavailable:
   - A GitHub PR for the session's branch, via `gh pr list` — only if `gh` is installed, and
     only during `refresh-links` (the one command allowed to touch the network). Results are
     cached in the `links` table; an `OPEN` PR is re-checked after 15 minutes, `MERGED`/`CLOSED`
     never again.
   - Ticket references matched against the branch name and first prompt, using
     `"ticket_patterns": [{"regex": "...", "url": "https://.../{ref}"}]` in `config.json`.
     **Empty by default** — nothing is matched until you configure a pattern.

A link with every `pr`-kind entry `MERGED`/`CLOSED`, or any link at all with `state: DONE`,
marks the session `done` automatically.

Terminal focus/resume support iTerm2 (`ITERM_SESSION_ID`) and Apple Terminal
(`TERM_SESSION_ID`); for anything else, `agentboard focus`/`resume` just activates the app by
`TERM_PROGRAM`.

## Backfill

`agentboard backfill [--days 30]` imports Claude Code transcripts
(`~/.claude/projects/*/*.jsonl`) and Codex rollouts (`~/.codex/sessions/*/*/*/rollout-*.jsonl`)
modified within the window. It streams files rather than loading them whole, and is idempotent:
re-running it never overwrites a field a live hook already set, only fills in ones that are
still `NULL`.

## CLI reference

```
agentboard hook --tool claude|codex
agentboard snapshot [--json] [--all]
agentboard paths [--json]
agentboard mark <tool> <session_id> done|open|snooze [--hours N] [--note TEXT]
agentboard link --kind pr|ticket|url --ref REF [--url URL] [--state ...] [--title T] [--session TOOL:ID]
agentboard status --text TEXT [--ttl MIN] [--session TOOL:ID]
agentboard backfill [--days 30]
agentboard refresh-links
agentboard install-hooks [--claude] [--codex] [--dry-run]
agentboard uninstall-hooks [--claude] [--codex] [--dry-run]
agentboard enable-codex-hooks [--dry-run]
agentboard focus <tool> <session_id>
agentboard resume <tool> <session_id> [--print]
agentboard prune [--days 90]
agentboard logs [--since 1h|ISO-8601] [--grep TEXT] [--lines N] [--app]
```

## Diagnostics

Everything AgentBoard does is recorded in one plain-text log, so a misbehaving click or a
skipped hook can be explained after the fact:

```
~/Library/Application Support/AgentBoard/agentboard.log      ($AGENTBOARD_HOME/agentboard.log)
```

One line per event, `key=value` pairs, UTC timestamps, greppable and `tail -f` friendly:

```
2026-10-17T05:40:12.123Z INFO cli.focus key=claude:abc123 term=iTerm.app tty=ttys016 iterm_id=w0t1p0:9F2C... strategy=iterm_script osa_rc=0 osa_out=found:tty osa_err="" outcome=matched_tty ms=412
```

**What is logged.** Every CLI subcommand (`cli.start` at debug, `cli.end cmd= exit= ms=` at info,
`cli.error` with the exception on a crash); every error the CLI prints to stderr; each focus
attempt (below); resume (the terminal command and how it was opened), mark (the triage written;
notes are redacted), refresh-links (each provider's name, exit code, row count and duration,
each `gh pr list` outcome, inbox files dropped), backfill (per-file parse failures and a summary),
prune, map-codex, and what install-hooks / uninstall-hooks / enable-codex-hooks changed or left
alone. Hooks log one `hook.event` line per event at debug (tool, event, session, pid, tty, daemon,
subagent) and an info `hook.drop` line only when an event is really dropped (no `session_id`,
empty or unparseable payload). `hook-errors.log` is unchanged and still records hook failures.

**Levels.** `debug`, `info` (the default), `warn`, `error`. Set the level with the
`AGENTBOARD_LOG_LEVEL` environment variable, or `"log_level": "debug"` in
`$AGENTBOARD_HOME/config.json` (the variable wins). Because the app runs `snapshot` every 3 to
15 seconds, `snapshot` and `paths` log at debug; at the default level they add a line only when
they fail or take longer than a second. Free text passed with `--note`, `--text` or `--title` is
logged as `<redacted:N chars>`.

**Rotation.** Growth is bounded: before a write would push `agentboard.log` past 2 MB it is
renamed over `agentboard.log.1`, replacing it. There is never a `.2`, so the log never takes more
than about 4 MB however long AgentBoard runs. `uninstall.sh --purge-data` removes both files.

**Reading it back.** `agentboard logs` prints the last 200 lines of `agentboard.log.1` followed by
`agentboard.log`. `--since 10m` (or `30s`, `2h`, `1d`, or an ISO-8601 time) limits the window,
`--grep TEXT` keeps lines containing TEXT, `--lines N` changes the tail length. `--app` also
merges the app's own OSLog lines (subsystem `io.github.agentboard`) from `log show` for the same
window, sorted by time, so a row click can be followed from the app's CLI invocation to the
AppleScript result.

**The app.** Every CLI call the app makes is logged to the macOS unified log (category `cli`):
redacted arguments, exit code, duration and the tail of stderr. Actions log at `notice`, failures
at `error`, and the background `snapshot`/`paths` polling at `debug`. macOS only keeps `debug`
lines while something is streaming them (`log stream --level debug --predicate 'subsystem ==
"io.github.agentboard"'`), so Settings → Diagnostics → **Verbose logging** promotes the polling
to `notice` and passes `AGENTBOARD_LOG_LEVEL=debug` to every CLI call: one switch turns on both
sides. **Open log** in the same section reveals `agentboard.log` in Finder.

**Debugging a click that focused the wrong tab.** Click the row, then run
`agentboard logs --grep cli.focus --lines 5`. The `outcome` says what happened:

- `matched_uuid`: iTerm found the session by its recorded unique id. A wrong tab here means the
  recorded `iterm_id` is stale.
- `matched_tty`: no session had that id, so the first session on the recorded `tty` was chosen. A
  tty reused by a newer tab is the usual cause of a wrong tab.
- `matched_unknown`: an older focus script matched without saying how.
- `activated_only`: nothing matched; the terminal app was only brought to the front (exit code is
  still 0). `osa_out`/`osa_err` show what the AppleScript said, e.g. a missing Automation
  permission.
- `not_found`: the Apple Terminal script failed outright (see `osa_err`).
- `no_terminal` / `unsupported_terminal`: the session has no recorded terminal, or one AgentBoard
  cannot drive.

For more detail turn on Verbose logging (or set `AGENTBOARD_LOG_LEVEL=debug`): each strategy is
then logged as a `cli.focus.try` line.

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
Command Line Tools ship no XCTest). With Xcode selected, `cd app && swift test` runs it. With only
the Command Line Tools, SwiftPM leaves their Testing.framework off the search paths and plain
`swift test` silently runs zero tests, so pass the path explicitly:

```
cd app
F=/Library/Developer/CommandLineTools/Library/Developer/Frameworks
swift test -Xswiftc -F -Xswiftc $F -Xlinker -rpath -Xlinker $F
```

`app/Fixtures/` holds snapshots for running the app without the CLI:
`AGENTBOARD_FIXTURE_PATH=app/Fixtures/snapshot-needs-you.json app/.build/release/AgentBoard`
(one session needing you), `snapshot-quiet.json` (only done/snoozed), or `snapshot.json` (a bit of
everything; also what `AGENTBOARD_USE_FIXTURE=1` loads from the app bundle).

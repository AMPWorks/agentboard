# Development Log — AgentBoard

A timestamped narrative of how AgentBoard was designed and built. Forward chronological: oldest
at the top, newest appended at the bottom. Each entry captures substantive decisions, milestones,
trade-offs and discoveries. Routine implementation activity (file moves, typos, dependency
bumps) is out of scope; git carries that history.

Entries use ISO 8601 date-time headers to the second in UTC,
`## YYYY-MM-DD HH:MM:SS UTC — Subject`; several entries a day are allowed, each with its own
subject. Keep entries to a few
tight paragraphs of plain technical prose: state the decision, the rationale and the trade-off.
Keep entries generic: no logins, hosts, machine paths, session ids or figures measured on one
user's data. Each task appends its entry in the same PR as the work it describes. The entries before the
README restructure were reconstructed from the commits and pull requests they name.

---

## 2026-10-15 20:21:29 UTC — Initial design: hooks record, the CLI derives, the app displays

AgentBoard landed as one squashed import (`491c1f1`, PR #1): a stdlib-only Python CLI in
`core/`, a SwiftUI menu bar app with a dashboard window in `app/`, and `install.sh`,
`uninstall.sh` and `build-app.sh` in `scripts/`. SPEC.md is the design contract. Claude Code and
Codex run `agentboard hook` on every lifecycle event; the hook appends the event, upserts the
session, prints nothing to stdout (Claude Code folds `SessionStart` and `UserPromptSubmit` output
into the model's context), always exits 0 and logs failures to `hook-errors.log`, with a target
of under 50 ms. `agentboard snapshot` derives exactly one state per session in a fixed priority
order: `done`, `snoozed`, `needs_you`, `interrupted`, `review`, `running`, `unconcluded`.

The app never opens the database; it shells out to the CLI for everything, so the CLI is the one
place state is computed. Integrations are generic only (`agentboard link` and `status`, an inbox
folder, provider commands from `config.json`, `gh` PR detection), and everything specific to the
original developer's machine was replaced with generic values before the import. `snapshot`
never touches the network.

A clean-environment install on a Mac whose bare `python3` is 3.9 shaped the scripts: they pick
the first Python 3.11 or newer (`AGENTBOARD_PYTHON3`, `python3`, then `python3.14` down to
`python3.11`) instead of failing, and the package refuses to import under an older interpreter
with one clear message. The same pass made malformed inbox and provider data a skip rather than
a crash, made the Swift snapshot model default missing fields instead of rejecting the whole
snapshot, and made the app's CLI runner drain both pipes so JSON is never truncated.

## 2026-10-17 03:53:09 UTC — The menu bar icon takes the most urgent session's color

The menu bar glyph now shows the color of the most urgent session (`df63270`, PR #2): red for
`needs_you`, orange for `interrupted`, teal for a live `review`, green for `running`, amber for
closed or unconcluded work, otherwise the plain template icon. A session that newly needs you
flashes the icon at 1 Hz until it is acknowledged, per session, by opening the popover, focusing
the dashboard or a timeout policy; with Reduce motion on, a static exclamation badge replaces the
flash. Notifications and the flash read the same "newly needs you" diff, so they always agree.

SwiftUI hands a `MenuBarExtra` label to the status item as a template image, which drops the
color, so the glyph is rendered with `ImageRenderer` into a non-template `NSImage`. The colors are
resolved against the system light/dark setting rather than the app's own Appearance override,
because the menu bar ignores that override.

Discovery: with only the Command Line Tools installed, SwiftPM passes their frameworks directory
to the generated test runner as `-I` instead of `-F`, so plain `swift test` builds, runs zero
tests and exits 0. Target-level flags do not reach the runner, so the `-Xswiftc -F` and
`-Xlinker -rpath` flags go on the command line. The app's tests use Swift Testing, since the
Command Line Tools ship no XCTest.

## 2026-10-21 00:22:16 UTC — A capped diagnostic log

Until this change (`5e41bff`, PR #3) the only persistent log was `hook-errors.log`, and a session
click that focused the wrong terminal tab left no trace. `diag.py` now writes one `key=value`
line per event to `agentboard.log` and never raises. Every CLI subcommand logs its start, end and
errors, and each focus attempt logs a `cli.focus.begin` line before any AppleScript runs (so a
hang is visible) and an `outcome` afterwards. Free text passed with `--note`, `--text` or
`--title` is redacted, and argparse abbreviations are disabled so `--not` cannot slip past the
redaction as `--note`. Focus exit codes stay unchanged on purpose: the `open -a` fallback still
returns 0, and only the log tells it apart from a real match.

The log's disk use had to stay bounded. The file rotates before a write would push it past 2 MB,
replacing `agentboard.log.1`; there is never a `.2`, so the two files together hold about 4 MB at
most. Several processes write at once, so rotation takes a non-blocking `flock` and renames only
if the path still names the writer's inode, which keeps two racing rotations from overwriting a
good `.1` with a nearly empty file. The trade-off: a writer that loses the race appends to the
file being rotated, so `.1` can run a few lines over the cap. Where `flock` is unsupported,
rotation goes ahead without the lock. `snapshot` and `paths`, which the app polls, log at debug,
and the hook imports `diag` lazily to keep its fast path cheap.

## 2026-10-21 03:20:58 UTC — One search grammar for the popover and the dashboard

The popover gets a filter field and the dashboard search covers every text field in the snapshot
(`97303cf`, PR #4). Both use one matcher, `SessionSearch`, so a query means the same thing in
each: whitespace-separated terms must all match, double quotes keep a phrase together, matching
ignores case and diacritics, and qualifiers (`repo:`, `path:`, `branch:`, `tool:`, `state:`,
`pr:`, `link:`) narrow a term. An unknown qualifier stays plain text, and plain terms do not
search link URLs. The menu bar icon and the flash still read every session, whatever the query,
and the dashboard's sidebar badges count what the filters keep so the badges and the list agree.

Escape and ⌘F are handled by one local key monitor scoped to the popover's window instead of
`.onKeyPress`, because the text field's editor sees Escape before SwiftUI does.

## 2026-10-21 18:09:39 UTC — Retained notes and the Updates feed

Sessions that run unattended scroll their important messages away under the next tick
(`1fbd44f`, PR #5). AgentBoard now keeps the ones worth reading as durable notes, shown as
Updates in the popover and the session detail until dismissed. Migration 5 adds a `notes` table
whose partial unique index covers only open notes, so only open notes deduplicate; a repeat
bumps a count instead of adding a row, and a note's fingerprint ignores timestamps, clock times
and tick counts. The classifier rules were ported unchanged from an earlier terminal-only
prototype rather than rewritten.

A marker line (`USER_ACTION:` by default, configurable as `note_markers`) makes a `needs_you`
note in any turn; `update` and `error` notes come only from unattended turns, judged from the
newest parent prompt against `note_unattended_prefixes`. An open `needs_you` note raises its
session to `needs_you` until a newer parent prompt releases it or it is dismissed. The hook
classifies each parent `Stop` inside the same transaction under a savepoint, so a classifier
failure rolls back only the note and never drops the event, and it imports the classifier lazily
to stay under its 50 ms budget. The corpus fixture that pins the rules is anonymized text in the
format loop sessions print, not a copy of real data.

## 2026-10-22 21:00:53 UTC — Quieting loop noise in Updates

On real data the Updates feed was mostly loop noise ("Nothing changed this tick", status lines
restating what was waiting) and pushed every session out of the popover, and Clear all dismissed
only the notes the popover listed (`62750bd`, PR #6). Idle wording and waiting restatements are
now quiet. The rule matches the lead sentence rather than only a line that ends in the phrase,
because real messages are often one paragraph.

A PR or issue reference the session already mentioned is no longer a milestone by itself, while
hard events (merged, opened, filed, failed, CI red) still count every time. The old milestone list
was split so that only "ready for review", "blocked" and "in Pending" are status words; without
the split, a loop restating that a PR is ready for review would have been kept on every tick.
Seen references are checked only against the same session's notes, and a rebuild uses only the
notes that came before each replayed event, so a later dismissed note cannot make an earlier
first mention look like a repeat.

The popover's Updates section lists every `needs_you` note but at most 3 other rows, and a
session with several update or error notes gets one grouped row. With an empty search, Clear all
now dismisses every open note; with a search it becomes Clear matching. The snapshot always lists
an older open `needs_you` note beyond the 20-per-session cap, so a request can never fall off the
list behind a loop's updates.

## 2026-10-24 07:08:06 UTC — Session lifecycle: clean exits, owned PRs, actionable Review

This change (`a4d86f8`, PR #7) makes a session's state and row mean something you can act on.
Links now record a PR's head branch, close time, reporter, creation time and first human response
(migration 6). A merged or closed PR retires a session only while the session is still on that
PR's branch and nothing was prompted after the merge, and nothing on main, master or develop ever
counts, so a merged PR on an old branch cannot hide a session that moved on. A clean exit goes to
`done` unless the session's own PR awaits you, a `needs_you` note is open or todos are left open.

A PR is the session's own if the session reported it, or if `gh` found it on the session's branch
created during the session; only an owned, open, unanswered PR keeps a session in Review.
Responses ignore bots, the PR's author and any login in the optional `automation_logins` key,
and the active `gh` login is excluded only as the PR's author, so a user who reviews PRs that
sessions open under a separate token clears them by reviewing. `snapshot` cannot ask `gh` who is
logged in, so a `links.self_authored` column records the one-account case for its wording. Every
Review row now carries a reason: an awaiting PR, an update note's headline, or "Your turn".

Reading the branch: one `git branch --show-current` call costs most of the hook's 50 ms budget,
so the hook reads the branch from the repository's `HEAD` file (following a worktree's `gitdir:`
file) and runs git only when that fails. Titles follow the work: `refresh-links` retitles a
session after 5 new prompts or a branch change by running the local `claude` CLI with Haiku.
The retitle runs with `AGENTBOARD_HOOK_SKIP=1` and `--no-session-persistence` so its own session
never shows up on the board, and since its stdin is untrusted text from other sessions, the
default command also passes `--tools ""` and `--strict-mcp-config`.

## 2026-10-24 07:10:01 UTC — An in-app help guide, kept in step by tests

The app gains a gear menu (Settings, AgentBoard Help, the log, Quit), a Help window with 12
searchable topics written as Swift data in `HelpContent.swift`, contextual ? buttons, and a
tooltip on every control (`4633640`, PR #8). It was stacked on the lifecycle PR so the guide could
document its states and config keys.

The guide is kept honest by tests rather than by review. `config_keys.py` registers every
`config.json` key core reads, and `test_help_coverage.py` scans core with `ast` for config
lookups (with a floor of the keys known today and a check for stale entries), then requires every
key in the guide and in the README's configuration table (now in the reference), every CLI
subcommand and long option
in the guide, and every UserDefaults key in `SettingsKeys.all` or marked `// not-a-setting:`.
`test_app_tooltips.py` is a heuristic lint that flags a control without `.help(`, with a
`// no-tooltip: <reason>` escape hatch.

## 2026-10-24 19:22:51 UTC — State accuracy and an incremental backfill

The first of two PRs from the 2026-10-24 review (`49c9d20`, PR #9). A finished turn now
survives background subagents, compaction and other kept events, and keeps its `last_event_at`.
A parent `Stop` records the background tasks still in flight and the session's crons, so a live
session waiting on background work reads Running, "Waiting on N background task(s)", and a cron
tick reads as unattended. Claude Code's `~/.claude/sessions/<pid>.json` status breaks the ties
the hooks leave open (`idle` reads as a finished turn, `busy` as running); an entry whose pid
differs from the session's known pid is ignored, because a file left by a crashed earlier process
says nothing about the current one.

Migration 7 adds `prompt_kind`. Injected prompts (task notifications, other agents' and
sessions' messages, configurable as `prompt_injected_prefixes`) still count as activity but no
longer reopen a done session, release a `needs_you` note, make a loop turn attended or count
toward retitling. The migration marks recorded prompts with a case-sensitive `GLOB` rather than
`LIKE`, so it marks the same rows as Python's `str.startswith`. `StopFailure` now reads the
documented error fields and records an `error` note.

The per-`Stop` todo scan resumes from a stored byte offset instead of re-reading the whole
transcript, and backfill parses transcripts with no transaction open, then writes each session
in its own short transaction, so hooks are never starved by a long backfill.

## 2026-10-24 19:23:36 UTC — Retention, an error banner and a version-aware Codex hooks flag

The second PR from the review (`926b889`, PR #10). `prune --auto` runs from the app after its
launch backfill and every 6 hours, gated by the CLI to one real run a day. It deletes sessions
idle longer than `prune_days` (default 90) but never one with an open `needs_you` note, a PR
awaiting you, an active snooze or a live process, and it compacts tool-use and subagent events
older than `compact_days` (default 7), since nothing reads them once the turn is over. `0` now
means never for both windows, a deliberate behavior change: `--days 0` used to delete every
session.

The popover and dashboard show a one-line banner only when something is wrong: the snapshot
failed, the CLI has been busy for over 30 seconds, or an action failed. Staleness counts only a
refresh that is actually in flight, so sleep and wake never trip it, and the banner never feeds
the menu bar icon.

Codex 0.124 and newer run hooks by default, so the install reminder reads `codex --version` (2 s
timeout) and warns only on `hooks = false`, an older Codex or an unknown version.
`enable-codex-hooks` changes nothing on a new Codex, and it refuses while the deprecated
`codex_hooks = false` is set, since the Codex docs do not say which key wins when both are set
and the patcher only ever adds lines. The tests that depend on
these messages put a `codex` stub on `PATH`, so they do not depend on the machine's Codex.

## 2026-10-27 03:31:12 UTC — Turn-finish notifications with click-to-focus

An opt-in setting, off by default, posts a notification when an attended session finishes its
turn (`32d73c0`, PR #11). The snapshot gains `turn_finished_at`, `turn_prompt_kind`,
`background_tasks` and `turn_ends_background_wait`, computed without reading `last_event`, so one
finished turn is one stable value that an idle reminder or a registry flap cannot change. A pure
`TurnFinishDetector`, primed on launch, considers each turn once; loop and cron turns stay
silent, and a `Stop` that listed background work stays silent until the turn that reports it
back. A turn that ends in `needs_you` notifies only through the needs-you toggle.

Replying in the session, or ending it, removes its delivered notification, and a newer turn
replaces it instead of stacking. Clicking either kind of notification focuses the session's
terminal and falls back to the dashboard with the session selected; a click that launches the app
is held until the first snapshot loads. One deliberate deviation from the plan: a running row that
already carries a finished time (a registry flap) waits to settle before its turn is recorded,
because recording it at once would silence the notification when the flap is the first thing
seen.

## 2026-10-27 04:50:13 UTC — Plan usage, model and context for any agent

Plan usage is a list of sources, not one built-in (`2daf70b`, PR #12): Claude Code's local
`rate-limits.json` cache, Codex's rate limits read from its session files, and a
`usage/<source>.json` file any tool can keep. Windows are labelled from the data, so nothing about
a plan, a billing model or a window name is built in; nothing is shown when no source exists, and
stale or already-reset numbers drop out. Claude Code's cache is undocumented, so it is read as a
best-effort cache with every field validated. `snapshot` still never touches the network.

Each session carries its model and context size (migration 8), from hooks, transcripts, Codex
rollouts, `agentboard status --model/--context-tokens` or inbox drops. The freshest measurement
wins, compared as parsed datetimes because the sources stamp at microsecond and millisecond
precision, and a value is never cleared except by a compaction that reports no size. The Codex
parser was grounded in the Codex source, which shows that `input_tokens` already includes cached
input. The context window's size is not known, so the row tag shows tokens, not a percentage.
Malformed usage or inbox files and absurd times or sizes are ignored instead of failing a
snapshot.

## 2026-10-29 04:04:41 UTC — The newer Claude Code hook events, and why some stay unregistered

Claude Code had started firing events AgentBoard did not listen to (`3457ed4`, PR #13). It now
registers `PreCompact`, `PostCompact`, `CwdChanged`, `Elicitation`, `ElicitationResult`,
`PermissionDenied` and `PostModelSwitch`. A `/compact`, a `cd` or another local-only command at an
idle prompt keeps a finished turn; a parent `CwdChanged` re-reads the branch at once; an MCP
elicitation is `needs_you` with the server's name, and its link and answer are never stored;
`PermissionDenied` is recorded as activity; `PostModelSwitch` updates the model without changing
state. Migration 9 stores each parent event's `permission_mode`, shown as a small tag on live rows.

Some events are deliberately not registered, and the reference names them so nobody adds them by
hand later. A registered `WorktreeCreate` hook replaces Claude Code's own `git worktree` step and
must print the new worktree's path; AgentBoard's hook prints nothing, so registering it would
break `claude --worktree`, `EnterWorktree` and worktree-isolated subagents, and `CwdChanged`
already covers the move. `TaskCreated` and `TaskCompleted` would cost one synchronous hook per
task for a delta the transcript re-parse at every `Stop` already recomputes.

## 2026-10-29 06:24:41 UTC — Closed and merged PRs hidden from session rows by default

Session rows kept a PR button for every PR whatever its state, so long-lived sessions showed
buttons for PRs merged days before (`164c71f`, PR #15). Rows now show only PRs that are not merged
or closed, a PR awaiting your review is never hidden, and the filter runs before the cap of three
so the "+N" counts only what would be shown. Settings → General → Show closed and merged PRs
brings them back. The change is display-only: the detail view, the context menu and search still
list every link, and session states are computed in core and do not change. Two days earlier a
direct fix (`9d1ebb3`) had made the buttons read `#N` even when a link's ref includes the repo.

## 2026-10-29 06:25:26 UTC — Async status-only hooks and stale-event handling

Every Claude Code event ran `agentboard hook` synchronously with a 5 s timeout, so a hook stalled
on the sqlite lock could hold up a tool call for up to 5 s (`a383084`, PR #16). The high-frequency
mid-turn events now run async: `PreToolUse`, `PostToolUse`, `PostToolUseFailure`,
`SubagentStart` and `SubagentStop` for Claude Code, and the four of those Codex has. The rest stay
synchronous: turn boundaries are ordered by event id, so their hooks must commit in firing order;
`claude -p` and Codex cancel background hooks at teardown, which would lose a `Stop` or
`SessionEnd`; and `UserPromptSubmit` stays synchronous by decision.

Async hooks can commit out of order, so the shim stamps each event with its process start time
before any import. An async event that commits after a newer settled state is still recorded but
cannot change the session's state, times or counts. Two narrow exceptions trade precision for
responsiveness: a stale `SubagentStop` within 1 s of the settled state still lowers the
background-task count, and a parent `PreToolUse` within 1 s after a needs-you state keeps the row
in Needs you, since it is the same tool call. The latter assumes Codex fires `PreToolUse` before
its permission request, which is untested against a real Codex. `install-hooks` rewrites existing
entries in place with byte-identical commands and warns that Codex needs the changed hooks trusted
again in `/hooks`.

## 2026-10-29 08:15:08 UTC — README for new users, the reference in docs/, and this log

The README had grown into a 1,154-line maintainer reference that opened with 150 lines of
installer internals. It is now a short page for someone who has just found the repository: what
AgentBoard does, light and dark screenshots taken from the fixture snapshot, a quick start,
install and uninstall, the Settings window and the common `config.json` keys, a feature tour,
privacy and troubleshooting. Nothing was deleted. The old text moved verbatim to
`docs/REFERENCE.md`, which keeps every section for maintainers and machine readers, and the
components and testing sections moved to `docs/DEVELOPMENT.md`.

The move was split into reviewable commits: a pure rename, a reorder that leaves the multiset of
lines unchanged, and a small set of edits (the title, an intro, anchor links in place of "see
below" references, and two promoted headings). The squash merge records README.md as rewritten,
but `git blame -C` still traces the reference's lines to the old README. Every README section
links to its reference anchor, and a new test checks every relative link, anchor and image in the
docs, and that each screenshot is a PNG of at most 1 MB with its light/dark twin. Another test
requires every CLI subcommand and option in the reference's CLI section.

This log starts here, backfilled from the commits and pull requests on main, and from now on each
task appends its entry before opening its PR.

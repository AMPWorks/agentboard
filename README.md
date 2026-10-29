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

Codex 0.124 and newer run hooks by default; only `hooks = false` under `[features]` in
`~/.codex/config.toml` turns them off. Older Codex needs `[features]` / `hooks = true` there
before it will act on `~/.codex/hooks.json` at all. `install.sh` never edits that file on its own
(it's your config, same as the rest of `config.toml`), but it reads it and runs `codex --version`
(2 s timeout) to decide what to say: nothing when the flag is `true` or Codex is 0.124 or newer; a
warning naming the line when `hooks = false` (or its deprecated alias `codex_hooks = false`) is
set, whatever the version; a warning with the exact snippet to add when Codex is older than 0.124
and the flag isn't set; and one softer line when the version can't be read. Pass
`--enable-codex-hooks` to have `install.sh` set it for you: on Codex 0.124 or newer with no
`hooks = false` it leaves the file alone and says so; with the deprecated `codex_hooks = false` it
refuses and asks you to delete that line (the Codex docs do not say which key wins when both are
set, and the patcher only ever adds lines); otherwise it backs up `config.toml` first,
then makes the smallest possible textual edit (add `hooks = true` under an existing `[features]`
section, or append a new one) — it never rewrites the file through a TOML parser/serializer, so
every other byte, including comments, is preserved. Either way, the first time hooks are registered (and on every later
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
- `--enable-codex-hooks`' edit to an existing `~/.codex/config.toml` (`[features]` /
  `hooks = true`) is **not** reversed by `uninstall.sh` — only its own backup file
  (`config.toml.agentboard-bak-<timestamp>`) is left beside it. That flag is a Codex-wide config
  choice, not something `uninstall.sh` should silently undo; restore the backup yourself if you
  want it reverted. A `config.toml` that `--enable-codex-hooks` created (none existed before, so
  there is no backup) is deleted, but only while it still holds nothing but that setting.
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

The schema (see `core/agentboard/db.py`) has these tables:

- `events` — an append-only log of every hook event ever received, per `(tool, session_id)`.
- `sessions` — one row per session: cwd, branch, title, prompts, last activity, liveness
  bookkeeping (pid, terminal info), a free-form `status_text`/`status_until` set via
  `agentboard status`, and the model it last ran with its context size (`model`,
  `context_tokens`, `context_at`; see Plan usage and context below).
- `triage` — the user's own done/snoozed/open decision for a session.
- `links` — PRs, tickets, or arbitrary URLs associated with a session, from whatever source
  reported them (see Links below).
- `notes` — retained updates: the few things a session said that are worth keeping (see Updates
  below), deduplicated per session with a repeat count, kept until dismissed. `notes_meta`
  records which recorded events `notes --rebuild` has already replayed.

Migrations are tracked with `PRAGMA user_version`; `db.connect()` applies any pending ones.

**Retention.** The app runs `agentboard prune --auto` after its launch backfill and every 6 hours;
the CLI itself gates it to one real run a day (`notes_meta.last_prune_at`, epoch seconds), so a
second app instance or a manual run cannot double it up. Each run:

- deletes sessions idle longer than `prune_days` (default 90), with all their events, triage,
  links and notes, except a session that still has an open `needs_you` note, a PR awaiting you, an
  active snooze or a live process (for a Claude session, only while `~/.claude/sessions/<pid>.json`
  still names it, so a crashed session whose pid was reused still ages out); those are skipped and
  counted in the `cli.prune` log line;
- compacts: deletes tool-use and subagent events (`PreToolUse`, `PostToolUse`,
  `PostToolUseFailure`, `SubagentStart`, `SubagentStop`, `PreCompact`, `PostCompact`) older than
  `compact_days` (default 7), from every session, live or ended. They are most of the table and
  nothing reads them once the turn is over; prompts, `Stop`s, notifications and session start/end
  rows are kept;
- deletes dismissed notes older than `prune_days`;
- runs `VACUUM` when the free pages exceed 8 MB and a quarter of the file, at most once a week
  (`notes_meta.last_vacuum_at`).

`0` for either window means never. `agentboard prune --dry-run` prints what a run would delete.

Next to the database live `agentboard.log` and `agentboard.log.1`, the diagnostic log (see
Diagnostics below), `hook-errors.log`, the hook's own failure log, and the `usage/` folder of
plan-usage files (see Plan usage and context below).

## Hooks

`agentboard hook --tool claude|codex` is registered for every lifecycle event in both tools. It
reads one JSON payload from stdin, records it, and **always exits 0 and prints nothing to
stdout** — Claude Code folds a hook's stdout into the model's context for `SessionStart` /
`UserPromptSubmit`, so any output there would leak into the conversation. Every exception is
caught and logged to `$AGENTBOARD_HOME/hook-errors.log` (capped at ~1 MB) instead of surfacing.

It targets well under 50 ms per call. The only things that ever shell out are:

- `git -C <cwd> branch --show-current`, and only when the branch can't be read straight from
  the repository's `HEAD` file (which is how it is normally read: one git call costs most of the
  50 ms budget). The branch is re-read on `SessionStart` / `UserPromptSubmit` and on the parent
  `Stop` / `SessionEnd` (so a branch created mid-turn is seen once the session goes idle or
  exits), or whenever a session's branch is still unknown.
- A short, targeted `ps` walk (at most 5 hops) to find the actual `claude`/`codex` process above
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
`install.sh --enable-codex-hooks` rather than directly. It is needed only for Codex older than
0.124, or to override an explicit `hooks = false`: when `codex --version` reports 0.124 or newer
and the flag isn't `false`, it prints that hooks are on by default and changes nothing (with or
without `--dry-run`).

## State

`agentboard snapshot [--json] [--all]` is the one thing the Swift app (and anything else) needs
to poll. It never touches the network and runs in well under 500 ms even with a few thousand
sessions. Each session is assigned exactly one `state`, in priority order:

`done` → `snoozed` → `needs_you` → `interrupted` → `review` → `running` → `unconcluded`

(see `core/agentboard/reconcile.py` and SPEC.md for the exact rules — permission requests and
failures mean `needs_you`, and so does an open `needs_you` note no newer prompt has answered (see
Updates below); live activity means `running`; a dead process or stale activity means
`interrupted`; anything not finished and not waiting on you is `unconcluded`).

A turn that failed on an API error (Claude Code's `StopFailure`) is `needs_you` with "<error>:
<first line of the message>", e.g. "rate_limit: API Error: Rate limit reached", and adds an
`error` note under Updates (repeats bump its count). A usage-limit pause that needs you
(`quota_auto_resume_stale`: the limit reset while the Mac slept and the wait needs Enter;
`quota_auto_resume_disabled`: auto-resume is off) is `needs_you` too; an auto-resume
(`quota_auto_resume_fired`) or any new activity makes the session `running` again.

**Review always says what to do.** A session is `review` only with a concrete reason in
`waiting_reason`, which the row shows as its second line:

- "PR #N awaits your review" — the session opened a PR that nobody but automation has reviewed or
  commented on yet (see Links below; "PR #N open, no response yet" when you and the session share
  one GitHub account). This holds even after the session exits, until you respond or it merges.
- the headline of an open `update` note newer than your last prompt, under a day old;
- "Your turn: <first line of its last message>" — an attended session stopped (or "Interrupted:
  your turn"), within 24 h of the end of that turn.

A loop session (its last prompt starts with a `note_unattended_prefixes` entry, or is one of the
crons its last Stop listed in `session_crons`) idling between ticks is not waiting on you: it is
`running` with "Loop idle between ticks" while its process is alive. A session whose last Stop
listed background tasks still in flight (Claude Code's `background_tasks`: subagents, shells,
monitors, ...) is `running` with "Waiting on N background task(s): <types>" until they finish,
then "Your turn". A background agent finishing after the turn, and a compaction (including a
`/compact` typed at an idle prompt) or a resume of a live session, never change a session's state.

Claude Code also writes `~/.claude/sessions/<pid>.json` with a `busy`/`idle` status; AgentBoard
reads it for every live Claude session as a tie-breaker when it is newer than the last hook
event: `idle` turns a stale activity event (a lost `Stop`) or a bare `SessionStart` into a
finished turn, and `busy` makes a finished or unknown one `running`; neither applies once the
process is gone. Other statuses (e.g. `shell`) carry no signal.
By default the snapshot excludes sessions marked `interactive: false` (non-interactive `claude -p`
/ SDK runs, Codex `exec` runs) and `done` sessions older than 7 days; `--all` includes everything.

Each session in the snapshot also carries four turn fields, which the app's turn-finish
notifications read (see Notifications):

- `turn_finished_at`: the time of the session's newest `Stop` while nothing has happened since
  (no newer prompt, not ended, no background tasks still listed by that Stop or the session),
  else null. An idle reminder, a compaction or a background agent finishing never changes it,
  so one finished turn keeps one value.
- `turn_prompt_kind`: what started the latest turn: `human`, `loop`, `cron`, `injected` (a task
  notification or another agent's message), or null for rows recorded before prompt kinds.
- `background_tasks`: how many background tasks the last `Stop` listed (null when unknown).
- `turn_ends_background_wait`: true when the latest turn was injected right after a `Stop` that
  listed background tasks, i.e. the turn that reports a background task's result back.

`agentboard paths --json` reports the resolved data/db/core directories (`{"home": ..., "db":
..., "core": ...}`), honoring `AGENTBOARD_HOME` exactly the way the rest of the CLI does. The app
calls this once at launch so its directory watcher watches the same place the CLI actually
reads/writes, falling back to `AGENTBOARD_HOME`, then the same default above, only if the call
fails.

Mark a session's own triage state directly:

```
agentboard mark <tool> <session_id> done|open|snooze [--hours N] [--note TEXT]
```

A new prompt in a session that was marked `done` reopens it automatically; a prompt Claude Code
injected itself (a finished background task, another agent's message; see Updates) does not.

A session can also reach `done` on its own, surfaced as `auto_done: true` with a `done_reason` in
the snapshot; `done_reason: "manual"` and `auto_done: false` means a human marked it done:

- **"clean exit"** — you exited it (`/exit`, logout; for Codex, any end after a finished turn).
  An exited session goes out of sight (Show hidden / the dashboard's Done still list it) unless
  it opened a PR that awaits you (it stays in Review), has an open `needs_you` note, or left
  todos open (`unconcluded`, "N todos left open"). A PR that was already on the branch before
  the session started never keeps it around. Any other end (`/clear`, `/resume`) is
  `unconcluded`, "Ended (<reason>)".
- **"pr merged"** — every PR recorded for the session's *current* feature branch is merged or
  closed and you haven't prompted it since. A merged PR on an older branch, or on
  main/master/develop, never hides a session that moved on.
- **"link done"** — any link reports `DONE`.

`agentboard mark <tool> <id> open` after an exit rejects these for that session ("Reopened")
until it genuinely resumes (a new prompt clears the exit marker and makes it live again).

## Help

The app has a built-in guide: open the gear menu (at the right of the popover footer, or in the
dashboard toolbar) and choose **AgentBoard Help**. It covers sessions and their states, the
popover, Updates, search, the dashboard, PR links and integrations, every Settings control, every
`config.json` key, the CLI, troubleshooting, and data and privacy, with a search field that finds
topics by their contents. The ? buttons next to each Settings section header and next to the
popover's Updates header and search field open the matching topic. The same gear menu opens
**Settings…** and **Open Log** and holds **Quit AgentBoard**, and every control in the app has a
tooltip saying what it does.

The guide mirrors this README (`app/Sources/AgentBoard/Help/HelpContent.swift`); tests check that
every `config.json` key, Settings key and CLI subcommand and option appears in it.

### Plan usage and context

**Plan usage.** The popover footer (above its buttons) and the dashboard sidebar (under the color
key) show one line per usage source that has numbers, led by the source's name, such as
`Claude · 5h 57% · 7d 70% · resets in 1h 55m` or `Codex · 5h 12% · 7d 31%`: the used percentage
of each limit window, in the source's order, and when the soonest one resets. A line turns orange
at 80% and red at 95% on any window. Hover it for each window's reset time, the file the numbers
came from and how old they are; once they are over an hour old the line says so
(`Claude as of 3h ago · ...`), because a source is only as fresh as its last report on this Mac
(use on another app or machine is missing until then). A window whose reset time has passed is
left out until newer numbers arrive; a spend window with dollars shows as `spend limit $314 of
$500`. Nothing about a plan, a billing model or a window name is built in: windows are labelled
from the data. **Settings → Show plan usage** (on by default) hides the lines.

Sources, all local files that every `snapshot` reads again (it never touches the network):

- **Claude Code's cache** `~/.claude/rate-limits.json`. It is undocumented (the same data the
  status line receives as `rate_limits`, which Claude Code has only on plans with usage limits,
  or behind a gateway that sets a spend limit), so it is read as a best-effort cache: every field
  validated, windows labelled from its keys by rule (`five_hour` → `5h`, `seven_day_opus` →
  `7d opus`, `spend_limit` → `spend limit`).
- **Codex.** When a Codex turn ends (and at the launch backfill), AgentBoard reads the
  `rate_limits` of the rollout's newest `token_count` event (the plan's primary and secondary
  windows, labelled from their length: `5h`, `7d`) and keeps them in
  `$AGENTBOARD_HOME/usage/codex.json`, never replacing newer numbers.
- **Any other agent or tool** keeps `$AGENTBOARD_HOME/usage/<source>.json` (the file name is the
  source's name) and replaces it whenever its numbers change, atomically (write a temporary file
  in the same folder, then rename it):

  ```json
  {"label": "My agent", "updated_at": "2026-10-26T12:00:00Z",
   "windows": [{"label": "5h", "used_percentage": 57, "resets_at": "2026-10-26T15:00:00Z"},
               {"label": "monthly", "used_percentage": 63, "used_usd": 314, "limit_usd": 500,
                "period": "monthly"}]}
  ```

  Only `windows` is required (each with a `label` and a numeric `used_percentage`); `label`
  defaults to the file name, `updated_at` (ISO 8601 or epoch seconds) to the file's modification
  time, and `resets_at` takes either form too.

Claude's cache is listed first, then the files by name, at most 8 sources of 8 windows each. Two
sources with the same name keep the one updated most recently, and a source not updated for 7
days is hidden. When there is no source at all, which is the normal case with an API key,
Bedrock or Vertex, most gateways, or no Claude, there is no line, no banner and no log line above
debug; a malformed file is skipped the same way.

**Model and context.** Each session row shows a small tag such as `Opus 5.5 · 308k` or
`gpt-5-codex · 45k` at the right end of the row, after any PR buttons: the model it last ran and its current context size in
tokens (what its latest request sent, cached tokens included). Any model id works: a Claude id is
shortened by rule (`claude-opus-5-5` → `Opus 5.5`; Bedrock and Vertex wrappers such as
`us.anthropic.` and `-v1:0` or `@20250805` are dropped), any other id is shown as it is, and an id
that runs long is cut in the middle. A session whose agent reported only a size shows
`Context 51k`. The
tooltip and the dashboard detail's **Model** and **Context** rows give the full id, the exact
count and how old it is. The context window's size is not known, so there is no percentage. Where
the values come from:

- `SessionStart` hooks send the model (Claude Code omits it after `/clear` and on recovery) and,
  on a resume or fork (Claude Code 2.1.251 and later), the context size. A `/compact` that sends
  no size drops the old one, so the tag shows only the model until the next reply.
- After every `Stop` (and at the launch backfill) the transcript's newest real reply refreshes both,
  so a `/model` switch or the post-compact size shows up by the next turn's end. Subagent lines and
  synthetic API-error lines are skipped.
- Codex: every Codex hook event names the model, so a `/model` switch shows at once; the
  rollout's newest `turn_context` gives the model too and its newest `token_count` event the
  context size (`last_token_usage.input_tokens`, which already includes cached input).
- Any other agent: `agentboard status --model ID --context-tokens N`, or `model`,
  `context_tokens` and `context_at` in an inbox drop file (see Links & integrations).

The freshest measurement wins, so a resumed session's context from its `SessionStart` stays until
a newer reply replaces it, and a re-parse that finds nothing never clears a stored value.

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

## Notifications

Settings → Notifications has two toggles, both off by default. macOS asks for permission the
first time you turn one on.

- **Notify when a session needs you** (`notifyOnNeedsYou`): one notification when a session
  newly enters `needs_you`, with the waiting reason as its text.
- **Notify when a session finishes a turn** (`notifyOnTurnFinished`): one notification when an
  attended, interactive session finishes its turn and lands in Review, titled with the project
  (the session title as subtitle) and showing the first line of the reply.

A finished turn is the snapshot's `turn_finished_at` (see State): the session's newest `Stop`
with no newer prompt, not ended, and no background tasks listed. Each turn notifies at most once,
however long the session sits idle afterwards or flickers between Running and Review. These
never notify: a loop session idling between ticks (it is Running), a turn whose `Stop` still
lists background tasks (the injected turn that reports them back notifies instead), a turn
started by another agent's message, sessions already in Review when AgentBoard launches (or
older sessions a backfill or **Show hidden** brings in), and the session selected in the
dashboard while the dashboard window is key. A turn that ends in `needs_you` (a needs_you update)
is left to the needs-you toggle: the turn toggle stays silent for it, also when the update later
resolves into Review. Turning the toggle on never replays old turns.

A newer turn of the same session replaces its delivered notification instead of stacking, and
your next prompt in that session removes it, as does the session ending (quitting it, or
`SessionEnd`); this cleanup still runs after you turn the toggle off. Notifications are grouped per session in
Notification Center. Clicking either kind brings the session's terminal tab to the front (iTerm2
and Terminal, via `agentboard focus`; for a session inside tmux, the window attached to its tmux
session, or a new iTerm window attached to it); when the session has no focusable terminal, the
dashboard opens with it selected. A click on an earlier notification while AgentBoard is not running
launches it and does the same once the first snapshot loads.

## Search

Both the menu bar popover and the dashboard have a search field, and both read a query the same
way:

- Words are separated by spaces, and a session must match **every** word (`retry codex`).
- Double quotes keep a phrase together: `"menu bar"`.
- Matching ignores case and accents and matches anywhere in a field, so `#4` also finds `#42`.
- A plain word searches the title, the first and last prompts, the last assistant message, the
  project, working directory and branch, the tool, the state (`needs_you` or `Needs you`), the
  waiting reason and status text, every link's kind, ref, title and state, the triage note, and
  the session id, and the text of the session's open updates (notes). It does not search link
  URLs; use `pr:` or `link:` for those.
- A `field:value` word narrows the match to one place. The value can be quoted
  (`branch:"feat x"`, `state:"needs you"`).

| Qualifier            | Searches                                             |
|----------------------|------------------------------------------------------|
| `repo:` / `project:` | project name and working directory                   |
| `path:`              | working directory                                    |
| `branch:`            | git branch                                           |
| `tool:`              | `claude` or `codex`                                  |
| `state:`             | state, raw (`needs_you`) or as shown (`Needs you`)   |
| `pr:`                | links of kind `pr`: ref, title and URL               |
| `link:`              | any link: kind, label, ref, title, URL and state     |

Spaces after the colon are ignored, so `branch: main` is the same as `branch:main` and
`state: "needs you"` the same as `state:"needs you"`. An unknown qualifier (`foo:bar`, or
`foo: bar` as two words) or one with nothing after it (`branch:` at the end of the query, or
followed by a known qualifier such as `branch: tool:codex`) is searched as plain text, colon
included. An unknown `foo:bar` or a quoted value is not a qualifier, so it still becomes the
value: `branch: foo:bar` searches branches for `foo:bar`, and `branch: "tool:codex"` for
`tool:codex`.

**Popover.** The field at the top takes the cursor when the popover opens, so you can type right
away; ⌘F focuses it again, the (x) button clears it, and Escape clears a non-empty query (with
an empty field Escape behaves as before). The query is kept when the popover closes, until you
clear it or quit AgentBoard, and it is selected when the popover reopens so typing replaces it.
While a query is set:

- the Needs you, Review, Running and Unconcluded sections show only matching sessions, with a
  count in each header, and Unconcluded opens if it has matches;
- matching Snoozed and Done sessions appear in their own sections at the bottom;
- a line under the field says how many sessions the query hides, in red when one of them needs
  you or was interrupted.

The menu bar icon, its color and the flash always reflect every session, whatever the query.

**Dashboard.** The search box in the toolbar filters the session list together with the state
sidebar and the tool and project pickers. The sidebar badges, including **All**, count the
sessions left after the pickers and the query, so they always match the list. When the filters
hide a session that needs you or was interrupted, a red line under the sidebar says how many,
with a **Clear** button that resets the search and both pickers and selects **All**.

## Links & integrations

AgentBoard has no built-in knowledge of any specific ticket system, team, or host — it's meant
to work unmodified on anyone's Mac. Everything environment-specific comes in through one of
these generic mechanisms, all handled in `core/agentboard/links.py`:

1. **`agentboard link` / `agentboard status`** — any script or skill reports a link, or sets a
   free-form current-activity label, for the session it's running in:

   ```
   agentboard link --kind pr|ticket|url --ref REF [--url URL] [--state OPEN|MERGED|CLOSED|DONE] [--title T] [--branch B] [--session TOOL:ID]
   agentboard status --text "phase/label" [--ttl MIN] [--session TOOL:ID]
   agentboard status --model MODEL_ID [--context-tokens N] [--session TOOL:ID]
   agentboard title --text "what this session is doing" [--session TOOL:ID]
   ```

   A PR reported this way counts as the session's own work (see below). `--branch` records the
   PR's head branch (default: the session's current branch); a link keeps its branch when the
   session later moves on. A status text is shown as the row's title while it lasts, with the
   session's own title under it. `--model` (any model id) and `--context-tokens` (the session's
   context size now) fill the row's model · context tag for an agent AgentBoard cannot read
   itself; give any of `--text`, `--model` and `--context-tokens`, and a context size never
   replaces a newer measurement (see Plan usage and context). `agentboard title` names the session until the work moves on
   far enough for a generated retitle; a title you set yourself in Claude Code (`/rename`) is
   never replaced.

   If `--session` is omitted, the session is resolved in order: a session-identifying
   environment variable the calling tool set (`CLAUDE_CODE_SESSION_ID`, `CODEX_SESSION_ID` /
   `CODEX_THREAD_ID`), then the caller's ancestor process tree matched against a recorded
   session pid, then the most recently active session whose cwd matches `$PWD`. If none of that
   resolves, the command prints a warning to stderr and still exits 0 — a missing or confused
   AgentBoard should never break the caller.

2. **Drop directory** — for tools that can't exec the CLI, drop a file in
   `$AGENTBOARD_HOME/inbox/*.json` shaped like
   `{"session": "tool:id" | null, "cwd": "...", "links": [...], "status": "...", "model": "...",
   "context_tokens": 51200, "context_at": "ISO 8601"}` (every field optional; `context_at`
   defaults to when the file is read, and a time later than that is clamped to it). It's ingested (using the same session resolution) and
   deleted the next time `agentboard snapshot` runs.

3. **Provider commands** — list external integrations in `$AGENTBOARD_HOME/config.json`:

   ```json
   {"providers": [{"name": "...", "command": ["..."]}]}
   ```

   `agentboard refresh-links` runs every provider on each run (30s timeout each), passing the open sessions as JSON on
   stdin, and reads back `[{"session": "tool:id", "links": [...]}]` from stdout. This is how a
   team-specific integration plugs in without any code in AgentBoard itself; see
   `examples/providers/` for a fully generic example (reads git trailers).

4. **Plan usage files** — any agent or tool keeps `$AGENTBOARD_HOME/usage/<source>.json` with its
   plan-usage windows; each source gets its own line in the popover footer. The shape is in Plan
   usage and context above.

5. **Built-in generic detectors**, both optional and silently skipped if unavailable:
   - A GitHub PR for the session's current branch, via `gh pr list` — only if `gh` is installed,
     and only during `refresh-links` (the one command allowed to touch the network). Results are
     cached in the `links` table with the PR's head branch, creation and merge/close times; an
     `OPEN` PR is re-checked after 15 minutes, `MERGED`/`CLOSED` never again, and a branch change
     looks again. Every other `OPEN` PR link a session has (one it reported from `main`, say) is
     re-checked by URL with `gh pr view` on the same schedule.
   - Ticket references matched against the branch name and first prompt, using
     `"ticket_patterns": [{"regex": "...", "url": "https://.../{ref}"}]` in `config.json`.
     **Empty by default** — nothing is matched until you configure a pattern.

A link with `state: DONE`, or every PR on the session's current feature branch `MERGED`/`CLOSED`
with no prompt since, marks the session `done` automatically (see State above).

**Owned PRs and "awaits you".** A PR is the session's own when the session reported it
(`agentboard link`, an inbox drop, a provider) or `gh` found it on the session's branch created
during the session; a PR inherited from the branch is shown but never keeps the session on the
board. An owned, open PR **awaits you** until someone other than automation reviews or comments
on it. `refresh-links` reads the PR's reviews and conversation comments and ignores GitHub bot
accounts, the PR's author, and any login listed in `config.json`:

```json
{"automation_logins": ["my-reviewer-bot", "my-author-bot"]}
```

`automation_logins` is empty by default; it is how a setup that runs extra accounts (a separate
reviewer bot, an author bot) declares them, and any integration may add to it. When the PR's
author *is* the logged-in account (you and the session share one account), that account's
reviews and comments count once they come while the session isn't mid-turn, and the reason reads
"PR #N open, no response yet". When the logged-in account is not the PR's author (you are the
default `gh` login and sessions open PRs with a separate token), your reviews and comments count
like anyone else's.

**PR buttons.** Popover and dashboard rows show one small button per PR (`#N`, green open, purple
merged, red closed, outlined when it awaits you) that opens it in the browser without focusing
the terminal; past three PRs a "+N" opens the detail view, whose Links section lists every link
with its state.

**Titles that follow the work.** Claude Code names a session once, so a long-lived session keeps
its opening summary. `refresh-links` retitles a session once it has 5 new prompts since its last
retitle (or changed branch) by running the local `claude` CLI with Haiku on a short summary of
its recent prompts and replies, at most 5 sessions per run and once an hour per session. Tune or
turn it off in `config.json`:

```json
{"retitle_command": ["claude", "-p", "--model", "haiku", "--tools", "", "--strict-mcp-config",
                     "--no-session-persistence", "Name this session..."],
 "retitle_min_prompts": 5, "retitle_max_per_run": 5}
```

`"retitle_command": null` disables it; any command that reads the context on stdin and prints
a title works. The command runs with `AGENTBOARD_HOOK_SKIP=1`, so its own Claude session never
shows up on the board. The context it reads is copied from other sessions, so keep it
tool-free: the default passes `--tools ""` and `--strict-mcp-config` so the model cannot read
files or call anything while naming a session.

### Updates (retained notes)

Sessions that run unattended print a final message every turn, and most of it is noise ("Quiet
tick, nothing changed."). The few messages a human actually has to read (a request for you, a
PR opened or merged, CI red, a plan ready, a repair, an API outage) are kept as **notes**: a
durable, cross-session list shown as **Updates** in the popover and the session detail, until you
dismiss them.

The hook classifies each parent (non-subagent) `Stop` message with generic prose rules in
`core/agentboard/notes.py`: a line starting with a marker (`USER_ACTION:` by default) is a
`needs_you` note in any turn; in an *unattended* turn (its latest parent, non-subagent prompt
starts with one of the configured prefixes, `# Autonomous loop` by default, or is one of the
session's scheduled crons) a transient failure
(`API Error`, `Request timed out`, ...) is an `error` note, idle and progress one-liners are
dropped, and a milestone (an `owner/repo#N` or `PR #N`, merged, approved, CI red/green, pushed,
opened, deployed, blocked, a URL, ...) or any message of 300+ characters is an `update` note.

Loop sessions mostly restate what is waiting, so these count as quiet (dropped, however long):

- a message that starts with idle wording: "Quiet", "Idle", "Nothing changed", "Nothing else
  changed", "Nothing new", "No change(s)", "Unchanged", "Still ..." ("Still quiet", "Still
  waiting"), "Holding", "Standing by", "Awaiting", ...;
- a message whose lead sentence only restates what waits on you ("Two things are still waiting
  on you:", "Both items are still waiting on you", "PR #5 still needs your `/ack-tested`", "PR #5
  is still blocked on your review", "The plan is still waiting for your approval"), unless that
  sentence also reports an event (merged, opened, deployed, filed, ...) or carries a link.

Either kind is still an `update` when it reports a hard event anywhere in the message (merged,
opened, filed, failed, CI red: "Nothing changed on the plan, but CI went red on PR #5"; not when
negated or pending, "PR #5 has not merged yet", "no tests failed", "None of the checks failed",
"needs to be merged", nor adjectival, "the merged PR", "PR #5 (merged yesterday)", "merged-upstream"
(while "I merged PR #7" counts), nor as
background in a since/until/once/if/unless clause, "the queue is empty since the logging work
merged"), or
news (a repair, a bug, a fix, something found or wrong, a heads-up) in 160+ characters. Softer events behind a quiet lead ("Nothing changed: PR #5 is
approved and deployed") restate a known state and stay quiet. A progress note on work another
agent is still doing ("still building", "still working", "holds the lock", "takes no action", "no
new work", "nothing new this tick", ...) is dropped too when it is under 400 characters and carries
no link, even without idle wording. A PR or issue reference makes a
milestone only the first time the session's notes (open or dismissed) mention that reference: "The
builder is still fixing PR #4's findings" is kept once, not every tick, while an event about it
("PR #4 merged", "opened", "deployed", "CI went red", "filed", "is now blocked") is kept every
time. A status ("ready for review", "in Pending") counts unless its own clause names only
references already seen ("Tick: PR #4 is in hand; the plan is ready for your review" is kept);
ordinals such as "step #2" are not references. A
repeat of an open note (same session, same kind, same first 200 characters once timestamps, clock
times, durations and tick counts are ignored; errors compare their first line only) bumps its count
(shown as `×N`) instead of adding a row; a repeat of a dismissed note starts a new one.

**Injected prompts are not replies.** Claude Code starts some turns itself: a background task
finishing (`<task-notification>`), a message from another agent (`<agent-message ...>`) or
another session (`<cross-session-message ...>`). The hook records each parent prompt's kind
(`human`, `loop`, `cron` or `injected`, by `prompt_injected_prefixes`). An injected prompt still
makes the session `running` (Claude works on it), but it is not you answering: it does not reopen
a session you marked done, release an open `needs_you` note, turn a loop session into an attended
one (the turn keeps the kind of the prompt before it), count toward retitling, or replace the
session's first/last prompt. The knobs live in `config.json`, and those are the only ones:

```json
{"note_markers": ["USER_ACTION:"], "note_unattended_prefixes": ["# Autonomous loop"],
 "prompt_injected_prefixes": ["<task-notification>", "<agent-message", "<cross-session-message"]}
```

After changing `note_unattended_prefixes` or `prompt_injected_prefixes`, run `agentboard notes
--rebuild --reset` so the recorded prompts are re-classified and the notes replayed under the new
lists.

Anything can also add a note directly:

```
agentboard note --kind needs_you|update|error --text T [--session TOOL:ID]
agentboard notes [--session TOOL:ID] [--kind K ...] [--all] [--json]
agentboard notes --dismiss ID [ID ...] | --dismiss-all [--session TOOL:ID] [--kind K ...]
agentboard notes --rebuild [--days N] [--reset]
```

`--dismiss-all` dismisses every open note, listed in the app or not; `--session` narrows it to one
session and `--kind needs_you|update|error` (one or more) to those kinds, so
`notes --dismiss-all --session claude:ID --kind update error` clears a loop session's updates but
keeps its requests. `--kind` also filters a listing.

`note` resolves the session exactly like `agentboard link` and, like it, warns and exits 0 when
it cannot. Inbox drop files and provider rows take the same shape in an optional `"notes"` key,
`[{"kind": "update", "text": "..."}]`; an entry with another kind or an empty text is skipped.

`notes --rebuild` replays the `Stop` messages already in the `events` table from before the notes
table existed (`install.sh` runs it after its backfill step), and a second run imports nothing.
Each rebuild first classifies any recorded prompt that has no kind yet; `--reset` re-classifies
all of them with the current prefix lists (keeping a prompt the hook recorded as a cron tick).
`--rebuild --days N` imports only the last N days, but still marks the older events as replayed, so
a later plain `--rebuild` skips them; only `--reset` revisits them. `--rebuild --reset` re-applies
changed rules: it drops the open notes it made itself (never dismissed ones, never ones added by
`note`, the inbox or a provider) and replays every recorded `Stop`, without reopening anything you
dismissed. Transcript backfill does not record per-turn `Stop` events, so notes start with what the
hook has recorded. `agentboard prune` drops dismissed notes older than its `prune_days` window (never,
when it is `0`), along with every note of a pruned session.

**An open `needs_you` note puts its session in `needs_you`** (red icon, notification, Needs you
section, the note's marker line as the waiting reason) until a newer prompt arrives in that
session or you dismiss the note. Manual done and snooze still win, and a permission prompt's own
reason wins over the note's. The note stays in Updates after a prompt releases the state; if the
next turn repeats the request, the note's count goes up and the session is red again.

In the popover, Updates sits above Needs you with a count and a clear button: each row shows the
kind (red bubble for needs you, red triangle for an error), the project, the first line, `×N` and
the time; a click opens the dashboard on the owning session, scrolled to that note's full text
and outlined (the session's context menu still has Focus), the chevron expands the full text in
place, and (x) dismisses it. The section is compact so it never pushes the sessions out of view: it lists
every `needs_you` note, but at most 3 other rows, with **Show all N** to expand it in place and
**Show fewer** to collapse it again. A session with two or more open update/error notes (a loop
session, in practice) gets one row: its newest note's text and time and `×N notes`, the session's
open update/error count; its chevron lists the notes (each with its own x), and its (x) dismisses
all of that session's open update/error notes (`notes --dismiss-all --session TOOL:ID --kind update
error`), never its `needs_you` notes, which always keep rows of their own. With an empty search,
**Clear all** runs `notes --dismiss-all`: every open note, including ones past the 20-per-session
list and on sessions the popover does not show. With a search, the button reads **Clear matching**
and dismisses every note the query keeps: all open notes of each session it matches on the
session's own fields, plus the notes it matches by their own text elsewhere. Its tooltip says
which. The session detail and the dashboard are not grouped or capped. The popover query filters it note by note: a note shows when the query
matches its own text or its session's fields (never a sibling note's text; a session's waiting
reason and last assistant message can repeat its newest note, so a term from them keeps all of its
notes). When a query hides an open `needs_you` note on a session in Review, Snoozed or Done, the
notice under the search field counts it in red ("1 update needs you"), and the dashboard sidebar
warns the same way; a hidden session in Needs you or Interrupted is counted once, as a session. A
session row shows a small badge with its number of open updates, the session detail lists them in
full with a Dismiss button each, and the dashboard sidebar shows the total the filters keep,
counted note by note like the popover's Updates header. Totals count all open notes
(`notes_count`), not only the listed ones; the snapshot lists the newest 20 per session plus any
older open `needs_you` note (a request is never capped out), so a header shows "Updates (20 of 25)"
when some are not listed.

After an upgrade that changes these rules, `agentboard notes --rebuild --reset` re-applies them to
the recorded history: it drops the open notes it made and replays every `Stop` through the new
rules, so earlier loop noise disappears, and it never reopens a dismissed note.

Terminal focus/resume support iTerm2 (`ITERM_SESSION_ID`) and Apple Terminal
(`TERM_SESSION_ID`); for anything else, `agentboard focus`/`resume` just activates the app by
`TERM_PROGRAM`.

**Sessions inside tmux.** The hook records the tmux server socket and pane from `$TMUX` and
`$TMUX_PANE` (environment only, no extra shell-out), and an event from a plain terminal clears
them again. Inside tmux the inherited iTerm/Terminal ids belong to whatever started the tmux
server, so `agentboard focus` ignores them and goes through tmux instead: it looks up the pane's
current tmux session (`display-message`; a gone pane or dead server fails with
`outcome=tmux_pane_gone`), selects its window and pane, then brings forward an iTerm2 or Terminal
tab that has a client attached to that tmux session. Those probes only look at apps that are
already running and never launch or activate one on a miss. With no attached client found, it
opens a **new** iTerm window running `tmux -S <socket> attach-session -t '$<id>'` (a shared
attach, never `-d`), or a Terminal window when iTerm isn't installed. tmux is found on `PATH`,
then at `/opt/homebrew/bin/tmux` and `/usr/local/bin/tmux`. Rows of a live tmux session show a
`tmux · <name>` tag (the name comes from one bounded `tmux list-panes -a` call per tmux server
per snapshot, so a rename shows up on the next poll), the detail view has a tmux row, and
dashboard search matches the tmux session name. Resume is unchanged.

## Backfill

`agentboard backfill [--days 30]` imports Claude Code transcripts
(`~/.claude/projects/*/*.jsonl`) and Codex rollouts (`~/.codex/sessions/*/*/*/rollout-*.jsonl`)
modified within the window. It streams files rather than loading them whole, and is idempotent:
re-running it never overwrites a field a live hook already set, only fills in ones that are
still `NULL` — except the title, which hooks never write: a newer Claude Code title replaces an
older one, a `/rename` (custom title) beats any generated one, and a first-prompt fallback never
replaces a real title.

The open-todo count (`todos_open`) needs the whole transcript (task updates are deltas), so it is
scanned incrementally: each session stores the byte offset the scan reached and the task state at
that point, and the next run (the hook starts a single-session `backfill --session` after every
`Stop`) reads only what was appended since. A trailing line Claude Code is still writing is left
for the next run, and a transcript that shrank or was rewritten restarts the scan from the
beginning. Transcripts are parsed with no database transaction open; each session is then written
in its own short transaction, so hooks never wait on a long backfill.

The model and context size (see Plan usage and context) are read from the same tail scan and follow
their own rule on hook and backfill rows alike: a parsed model replaces the stored one, a parsed
context size replaces it only when it was measured at least as recently, and neither is cleared
(the one exception is a `/compact` `SessionStart` without a size, which marks the size unknown).
A Codex rollout's tail scan also picks up the plan's rate limits from the newest `token_count`
event it parses, which the backfill keeps in `usage/codex.json` (the newest across all rollouts).

## Configuration (config.json)

Everything the CLI can be told lives in one optional JSON object, `config.json` in the data
directory (`~/Library/Application Support/AgentBoard/config.json`, or
`$AGENTBOARD_HOME/config.json`; `agentboard paths` prints the directory). The file does not exist
until you create it, every key is optional, and a missing or unreadable file means every default.
It is read fresh by each command, so an edit applies on the next snapshot, hook event or
`refresh-links`. The app's own settings (Settings window) are separate and live in its macOS
defaults (`io.github.agentboard`). Every key core reads is registered in
`core/agentboard/config_keys.py`:

| Key | Type | Default | What it does |
|-----|------|---------|--------------|
| `providers` | list of objects | none | External integrations `refresh-links` runs: `name` labels one in the log, `command` is the argv to run (see Links & integrations). |
| `ticket_patterns` | list of objects | none | Ticket references matched against each session's branch and first prompt: `regex` is a regular expression, `url` a link with `{ref}` where the match goes. |
| `automation_logins` | list of strings | none | GitHub logins (a reviewer bot, an author bot) whose reviews and comments are not a response from you when deciding whether a PR awaits your review. |
| `note_markers` | list of strings | `["USER_ACTION:"]` | A final-message line starting with one of these becomes a `needs_you` note; `[]` turns marker detection off. |
| `note_unattended_prefixes` | list of strings | `["# Autonomous loop"]` | A turn whose prompt starts with one of these is unattended (loop) work. |
| `prompt_injected_prefixes` | list of strings | `["<task-notification>", "<agent-message", "<cross-session-message"]` | A prompt starting with one of these was injected by Claude Code (a finished background task, another agent's or session's message): it counts as activity but never as you replying. |
| `retitle_command` | argv list, string, or `null` | the local `claude` CLI with Haiku | Reads a session's recent context on stdin and prints a new title; `null` turns retitling off. |
| `retitle_min_prompts` | integer >= 1 | `5` | New prompts since the last retitle before a session is retitled again. |
| `retitle_max_per_run` | integer >= 0 | `5` | At most this many sessions retitled per `refresh-links`; `0` turns it off. |
| `prune_days` | integer >= 0 | `90` | `prune` deletes sessions idle this many days (never one with an open `needs_you` note, a PR awaiting you, an active snooze or a live process) and dismissed notes older than this; `0` never deletes. |
| `compact_days` | integer >= 0 | `7` | `prune` deletes tool-use and subagent events older than this many days; `0` never compacts. |
| `log_level` | `debug`/`info`/`warn`/`error` | `info` | The diagnostic log's level; `AGENTBOARD_LOG_LEVEL` wins over it. |

```json
{
  "providers": [{"name": "trailers", "command": ["/path/to/provider"]}],
  "ticket_patterns": [{"regex": "[A-Z][A-Z0-9]+-\\d+", "url": "https://tickets.example.com/browse/{ref}"}],
  "automation_logins": ["my-reviewer-bot"],
  "note_markers": ["USER_ACTION:"],
  "note_unattended_prefixes": ["# Autonomous loop"],
  "prompt_injected_prefixes": ["<task-notification>", "<agent-message", "<cross-session-message"],
  "retitle_min_prompts": 5,
  "retitle_max_per_run": 5,
  "prune_days": 90,
  "compact_days": 7,
  "log_level": "info"
}
```

## CLI reference

```
agentboard hook --tool claude|codex
agentboard snapshot [--json] [--all]
agentboard paths [--json]
agentboard mark <tool> <session_id> done|open|snooze [--hours N] [--note TEXT]
agentboard link --kind pr|ticket|url --ref REF [--url URL] [--state ...] [--title T] [--branch B] [--session TOOL:ID]
agentboard status [--text TEXT] [--ttl MIN] [--model ID] [--context-tokens N] [--session TOOL:ID]
agentboard title --text TEXT [--session TOOL:ID]
agentboard note --kind needs_you|update|error --text T [--session TOOL:ID]
agentboard notes [--session TOOL:ID] [--kind K ...] [--all] [--json]
agentboard notes --dismiss ID [ID ...] | --dismiss-all [--session TOOL:ID] [--kind K ...]
agentboard notes --rebuild [--days N] [--reset] [--json]
agentboard backfill [--days 30] [--session TOOL:ID --transcript PATH]
agentboard refresh-links
agentboard install-hooks [--claude] [--codex] [--dry-run]
agentboard uninstall-hooks [--claude] [--codex] [--dry-run]
agentboard enable-codex-hooks [--dry-run]
agentboard focus <tool> <session_id>
agentboard resume <tool> <session_id> [--print]
agentboard map-codex --session ID --cwd DIR [--at TIME]
agentboard prune [--days N] [--compact-days N] [--dry-run] [--auto] [--vacuum]
agentboard logs [--since 1h|ISO-8601] [--grep TEXT] [--lines N] [--app]
```

`hook`, `map-codex` and `backfill --session` are what the hooks run; you never need to call them
yourself. `prune` is the retention sweep described under Data: `--days` and `--compact-days`
override `prune_days` and `compact_days`, and `0` means never (until this release `--days 0`
deleted every session; it now deletes none). `--auto` runs only when the last auto run was 24 hours
ago or more and may vacuum on its own; `--vacuum` always vacuums afterwards; `--dry-run` prints the
counts and changes nothing (with `--auto` it ignores the daily gate, so it previews the next real
run). `install-hooks --bin-path` only previews a different hook path for `install.sh
--dry-run`.

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

**In the app.** A one-line banner under the popover's search field and at the top of the
dashboard's session list appears only when something is wrong: **Snapshot failed: …** (the CLI or
the database is unreadable; the last good snapshot stays on screen), **Waiting on the CLI for N s**
(a snapshot has been running for over 30 seconds), **No snapshot yet; the CLI is not answering**
(nothing has come back within 45 seconds of launch) or **<action> failed: …** (a mark, focus,
dismiss or prune that did not work; it clears after 15 seconds or with its x; a prune that fails
at launch, usually because `install.sh`'s own backfill still holds the database, is only logged
and retried once a minute later, and the banner shows only if the retry fails too). Its **Open log**
button reveals `agentboard.log`. It never changes the menu bar icon. Settings → Diagnostics adds a
`hook-errors.log` row (size, last write, **Reveal**) when that file is not empty.

**Levels.** `debug`, `info` (the default), `warn`, `error`. Set the level with the
`AGENTBOARD_LOG_LEVEL` environment variable, or `"log_level": "debug"` in
`$AGENTBOARD_HOME/config.json` (the variable wins). Because the app runs `snapshot` every 3 to
15 seconds, `snapshot` and `paths` log at debug; at the default level they add a line only when
they fail or take longer than a second. The same goes for the children hooks spawn once per
assistant turn (`backfill --session`, `map-codex`): a routine success is debug, while a failure,
a slow run, an unexpected backfill result or an unmapped Codex session is info. Free text passed with `--note`, `--text` or `--title` is
logged as `<redacted:N chars>`.

**Rotation.** Growth is bounded: before a write would push `agentboard.log` past 2 MB it is
renamed over `agentboard.log.1`, replacing it. There is never a `.2`, so the log never takes more
than about 4 MB however long AgentBoard runs. (When several processes write at the moment of a
rotation, a writer that loses the race appends to the file being rotated, so `.1` can exceed
2 MB by a few lines.) Rotation normally takes an advisory `flock`; if `AGENTBOARD_HOME` is on a
filesystem that does not support one (some network or SMB volumes), it rotates without the lock,
so the cap still holds and the only cost is a rare race that can overwrite `.1` early.
`uninstall.sh --purge-data` removes both files.

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
`agentboard logs --grep cli.focus --lines 5`. Each click writes a `cli.focus.begin` line (the
recorded term, tty and iTerm id) before any AppleScript runs, then one `cli.focus` line whose
`outcome` says what happened; a `begin` with no matching `cli.focus` line means the process
died mid-attempt:

- `matched_uuid`: iTerm found the session by its recorded unique id. A wrong tab here means the
  recorded `iterm_id` is stale.
- `matched_tty`: no session had that id, so the first session on the recorded `tty` was chosen. A
  tty reused by a newer tab is the usual cause of a wrong tab.
- `matched_unknown`: the focus script exited 0 but did not say how it matched (an older iTerm
  script's bare `found`, or an Apple Terminal answer the CLI did not recognise; see `osa_out`).
- `activated_only`: nothing matched; the terminal app was only brought to the front (exit code is
  still 0). `osa_out`/`osa_err` show what the AppleScript said, e.g. a missing Automation
  permission.
- `activate_failed`: the `open -a` fallback itself exited non-zero (see `open_rc`).
- `not_found`: the Apple Terminal focus script failed outright (non-zero `osa_rc`, see
  `osa_err`). A failing iTerm script falls back to `open -a` instead, so it ends in
  `activated_only` or `activate_failed`.
- `timeout`: an AppleScript or `open -a` call did not finish within 10 seconds (often a pending
  Automation permission prompt) and was killed; logged at warn and `focus` exits 1. `resume`
  does the same and logs `outcome=timeout`.
- `no_terminal` / `unsupported_terminal`: the session has no recorded terminal, or one AgentBoard
  cannot drive.
- `tmux_client_focused`: the session runs inside tmux, and an iTerm2 or Terminal tab whose tty is
  an attached tmux client (`client_tty`) was brought to the front.
- `tmux_attached_new_window` / `tmux_attached_terminal`: no attached client was found in a
  running iTerm2 or Terminal, so a new iTerm window (Terminal when iTerm isn't installed, see
  `iterm_installed`) was opened running `tmux attach-session` to the session.
- `tmux_pane_gone`: the recorded tmux pane or its server no longer exists; nothing opens and
  `focus` exits 1. `tmux_missing` means no tmux binary was found, and `tmux_attach_failed` that
  the attach AppleScript exited non-zero.

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

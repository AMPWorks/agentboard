# AgentBoard — spec

All setup/installation is done by scripts in the repo (`scripts/install.sh`,
`scripts/uninstall.sh`, `scripts/build-app.sh`); README documents only those. Agents and
humans install by running the scripts, never by hand-editing config.

A local-only macOS menu bar dashboard showing the state of every Claude Code and Codex CLI
session on this Mac: what needs the user, what has output to review, what is running, and what
older work is still unconcluded.

## Components

```
agentboard/
  core/                     Python 3 (stdlib only, >=3.11)
    agentboard/             package
      db.py                 schema + connection (WAL, busy_timeout)
      hook.py               hook entrypoint logic
      reconcile.py          state derivation -> snapshot
      backfill.py           import past transcripts
      links.py              link CLI, inbox, providers, generic detectors
      cli.py                argparse CLI
    bin/agentboard          executable shim -> agentboard.cli:main
    tests/                  unittest, run with `python3 -m unittest discover core/tests`
  app/                      SwiftPM package, macOS 14+, SwiftUI, no third-party deps
    Package.swift
    Sources/AgentBoard/...
  scripts/build-app.sh      swift build -c release, assemble ~/Applications/AgentBoard.app
                            (Info.plist with LSUIElement=1, ad-hoc codesign)
  scripts/install.sh        one-shot setup on any Mac: checks prerequisites (python3 >= 3.11,
                            Xcode CLT/swift), builds + installs the app (build-app.sh), installs
                            hooks for whichever of claude/codex are present, runs backfill,
                            optionally enables launch at login. Flags: --no-hooks, --no-app,
                            --dry-run, --prefix. Idempotent; safe to re-run to upgrade.
  scripts/uninstall.sh      removes hooks (restoring nothing else), app, optionally data dir
  README.md
```

Data dir: `~/Library/Application Support/AgentBoard/` containing `agentboard.db`.
Override with env `AGENTBOARD_HOME` (tests must use a temp dir).

## Hook (`agentboard hook --tool claude|codex`)

Registered for every event in both tools. Requirements:

- Read JSON from stdin. **Print nothing to stdout** (Claude adds stdout of SessionStart /
  UserPromptSubmit to model context). Always exit 0, swallow every exception (log to
  `$AGENTBOARD_HOME/hook-errors.log`, capped ~1 MB).
- Fast: target < 50 ms. No network. Only shell out to git (`git -C cwd branch --show-current`,
  1 s timeout) on SessionStart / UserPromptSubmit or when the session has no branch yet.
- Capture from env: `ITERM_SESSION_ID`, `TERM_PROGRAM`, `TERM_SESSION_ID`. Capture
  `os.getppid()` as the agent process pid (verify: for Claude the hook's parent may be a shell
  spawned by claude; walk up with `ps -o ppid= -p` at most 3 levels to find a process whose
  command contains `claude` or `codex`; record that pid).
- Insert into `events`; upsert `sessions`.

Claude events: SessionStart(source, model), UserPromptSubmit(prompt), PreToolUse/PostToolUse/
PostToolUseFailure(tool_name), PermissionRequest(tool_name), Notification(notification_type,
message), Stop(last_assistant_message), StopFailure(reason, error_message), SubagentStart/
SubagentStop(agent_id), SessionEnd(reason). Common: session_id, transcript_path, cwd,
hook_event_name. Events carrying `agent_id` come from subagents: record them but they only
ever count as "running" activity, never Stop/review for the parent.

Codex events (hooks.json): SessionStart, SessionEnd, UserPromptSubmit(prompt),
PreToolUse/PostToolUse, PermissionRequest, Stop(last_assistant_message), Interrupt,
SubagentStart/SubagentStop, PreCompact/PostCompact. Common: session_id, transcript_path, cwd,
hook_event_name, model, turn_id. No Notification event.

Truncate stored text (prompt / message / last_assistant) to 4000 chars.

## Schema

```
events(id INTEGER PK, ts TEXT, tool TEXT, session_id TEXT, event TEXT, agent_id TEXT,
       notification_type TEXT, tool_name TEXT, text TEXT, extra TEXT /*json*/)
sessions(tool TEXT, session_id TEXT, cwd TEXT, branch TEXT, title TEXT,
         first_prompt TEXT, last_prompt TEXT, last_assistant TEXT,
         started_at TEXT, updated_at TEXT, last_event TEXT, last_event_at TEXT,
         waiting_reason TEXT, pid INTEGER, iterm_session_id TEXT, term_program TEXT,
         transcript_path TEXT, origin TEXT /*hook|backfill*/, interactive INTEGER,
         ended_at TEXT, PRIMARY KEY(tool, session_id))
triage(tool, session_id, status TEXT /*done|snoozed|open*/, until TEXT, note TEXT,
       updated_at TEXT, PRIMARY KEY(tool, session_id))
links(tool, session_id, kind TEXT /*pr|ticket*/, ref TEXT, url TEXT, state TEXT,
      title TEXT, checked_at TEXT, PRIMARY KEY(tool, session_id, kind, ref))
```
Add a `schema_version` pragma (user_version) and simple migrations.

## State derivation (`reconcile.py`)

Per session, compute `state` in priority order:

1. `done` — triage.status = done, OR every linked PR is MERGED/CLOSED, OR any link with state DONE
   PR state is merged/closed (see Links). A new UserPromptSubmit after the triage timestamp
   reopens it (triage reset to open).
2. `snoozed` — triage.status = snoozed and until > now (falls through when expired).
3. `needs_you` — last non-subagent event is PermissionRequest, or Notification with type in
   {permission_prompt, elicitation_dialog, agent_needs_input}, or StopFailure, and no later
   activity event. `waiting_reason` = tool_name / notification message / error.
4. `running` — last event is an activity event (UserPromptSubmit, Pre/PostToolUse, Subagent*,
   PreCompact/PostCompact) AND process alive (or no pid known and last event < 10 min old).
5. `interrupted` — would be running but process dead or activity stale > 30 min. Shown with
   needs_you group styling but distinct label.
6. `review` — last turn finished (Stop, Interrupt, Notification idle_prompt, SessionEnd after
   Stop) and not done; `updated_at` within 24 h.
7. `unconcluded` — as review but older than 24 h, or ended sessions never marked done.

Liveness: `kill -0 pid` (os.kill(pid, 0); PermissionError counts as alive). For Claude, also
treat `~/.claude/sessions/<pid>.json` with matching `sessionId` as confirmation; files there
may contain only `{pid, sessionId}` — use `status`/`waitingFor` only if present.
Codex rollout `event_msg` payload types `task_started` / `task_complete` / `turn_aborted` may
be used as a fallback when hooks are missing for a codex session.

Hide non-interactive sessions by default (`interactive=0`): Claude `claude -p` / SDK workers
(detect: transcript entries `"entrypoint"` / `userType`, or session-registry `kind`/
`entrypoint` not interactive/cli; if unknown assume interactive), Codex rollouts whose
session_meta `source` is `exec`. Snapshot includes them with `interactive:false`; app filters.

## Links & integrations (`links.py`)

AgentBoard must be portable: no knowledge of any specific user, team, skill, ticket system or
host is built in. Everything environment-specific arrives through these generic mechanisms:

1. **`agentboard link` CLI** — any skill/script/hook reports a link for the session it runs in:
   `agentboard link --kind pr|ticket|url --ref REF [--url URL] [--state OPEN|MERGED|CLOSED|DONE]
   [--title T] [--session TOOL:ID]`. Also `agentboard status --text "phase/label" [--ttl MIN]`
   to set a free-form current-activity label shown on the row (replaces any skill-specific
   status files). Session resolution when `--session` is omitted, in order: env vars that
   identify the session if the agent sets any (verify empirically which, e.g. CLAUDE_SESSION_ID /
   CODEX_THREAD_ID; skip if none), else walk the caller's ancestor pids and match a recorded
   session pid, else most recently active session whose cwd equals/contains $PWD. Exit 0 with
   a stderr warning if unresolved (callers must never break because AgentBoard is absent or
   confused). A state of MERGED/CLOSED/DONE on links counts toward auto-done.
2. **Drop directory** — `$AGENTBOARD_HOME/inbox/*.json`, each `{"session": "tool:id" | null,
   "cwd": "...", "links": [...], "status": "..."}`; ingested and deleted on next snapshot. For
   tools that can't exec the CLI.
3. **Provider commands** — `$AGENTBOARD_HOME/config.json` `"providers": [{"name": "...",
   "command": ["..."], "interval_minutes": 10}]`. `refresh-links` runs each (timeout 30 s),
   passing the open sessions as JSON on stdin; the command prints `[{"session": "tool:id",
   "links": [...]}]`. This is how a team-specific integration (e.g. a ticket-workflow skill's
   state files, a JIRA lookup) plugs in without code in AgentBoard. Ship one example provider
   under `examples/providers/` that is fully generic (e.g. reads git trailers), not tied to
   any team.
4. **Built-in generic detectors** (each optional, silently skipped if unavailable):
   - GitHub PR by branch via `gh` if installed+authenticated: `gh pr list --head <branch>
     --state all --limit 1 --json number,state,url,title` in cwd, only in `refresh-links`,
     cached (re-check OPEN after 15 min, never re-check MERGED/CLOSED). Skip default branches.
   - Ticket refs via `config.json` `"ticket_patterns": [{"regex": "[A-Z][A-Z0-9]+-\\d+",
     "url": "https://example.atlassian.net/browse/{ref}"}]` — **empty by default**; matched
     against branch and first prompt.

Snapshot never hits the network. No paths outside ~/.claude, ~/.codex and $AGENTBOARD_HOME are
read by default. The app must locate the CLI without a hard-coded user path (bundled copy in
the .app's Resources, then PATH, then UserDefaults override).

Terminal focus: support iTerm2 (ITERM_SESSION_ID) and Apple Terminal (TERM_SESSION_ID / tty via
AppleScript); for other terminals just activate the app by TERM_PROGRAM / bundle; report
`can_focus` accordingly. Resume opens in the same terminal app the session used when possible.

## Backfill (`agentboard backfill --days 30`)

Idempotent. Never overwrite fields that came from hooks (origin=hook) except filling NULLs.
- Claude: `~/.claude/projects/*/*.jsonl` (top-level only; skip `subagents/` dirs). Per file:
  session id = filename stem; cwd, gitBranch from entries; first & last real user prompt
  (skip tool_result / meta / command-caveat entries); last assistant text; first/last
  timestamps; whether last turn finished. Title: `summary`/`custom-title` entries if present,
  else first prompt first line.
- Codex: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`: session_meta (id, cwd, source,
  timestamp), user messages, last agent message, task_complete/turn_aborted, git branch if in
  session_meta.git.
- Only files with mtime within --days. Sessions imported by backfill get last_event='Stop'
  semantics (review/unconcluded) unless evidence they are mid-turn and alive.
- Must handle very large files efficiently (stream lines; for last-message, read tail).

## CLI

```
agentboard hook --tool claude|codex          (stdin payload)
agentboard snapshot [--json] [--all]         JSON to stdout (contract below); text table w/o --json
agentboard mark <tool> <session_id> done|open|snooze [--hours N] [--note TEXT]
agentboard backfill [--days 30]
agentboard refresh-links
agentboard install-hooks [--claude] [--codex] [--dry-run]
agentboard uninstall-hooks [--claude] [--codex]
agentboard focus <tool> <session_id>         activate iTerm2 tab by ITERM_SESSION_ID (osascript)
agentboard resume <tool> <session_id> [--print]  open new iTerm2 tab running resume cmd
agentboard prune [--days 90]
```

install-hooks: back up the file first (`<file>.agentboard-bak-<ts>`), then append one matcher
group `{"hooks":[{"type":"command","command":"<abs path>/bin/agentboard hook --tool claude",
"timeout":5}]}` to each event in `~/.claude/settings.json` (preserving existing hooks and all
other keys / ordering; idempotent — detect existing agentboard entry). For Codex, same in
`~/.codex/hooks.json` and print that the user must trust the new hooks via `/hooks` in Codex.
Never touch `~/.codex/config.toml`.

Resume commands: Claude `cd <cwd> && claude --resume <id>`; Codex `cd <cwd> && codex resume <id>`.
Focus: iTerm `ITERM_SESSION_ID` is `w0t1p0:<UUID>`; match `unique id` of iTerm sessions to UUID,
then select the tab/window and activate iTerm. Exit non-zero with message if not found.

## Snapshot JSON contract (consumed by the Swift app)

```json
{
  "generated_at": "ISO8601",
  "counts": {"needs_you": 0, "interrupted": 0, "running": 0, "review": 0,
             "unconcluded": 0, "snoozed": 0, "done": 0},
  "sessions": [
    {
      "key": "claude:<session_id>",
      "tool": "claude|codex",
      "session_id": "...",
      "state": "needs_you|interrupted|running|review|unconcluded|snoozed|done",
      "waiting_reason": "string|null",
      "status_text": "string|null",
      "title": "string",
      "cwd": "/abs", "project": "basename of cwd", "branch": "string|null",
      "first_prompt": "string|null", "last_prompt": "string|null",
      "last_assistant": "string|null",
      "started_at": "ISO", "updated_at": "ISO", "ended_at": "ISO|null",
      "alive": true, "interactive": true,
      "can_focus": true,
      "resume_command": "cd ... && claude --resume ...",
      "links": [{"kind": "pr|ticket", "ref": "ABC-123|#26", "url": "...", "state": "OPEN|MERGED|CLOSED|null", "title": "..."}],
      "triage": {"status": "open|done|snoozed", "until": "ISO|null", "note": "string|null"}
    }
  ]
}
```
Sorted: state priority (needs_you, interrupted, review, running, unconcluded, snoozed, done),
then updated_at desc. Default excludes done older than 7 days and non-interactive sessions;
`--all` includes everything. Must run in < 500 ms with a few thousand sessions.

## Mac app (SwiftUI, `app/`)

- `MenuBarExtra` with `.window` style. Label: SF Symbol (e.g. `square.stack.3d.up`) plus count
  of needs_you+interrupted when > 0 (and a subtle second count for review). Menu bar icon
  color: the most urgent session's color (needs_you red, interrupted orange, alive review teal,
  running green, closed amber; template glyph otherwise), rendered as a non-template image
  resolved for the system light/dark setting; filled variant when needs_you+interrupted > 0.
  Red flashes at 1 Hz when a session newly enters needs_you, until the popover opens / the
  dashboard becomes key / the session leaves needs_you (per-session acknowledgement; timeout
  and "never" options in Settings); Reduce motion shows an exclamation badge instead.
- Popover: compact grouped list (Needs you, Review, Running; Unconcluded collapsed with count),
  each row: tool badge (C / X), title, project · branch, relative time, waiting_reason.
  Row click = focus; context menu & hover buttons: Focus, Resume, Copy resume cmd, Mark done,
  Snooze 1d, Open PR/ticket. Footer: "Open Dashboard", "Refresh", "Quit".
- Dashboard `Window` scene: NavigationSplitView — sidebar of states with counts + filter by
  tool/project + search; list; detail pane showing title, cwd, branch, links, timestamps,
  first prompt, last prompt, last assistant message (scrollable, selectable, rendered as
  plain text or basic markdown via AttributedString), action buttons.
- Global hotkey ⌃⌥⌘A toggles the dashboard window (Carbon `RegisterEventHotKey`).
- Data: run `agentboard snapshot --json` (CLI located per Links section: bundled Resources copy, PATH, then UserDefaults `agentboardPath`) every 3 s while popover/window visible, every 15 s
  otherwise; also watch the db directory with DispatchSource / FSEvents and refresh (debounced
  500 ms). Run `agentboard refresh-links` every 10 min in background. Run `agentboard backfill
  --days 30` once at launch (background).
- Actions shell out to the CLI (`mark`, `focus`, `resume`, `resume --print` for copy).
- Notifications: post a UserNotification when a session transitions into needs_you (optional,
  off by default toggle in a small Settings view) — existing notify.py already notifies.
- Launch at login toggle via `SMAppService.mainApp`.
- Never block main thread on Process; decode with Codable; tolerate unknown fields/states.
- Appearance: Settings picker "System / Dark / Light", default **System** (UserDefaults key
  `appearance`); apply via `NSApp.appearance` (nil for System). Design and check every view in
  dark mode first (the user's default); use semantic colors only, no hard-coded light colors.

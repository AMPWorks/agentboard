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
  scripts/uninstall.sh      removes its own hooks (other hooks stay), app, optionally data dir
  README.md
```

Data dir: `~/Library/Application Support/AgentBoard/` containing `agentboard.db`.
Override with env `AGENTBOARD_HOME` (tests must use a temp dir).

## Hook (`agentboard hook --tool claude|codex`)

Registered for every event in both tools. Requirements:

- Read JSON from stdin. **Print nothing to stdout** (Claude adds stdout of SessionStart /
  UserPromptSubmit to model context). Always exit 0, swallow every exception (log to
  `$AGENTBOARD_HOME/hook-errors.log`, capped ~1 MB).
- Fast: target < 50 ms. No network. The branch is re-read on SessionStart / UserPromptSubmit and
  (added later) on parent Stop / SessionEnd, so a branch created mid-turn is seen once the
  session goes idle or exits, and whenever the session has no branch yet. It is read from the
  repository's HEAD file (following a worktree's `gitdir:` file); only when that is missing or
  in a form AgentBoard doesn't parse does it shell out to git (`git -C cwd branch
  --show-current`, 1 s timeout), since one git call costs most of the 50 ms budget. While
  `AGENTBOARD_HOOK_SKIP` is set (AgentBoard's own retitle command) the hook records nothing.
- Capture from env: `ITERM_SESSION_ID`, `TERM_PROGRAM`, `TERM_SESSION_ID`. Capture
  `os.getppid()` as the agent process pid (verify: for Claude the hook's parent may be a shell
  spawned by claude; walk up with `ps -o ppid= -p` at most 5 levels to find a process whose
  command contains `claude` or `codex`; record that pid).
- Insert into `events`; upsert `sessions`.

Claude events: SessionStart(source, model), UserPromptSubmit(prompt), PreToolUse/PostToolUse/
PostToolUseFailure(tool_name), PermissionRequest(tool_name), Notification(notification_type,
message), Stop(last_assistant_message, background_tasks, session_crons), StopFailure(error,
error_details, last_assistant_message; read as "<error>: <first line>"), SubagentStart/
SubagentStop(agent_id), SessionEnd(reason). Common: session_id, transcript_path, cwd,
hook_event_name. Events carrying `agent_id` come from subagents: record them but they only
ever count as "running" activity, never Stop/review for the parent. (Added later:) a tagged
SubagentStart/SubagentStop after the parent's turn ended (last_event Stop, Interrupt, a
relevant Notification, PermissionRequest, StopFailure or a needs_you tool) keeps that
last_event, and a kept finished turn keeps its `last_event_at`. A `SessionStart` with
`source` `compact`, or `resume` while the session has not ended, keeps the session's
`last_event`/`waiting_reason` (the event row is still recorded), and so does a parent
`PreCompact`/`PostCompact` after the turn ended (a `/compact` at an idle prompt); mid-turn
they are activity as before. A parent Stop stores the
count and `type` labels of its `background_tasks` still in flight (status not terminal) and
the count and prompts of its `session_crons`; a tagged SubagentStop after the turn removes one
`subagent` entry.

Codex events (hooks.json): SessionStart, SessionEnd, UserPromptSubmit(prompt),
PreToolUse/PostToolUse, PermissionRequest, Stop(last_assistant_message), Interrupt,
SubagentStart/SubagentStop, PreCompact/PostCompact. Common: session_id, transcript_path, cwd,
hook_event_name, model, turn_id. No Notification event.

Truncate stored text (prompt / message / last_assistant) to 4000 chars.

## Schema

```
events(id INTEGER PK, ts TEXT, tool TEXT, session_id TEXT, event TEXT, agent_id TEXT,
       notification_type TEXT, tool_name TEXT, text TEXT, extra TEXT /*json*/,
       prompt_kind TEXT /*human|loop|cron|injected, parent prompts; NULL reads as human*/)
sessions(tool TEXT, session_id TEXT, cwd TEXT, branch TEXT, title TEXT,
         first_prompt TEXT, last_prompt TEXT, last_assistant TEXT,
         started_at TEXT, updated_at TEXT, last_event TEXT, last_event_at TEXT,
         waiting_reason TEXT, pid INTEGER, iterm_session_id TEXT, term_program TEXT,
         transcript_path TEXT, origin TEXT /*hook|backfill*/, interactive INTEGER,
         ended_at TEXT, ..., title_source TEXT /*custom|generated|cli|ai|summary|fallback*/,
         retitled_at TEXT, retitle_prompt_count INTEGER, retitle_branch TEXT,
         background_tasks INTEGER, background_types TEXT /*json list*/,
         session_crons INTEGER, cron_prompts TEXT /*json list*/ /*from the last parent Stop*/,
         todos_scan_offset INTEGER, todos_scan_state TEXT /*json; incremental todo scan*/,
         model TEXT, context_tokens INTEGER, context_at TEXT /*when the size was measured*/,
         PRIMARY KEY(tool, session_id))
triage(tool, session_id, status TEXT /*done|snoozed|open*/, until TEXT, note TEXT,
       updated_at TEXT, PRIMARY KEY(tool, session_id))
links(tool, session_id, kind TEXT /*pr|ticket*/, ref TEXT, url TEXT, state TEXT,
      title TEXT, checked_at TEXT,
      branch TEXT /*PR head branch, else the session's branch when first recorded*/,
      closed_at TEXT /*merge/close time, stamped once*/,
      source TEXT /*gh|cli|inbox|provider|ticket: who reported it*/,
      created_at TEXT /*PR creation time from gh*/,
      responded_at TEXT /*first human review or comment*/,
      self_authored INTEGER /*PR author is the active gh login*/,
      PRIMARY KEY(tool, session_id, kind, ref))
notes(id INTEGER PK, tool TEXT, session_id TEXT, ts TEXT /*first seen*/, last_ts TEXT,
      kind TEXT /*needs_you|update|error*/, text TEXT /*capped at 4000*/,
      fingerprint TEXT /*kind + normalized first 200 chars*/, count INTEGER,
      source TEXT /*stop|cli|inbox|provider|backfill*/, event_id INTEGER, last_event_id INTEGER,
      dismissed_at TEXT)            -- UNIQUE(tool, session_id, fingerprint) WHERE dismissed_at IS NULL
notes_meta(key TEXT PK, value INTEGER)   -- hook_from, rebuilt_through (events.id marks)
```
(`notes`/`notes_meta` added after the original spec, migration 5; the link lifecycle columns
and the session title columns are migration 6; `prompt_kind` and the background, cron and todo
scan columns are migration 7, which also marks recorded injected prompts by the default
prefixes; `model`/`context_tokens`/`context_at` are migration 8. All of them are NULL on legacy
rows.)
Add a `schema_version` pragma (user_version) and simple migrations.

## State derivation (`reconcile.py`)

Per session, compute exactly one `state`; the first rule that applies wins. (Rewritten after
the original spec; every `review` carries a `waiting_reason` that says what to do.)

1. `done` (manual) — triage.status = done. A new UserPromptSubmit after the triage timestamp
   reopens it (triage reset to open).
2. `snoozed` — triage.status = snoozed and until > now (falls through when expired).
3. `needs_you` — last non-subagent event is PermissionRequest, or Notification with type in
   {permission_prompt, elicitation_dialog, agent_needs_input, quota_auto_resume_stale,
   quota_auto_resume_disabled}, or StopFailure, and no later activity event. `waiting_reason` =
   tool_name / notification message (the usage-limit pauses fall back to "Usage limit reset
   while asleep: press Enter to resume" / "Paused at a usage limit; auto-resume is off") /
   "<error>: <first line of the message>" for a StopFailure (e.g. "rate_limit: API Error: Rate
   limit reached"), which also records an `error` note. A StopFailure stays needs_you until new
   activity, including a `quota_auto_resume_fired` Notification (read as activity). Then the
   registry fallback, then an open (undismissed) note of kind `needs_you` while no parent
   UserPromptSubmit is newer than the note's `last_ts` (compared as parsed times);
   `waiting_reason` = the note's marker line, else its first line.
4. `running` — last event is an activity event (UserPromptSubmit, Pre/PostToolUse, Subagent*,
   PreCompact/PostCompact) AND process alive (or no pid known and last event < 10 min old).
   `interrupted` — would be running but process dead or activity stale > 30 min (older than
   24 h: `unconcluded`, reason "interrupted"). Before this rule the session registry breaks
   ties (see Liveness), only while the process is alive: `idle` turns an activity event, or an
   unknown last event (SessionStart, a SessionEnd a resume un-ended), into a finished turn
   (read as Stop); `busy` turns a finished or unknown one into `running`.
   SessionStart never changes state on compaction, nor on resume of a live session, and a
   PreCompact/PostCompact after a finished turn does not either (the hook keeps the previous
   last_event).
5. `review`, "PR #N awaits your review" — the session owns a PR that awaits the user (see
   Links: owned, OPEN, no human response yet), live or ended, ahead of every done rule. When
   the PR's author is the active gh login the reason reads "PR #N open, no response yet".
6. `unconcluded`, "Reopened" — the user ran `mark open` after the session ended: neither of
   the next two rules retires it.
7. `done` ("pr merged") — the session is on a feature branch (not main/master/develop), at
   least one PR link records that same branch, every such link is MERGED/CLOSED, and no parent
   prompt is newer than the newest of their `closed_at` (legacy NULL: `checked_at`). A link
   with no recorded branch never counts. `done` ("link done") — any link reports DONE.
8. Ended sessions (`ended_at` set). A **clean exit** — Claude `prompt_input_exit`/`logout` (the
   user typed /exit at the prompt, so the turn was over, whatever the events table holds), or
   any recorded Codex end reason after a finished turn — is `done` ("clean exit") unless
   `todos_open` > 0 (`unconcluded`, "N todos left open"). A Codex end mid-turn is
   `unconcluded`, "Ended mid-turn"; any other end (Claude `clear`/`resume`/`other`) is
   `unconcluded`, "Ended (<reason>)". SessionEnd is never a review event, and update notes do
   not hold ended sessions. Only owned PRs (rule 5) keep an exited session visible; PRs that
   merely share its branch from before never do.
9. Live sessions: a finished turn (Stop, Interrupt, Notification idle_prompt) whose last Stop
   listed background tasks still in flight, with the process alive → `running`, "Waiting on N
   background task(s): <types>", ahead of the notes below. An open `update` note newer than the
   last parent prompt and under 24 h old → `review` with its headline. Last event Stop, Interrupt or Notification idle_prompt within
   24 h of `last_event_at` (which stays at the finished turn through background subagent
   events and compactions; `updated_at` when unset), in an attended turn → `review`, "Your turn: <first line of the last
   assistant message>" ("Your turn" alone when there is none; Interrupt: "Interrupted: your
   turn"). In an unattended turn (the last prompt starts with a `note_unattended_prefixes`
   entry, or is one of the session's `session_crons` prompts) → `running`, "Loop idle between
   ticks" while the process is alive, else `unconcluded`.
10. Otherwise `unconcluded`.

Liveness: `kill -0 pid` (os.kill(pid, 0); PermissionError counts as alive). For Claude, also
treat `~/.claude/sessions/<pid>.json` with matching `sessionId` as confirmation; files there
may contain only `{pid, sessionId}` — use `status`/`waitingFor` only if present. (Added later:)
the file is read for every Claude session, pid known or not (never one whose pid differs from
the session's known pid; when several files share a `sessionId`, the one with a live pid, then
the newest `statusUpdatedAt`, wins). Its `status` is a tie-breaker: `busy`/`idle` count when
`statusUpdatedAt` (ms since the epoch) is newer than the session's `last_event_at`, and `idle`
also counts when last_event is SubagentStart/SubagentStop and it is newer than the session's
latest parent prompt (any kind). Any other status (e.g. `shell`) is no signal; `waiting` with
no hook event at all is still the needs_you fallback.
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
   [--title T] [--branch B] [--session TOOL:ID]` (`--branch` is the PR's head branch; default
   the session's current branch). Also `agentboard status --text "phase/label" [--ttl MIN]`
   to set a free-form current-activity label shown on the row (replaces any skill-specific
   status files), and `agentboard status --model ID [--context-tokens N]` (any of `--text`,
   `--model`, `--context-tokens`) for any agent to report its session's model and context size,
   with the hook/backfill rule: the model replaces the stored one, a context size (measured now)
   is written only when not older than the stored `context_at`. Session resolution when `--session` is omitted, in order: env vars that
   identify the session if the agent sets any (verify empirically which, e.g. CLAUDE_SESSION_ID /
   CODEX_THREAD_ID; skip if none), else walk the caller's ancestor pids and match a recorded
   session pid, else most recently active session whose cwd equals/contains $PWD. Exit 0 with
   a stderr warning if unresolved (callers must never break because AgentBoard is absent or
   confused). A state of MERGED/CLOSED/DONE on links counts toward auto-done (see State
   derivation rule 7 for the branch rule).
2. **Drop directory** — `$AGENTBOARD_HOME/inbox/*.json`, each `{"session": "tool:id" | null,
   "cwd": "...", "links": [...], "status": "...", "model": "...", "context_tokens": N,
   "context_at": "ISO"}` (all optional; model/context as for `status`, `context_at` defaulting
   to the ingest time and clamped to it when later, and a stored `context_at` later than now
   never blocks a new size); ingested and deleted on next snapshot. For tools that can't exec the
   CLI. (Plan usage has its own, persistent drop directory, `usage/`; see the snapshot contract.)
3. **Provider commands** — `$AGENTBOARD_HOME/config.json` `"providers": [{"name": "...",
   "command": ["..."]}]`. Every `refresh-links` runs each (timeout 30 s),
   passing the open sessions as JSON on stdin; the command prints `[{"session": "tool:id",
   "links": [...]}]`. This is how a team-specific integration (e.g. a ticket-workflow skill's
   state files, a JIRA lookup) plugs in without code in AgentBoard. Ship one example provider
   under `examples/providers/` that is fully generic (e.g. reads git trailers), not tied to
   any team.
4. **Built-in generic detectors** (each optional, silently skipped if unavailable):
   - GitHub PR by branch via `gh` if installed+authenticated: `gh pr list --head <branch>
     --state all --limit 1 --json number,state,url,title,createdAt,mergedAt,closedAt,
     headRefName,author` in cwd, only in `refresh-links`. The newest PR link recorded for the
     session's *current* branch decides whether to look (none: look, so a branch change or a
     legacy row with no branch re-checks; OPEN: after 15 min; MERGED/CLOSED: never). Skip
     default branches.
   - Per-link PR refresh (added later): every OPEN PR link with a URL, whatever its source or
     branch, is re-checked with `gh pr view <url> --json ...` (no cwd; once per URL per run,
     every row with that URL updated; same 15 min throttle), so PRs a session reported from
     main, and every PR after its first, stay current. A URL-less OPEN PR link is looked up by
     number in the session's cwd when that still exists. Manual-done sessions and sessions
     idle over 7 days are skipped unless one of their PRs awaits the user. MERGED/CLOSED links
     are never re-fetched.
   - Ownership and responses: a PR is the session's **own** when the session reported it
     (`source` cli/inbox/provider) or gh found it with `created_at` inside the session's
     lifetime; anything else is inherited and never keeps a session on the board. For OPEN
     PRs `refresh-links` reads `gh api repos/{o}/{r}/pulls/{n}/reviews` and
     `issues/{n}/comments` (REST, which carries `user.type`) and stores the earliest response
     by a human as `responded_at`: not a bot (`type` Bot or a `[bot]` login), not the PR's
     author, not a login in `config.json` `"automation_logins"` (no defaults; an integration
     that runs extra accounts lists them there). The active `gh api user` login is excluded
     only as the PR's author: one-account setups (the PR author is the active login) count
     that login's responses made while the owning session was not mid-turn (with no recorded
     parent turn events, only those after it ended); when the active
     login is not the author (the user is the default gh login and sessions author PRs with a
     separate token) it counts like any other human. A PR **awaits you** while
     it is owned, OPEN, has a URL and no `responded_at`.
   - Ticket refs via `config.json` `"ticket_patterns": [{"regex": "[A-Z][A-Z0-9]+-\\d+",
     "url": "https://example.atlassian.net/browse/{ref}"}]` — **empty by default**; matched
     against branch and first prompt.

Snapshot never hits the network. No paths outside ~/.claude, ~/.codex and $AGENTBOARD_HOME are
read by default. The app must locate the CLI without a hard-coded user path (bundled copy in
the .app's Resources, then PATH, then UserDefaults override).

Terminal focus: support iTerm2 (ITERM_SESSION_ID) and Apple Terminal (TERM_SESSION_ID / tty via
AppleScript); for other terminals just activate the app by TERM_PROGRAM / bundle; report
`can_focus` accordingly. Resume opens in the same terminal app the session used when possible.
tmux (added after the original spec): the hook records `$TMUX`'s socket path and `$TMUX_PANE`
(env only) in `sessions.tmux_socket`/`tmux_pane`; a session with a pane is always `can_focus`,
and focus goes through tmux, never the iTerm/Terminal ids it inherited from the tmux server.
The snapshot's `tmux` is true for such a session; `tmux_session` is its tmux session name, read
for alive, not-ended rows with one bounded (1 s) `tmux list-panes -a` per socket, else null.

## Backfill (`agentboard backfill --days 30`)

Idempotent. Never overwrite fields that came from hooks (origin=hook) except filling NULLs.
- Claude: `~/.claude/projects/*/*.jsonl` (top-level only; skip `subagents/` dirs). Per file:
  session id = filename stem; cwd, gitBranch from entries; first & last real user prompt
  (skip tool_result / meta / command-caveat entries); last assistant text; first/last
  timestamps; whether last turn finished. Title (with `title_source`): the newest
  `custom-title` in the tail (scanned to the tail's byte cap for one) beats the newest
  `ai-title`/`summary`, else first prompt first line (`fallback`). A re-parse may replace a
  hook-origin row's title (the hook never writes one), but a fallback never replaces a real
  title, a custom title yields only to a newer custom one, and a generated or `cli` title
  only to a custom one.
- Titles that follow the work (added later): Claude Code writes `ai-title` once and repeats
  it, so `refresh-links` retitles sessions with `retitle_min_prompts` (default 5) new parent
  prompts since their last retitle, or a branch change since then, by running
  `retitle_command` (default: the local `claude` CLI, `claude -p --model haiku --tools ""
  --strict-mcp-config --no-session-persistence <instruction>`, with no tools since stdin is
  untrusted session text, when installed; `null` turns it off) with the
  current title, project, branch and last few prompts/final messages (capped at 4000 chars)
  on stdin; its first output line, trimmed to 60 chars, becomes the title
  (`title_source='generated'`). At most `retitle_max_per_run` (default 5) per run, most
  recently active first; never a done, ended, user-named or week-idle session; at most one
  attempt per session per hour; failures are logged and leave the title. `snapshot` never
  retitles. `agentboard title --text T` names a session (`cli`) and resets the baseline.
- Model and context size (migration 8): the newest real assistant line in the tail (not a
  sidechain line, not a synthetic API-error line whose model starts with `<`; tool_use-only
  lines count) gives `model` and `context_tokens` = `input_tokens + cache_read_input_tokens +
  cache_creation_input_tokens`, with its timestamp as `context_at`. Any parent hook event that
  sends a non-empty string `model` writes it (Claude Code: `SessionStart` only; Codex: every
  event, per codex-rs `hooks/src/schema.rs`; `Subagent*` and subagent-tagged events never do),
  and a parent `SessionStart` on resume/fork writes `context_tokens` with the event time;
  a `source: compact` SessionStart without `context_tokens` sets `context_tokens` NULL with the
  event time as `context_at`, so the pre-compact size is not shown until the next reply.
  The freshest measurement wins on both row origins: a parsed model replaces the stored one,
  a parsed context size only when its `context_at` is at least as new (or the stored one is
  later than now, which no real measurement is), and neither is ever cleared by a parse that
  found none.
- Codex: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`: session_meta (id, cwd, source,
  timestamp), user messages, last agent message, task_complete/turn_aborted, git branch if in
  session_meta.git. Model from the newest `turn_context` payload's `model`; context size from
  the newest `token_count` event with usage: `info.last_token_usage.input_tokens` alone (it
  already includes `cached_input_tokens`), with that line's timestamp as `context_at`. Plan
  usage from that same newest `token_count` event's `rate_limits` (codex-rs
  `RateLimitSnapshot`, null under API-key auth), only for the plan's own limit (`limit_id`
  absent or `codex`): `primary`/`secondary` windows of `used_percent`, `window_minutes`
  (labelled `5h`, `7d`, ...; 299 rounds to 5h) and `resets_at` (epoch; older releases sent
  `resets_in_seconds`, taken relative to the event time). Written to
  `$AGENTBOARD_HOME/usage/codex.json` (label `Codex`, `updated_at` the event time) unless that
  file already holds newer numbers.
- Only files with mtime within --days. Sessions imported by backfill get last_event='Stop'
  semantics (review/unconcluded) unless evidence they are mid-turn and alive.
- Must handle very large files efficiently (stream lines; for last-message, read tail).

## CLI

```
agentboard hook --tool claude|codex          (stdin payload)
agentboard snapshot [--json] [--all]         JSON to stdout (contract below); text table w/o --json
agentboard mark <tool> <session_id> done|open|snooze [--hours N] [--note TEXT]
agentboard backfill [--days 30]
agentboard note --kind needs_you|update|error --text T [--session TOOL:ID]
agentboard notes [--session TOOL:ID] [--all] [--json]          list open (or all) notes
agentboard notes --dismiss ID... | --dismiss-all [--session TOOL:ID]
agentboard notes --rebuild [--days N] [--reset] [--json]       replay recorded Stop events
agentboard refresh-links                     links, PR states/responses, retitles (network)
agentboard link --kind pr|ticket|url --ref R [--url U] [--state S] [--title T] [--branch B] [--session TOOL:ID]
agentboard status [--text T] [--ttl MIN] [--model ID] [--context-tokens N] [--session TOOL:ID]
agentboard title --text T [--session TOOL:ID]   name a session (a user's custom title is kept)
agentboard install-hooks [--claude] [--codex] [--dry-run]
agentboard uninstall-hooks [--claude] [--codex]
agentboard focus <tool> <session_id>         activate iTerm2 tab by ITERM_SESSION_ID (osascript);
                                             inside tmux: focus an attached client or attach a new window
agentboard resume <tool> <session_id> [--print]  open new iTerm2 tab running resume cmd
agentboard prune [--days N] [--compact-days N] [--dry-run] [--auto] [--vacuum]   retention
agentboard logs [--since 1h] [--grep T] [--lines N] [--app]   print the diagnostic log
```

Retention (added after the original spec): `prune` deletes sessions idle longer than
`prune_days` (config, default 90) except one with an open needs_you note, an awaiting PR, an
active snooze or a live pid; deletes tool-use/subagent events older than `compact_days` (default
7) from every session; deletes dismissed notes older than `prune_days`. `0` means never.
`--auto` runs at most once per 24 h (`notes_meta.last_prune_at`, epoch seconds) and vacuums only
past a free-page threshold, at most weekly (`last_vacuum_at`); the app calls it every 6 h.

Diagnostic log (added after the original spec): every subcommand, hook and app action writes
`key=value` lines to `$AGENTBOARD_HOME/agentboard.log` through `core/agentboard/diag.py`
(level from `AGENTBOARD_LOG_LEVEL`, else `config.json` `log_level`, else `info`). It rotates
once to `agentboard.log.1` at 2 MB (never a `.2`), never raises, and `snapshot`/`paths` stay at
debug unless they fail or run over 1 s. `focus` logs an `outcome` (`matched_uuid`, `matched_tty`,
`matched_unknown`, `activated_only`, `activate_failed`, `not_found`, `timeout`, `no_terminal`,
`unsupported_terminal`, and for tmux sessions `tmux_client_focused`, `tmux_attached_new_window`,
`tmux_attached_terminal`, `tmux_pane_gone`, `tmux_missing`, `tmux_attach_failed`;
osascript/`open -a`/tmux calls are bounded at 10 s, and a timeout exits 1); its other
exit codes are unchanged. `hook-errors.log` is still written as described under Hook.

install-hooks: back up the file first (`<file>.agentboard-bak-<ts>`), then append one matcher
group `{"hooks":[{"type":"command","command":"<abs path>/bin/agentboard hook --tool claude",
"timeout":5}]}` to each event in `~/.claude/settings.json` (preserving existing hooks and all
other keys / ordering; idempotent — detect existing agentboard entry). For Codex, same in
`~/.codex/hooks.json` and print that the user must trust the new hooks via `/hooks` in Codex.
Never touch `~/.codex/config.toml`.

Resume commands: Claude `cd <cwd> && claude --resume <id>`; Codex `cd <cwd> && codex resume <id>`.
Focus: iTerm `ITERM_SESSION_ID` is `w0t1p0:<UUID>`; match `unique id` of iTerm sessions to UUID,
then select the tab/window and activate iTerm. Exit non-zero with message if not found.
tmux focus (when `tmux_pane` is set): `tmux -S <socket> display-message -p -t <pane>` gives the
pane's session id, name and window (failure: `tmux_pane_gone`, exit 1); `select-window` and
`select-pane` it; `list-clients -t <session-id>` gives client ttys, each probed in iTerm2 (if
installed, per `open -Ra iTerm`) and Terminal by tty with scripts that return `not_running`
for an app that isn't running and never activate on a miss; with no match, a new iTerm window
(Terminal if iTerm is missing) runs `<tmux> -S <socket> attach-session -t '$<id>'`.

## Snapshot JSON contract (consumed by the Swift app)

```json
{
  "generated_at": "ISO8601",
  "counts": {"needs_you": 0, "interrupted": 0, "running": 0, "review": 0,
             "unconcluded": 0, "snoozed": 0, "done": 0, "notes": 0},
  "sessions": [
    {
      "key": "claude:<session_id>",
      "tool": "claude|codex",
      "session_id": "...",
      "state": "needs_you|interrupted|running|review|unconcluded|snoozed|done",
      "waiting_reason": "string|null",
      "status_text": "string|null",
      "title": "string", "title_source": "custom|generated|cli|ai|summary|fallback|null",
      "cwd": "/abs", "project": "basename of cwd", "branch": "string|null",
      "first_prompt": "string|null", "last_prompt": "string|null",
      "last_assistant": "string|null",
      "started_at": "ISO", "updated_at": "ISO", "ended_at": "ISO|null",
      "alive": true, "interactive": true,
      "can_focus": true,
      "tmux": false, "tmux_session": "string|null",
      "resume_command": "cd ... && claude --resume ...",
      "links": [{"kind": "pr|ticket", "ref": "ABC-123|#26", "url": "...", "state": "OPEN|MERGED|CLOSED|null", "title": "...",
                 "branch": "string|null", "owned": false, "awaits_you": false}],
      "triage": {"status": "open|done|snoozed", "until": "ISO|null", "note": "string|null"},
      "notes": [{"id": 1, "kind": "needs_you|update|error", "text": "...", "ts": "ISO",
                 "last_ts": "ISO", "count": 1, "source": "stop|cli|inbox|provider|backfill"}],
      "notes_count": 1,
      "turn_finished_at": "ISO|null", "turn_prompt_kind": "human|loop|cron|injected|null",
      "background_tasks": 0, "turn_ends_background_wait": false,
      "model": "string|null", "context_tokens": 308011, "context_at": "ISO|null"
    }
  ],
  "usage": [
    {"source": "claude", "label": "Claude", "updated_at": "ISO|null",
     "origin": "~/.claude/rate-limits.json",
     "windows": [{"label": "5h", "used_percentage": 57, "resets_at": "ISO"},
                 {"label": "7d", "used_percentage": 70, "resets_at": "ISO"}]}
  ]
}
```
Sorted: state priority (needs_you, interrupted, review, running, unconcluded, snoozed, done),
then updated_at desc. Default excludes done older than 7 days and non-interactive sessions;
`--all` includes everything. Must run in < 500 ms with a few thousand sessions.
(Added after the original spec.) `notes` lists a session's open notes, newest `last_ts` first,
capped at 20 plus any older open `needs_you` notes (never capped out); `notes_count` is the uncapped open count; `counts.notes` sums `notes_count` over
the sessions this snapshot emits. Older CLIs omit all three; the app defaults them to empty/0.
Links' `branch`/`owned`/`awaits_you` and `title_source` were added with migration 6; the app
decodes them as optionals.
Turn fields (added for turn-finish notifications): `turn_finished_at` is the ts of the
session's newest parent `Stop` event when that Stop is newer than its newest parent prompt of
any kind, `ended_at` is null, the session is not waiting on background tasks
(`reconcile.background_wait_reason`), and that Stop event's own `extra.background_tasks` is not
above 0; null otherwise. It never reads `last_event`, so an `idle_prompt` Notification, a kept
marker event or a registry flap leaves it unchanged: it identifies one finished turn.
`turn_prompt_kind` is `events.prompt_kind` of the newest parent prompt (null for legacy rows,
read as human). `background_tasks` is `sessions.background_tasks` from the last parent Stop
(null when unknown, e.g. Codex). `turn_ends_background_wait` is true when the newest parent
prompt is injected and the newest parent Stop before it (by event id) listed background tasks:
the `<task-notification>` turn that reports a background task back. Three grouped queries, as
for `last_parent_prompt_times`.
Added with migration 8: each session's `model`, `context_tokens` (absolute tokens; the
context window size is not known) and `context_at` (null when unknown), and the top-level
`usage`: a list of plan-usage sources, empty when there is none (the normal case for API-key,
Bedrock/Vertex and most gateway setups). Each source has `source` (its name), `label` (display
name), `updated_at` (when the numbers were measured, null when unknown), `origin` (the file it
was read from, for tooltips) and an ordered `windows` list; each window has `label`,
`used_percentage` (floored at 0, not capped: a spend limit runs past 100 once exceeded; rounded
to one decimal), and optionally `resets_at` and the spend fields `used_usd`, `limit_usd`,
`period`. Nothing names a plan or a window: labels come from the data. Sources (`usage.py`),
all local files read on every call:
- Claude Code's undocumented cache `~/.claude/rate-limits.json` (source `claude`), when it
  exists and has a window: every top-level object with a numeric `used_percentage` is a window,
  in file order, labelled from its key by rule (a leading count and time unit shorten,
  `five_hour` -> `5h`, `seven_day_opus` -> `7d opus`; anything else reads as words,
  `spend_limit` -> `spend limit`); `cached_at` becomes `updated_at`; epochs become ISO.
- The drop directory `$AGENTBOARD_HOME/usage/<source>.json`, which any agent or tool may keep
  (replace it atomically: write a temp file, then rename): `{"label": "...", "updated_at":
  "ISO or epoch", "windows": [{"label": "5h", "used_percentage": 57, "resets_at": "ISO or
  epoch", "used_usd": 1, "limit_usd": 2, "period": "..."}]}`; only `windows` is required, the
  source name is the file name, `label` defaults to it and `updated_at` to the file's mtime.
  Backfill writes `usage/codex.json` from Codex rollouts (see Backfill).
Claude's cache comes first, then files by name, at most 8 sources of 8 windows. Two sources
with the same name keep the one with the newer `updated_at`; a source whose `updated_at` is
more than 7 days old is left out. Problems are logged at debug only; `snapshot` never touches
the network.

Notes (`notes.py`, added after the original spec): the hook classifies each parent Stop message
inside its transaction (`needs_you` for a line starting with a `config.json` `note_markers`
entry, default `USER_ACTION:`, in any turn; `update`/`error` only in a turn whose latest parent
(non-subagent) prompt starts with a `note_unattended_prefixes` entry, default `# Autonomous
loop`); a failure there never drops the event. A repeat of an open note bumps `count`/`last_ts`; a repeat of a dismissed one
is a new row. `notes --rebuild` replays events at or below the `hook_from` mark recorded by the
migration; inbox drops and provider rows accept an optional `"notes": [{"kind", "text"}]`.

## Mac app (SwiftUI, `app/`)

- `MenuBarExtra` with `.window` style. Label: SF Symbol (e.g. `square.stack.3d.up`) plus count
  of needs_you+interrupted when > 0 (and a subtle second count for review). Menu bar icon
  color: the most urgent session's color (needs_you red, interrupted orange, alive review teal,
  running green, closed amber; template glyph otherwise), rendered as a non-template image
  resolved for the system light/dark setting; filled variant when needs_you+interrupted > 0.
  Red flashes at 1 Hz when a session newly enters needs_you, until the popover opens / the
  dashboard becomes key / the session leaves needs_you (per-session acknowledgement; timeout
  and "never" options in Settings); Reduce motion shows an exclamation badge instead.
- Popover: a "Filter sessions" search field on top (focused on open; query kept in memory until
  cleared, (x) button, Escape clears, ⌘F focuses), then a compact grouped list (Needs you,
  Review, Running; Unconcluded collapsed with count). While a query is set the sections show
  only matches with counts, Unconcluded opens if it has matches, Snoozed and Done sections are
  added, and a line reports how many sessions are hidden (emphasized if one needs you). The
  menu bar icon ignores the query. Each row: tool badge (C / X), title, project · branch,
  relative time, waiting_reason, and a badge with the open-notes count. (Added later.) The
  primary line is a live `agentboard status` text when set, with the session title under
  it; otherwise the title. One button per PR link (glyph + `#N`, tinted open/merged/closed,
  outlined when it awaits the user) opens the PR without focusing the terminal; more than
  three show three plus "+N", which opens the detail view.
  (Added after the original spec.) An **Updates** section above Needs you lists every open note
  of the (filtered) sessions newest first: kind glyph, project, first line, `×N`, relative time;
  click focuses the owning session, a chevron expands the full text, (x) dismisses, and the
  header has a count and "Clear all" (`agentboard notes --dismiss`).
  Row click = focus; context menu & hover buttons: Focus, Resume, Copy resume cmd, Mark done,
  Snooze 1d, Open PR/ticket. Footer: "Open Dashboard", "Refresh", "Quit".
- Dashboard `Window` scene: NavigationSplitView — sidebar of states with counts + filter by
  tool/project + search (title, prompts, last assistant message, project, cwd, branch, tool,
  state, waiting reason, status text, links, triage note, session id; quoted phrases and
  `repo:`/`path:`/`branch:`/`tool:`/`state:`/`pr:`/`link:` qualifiers; the same matcher as the
  popover). Sidebar counts reflect the pickers and the search, with a warning line (and a
  Clear button that also selects All) when the filters hide a needs_you or interrupted
  session; list; detail pane showing title, cwd, branch, links, timestamps, first prompt, last prompt, last assistant
  message (scrollable, selectable, rendered as plain text or basic markdown via AttributedString),
  an Updates section above the last assistant message (each open note in full with count, time
  and a Dismiss button), action buttons. The sidebar also shows the open-notes total the
  filters keep, counted note by note like the popover's Updates header.
- Global hotkey ⌃⌥⌘A toggles the dashboard window (Carbon `RegisterEventHotKey`).
- Data: run `agentboard snapshot --json` (CLI located per Links section: bundled Resources copy, PATH, then UserDefaults `agentboardPath`) every 3 s while popover/window visible, every 15 s
  otherwise; also watch the db directory with DispatchSource / FSEvents and refresh (debounced
  500 ms). Run `agentboard refresh-links` every 10 min in background. Run `agentboard backfill
  --days 30` once at launch (background).
- Actions shell out to the CLI (`mark`, `focus`, `resume`, `resume --print` for copy).
- Notifications: post a UserNotification when a session transitions into needs_you (optional,
  off by default toggle in a small Settings view) — existing notify.py already notifies.
- (Added later.) Turn-finish notifications (`notifyOnTurnFinished`, off by default): one per
  attended Review turn, keyed by `turn_finished_at` (`TurnFinishDetector`: primed at launch,
  each turn considered once, loop/cron turns and injected ones that do not end a background
  wait stay silent), identifier `turn:<key>` so a newer turn replaces it, removed when a
  non-injected prompt answers it, skipped for the session selected while the dashboard is key.
  Both kinds carry `userInfo` `sessionKey`/`kind`, thread per session and category `session`;
  the app is the `UNUserNotificationCenter` delegate, and a click focuses the session (or opens
  the dashboard with it selected). Skipped entirely without an app bundle (fixture binary).
- Launch at login toggle via `SMAppService.mainApp`.
- Never block main thread on Process; decode with Codable; tolerate unknown fields/states.
- Appearance: Settings picker "System / Dark / Light", default **System** (UserDefaults key
  `appearance`); apply via `NSApp.appearance` (nil for System). Design and check every view in
  dark mode first (the user's default); use semantic colors only, no hard-coded light colors.

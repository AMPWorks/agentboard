import Foundation

/// The in-app guide, topic by topic. It mirrors README.md: when a behavior,
/// a Settings control, a config.json key (`core/agentboard/config_keys.py`)
/// or a CLI command changes, change both. Tests check that every config key,
/// every Settings key and every CLI subcommand and long option appears here.
///
/// Content stays generic: no accounts, hosts or paths of any one machine;
/// paths are written as `~/…` or via `agentboard paths`.
enum HelpContent {
    static let topics: [HelpTopic] = [
        gettingStarted, sessionStates, popover, updates, search, dashboard,
        links, settings, config, cli, diagnostics, privacy,
    ]

    static func topic(_ id: HelpTopic.ID) -> HelpTopic? {
        topics.first { $0.id == id }
    }

    // MARK: - 1. Getting started

    static let gettingStarted = HelpTopic(
        id: .gettingStarted, title: "Getting started", symbol: "sparkles",
        blocks: [
            .paragraph("AgentBoard shows the state of every **Claude Code** and **Codex CLI** session on this Mac: what needs you, what has output to review, what is running, and what older work is still unconcluded. Everything stays on your Mac: there is no server, no account and no telemetry."),
            .heading("How it works"),
            .bullets([
                "Hooks installed into Claude Code (`~/.claude/settings.json`) and Codex (`~/.codex/hooks.json`) run `agentboard hook` on every session event. The hook records the event in a local database and never prints anything into your conversation.",
                "The menu bar app asks the `agentboard` command line tool for a snapshot of every session every 3 seconds while the popover or dashboard is open, every 15 seconds otherwise, and right away when the data directory changes.",
                "At launch the app imports the last 30 days of transcripts (`agentboard backfill`), then refreshes PR links, ticket links and titles (`agentboard refresh-links`) every 10 minutes.",
                "Each session gets exactly one state (see **Session states**), which decides where it shows up and what color it has.",
            ]),
            .heading("The menu bar icon"),
            .paragraph("The icon takes the color of the most urgent session, so you can tell whether an agent is waiting on you without opening anything. First match wins:"),
            .keyValue([
                HelpRow(key: "Red, flashing", value: "A session needs you. It flashes until you have seen it, then stays solid red while anything still needs you."),
                HelpRow(key: "Orange", value: "A session was interrupted."),
                HelpRow(key: "Teal", value: "A session in Review whose process is still running."),
                HelpRow(key: "Green", value: "A session is running."),
                HelpRow(key: "Amber", value: "Only unconcluded sessions, or Review sessions whose process has exited."),
                HelpRow(key: "Monochrome", value: "Nothing worth coloring: no sessions, or only Done and Snoozed."),
            ]),
            .paragraph("The first number next to the icon counts sessions that need you or were interrupted; the second, dimmer number counts sessions in Review. With **Reduce motion** on (System Settings → Accessibility → Display) the icon shows an exclamation badge instead of flashing. Settings → Menu bar icon turns coloring and flashing on or off."),
            .heading("Popover and dashboard"),
            .bullets([
                "**The popover** opens when you click the menu bar icon: a compact list of the sessions that matter now, the Updates feed and a search field. Click a row to jump to its terminal.",
                "**The dashboard** is a full window with every session, filters by state, tool and project, and a detail pane for the selected session. Open it with **Open Dashboard** in the popover footer or the hotkey.",
            ]),
            .shortcut(keys: "⌃⌥⌘A", action: "Show or hide the dashboard from anywhere."),
            .paragraph("The gear button (in the popover footer and the dashboard toolbar) opens **Settings…**, this **AgentBoard Help**, **Open Log** and **Quit AgentBoard**. Every button and control has a tooltip: hover over it to see what it does. The ? buttons next to section headers open the matching help topic."),
            .heading("Installing"),
            .paragraph("`scripts/install.sh` (from a checkout of AgentBoard) installs the command line tool, the hooks and the app, and imports recent history. Re-run it after an upgrade, or after installing Claude Code or Codex for the first time so the new tool gets hooks. Pass `--dry-run` to preview every change first. Codex also needs `hooks = true` under `[features]` in `~/.codex/config.toml` (`install.sh --enable-codex-hooks` adds it) and the new hooks trusted with `/hooks` inside Codex."),
        ])

    // MARK: - 2. Session states

    static let sessionStates = HelpTopic(
        id: .sessionStates, title: "Session states", symbol: "circle.grid.2x2",
        blocks: [
            .paragraph("Each session is in exactly one state. The rules are checked in this order and the first that applies wins: **Done**, **Snoozed**, **Needs you**, **Interrupted**, **Review**, **Running**, **Unconcluded**. The row's dot, the title color and the dashboard sidebar all follow the state."),
            .keyValue([
                HelpRow(key: "Done (gray)", value: "You marked it done, or it finished on its own (see below). Out of sight in the popover unless you search; listed under Done in the dashboard. A new prompt in a session you marked done reopens it (a prompt Claude Code injects itself, such as a finished background task's notification, does not)."),
                HelpRow(key: "Snoozed (gray)", value: "Hidden for a while with **Snooze 1 Day** (or `agentboard mark … snooze --hours N`). It comes back on its own when the snooze runs out."),
                HelpRow(key: "Needs you (red)", value: "The session is blocked on you: a permission prompt, a question or input request, a turn that failed (an API error such as “rate_limit: API Error: Rate limit reached”, which also adds an error update), a usage-limit pause that needs Enter or has auto-resume off, or an open **needs_you** update that no newer prompt has answered. The reason (the tool asking, the message) is the row's last line."),
                HelpRow(key: "Interrupted (orange)", value: "It was working, but its process died or nothing happened for 30 minutes. After 24 hours it becomes Unconcluded."),
                HelpRow(key: "Review (teal while running, amber once exited)", value: "It has something for you to act on, and the row always says what (see **Review reasons**)."),
                HelpRow(key: "Running (green)", value: "It is working right now: recent activity and a live process (or, with no process known, an event in the last 10 minutes). A loop session idling between ticks is Running with “Loop idle between ticks”. A session whose last turn left background tasks running (subagents, shells, monitors) is Running with “Waiting on N background task(s): …” until they finish."),
                HelpRow(key: "Unconcluded (amber)", value: "Anything not finished and not waiting on you: ended another way (“Ended (clear)”), ended mid-turn, todos left open (“N todos left open”), reopened after an exit (“Reopened”), or a Review that is older than a day."),
            ]),
            .heading("Review reasons"),
            .paragraph("A session is in Review only with a concrete reason, shown as the row's last line:"),
            .bullets([
                "**PR #N awaits your review**: the session opened a PR that nobody but automation has reviewed or commented on yet. This holds even after the session exits, until you respond or the PR merges. When you and the session share one GitHub account it reads **PR #N open, no response yet**.",
                "**The headline of an open update**: an `update` note newer than your last prompt and under a day old (a loop session reporting a milestone).",
                "**Your turn: <first line of its last message>**: an attended session stopped and is waiting for your next prompt, within 24 hours. An interrupted turn reads **Interrupted: your turn**.",
            ]),
            .paragraph("A background agent finishing after a turn, and a compaction (or resuming a live session), never change a session's state. For a live Claude session AgentBoard also reads Claude Code's own `~/.claude/sessions/<pid>.json` status when it is newer than the last hook event: **idle** turns a stale activity event into a finished turn, **busy** makes a finished one Running. Other statuses carry no signal."),
            .heading("Sessions that finish on their own"),
            .paragraph("A session can reach Done by itself. Its row then shows a small **auto** badge whose tooltip gives the reason:"),
            .keyValue([
                HelpRow(key: "clean exit", value: "You exited it (`/exit`, logout; for Codex, any end after a finished turn). An exited session goes out of sight unless it opened a PR that awaits you (it stays in Review), has an open needs_you update, or left todos open (Unconcluded). A PR that was already on the branch before the session started never keeps it around. Any other end (`/clear`, `/resume`) is Unconcluded, “Ended (<reason>)”."),
                HelpRow(key: "pr merged", value: "Every PR recorded for the session's current feature branch is merged or closed, and you have not prompted it since. A merged PR on an older branch, or on main, master or develop, never hides a session that moved on."),
                HelpRow(key: "link done", value: "A link reports the state `DONE` (from `agentboard link --state DONE`, an inbox file or a provider)."),
            ]),
            .paragraph("**Reopen** (or `agentboard mark <tool> <id> open`) on an exited session keeps it on the board as Unconcluded, “Reopened”, until it genuinely resumes."),
            .heading("Hidden sessions"),
            .paragraph("By default the board leaves out non-interactive runs (`claude -p`, SDK workers, `codex exec`) and Done sessions older than 7 days. **Show hidden** in the dashboard toolbar includes them (it passes `--all` to the snapshot)."),
        ])

    // MARK: - 3. The popover

    static let popover = HelpTopic(
        id: .popover, title: "The popover", symbol: "menubar.rectangle",
        blocks: [
            .paragraph("Click the menu bar icon to open the popover. From top to bottom: the search field, the **Updates** feed (see Updates), the session sections, and the footer."),
            .heading("Sections"),
            .keyValue([
                HelpRow(key: "Needs you", value: "Sessions that need you, and interrupted ones (with an orange **Interrupted** tag)."),
                HelpRow(key: "Review", value: "Sessions with something for you to act on."),
                HelpRow(key: "Running", value: "Sessions working right now."),
                HelpRow(key: "Unconcluded", value: "Collapsed by default, with a count; click its header to expand it. It opens by itself while a search has matches in it."),
                HelpRow(key: "Snoozed, Done", value: "Only while you search, at the bottom."),
            ]),
            .paragraph("Within a section the most recently updated session comes first."),
            .heading("A session row"),
            .bullets([
                "The **dot** is the state color (hover for the color key), and the round **C** or **X** badge is the tool (Claude Code or Codex).",
                "The **first line** is what the session says it is doing right now (an `agentboard status` text) when it has one, else its title. Titles follow the work: see PRs, links and integrations.",
                "Small tags: **Interrupted**; a speech bubble with the number of open updates (red when one needs you); **auto** when the session finished on its own. The time is when it last changed.",
                "The **second line** is the session's own title when a status text leads the first line.",
                "Then the **project · branch**, and one button per PR (`#N`: green open, purple merged, red closed, outlined and bold when it awaits your review). A PR button opens the PR in the browser without touching the terminal; past three PRs a **+N** button opens the session in the dashboard.",
                "The **last line** is the waiting reason: what the session needs from you, or why it is in Review.",
            ]),
            .heading("Clicking a row"),
            .bullets([
                "**Click** a live session to bring its terminal tab to the front (iTerm2 and Apple Terminal; other terminals are just activated). A session with no live terminal opens in the dashboard instead.",
                "**Hover** to show quick buttons: copy the resume command, and mark the session done.",
                "**Right-click** for every action: **Focus**, **Resume** (once the process has exited), **Copy Resume Command**, **Mark Done** (or **Reopen** for a done session), **Snooze 1 Day**, and **Open** for each of its links.",
            ]),
            .heading("Footer"),
            .paragraph("**Open Dashboard** opens the full window, **Refresh** reloads the snapshot right away, and the gear menu holds **Settings…**, **AgentBoard Help**, **Open Log** and **Quit AgentBoard**."),
            .heading("Keyboard"),
            .shortcut(keys: "Type", action: "The search field has the cursor when the popover opens, so you can type a filter right away."),
            .shortcut(keys: "⌘F", action: "Focus the search field again."),
            .shortcut(keys: "Escape", action: "Clear the search; with an empty search, close the popover."),
        ])

    // MARK: - 4. Updates

    static let updates = HelpTopic(
        id: .updates, title: "Updates", symbol: "text.bubble",
        blocks: [
            .paragraph("Sessions that run unattended print a final message every turn, and most of it is noise (“Quiet tick, nothing changed.”). The few messages you actually have to read are kept as **updates** (notes): a list across all sessions, shown as **Updates** in the popover and in each session's detail, until you dismiss them."),
            .heading("Kinds of update"),
            .keyValue([
                HelpRow(key: "Needs you (red bubble)", value: "A line starting with a marker (`USER_ACTION:` by default) in any turn. It puts its session in **Needs you** until a newer prompt arrives in that session or you dismiss it. The update itself stays until you dismiss it; if the next turn repeats the request, its count goes up and the session is red again."),
                HelpRow(key: "Error (red triangle)", value: "A transient failure in an unattended turn: an API error, a timeout."),
                HelpRow(key: "Update (info)", value: "A milestone in an unattended turn: a PR or issue opened, merged, approved or deployed, CI red or green, something pushed or blocked, a link, or any message of 300 or more characters."),
            ]),
            .paragraph("A turn is **unattended** when its prompt starts with one of the configured prefixes (`# Autonomous loop` by default) or is one of the session's scheduled crons. Both the marker and the prefixes are set in config.json (`note_markers`, `note_unattended_prefixes`)."),
            .paragraph("Some turns are started by Claude Code itself: a background task finishing (`<task-notification>`), a message from another agent or session (`<agent-message …>`, `<cross-session-message …>`). Such an **injected** prompt keeps the session Running while Claude works on it, but it is not you replying: it does not reopen a session you marked done, release a needs_you update, make a loop session attended or count toward retitling. The prefixes are `prompt_injected_prefixes` in config.json; after changing them (or `note_unattended_prefixes`), run `agentboard notes --rebuild --reset`."),
            .heading("What counts as quiet"),
            .bullets([
                "A message that starts with idle wording (“Quiet”, “Idle”, “Nothing changed”, “No changes”, “Still waiting”, “Standing by”, “Awaiting”) is dropped, however long it is.",
                "So is a message whose first sentence only restates what is waiting on you (“Two things are still waiting on you:”, “PR #5 is still blocked on your review”).",
                "Either kind is still kept when it reports a hard event anywhere: merged, opened, filed, failed or CI red (“…but CI went red on PR #5”). A negated or pending event (“PR #5 has not merged yet”, “no tests failed”), an adjectival one (“the merged PR”) or one in a since/until/once/if/unless clause (“idle since the logging work merged”) does not count. Softer words (approved, deployed, pushed) behind a quiet lead restate a known state and stay quiet.",
                "Either kind is also kept when it reports news (fixed, a bug or regression, something found, broke or wrong, a repair or correction, a heads-up) and is at least 160 characters long. A short quiet tick such as “Quiet. Fixed the flaky test.” is still dropped.",
                "A progress note on work another agent is still doing (“still building”, “still working”, “holds the lock”, “takes no action”, “no new work”, “nothing new this tick”) is dropped when it is under 400 characters and has no link, even without idle wording.",
                "A PR or issue reference counts as a milestone only the first time the session mentions it; an event about it (merged, opened, CI red) counts every time.",
                "A repeat of an open update (same session and kind, same text once times and counters are ignored) raises its count, shown as **×N**, instead of adding a row.",
            ]),
            .heading("In the popover"),
            .bullets([
                "Updates sits above Needs you with a count and a clear button. Each row shows the kind, the tool badge, the project, the first line, ×N and the time.",
                "**Click** a row to open the dashboard on its session, scrolled to that update's full text and outlined. The **chevron** expands the full text in place, and **(x)** dismisses it. Right-click for the session's actions.",
                "The section stays compact so it never pushes the sessions out of view: every needs_you update, and at most 3 other rows. **Show all N** expands it in place and **Show fewer** collapses it again.",
                "A session with two or more open update and error notes (a loop session, in practice) gets one grouped row with **×N notes**. Its chevron lists them, each with its own x, and its (x) dismisses all of that session's update and error notes, never its needs_you ones.",
                "With an empty search the button reads **Clear all** and dismisses every open update, including ones not listed. While you search it reads **Clear matching** and dismisses only the updates the search keeps.",
            ]),
            .heading("In the dashboard"),
            .paragraph("The session detail lists every open update in full with a **Dismiss** button each, and **Dismiss All** when there is more than one. The dashboard sidebar shows how many open updates the filters keep. A session lists its newest 20 updates (plus any older needs_you one), so a header can read “Updates (20 of 25)”."),
            .heading("Adding and managing updates"),
            .paragraph("Any script can add one with `agentboard note --kind needs_you|update|error --text T`, list them with `agentboard notes`, and dismiss them with `agentboard notes --dismiss ID` or `--dismiss-all`. After an upgrade that changes the rules, `agentboard notes --rebuild --reset` re-applies them to the recorded history without reopening anything you dismissed."),
        ])

    // MARK: - 5. Search and filters

    static let search = HelpTopic(
        id: .search, title: "Search and filters", symbol: "magnifyingglass",
        blocks: [
            .paragraph("The popover and the dashboard each have a search field, and both read a query the same way."),
            .bullets([
                "Words are separated by spaces, and a session must match **every** word (`retry codex`).",
                "Double quotes keep a phrase together: `\"menu bar\"`.",
                "Matching ignores case and accents and matches anywhere in a field, so `#4` also finds `#42`.",
                "A plain word searches the title, the first and last prompts, the last assistant message, the project, directory and branch, the tool, the state (`needs_you` or `Needs you`), the waiting reason and status text, every link's kind, ref, title and state, the triage note, the session id, and the text of the session's open updates. It does not search link URLs; use `pr:` or `link:` for those.",
                "A `field:value` word narrows the match to one place. The value can be quoted (`branch:\"feat x\"`, `state:\"needs you\"`).",
            ]),
            .keyValue([
                HelpRow(key: "`repo:` / `project:`", value: "Project name and working directory."),
                HelpRow(key: "`path:`", value: "Working directory."),
                HelpRow(key: "`branch:`", value: "Git branch."),
                HelpRow(key: "`tool:`", value: "`claude` or `codex`."),
                HelpRow(key: "`state:`", value: "State, raw (`needs_you`) or as shown (`Needs you`)."),
                HelpRow(key: "`pr:`", value: "Links of kind PR: ref, title and URL."),
                HelpRow(key: "`link:`", value: "Any link: kind, label, ref, title, URL and state."),
            ]),
            .paragraph("Spaces after the colon are ignored, so `branch: main` is the same as `branch:main`. An unknown qualifier (`foo:bar`) or one with nothing after it is searched as plain text, colon included."),
            .heading("In the popover"),
            .bullets([
                "The field takes the cursor when the popover opens; ⌘F focuses it again, the (x) button clears it, and Escape clears a non-empty query.",
                "The query is kept when the popover closes, until you clear it or quit AgentBoard, and it is selected when the popover reopens so typing replaces it.",
                "While a query is set, each section shows only matching sessions with a count in its header, matching Snoozed and Done sessions appear at the bottom, and the Updates feed keeps only matching updates.",
                "A line under the field says how many sessions the query hides, in red when one of them (or a hidden update) needs you.",
                "The menu bar icon, its color and the flash always reflect every session, whatever the query.",
            ]),
            .heading("In the dashboard"),
            .bullets([
                "The search box in the toolbar filters the session list together with the state sidebar and the **Tool** (All Tools, Claude, Codex) and **Project** pickers.",
                "The sidebar badges, including **All**, count the sessions left after the pickers and the query, so they always match the list.",
                "When the filters hide a session or update that needs you, a red line under the sidebar says how many, with a **Clear** button that resets the search and both pickers and selects **All**.",
            ]),
        ])

    // MARK: - 6. The dashboard

    static let dashboard = HelpTopic(
        id: .dashboard, title: "The dashboard", symbol: "rectangle.split.3x1",
        blocks: [
            .paragraph("The dashboard window has three columns: the state sidebar, the session list and the selected session's detail. Open it from the popover footer or with ⌃⌥⌘A."),
            .heading("Sidebar"),
            .bullets([
                "**All** and one entry per state (Needs you, Interrupted, Review, Running, Unconcluded, Snoozed, Done), each with a count of the sessions the filters keep. Select one to list only that state.",
                "**Updates** shows how many open updates the filters keep.",
                "A red warning with **Clear** appears when the filters hide something that needs you.",
                "At the bottom, the color key for the row dots.",
            ]),
            .heading("Toolbar"),
            .keyValue([
                HelpRow(key: "Search", value: "Filters the list (see Search and filters)."),
                HelpRow(key: "Tool", value: "All Tools, Claude or Codex."),
                HelpRow(key: "Project", value: "All Projects or one project."),
                HelpRow(key: "Show hidden", value: "Also list non-interactive runs and Done sessions older than 7 days."),
                HelpRow(key: "Refresh", value: "Reload the snapshot now."),
                HelpRow(key: "Gear", value: "Settings…, AgentBoard Help, Open Log, Quit AgentBoard."),
            ]),
            .heading("Session list"),
            .bullets([
                "Rows are sorted by state (most urgent first), then by last update. They show the same dot, badges, titles and PR buttons as the popover.",
                "**Double-click** (or press Return on) a live session to focus its terminal. **Right-click** for the full action menu.",
                "**Hover** for quick buttons: Resume (once the process has exited), copy the resume command, mark done, snooze for a day, and open its first non-PR link (a ticket or URL).",
            ]),
            .heading("Detail"),
            .bullets([
                "**Header**: tool badge, title (and the session's own title under a status text), state, the waiting reason in orange, and the done reason.",
                "**Action bar**: **Focus** brings the terminal tab to the front; **Resume** opens a new terminal tab running `claude --resume <id>` or `codex resume <id>` in the session's directory (shown once its process has exited); **Copy Resume Command** copies that command; **Mark Done** hides the session (**Reopen** brings a done one back); **Snooze 1d** hides it for 24 hours.",
                "**Metadata**: project, directory, branch, when it started, last updated and ended, and whether its process is alive.",
                "**Links**: every PR, ticket and URL with its state, and **awaits you** on a PR waiting for your review. Click one to open it.",
                "**First prompt**, **Last prompt**, the session's **Updates** (each with Dismiss), and the **Last assistant message**.",
            ]),
        ])

    // MARK: - 7. PRs, links and integrations

    static let links = HelpTopic(
        id: .links, title: "PRs, links and integrations", symbol: "link",
        blocks: [
            .paragraph("AgentBoard has no built-in knowledge of any ticket system, team or host. Everything specific to your setup comes in through these generic mechanisms."),
            .heading("GitHub PRs"),
            .bullets([
                "If the GitHub CLI `gh` is installed and logged in, `agentboard refresh-links` (run by the app at launch and every 10 minutes) looks up the PR for each session's current branch with `gh pr list`. Main, master and develop are skipped.",
                "An open PR is re-checked after 15 minutes; merged and closed PRs are never fetched again; a branch change looks again.",
                "**Owned PRs**: a PR is the session's own when the session reported it (`agentboard link`, an inbox file, a provider) or `gh` found it on the session's branch and it was created while the session was running. A PR inherited from the branch is shown but never keeps the session on the board.",
                "**Awaits your review**: an owned, open PR awaits you until someone other than automation reviews or comments on it. Bots, the PR's author, the account `gh` is logged in as, and any login listed in `automation_logins` do not count. When the PR's author is the logged-in account (you and the session share one account), that account's responses count once they come while the session is not mid-turn, and the reason reads “PR #N open, no response yet”.",
                "**PR buttons** on rows open each PR directly; the detail's Links section lists every link with its state.",
            ]),
            .heading("Reporting links, status and titles"),
            .paragraph("Any script, skill or hook running inside a session can report to it. With no `--session TOOL:ID`, the session is found from the caller's environment, its process tree, or the current directory; if none matches, the command warns and still exits 0."),
            .code(#"""
            agentboard link --kind pr|ticket|url --ref REF [--url URL] [--state OPEN|MERGED|CLOSED|DONE] [--title T] [--branch B]
            agentboard status --text "phase/label" [--ttl MIN]
            agentboard title --text "what this session is doing"
            """#),
            .bullets([
                "`link` records a PR, ticket or URL. A PR reported this way counts as the session's own; `--branch` is its head branch (default: the session's current branch).",
                "`status` sets what the session is doing right now; the row shows it as its first line until `--ttl` minutes pass.",
                "`title` names the session until its work moves on far enough for a generated retitle. A title you set yourself in Claude Code (`/rename`) is never replaced.",
            ]),
            .heading("Inbox drop directory"),
            .paragraph("A tool that cannot run the CLI can drop a JSON file in the `inbox/` folder of the data directory (`agentboard paths` shows it): `{\"session\": \"tool:id\" or null, \"cwd\": \"...\", \"links\": [...], \"status\": \"...\", \"notes\": [...]}`. It is read and deleted on the next snapshot."),
            .heading("Providers"),
            .paragraph("List external integrations under `providers` in config.json. `refresh-links` runs each command (30 second timeout) with the open sessions as JSON on stdin and reads back `[{\"session\": \"tool:id\", \"links\": [...], \"notes\": [...]}]` from stdout. This is how a team-specific integration plugs in without any code in AgentBoard; `examples/providers/` in the AgentBoard checkout has a generic one that reads git trailers."),
            .heading("Ticket patterns"),
            .paragraph("`ticket_patterns` in config.json turns ticket references in a session's branch name or first prompt into links: each pattern is a regular expression and a URL with `{ref}` where the match goes. Nothing is matched until you add a pattern."),
            .heading("Titles that follow the work"),
            .paragraph("Claude Code names a session once, so a long-lived session keeps its opening summary. `refresh-links` retitles a session once it has `retitle_min_prompts` (default 5) new prompts since its last retitle, or changed branch, by running `retitle_command` (default: the local `claude` CLI with Haiku) on a short summary of its recent prompts and replies; at most `retitle_max_per_run` (default 5) sessions per run, and at most once an hour per session. Done, ended, user-named and week-idle sessions are left alone. Set `\"retitle_command\": null` to turn it off."),
        ])

    // MARK: - 8. Settings

    static let settings = HelpTopic(
        id: .settings, title: "Settings", symbol: "gearshape",
        blocks: [
            .paragraph("Open Settings from the gear menu in the popover or the dashboard (**Settings…**). Changes apply right away. Each setting below gives its default and the key it is stored under in the app's defaults (domain `io.github.agentboard`)."),
            .heading("Appearance"),
            .keyValue([
                HelpRow(key: "Appearance", value: "**System** (default), **Dark** or **Light** for AgentBoard's windows. The menu bar icon follows the system setting either way. Key `appearance`."),
            ]),
            .heading("Menu bar icon"),
            .keyValue([
                HelpRow(key: "Color the menu bar icon by session state", value: "On by default. Off shows the plain monochrome icon. Key `menuBarIconColored`."),
                HelpRow(key: "Flash when a session needs you", value: "On by default; needs coloring. The icon flashes when a session newly needs you. Key `menuBarIconFlash`."),
                HelpRow(key: "Stop flashing", value: "**When I open AgentBoard** (default): stops when you open the popover or the dashboard. **After 30 seconds** / **After 2 minutes**: also stops on its own after that long. **Never (until handled)**: ignores opening AgentBoard and stops only when every flashing session has left Needs you. Key `menuBarIconFlashStop` (`onOpen`, `after30Seconds`, `after2Minutes`, `never`)."),
            ]),
            .heading("Notifications"),
            .keyValue([
                HelpRow(key: "Notify when a session needs you", value: "Off by default. Posts a macOS notification when a session newly needs you; macOS asks for permission the first time you turn it on. Key `notifyOnNeedsYou`."),
            ]),
            .heading("General"),
            .keyValue([
                HelpRow(key: "Launch at login", value: "Off by default. Starts AgentBoard when you log in. Stored by macOS (System Settings → General → Login Items), not in AgentBoard's defaults."),
            ]),
            .heading("CLI & Data"),
            .keyValue([
                HelpRow(key: "agentboard path override", value: "Empty by default. AgentBoard looks for the `agentboard` CLI in its own app bundle, then on `$PATH`, then here. Key `agentboardPath`."),
                HelpRow(key: "Use fixture data (development)", value: "Off by default. Shows a saved sample snapshot instead of calling the CLI; actions are logged, not run. Key `agentboardUseFixture`."),
                HelpRow(key: "Fixture path", value: "Shown while fixture data is on. Empty uses the sample bundled in the app. Key `agentboardFixturePath`."),
            ]),
            .heading("Diagnostics"),
            .keyValue([
                HelpRow(key: "Verbose logging", value: "Off by default. Also logs every background snapshot and the CLI's debug detail (it passes `AGENTBOARD_LOG_LEVEL=debug` to every CLI call). Key `verboseLogging`."),
                HelpRow(key: "Open log", value: "Reveals `agentboard.log` in Finder (also in the gear menu as **Open Log**)."),
            ]),
            .paragraph("Settings that change what the CLI records or detects (update markers, PR automation accounts, retitling, the log level) live in config.json instead: see **config.json reference**."),
        ])

    // MARK: - 9. config.json reference

    static let config = HelpTopic(
        id: .config, title: "config.json reference", symbol: "curlybraces",
        blocks: [
            .paragraph("The command line tool reads optional settings from `config.json` in the data directory: `~/Library/Application Support/AgentBoard/config.json` by default, or `$AGENTBOARD_HOME/config.json`. Run `agentboard paths` to see the directory in use. The file does not exist until you create it; it is one JSON object, and every key is optional. A missing or unreadable file means all defaults."),
            .paragraph("It is read fresh by every command, so an edit applies on the next snapshot (a few seconds), the next hook event, or the next `refresh-links` (every 10 minutes, or at app launch), depending on the key."),
            .keyValue([
                HelpRow(key: "`providers`", value: "List of objects, default none. External integrations `refresh-links` runs: `name` (a label for the log) and `command` (the argv list to run). See PRs, links and integrations."),
                HelpRow(key: "`ticket_patterns`", value: "List of objects, default none. `regex` (a regular expression) and `url` (a link template with `{ref}`), matched against each session's branch and first prompt."),
                HelpRow(key: "`automation_logins`", value: "List of GitHub logins, default none. Extra accounts (a reviewer bot, an author bot) whose reviews and comments are not a response from you when deciding whether a PR awaits your review."),
                HelpRow(key: "`note_markers`", value: "List of strings, default `[\"USER_ACTION:\"]`. A final-message line starting with one of these becomes a needs_you update. An empty list turns marker detection off."),
                HelpRow(key: "`note_unattended_prefixes`", value: "List of strings, default `[\"# Autonomous loop\"]`. A turn whose prompt starts with one of these is unattended: its errors and milestones become updates, and an idle loop session stays Running."),
                HelpRow(key: "`prompt_injected_prefixes`", value: "List of strings, default `[\"<task-notification>\", \"<agent-message\", \"<cross-session-message\"]`. A prompt starting with one of these was injected by Claude Code (a finished background task, another agent's or session's message): it keeps the session Running but never counts as you replying."),
                HelpRow(key: "`retitle_command`", value: "Argv list or shell-style string, or `null`. Default: the local `claude` CLI with Haiku, when installed. Reads a session's recent context on stdin and prints a new title. `null` turns retitling off."),
                HelpRow(key: "`retitle_min_prompts`", value: "Integer ≥ 1, default `5`. New prompts since the last retitle before a session is retitled again."),
                HelpRow(key: "`retitle_max_per_run`", value: "Integer ≥ 0, default `5`. At most this many sessions retitled per `refresh-links`; `0` turns it off."),
                HelpRow(key: "`log_level`", value: "`debug`, `info`, `warn` or `error`, default `info`. The diagnostic log's level; the `AGENTBOARD_LOG_LEVEL` environment variable wins over it."),
            ]),
            .heading("Example"),
            .code(#"""
            {
              "providers": [{"name": "trailers", "command": ["/path/to/provider"]}],
              "ticket_patterns": [{"regex": "[A-Z][A-Z0-9]+-\\d+", "url": "https://tickets.example.com/browse/{ref}"}],
              "automation_logins": ["my-reviewer-bot"],
              "note_markers": ["USER_ACTION:"],
              "note_unattended_prefixes": ["# Autonomous loop"],
              "prompt_injected_prefixes": ["<task-notification>", "<agent-message", "<cross-session-message"],
              "retitle_min_prompts": 5,
              "retitle_max_per_run": 5,
              "log_level": "info"
            }
            """#),
        ])

    // MARK: - 10. CLI reference

    static let cli = HelpTopic(
        id: .cli, title: "CLI reference", symbol: "terminal",
        blocks: [
            .paragraph("The app does everything through the `agentboard` command line tool, and you can too. `--session TOOL:ID` (for example `claude:<session id>`) picks a session; without it, commands run from inside a session find it on their own."),
            .heading("Looking at sessions"),
            .keyValue([
                HelpRow(key: "`agentboard snapshot`", value: "Every session and its state. `--json` prints the JSON the app reads; `--all` includes hidden sessions. Never touches the network."),
                HelpRow(key: "`agentboard paths`", value: "The data directory, database and installed core. `--json` for machine-readable output."),
                HelpRow(key: "`agentboard logs`", value: "The diagnostic log. `--since 10m` (or `30s`, `2h`, `1d`, an ISO-8601 time), `--grep TEXT`, `--lines N` (default 200 without `--since`), `--app` to merge the app's own log lines."),
            ]),
            .heading("Acting on a session"),
            .keyValue([
                HelpRow(key: "`agentboard mark <tool> <id> done|open|snooze`", value: "Set your own triage. `--hours N` for how long to snooze; `--note TEXT` to attach a note."),
                HelpRow(key: "`agentboard focus <tool> <id>`", value: "Bring the session's terminal tab to the front."),
                HelpRow(key: "`agentboard resume <tool> <id>`", value: "Open a new terminal tab that resumes the session. `--print` only prints the command."),
            ]),
            .heading("Reporting from inside a session"),
            .keyValue([
                HelpRow(key: "`agentboard link`", value: "Record a link: `--kind pr|ticket|url`, `--ref REF`, and optionally `--url`, `--state OPEN|MERGED|CLOSED|DONE`, `--title`, `--branch` (a PR's head branch), `--session`."),
                HelpRow(key: "`agentboard status`", value: "Set what the session is doing: `--text`, `--ttl MIN` (minutes until it expires), `--session`."),
                HelpRow(key: "`agentboard title`", value: "Name the session: `--text`, `--session`. A title you set with `/rename` is kept."),
                HelpRow(key: "`agentboard note`", value: "Add an update: `--kind needs_you|update|error`, `--text`, `--session`."),
            ]),
            .heading("Updates"),
            .keyValue([
                HelpRow(key: "`agentboard notes`", value: "List open updates. `--session`, `--kind K ...`, `--all` (include dismissed), `--json`."),
                HelpRow(key: "`agentboard notes --dismiss ID ...`", value: "Dismiss these updates."),
                HelpRow(key: "`agentboard notes --dismiss-all`", value: "Dismiss every open update; `--session` and `--kind` narrow it."),
                HelpRow(key: "`agentboard notes --rebuild`", value: "Classify recorded messages the hook has not seen. `--days N` limits it to recent ones; `--reset` drops the updates it made itself and re-applies the current rules (never reopens a dismissed one)."),
            ]),
            .heading("Data and links"),
            .keyValue([
                HelpRow(key: "`agentboard backfill`", value: "Import Claude Code transcripts and Codex rollouts from the last `--days N` (default 30). Never overwrites what the hooks recorded. The hooks also run it with `--session TOOL:ID` and `--transcript PATH` to re-read one session."),
                HelpRow(key: "`agentboard refresh-links`", value: "Read the inbox, run providers, look up GitHub PRs and their reviews, and retitle sessions. The only command that touches the network."),
                HelpRow(key: "`agentboard prune`", value: "Delete sessions (and their events, links and updates) not updated in `--days N` (default 90)."),
                HelpRow(key: "`agentboard map-codex`", value: "Internal: match a Codex session to its rollout file (`--session`, `--cwd`, `--at`). The Codex hook runs it."),
            ]),
            .heading("Hooks"),
            .keyValue([
                HelpRow(key: "`agentboard install-hooks`", value: "Register the hooks. `--claude` / `--codex` limit it to one tool; `--dry-run` shows the change. Backs up the file first and leaves other hooks alone."),
                HelpRow(key: "`agentboard uninstall-hooks`", value: "Remove only AgentBoard's own hook entries. `--claude`, `--codex`, `--dry-run`."),
                HelpRow(key: "`agentboard enable-codex-hooks`", value: "Turn on `hooks = true` under `[features]` in `~/.codex/config.toml` (backed up first). `--dry-run` previews."),
            ]),
            .heading("Environment variables"),
            .keyValue([
                HelpRow(key: "`AGENTBOARD_HOME`", value: "The data directory (database, logs, config.json, inbox). Default `~/Library/Application Support/AgentBoard`. `install.sh` bakes a non-default value into the installed CLI."),
                HelpRow(key: "`AGENTBOARD_LOG_LEVEL`", value: "`debug`, `info`, `warn` or `error`; wins over config.json `log_level`."),
                HelpRow(key: "`AGENTBOARD_USE_FIXTURE=1`", value: "Start the app on its bundled sample snapshot instead of the CLI (development)."),
                HelpRow(key: "`AGENTBOARD_FIXTURE_PATH`", value: "Start the app on this snapshot JSON file instead of the CLI (development)."),
                HelpRow(key: "`AGENTBOARD_HOOK_SKIP=1`", value: "The hook ignores every event while it is set; the retitle command runs with it so its own session never shows up."),
                HelpRow(key: "`AGENTBOARD_CORE_DIR`, `AGENTBOARD_PYTHON3`", value: "For `install.sh`: where to install the CLI's core files, and which Python (3.11 or newer) to use."),
            ]),
        ])

    // MARK: - 11. Diagnostics and troubleshooting

    static let diagnostics = HelpTopic(
        id: .diagnostics, title: "Diagnostics and troubleshooting", symbol: "stethoscope",
        blocks: [
            .heading("Logs"),
            .bullets([
                "`agentboard.log` in the data directory records every CLI command, each focus attempt, refresh-links (each provider and `gh` call), backfill, and what the hook installers changed. One line per event with `key=value` fields. Text passed with `--note`, `--text` or `--title` is logged only as its length.",
                "It rotates at 2 MB to `agentboard.log.1`, so it never takes more than about 4 MB.",
                "`hook-errors.log`, next to it, records hook failures; hooks never surface errors in your session.",
                "The app logs every CLI call to the macOS unified log (subsystem `io.github.agentboard`).",
                "**Open Log** (gear menu, or Settings → Diagnostics) reveals the log in Finder; `agentboard logs` prints it, and `agentboard logs --app` merges the app's lines in.",
                "**Verbose logging** in Settings → Diagnostics turns on debug detail for both the app and the CLI. Without the app, set `AGENTBOARD_LOG_LEVEL=debug` or `\"log_level\": \"debug\"` in config.json.",
            ]),
            .heading("Common problems"),
            .keyValue([
                HelpRow(key: "No sessions shown", value: "Run `agentboard snapshot` in a terminal. If it lists nothing, the hooks are probably not installed: re-run `scripts/install.sh` (after installing Claude Code or Codex for the first time too). Sessions started before the hooks were installed appear after a backfill. Check **Show hidden** for non-interactive runs."),
                HelpRow(key: "Codex sessions missing", value: "Codex needs `hooks = true` under `[features]` in `~/.codex/config.toml` (`agentboard enable-codex-hooks`) and the hooks trusted with `/hooks` inside Codex."),
                HelpRow(key: "“agentboard CLI not found”", value: "The app looks in its own bundle, then `$PATH`, then Settings → CLI & Data → agentboard path override. Re-run `scripts/install.sh` or set the override."),
                HelpRow(key: "No PR buttons", value: "PR detection needs the GitHub CLI: install `gh` and run `gh auth login`. PRs appear after the next `refresh-links` (at launch and every 10 minutes). `agentboard logs --grep gh` shows each lookup."),
                HelpRow(key: "A PR still “awaits your review”", value: "Only a review or comment by someone other than automation counts. If a bot account of yours is answering, it is ignored only when it is a GitHub bot or listed in `automation_logins`; if you share one account with the session, respond while the session is idle."),
                HelpRow(key: "Stale titles", value: "Titles are refreshed by `refresh-links` after 5 new prompts or a branch change, if the `claude` CLI is installed. Name a session yourself with `/rename` or `agentboard title --text`."),
                HelpRow(key: "Clicking a row focuses the wrong tab", value: "Run `agentboard logs --grep cli.focus --lines 5` and read the `outcome`. Focus works with iTerm2 and Apple Terminal and may need the Automation permission for them (System Settings → Privacy & Security → Automation)."),
                HelpRow(key: "Too many or too few updates", value: "Adjust `note_markers`, `note_unattended_prefixes` and `prompt_injected_prefixes` in config.json, then run `agentboard notes --rebuild --reset`."),
                HelpRow(key: "Menu bar icon stays red", value: "A session still needs you, or an open needs_you update has not been answered by a newer prompt. Open the popover, then answer the session or dismiss the update."),
            ]),
        ])

    // MARK: - 12. Data and privacy

    static let privacy = HelpTopic(
        id: .privacy, title: "Data and privacy", symbol: "lock.shield",
        blocks: [
            .paragraph("Everything AgentBoard knows stays on your Mac. There is no server, no account and no telemetry."),
            .heading("What is stored where"),
            .keyValue([
                HelpRow(key: "Data directory", value: "`~/Library/Application Support/AgentBoard` (or `$AGENTBOARD_HOME`; `agentboard paths` shows it): `agentboard.db` (sessions, events, triage, links, updates) with its transient `agentboard.db-wal` and `agentboard.db-shm`, `agentboard.log` and `agentboard.log.1`, `hook-errors.log`, `install-manifest.json` (what `install-hooks` created or backed up, so uninstalling can undo exactly that), the optional `config.json`, the `inbox/` folder and the `reparse-locks/` folder (one small marker per session, so the end-of-turn hooks start at most one transcript re-read every few seconds)."),
                HelpRow(key: "Installed CLI", value: "`~/.local/share/agentboard/core` (a copy of the core files; the hooks run it)."),
                HelpRow(key: "Hooks", value: "Entries in `~/.claude/settings.json` and `~/.codex/hooks.json`, backed up before every change."),
                HelpRow(key: "App settings", value: "The macOS defaults domain `io.github.agentboard`."),
            ]),
            .paragraph("Besides its own data directory, AgentBoard reads only Claude Code's and Codex's files under `~/.claude` and `~/.codex` (transcripts, the session registry) and each session's git branch."),
            .heading("Network"),
            .paragraph("Only `agentboard refresh-links` uses the network, and only through tools you set up: the GitHub CLI `gh` (PRs and their reviews), the provider commands in config.json, and the retitle command (by default the local `claude` CLI, which sends a short summary of a session's recent prompts and replies to the model). The snapshot the app polls never touches the network. Set `\"retitle_command\": null` to turn retitling off."),
            .heading("Removing AgentBoard"),
            .paragraph("`scripts/uninstall.sh` removes the app, the installed CLI and only AgentBoard's own hook entries; any other hooks in `~/.claude/settings.json` and `~/.codex/hooks.json` are left as they are. When removing those entries leaves a file exactly as it was before the install, the original bytes are written back from the backup `install-hooks` made, so its formatting returns too; if the install created the file and nothing else is left in it, the file is deleted. A `hooks = true` that `enable-codex-hooks` added to an existing `~/.codex/config.toml` stays (its backup sits beside it); a `config.toml` that `enable-codex-hooks` created and that still holds nothing else is deleted. The data directory is kept unless you pass `--purge-data`. `--dry-run` previews; `--no-hooks` and `--no-app` skip parts. `agentboard prune --days N` deletes old sessions without uninstalling."),
        ])
}

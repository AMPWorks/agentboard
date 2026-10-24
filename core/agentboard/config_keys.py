"""Every `config.json` key core reads, in one registry.

`config.json` lives in the data directory (`$AGENTBOARD_HOME`, see
`agentboard paths`). Each module reads its own keys with lenient defaults;
this registry is what documents them. `core/tests/test_help_coverage.py`
fails when core reads a key that is not listed here, when a listed key (or
sub-field) no longer appears in core, and when a listed key is missing from
README.md or the in-app guide (`app/Sources/AgentBoard/Help/HelpContent.swift`).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ConfigKey:
    key: str
    type: str
    default: str
    description: str
    # For a list of objects: the object fields core reads.
    fields: tuple = ()


CONFIG_KEYS = (
    ConfigKey(
        "providers", "list of objects", "none",
        "External integrations `refresh-links` runs, each with the open sessions as JSON on "
        "stdin, reading back links, statuses and notes.",
        fields=("name", "command"),
    ),
    ConfigKey(
        "ticket_patterns", "list of objects", "none",
        "Ticket references matched against each session's branch and first prompt; `url` "
        "has `{ref}` where the match goes.",
        fields=("regex", "url"),
    ),
    ConfigKey(
        "automation_logins", "list of strings", "none",
        "GitHub logins (a reviewer bot, an author bot) whose reviews and comments are not "
        "a response from the user when deciding whether a PR awaits review.",
    ),
    ConfigKey(
        "note_markers", "list of strings", '["USER_ACTION:"]',
        "A final-message line starting with one of these becomes a needs_you note; an "
        "empty list turns marker detection off.",
    ),
    ConfigKey(
        "note_unattended_prefixes", "list of strings", '["# Autonomous loop"]',
        "A turn whose prompt starts with one of these is unattended (loop) work.",
    ),
    ConfigKey(
        "prompt_injected_prefixes", "list of strings",
        '["<task-notification>", "<agent-message", "<cross-session-message"]',
        "A prompt starting with one of these was injected by Claude Code (a finished "
        "background task, another agent's or session's message): it counts as activity but "
        "never as the user replying.",
    ),
    ConfigKey(
        "retitle_command", "argv list, string or null",
        "the local claude CLI with Haiku, when installed",
        "Reads a session's recent context on stdin and prints a new title; null turns "
        "retitling off.",
    ),
    ConfigKey(
        "retitle_min_prompts", "integer >= 1", "5",
        "New prompts since the last retitle before a session is retitled again.",
    ),
    ConfigKey(
        "retitle_max_per_run", "integer >= 0", "5",
        "At most this many sessions retitled per `refresh-links`; 0 turns it off.",
    ),
    ConfigKey(
        "prune_days", "integer >= 0", "90",
        "`prune` deletes sessions idle this many days (never one with an open needs_you "
        "note, a PR awaiting you, an active snooze or a live process) and dismissed notes "
        "older than this; 0 never deletes.",
    ),
    ConfigKey(
        "compact_days", "integer >= 0", "7",
        "`prune` deletes tool-use and subagent events older than this many days; 0 never "
        "compacts.",
    ),
    ConfigKey(
        "log_level", "debug | info | warn | error", "info",
        "The diagnostic log's level; `AGENTBOARD_LOG_LEVEL` wins over it.",
    ),
)

CONFIG_KEY_NAMES = tuple(entry.key for entry in CONFIG_KEYS)

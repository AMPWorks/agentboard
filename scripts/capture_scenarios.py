#!/usr/bin/env python3
"""Popover screenshot scenarios for scripts/capture-popover.sh.

Writes one snapshot fixture per scenario into OUT_DIR, every timestamp
shifted so the newest reads "now" (docs/DEVELOPMENT.md, "Regenerating the
screenshots", step 2), plus OUT_DIR/scenarios.tsv with one line per capture:

    name <TAB> fixture path <TAB> defaults <TAB> query

`defaults` is a space-separated list of key=type:value (type is string,
bool, int or float) written into the capture app's own defaults domain;
`query` is typed into the popover's search field ("" for none). Every
session is built from app/Fixtures/snapshot.json, so only its fixture
projects, branches and titles can appear. Stdlib only, Python 3.9+.

    capture_scenarios.py FIXTURE OUT_DIR [--only REGEX] [--list]
"""

import argparse
import copy
import datetime as dt
import json
import re
import sys
from pathlib import Path

PROJECTS = ["widget-service", "billing-api", "metrics-pipeline", "directory-tools",
            "release-tools", "ops-tools", "ai-skills", "app"]
TITLES = [
    "Fix flaky retry logic in reconcile loop", "Add rollup dedupe for late events",
    "Wire TTL cache into lookup path", "Backfill parser for the second CLI",
    "Investigate counter switch-case fallthrough", "Draft release stage 3 plan",
    "Tighten outage API cleanup", "Document lookup_name and its caveats",
    "Speed up schema migration dry run", "Split the dashboard view",
    "Audit comment hygiene skill", "Harden the terminal focus helper",
]
BRANCHES = ["fix-retry-flake", "rollup-dedupe", "codex/lookup-cache", "parser-v2",
            "fix-counter-switch-case", "release-stage3-plan", "outage-api-cleanup",
            "codex/lookup-doc", "schema-dry-run", "dashboard-split"]
# Reasons as the CLI emits them (hook.py _extract_waiting_reason, reconcile).
REASONS = {
    "needs_you": ["Bash", "Edit", "github: Which repository should the issue go to?",
                  "rate_limit: API Error: Rate limit reached", "question"],
    "interrupted": [None],
    "review": ["Your turn: Summary", "PR #231 awaits your review", "Your turn: tests pass"],
    "running": [None, "Waiting on 1 background task: shell"],
    "unconcluded": [None, "3 todos left open"],
    "done": [None],
}
# A mixed board, in the order sessions are added.
MIX = ["needs_you", "running", "review", "running", "interrupted", "done", "unconcluded",
       "needs_you", "running", "review", "done", "running"]
MODES = ["plan", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "default", None]


def parse_ts(text):
    return dt.datetime.strptime(text[:19], "%Y-%m-%dT%H:%M:%S")


def shift_to_now(text, generated_at):
    """Shifts every ISO timestamp in a fixture's JSON text by the same delta."""
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None, microsecond=0)
    delta = now - parse_ts(generated_at)
    return re.sub(r"(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(\.\d+)?Z",
                  lambda m: (parse_ts(m.group(1)) + delta).strftime("%Y-%m-%dT%H:%M:%S")
                  + (m.group(2) or "") + "Z", text)


class Board:
    """Builds sessions from the sample fixture's own sessions."""

    def __init__(self, sample):
        self.sample = sample
        self.by_state = {}
        for session in sample["sessions"]:
            self.by_state.setdefault(session["state"], []).append(session)
        self.base = parse_ts(sample["generated_at"])

    def ts(self, minutes_ago):
        return (self.base - dt.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")

    def session(self, index, state, **overrides):
        templates = self.by_state.get(state) or self.by_state["running"]
        session = copy.deepcopy(templates[index % len(templates)])
        project = PROJECTS[index % len(PROJECTS)]
        session.update({
            "key": "%s:scenario-%d" % (session["tool"], index),
            "session_id": "scenario-%d" % index,
            "state": state,
            "title": TITLES[index % len(TITLES)],
            "status_text": None,
            "project": project,
            "cwd": "/Users/dev/projects/" + project,
            "branch": BRANCHES[index % len(BRANCHES)],
            "waiting_reason": REASONS[state][index % len(REASONS[state])],
            "updated_at": self.ts(2 + index * 7),
            "notes": [],
            "notes_count": 0,
            "desktop": False,
            "tmux_session": None,
            "permission_mode": MODES[index % len(MODES)] if session["tool"] == "claude" else None,
            "ended_at": None if state not in ("done", "unconcluded") else self.ts(1 + index * 7),
        })
        session.update(overrides)
        # A template of the other tool keeps its model; match the tool.
        if session["tool"] == "claude" and not str(session.get("model", "")).startswith("claude"):
            session["model"] = "claude-opus-5-5"
        elif session["tool"] == "codex" and str(session.get("model", "")).startswith("claude"):
            session["model"] = "gpt-5-codex"
        return session

    def snapshot(self, sessions):
        counts = {k: 0 for k in ["needs_you", "interrupted", "running", "review",
                                 "unconcluded", "snoozed", "done"]}
        for session in sessions:
            counts[session["state"]] = counts.get(session["state"], 0) + 1
        return {"generated_at": self.sample["generated_at"], "counts": counts,
                "sessions": sessions, "usage": copy.deepcopy(self.sample.get("usage", []))}

    def mixed(self, count):
        return self.snapshot([self.session(i, MIX[i % len(MIX)]) for i in range(count)])


LONG = ("Investigate why the nightly rollup double-counts late events after the "
        "retention window moves, and add a regression test that replays a full day")


def scenarios(sample):
    """(name, snapshot dict or raw text, defaults, query) per scenario."""
    board = Board(sample)
    fixed = ["popoverAutoWidth=bool:false", "popoverWidth=float:460"]
    shown = ["updatesPanelMode=string:alwaysShown"]
    out = []

    def add(name, snap, defaults=(), query=""):
        out.append((name, snap, list(defaults), query))

    add("sample", sample, fixed)
    add("sample-full-width", sample)
    # The README's popover-light/-dark.png (docs/DEVELOPMENT.md).
    add("readme-popover", sample, ["popoverAutoWidth=bool:false", "popoverWidth=float:430",
                                   "popoverHeight=float:620", "showClosedAndMergedPRs=bool:true"])
    add("empty", board.snapshot([]), fixed)
    add("one-session", board.snapshot([board.session(1, "running")]), fixed)
    add("five-sessions", board.mixed(5), fixed)
    add("fifteen-sessions", board.mixed(15), fixed)
    add("forty-sessions", board.mixed(40), fixed)
    add("forty-tall", board.mixed(40), fixed + ["popoverHeight=float:720"])
    add("forty-auto-expand", board.mixed(40), fixed + ["popoverAutoExpand=bool:true"])
    add("forty-narrow", board.mixed(40), ["popoverAutoWidth=bool:false", "popoverWidth=float:320"])
    add("many-needs-you", board.snapshot(
        [board.session(i, "needs_you") for i in range(9)]
        + [board.session(20 + i, "running") for i in range(3)]), fixed)
    add("all-running", board.snapshot([board.session(i, "running") for i in range(8)]), fixed)
    add("done-with-query", board.snapshot(
        [board.session(i, "done") for i in range(10)] + [board.session(30, "running")]),
        fixed, query="state:done")
    for size in ["small", "default", "large"]:
        add("updates-%s-font" % size, sample, fixed + shown + ["updatesFontSize=string:" + size])
    long_text = [
        board.session(0, "needs_you", title=LONG,
                      branch="fix/very-long-branch-name-for-the-rollup-overcount-regression-test",
                      waiting_reason="github: " + LONG),
        board.session(1, "running", status_text="Building the dashboard split view: " + LONG),
        board.session(2, "review", project="metrics-pipeline-with-a-long-project-name",
                      waiting_reason="Your turn: " + LONG),
    ]
    add("long-text", board.snapshot(long_text), fixed)
    add("long-text-narrow", board.snapshot(long_text), ["popoverAutoWidth=bool:false",
                                                        "popoverWidth=float:320"])
    # The rubric's row checks: every permission mode, with and without a
    # status line, a desktop session, tmux, notes and the interrupted tag.
    modes = []
    for i, mode in enumerate(MODES):
        modes.append(board.session(i, "running" if i % 2 else "needs_you", tool="claude",
                                   key="claude:mode-%d" % i, permission_mode=mode,
                                   status_text="Running the test suite" if i % 3 == 0 else None))
    modes[1]["desktop"] = True
    modes[2]["tmux_session"] = "agentboard-dev"
    modes.append(board.session(40, "interrupted", tool="claude", key="claude:mode-int",
                               permission_mode="plan"))
    add("row-modes", board.snapshot(modes), fixed)

    def note(note_id, kind, text, minutes_ago, count=1):
        return {"id": note_id, "kind": kind, "text": text, "ts": board.ts(minutes_ago),
                "last_ts": board.ts(minutes_ago), "count": count, "source": "stop"}

    def with_notes(session, *notes):
        session["notes"] = list(notes)
        session["notes_count"] = len(notes)
        return session

    # Always shown with an hours-old info note and sessions that need you:
    # the needs_you and error notes sit above the sections, the info note
    # after them.
    add("updates-always-mixed", board.snapshot([
        with_notes(board.session(0, "needs_you"),
                   note(301, "needs_you", "USER_ACTION: approve the schema migration", 4)),
        board.session(7, "needs_you"),
        with_notes(board.session(2, "review"),
                   note(302, "update", "**PR #57 opened** for the rollup writeup; CI is green", 300)),
        with_notes(board.session(3, "running"),
                   note(303, "error", "Request timed out while calling the model API.", 20, count=4)),
        board.session(8, "running"),
    ]), fixed + shown)
    # Two notes of one project from two sessions: the row header names each
    # session's title after the project; one note repeated ("seen 3 times").
    add("two-notes-one-project", board.snapshot([
        with_notes(board.session(0, "review", project=PROJECTS[0], cwd="/Users/dev/projects/" + PROJECTS[0]),
                   note(311, "update", "Tests pass; the branch is ready for review", 12, count=3)),
        with_notes(board.session(1, "review", project=PROJECTS[0], cwd="/Users/dev/projects/" + PROJECTS[0]),
                   note(312, "update", "Tests pass; the branch is ready for review", 30)),
    ]), fixed + shown)
    # PR buttons on rows with and without a model tag: they stay at the
    # trailing edge either way.
    pr = {"kind": "pr", "ref": "#231", "url": "https://github.com/example/billing-api/pull/231",
          "state": "OPEN", "title": "Fix missing switch case for block counter"}
    def no_model(session):
        # After board.session, which gives every Claude row a model.
        session.update(model=None, context_tokens=None)
        return session

    add("row-pr-no-model", board.snapshot([
        board.session(2, "review", tool="claude", links=[dict(pr)]),
        no_model(board.session(9, "review", tool="claude", links=[dict(pr, ref="#232")])),
        board.session(4, "running", tool="claude", links=[dict(pr, ref="#233")]),
        no_model(board.session(5, "running", tool="claude", links=[dict(pr, ref="#234")])),
    ]), fixed)
    add("banner", "{ this is not a snapshot", fixed)
    return out


def main(argv):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("fixture")
    parser.add_argument("out_dir")
    parser.add_argument("--only", help="regex on scenario names")
    parser.add_argument("--appearances", default="light,dark")
    parser.add_argument("--list", action="store_true", help="print the names only")
    args = parser.parse_args(argv)

    sample = json.loads(Path(args.fixture).read_text())
    chosen = [s for s in scenarios(sample) if not args.only or re.search(args.only, s[0])]
    appearances = [a for a in args.appearances.split(",") if a]
    if args.list:
        for name, *_ in chosen:
            for look in appearances:
                print("%s-%s" % (name, look))
        return 0
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    lines = []
    for name, snap, defaults, query in chosen:
        path = out / (name + ".json")
        text = snap if isinstance(snap, str) else shift_to_now(json.dumps(snap, indent=1),
                                                               sample["generated_at"])
        path.write_text(text)
        for look in appearances:
            settings = defaults + ["appearance=string:" + look.capitalize()]
            lines.append("\t".join(["%s-%s" % (name, look), str(path), " ".join(settings), query]))
    (out / "scenarios.tsv").write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env bash
# Captures the menu bar popover in many states (scripts/capture_scenarios.py)
# by running the built app on generated fixtures and screenshotting its
# panel. Quiet: one summary line on success; on failure what failed plus
# the path to the full log.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORK_DIR="$ROOT_DIR/app/build/usability"
LOG="$WORK_DIR/capture-popover.log"

usage() {
  cat <<USAGE
Usage: $0 [--verbose] [--binary PATH] [--out DIR] [--only REGEX] [--list]

Runs the bare app binary (default app/.build/release/AgentBoard, from
scripts/build-app.sh --no-install) once per scenario on a fixture generated
from app/Fixtures/snapshot.json, opens the popover by clicking its menu bar
icon, and saves the popover panel as DIR/<scenario>.png.

  --binary PATH   the AgentBoard binary to run
  --out DIR       where the PNGs go (default app/build/usability/shots)
  --only REGEX    only the scenarios whose name matches
  --list          print the scenario names and exit
  -v, --verbose   stream the full output (also: AGENTBOARD_VERBOSE=1)
  -h, --help      show this help

Needs Screen Recording and Accessibility permission for the terminal, and
refuses to run while another AgentBoard is running (its menu bar icon and
⌃⌥⌘A hotkey would clash): quit it first. Each run copies the binary under a
unique name so its settings land in a defaults domain of its own, uses an
empty temporary AGENTBOARD_HOME, sets light or dark through the app's own
Appearance setting, and removes the domain and the data directory on exit.
Output is always written to $LOG.
USAGE
}

VERBOSE=0
case "${AGENTBOARD_VERBOSE:-}" in
  ''|0) ;;
  *) VERBOSE=1 ;;
esac
BINARY="$ROOT_DIR/app/.build/release/AgentBoard"
OUT_DIR="$WORK_DIR/shots"
ONLY=""
LIST=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    -v|--verbose) VERBOSE=1; shift ;;
    --binary) BINARY="${2:?--binary needs a path}"; shift 2 ;;
    --out) OUT_DIR="${2:?--out needs a directory}"; shift 2 ;;
    --only) ONLY="${2:?--only needs a regex}"; shift 2 ;;
    --list) LIST=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown argument: $1" >&2; usage >&2; exit 1 ;;
  esac
done

PYTHON="$(command -v python3 || true)"
if [[ -z "$PYTHON" ]]; then
  echo "capture-popover: python3 not found" >&2
  exit 1
fi
scenario_args=("$ROOT_DIR/app/Fixtures/snapshot.json" "$WORK_DIR/fixtures")
if [[ -n "$ONLY" ]]; then scenario_args+=(--only "$ONLY"); fi
if [[ "$LIST" == 1 ]]; then
  exec "$PYTHON" "$SCRIPT_DIR/capture_scenarios.py" "${scenario_args[@]}" --list
fi

mkdir -p "$WORK_DIR" "$OUT_DIR"
: >"$LOG"

log() {
  printf '%s\n' "$*" >>"$LOG"
  if [[ "$VERBOSE" == 1 ]]; then printf '%s\n' "$*"; fi
}

fail() {
  echo "capture-popover: $*" >&2
  echo "full log: $LOG" >&2
  exit 1
}

if [[ "$(uname -s)" != Darwin ]]; then fail "macOS only"; fi
if [[ ! -x "$BINARY" ]]; then fail "no app binary at $BINARY (run scripts/build-app.sh --no-install)"; fi
if pgrep -x AgentBoard >/dev/null 2>&1; then
  fail "AgentBoard is running; quit it first (gear menu → Quit AgentBoard)"
fi
# A copy left by an earlier run that was killed before cleanup has the same
# menu bar item and hotkey clash.
if pgrep -f "$WORK_DIR/bin/AgentBoardCapture" >/dev/null 2>&1; then
  fail "a capture copy from an earlier run is still running; quit it first (pkill -f '$WORK_DIR/bin/AgentBoardCapture')"
fi
if ! osascript -e 'tell application "System Events" to count processes' >>"$LOG" 2>&1; then
  fail "System Events is not scriptable: give the terminal Accessibility permission"
fi

# The helper that clicks the menu bar icon and finds the panel's window.
TOOL="$WORK_DIR/tools/capture-tool"
if [[ ! -x "$TOOL" || "$SCRIPT_DIR/capture_tool.swift" -nt "$TOOL" ]]; then
  mkdir -p "$WORK_DIR/tools"
  log "compiling $TOOL"
  swiftc -O "$SCRIPT_DIR/capture_tool.swift" -o "$TOOL" >>"$LOG" 2>&1 || fail "could not compile capture_tool.swift"
fi

rm -rf "$WORK_DIR/fixtures"
"$PYTHON" "$SCRIPT_DIR/capture_scenarios.py" "${scenario_args[@]}" >>"$LOG" 2>&1 \
  || fail "could not generate the scenario fixtures"

# A unique executable name: a bundle-less binary's defaults domain is its name.
DOMAINS_BEFORE="$(defaults domains | tr ',' '\n' | sed 's/^ *//')"
NAME="AgentBoardCapture$$"
while printf '%s\n' "$DOMAINS_BEFORE" | grep -qx "$NAME"; do NAME="AgentBoardCapture$$-$RANDOM"; done
mkdir -p "$WORK_DIR/bin"
APP="$WORK_DIR/bin/$NAME"
cp "$BINARY" "$APP"
DATA_HOME="$(mktemp -d "${TMPDIR:-/tmp}/agentboard-capture.XXXXXX")"
APP_PID=""

stop_app() {
  if [[ -n "$APP_PID" ]] && kill -0 "$APP_PID" 2>/dev/null; then
    kill "$APP_PID" 2>/dev/null || true
    local i
    for i in 1 2 3 4 5 6 7 8 9 10; do
      kill -0 "$APP_PID" 2>/dev/null || break
      /bin/sleep 0.3
    done
    kill -9 "$APP_PID" 2>/dev/null || true
  fi
  APP_PID=""
}

cleanup() {
  stop_app
  # Only the domain this run created; never one that existed before.
  if ! printf '%s\n' "$DOMAINS_BEFORE" | grep -qx "$NAME"; then
    defaults delete "$NAME" >/dev/null 2>&1 || true
    rm -f "$HOME/Library/Preferences/$NAME.plist"
  fi
  rm -f "$APP"
  if [[ -n "$DATA_HOME" && -d "$DATA_HOME" ]]; then rm -rf "$DATA_HOME"; fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# Waits for the status item, clicks it, and prints the panel's window id.
open_popover() {
  local i pos="" x y w h id=""
  for i in $(seq 1 40); do
    pos="$(osascript -e "tell application \"System Events\" to tell process \"$NAME\" to get {position, size} of menu bar item 1 of menu bar 2" 2>/dev/null || true)"
    [[ -n "$pos" ]] && break
    /bin/sleep 0.25
  done
  [[ -n "$pos" ]] || return 1
  IFS=', ' read -r x y w h <<<"$pos"
  "$TOOL" click "$((x + w / 2))" "$((y + h / 2))"
  for i in $(seq 1 20); do
    id="$("$TOOL" window "$APP_PID" 2>/dev/null || true)"
    [[ -n "$id" ]] && break
    /bin/sleep 0.25
  done
  [[ -n "$id" ]] || return 1
  printf '%s\n' "$id"
}

capture() {
  local name="$1" fixture="$2" settings="$3" query="$4" setting key value type id
  defaults delete "$NAME" >/dev/null 2>&1 || true
  for setting in $settings; do
    key="${setting%%=*}"
    value="${setting#*=}"
    type="${value%%:*}"
    value="${value#*:}"
    defaults write "$NAME" "$key" "-$type" "$value"
  done
  log "== $name ($settings) query='$query'"
  AGENTBOARD_HOME="$DATA_HOME" AGENTBOARD_FIXTURE_PATH="$fixture" "$APP" >>"$LOG" 2>&1 &
  APP_PID=$!
  if ! id="$(open_popover)"; then
    stop_app
    return 1
  fi
  if [[ -n "$query" ]]; then
    # Set the search field through Accessibility rather than typing, so no
    # keystroke can reach another app (the panel's app is never frontmost).
    if ! osascript -e "tell application \"System Events\" to tell process \"$NAME\" to set value of text field 1 of group 1 of window 1 to \"$query\"" >>"$LOG" 2>&1; then
      log "could not set the search field"
      stop_app
      return 1
    fi
  fi
  # Let the list settle (auto-expand, scroll, fonts) before the shot.
  /bin/sleep 1.2
  id="$("$TOOL" window "$APP_PID" 2>/dev/null || printf '%s' "$id")"
  screencapture -x -o -l "$id" "$OUT_DIR/$name.png" >>"$LOG" 2>&1 || { stop_app; return 1; }
  stop_app
  [[ -s "$OUT_DIR/$name.png" ]]
}

total=0
failed=()
while IFS=$'\t' read -r name fixture settings query; do
  [[ -n "$name" ]] || continue
  total=$((total + 1))
  if capture "$name" "$fixture" "$settings" "$query"; then
    log "ok $name"
  else
    log "FAILED $name"
    failed+=("$name")
  fi
done <"$WORK_DIR/fixtures/scenarios.tsv"

if [[ ${#failed[@]} -gt 0 ]]; then
  fail "${#failed[@]} of $total scenarios failed: ${failed[*]}"
fi
[[ $total -gt 0 ]] || fail "no scenario matches --only '$ONLY' (--list prints the names)"
echo "captured $total popover screenshots in $OUT_DIR"

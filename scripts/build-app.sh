#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="$ROOT_DIR/app"
CORE_DIR="$ROOT_DIR/core"
BUILD_DIR="$APP_DIR/build"
APP_BUNDLE="$BUILD_DIR/AgentBoard.app"
PREFIX="$HOME/Applications"
BUNDLE_ID="io.github.agentboard"

# See install.sh's identical export for why.
export PYTHONDONTWRITEBYTECODE=1

NO_INSTALL=0
# Quiet by default: step lines and tool output go to $BUILD_LOG only, and
# the terminal gets the SDK note (if any) plus one summary line, or the
# failing lines plus the log path. --verbose / AGENTBOARD_VERBOSE=1 shows
# everything as it runs (and still writes the log).
VERBOSE=0
case "${AGENTBOARD_VERBOSE:-}" in
  ''|0) ;;
  *) VERBOSE=1 ;;
esac
BUILD_LOG="$BUILD_DIR/build-app.log"
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-install)
      NO_INSTALL=1
      shift
      ;;
    --verbose)
      VERBOSE=1
      shift
      ;;
    --prefix)
      if [[ $# -lt 2 ]]; then
        echo "error: --prefix requires a directory argument" >&2
        exit 1
      fi
      PREFIX="$2"
      shift 2
      ;;
    *)
      echo "Unknown argument: $1" >&2
      echo "Usage: $0 [--no-install] [--prefix DIR] [--verbose]" >&2
      exit 1
      ;;
  esac
done
INSTALL_DIR="$PREFIX"

# Identical to install.sh's _bundle_identifier_matches -- see its
# comment. Duplicated rather than shared so this script keeps working
# standalone.
_bundle_identifier_matches() {
  local bundle="$1" id
  id="$(defaults read "$bundle/Contents/Info" CFBundleIdentifier 2>/dev/null)" || return 1
  [[ "$id" == "$BUNDLE_ID" ]]
}

# Identical to install.sh's _app_bundle_refusal_reason -- see its
# comment. Duplicated rather than shared so this script keeps working
# standalone.
_app_bundle_refusal_reason() {
  local bundle="$1"
  if [[ -L "$bundle" ]]; then
    echo "$bundle exists and is a symlink, not a plain directory AgentBoard installed; refusing to touch it"
  elif [[ -e "$bundle" ]]; then
    if [[ -d "$bundle" ]]; then
      if ! _bundle_identifier_matches "$bundle"; then
        echo "$bundle exists and doesn't look like AgentBoard (CFBundleIdentifier isn't $BUNDLE_ID); refusing to touch it"
      fi
    else
      echo "$bundle exists and is a file, not a directory AgentBoard installed; refusing to touch it"
    fi
  fi
}

# Identical to swift-test.sh's _swiftui_macro_probe. Duplicated rather
# than shared so this script keeps working standalone.
#
# Typechecks a tiny SwiftUI view that uses @State and @AppStorage against
# SDK $1 (empty: the toolchain default), the same way `swift build` picks
# its SDK (SDKROOT). Some SDKs declare those property wrappers as macros
# from a SwiftUIMacros plugin that a given toolchain may not ship (seen
# with a Command Line Tools release whose default SDK is newer than its
# plugins). Returns 0 when the SDK works, 1 when it fails for that reason,
# and 2 when the check itself was inconclusive (timeout, no compiler, any
# other error), printing the reason on stdout -- so a broken module cache
# or a sandbox denial never passes for "this SDK lacks the plugin".
# Bounded by AGENTBOARD_SDK_PROBE_TIMEOUT whole seconds (default 60) with a
# bash 3.2 watchdog: /usr/bin/timeout doesn't exist on older macOS. Only
# the builtin kill is used, never a kill/pkill looked up on PATH.
_swiftui_macro_probe() {
  local sdk="$1" limit="${AGENTBOARD_SDK_PROBE_TIMEOUT:-60}" dir pid wpid rc=0 reason
  # The watchdog counts whole seconds: anything but a positive integer
  # without leading zeros (bash reads 08 as octal) means the default.
  case "$limit" in ''|*[!0-9]*|0*) limit=60 ;; esac
  dir="$(mktemp -d "${TMPDIR:-/tmp}/agentboard-sdk-probe.XXXXXX" 2>/dev/null)" || dir=""
  if [[ -z "$dir" || ! -d "$dir" ]]; then
    echo "could not create a temporary directory"
    return 2
  fi
  # No line may start with "}": the tests extract this function with sed.
  cat > "$dir/probe.swift" <<'SWIFT'
import SwiftUI
struct AgentBoardSDKProbe: View {
    @State private var count = 0
    @AppStorage("k") private var flag = false
    var body: some View { Text("\(count) \(flag)") } }
SWIFT
  if [[ -n "$sdk" ]]; then
    SDKROOT="$sdk" xcrun swiftc -typecheck "$dir/probe.swift" >"$dir/out" 2>&1 </dev/null &
  else
    env -u SDKROOT xcrun swiftc -typecheck "$dir/probe.swift" >"$dir/out" 2>&1 </dev/null &
  fi
  pid=$!
  # The watchdog ticks once a second and leaves as soon as the probe is
  # gone, so a watchdog killed below orphans at most one `sleep 1`. It
  # writes the timeout marker before killing the probe, so the probe's
  # exit can't be seen before the marker exists. Its output must not reach
  # the caller: a sleep holding a $(...) pipe open would stall the caller.
  ( i=0; while [[ "$i" -lt "$limit" ]]; do sleep 1; kill -0 "$pid" 2>/dev/null || exit 0; i=$((i + 1)); done; : > "$dir/timedout"; kill "$pid" 2>/dev/null ) >/dev/null 2>&1 &
  wpid=$!
  wait "$pid" 2>/dev/null || rc=$?
  kill "$wpid" 2>/dev/null
  wait "$wpid" 2>/dev/null || true
  if [[ "$rc" -eq 0 ]]; then
    rc=0
  elif [[ -f "$dir/timedout" ]]; then
    echo "timed out after ${limit}s"
    rc=2
  elif grep -Eq "SwiftUIMacros|external macro implementation type" "$dir/out" 2>/dev/null; then
    rc=1
  else
    reason="$(grep -m1 "error:" "$dir/out" 2>/dev/null | sed -e 's/^[[:space:]]*//' | cut -c1-200)" || reason=""
    [[ -n "$reason" ]] || reason="swiftc exited with status $rc"
    printf '%s\n' "$reason"
    rc=2
  fi
  case "$dir" in
    */agentboard-sdk-probe.*) rm -rf -- "$dir" ;;
  esac
  return "$rc"
}

# Identical to swift-test.sh's pick_fallback_sdkroot. Duplicated rather
# than shared so this script keeps working standalone.
#
# Prints the SDK `swift` should build with (empty: the toolchain default,
# so the caller leaves SDKROOT alone) and at most one note/warning line on
# stderr. Decides only by probing what is installed, never by OS or Swift
# version: AGENTBOARD_SDKROOT=default skips the check, AGENTBOARD_SDKROOT=DIR
# forces an SDK, a preset SDKROOT is respected; otherwise, when the default
# SDK fails _swiftui_macro_probe, the newest older MacOSX*.sdk next to it
# that passes is chosen. Returns non-zero only for a bad AGENTBOARD_SDKROOT.
pick_fallback_sdkroot() {
  local choice="${AGENTBOARD_SDKROOT:-}" tab nl default_path default_real default_ver sdk_dir
  local rc reason cand real ver base maj min pat dmaj dmin dpat list="" seen path shown
  tab="$(printf '\t')"
  nl=$'\n'
  if [[ "$choice" == "default" ]]; then
    return 0
  fi
  if [[ -n "$choice" ]]; then
    if [[ -d "$choice" ]]; then
      (cd -P -- "$choice" && pwd -P) || return 1
      return 0
    fi
    echo "error: AGENTBOARD_SDKROOT=$choice is not an SDK directory" >&2
    return 1
  fi
  if [[ -n "${SDKROOT:-}" ]]; then
    return 0
  fi
  default_path="$(xcrun --sdk macosx --show-sdk-path 2>/dev/null)" || default_path=""
  if [[ -z "$default_path" || ! -d "$default_path" ]]; then
    return 0
  fi
  rc=0
  reason="$(_swiftui_macro_probe "")" || rc=$?
  if [[ "$rc" -eq 0 ]]; then
    return 0
  fi
  if [[ "$rc" -ne 1 ]]; then
    echo "note: could not check the macOS SDK for SwiftUI macro support ($reason); using the default" >&2
    return 0
  fi
  default_real="$(cd -P -- "$default_path" && pwd -P)" || default_real="$default_path"
  sdk_dir="$(cd -P -- "$(dirname "$default_path")" && pwd -P)" || sdk_dir="$(dirname "$default_path")"
  default_ver="$(xcrun --sdk "$default_path" --show-sdk-version 2>/dev/null)" || default_ver=""
  if [[ -z "$default_ver" ]]; then
    base="$(basename "$default_real")"
    default_ver="${base#MacOSX}"
    default_ver="${default_ver%.sdk}"
  fi
  IFS=. read -r dmaj dmin dpat _ <<EOF
$default_ver
EOF
  case "$dmaj" in ''|*[!0-9]*) dmaj="" ;; esac
  case "$dmin" in ''|*[!0-9]*) dmin=0 ;; esac
  case "$dpat" in ''|*[!0-9]*) dpat=0 ;; esac
  seen="$nl"
  if [[ -n "$dmaj" ]]; then
    for cand in "$sdk_dir"/MacOSX*.sdk; do
      [[ -d "$cand" ]] || continue
      real="$(cd -P -- "$cand" && pwd -P)" || continue
      [[ "$real" != "$default_real" ]] || continue
      case "$seen" in *"$nl$real$nl"*) continue ;; esac
      seen="$seen$real$nl"
      ver="$(xcrun --sdk "$real" --show-sdk-version 2>/dev/null)" || ver=""
      if [[ -z "$ver" ]]; then
        base="$(basename "$real")"
        ver="${base#MacOSX}"
        ver="${ver%.sdk}"
      fi
      maj="" min="" pat=""
      IFS=. read -r maj min pat _ <<EOF
$ver
EOF
      case "$maj" in ''|*[!0-9]*) continue ;; esac
      case "$min" in ''|*[!0-9]*) min=0 ;; esac
      case "$pat" in ''|*[!0-9]*) pat=0 ;; esac
      if (( maj < dmaj || (maj == dmaj && (min < dmin || (min == dmin && pat < dpat))) )); then
        list="$list$maj$tab$min$tab$pat$tab$real$nl"
      fi
    done
  fi
  if [[ -n "$list" ]]; then
    while IFS="$tab" read -r maj min pat path; do
      [[ -n "$path" ]] || continue
      rc=0
      _swiftui_macro_probe "$path" >/dev/null </dev/null || rc=$?
      if [[ "$rc" -eq 0 ]]; then
        shown="$maj.$min"
        [[ "$pat" == "0" ]] || shown="$shown.$pat"
        echo "note: the default macOS SDK ($default_ver) can't expand SwiftUI macros with this toolchain; building with $(basename "$path") ($shown). Set AGENTBOARD_SDKROOT=default to skip this check." >&2
        printf '%s\n' "$path"
        return 0
      fi
    done < <(printf '%s' "$list" | sort -t "$tab" -k1,1nr -k2,2nr -k3,3nr)
  fi
  echo "warning: the default macOS SDK ($default_ver) can't expand SwiftUI macros and no older SDK in $sdk_dir can either; the build will likely fail (install Xcode, or set AGENTBOARD_SDKROOT to a working SDK)" >&2
  return 0
}

if ! command -v swift >/dev/null 2>&1; then
  echo "error: swift not found. Install Xcode or the Swift toolchain (e.g. Xcode Command Line Tools) and try again." >&2
  exit 1
fi

mkdir -p "$BUILD_DIR"
: > "$BUILD_LOG"

# A step line: on the terminal only with --verbose, always in the log.
say() {
  if [[ "$VERBOSE" -eq 1 ]]; then
    printf '%s\n' "$*" | tee -a "$BUILD_LOG"
  else
    printf '%s\n' "$*" >>"$BUILD_LOG"
  fi
}

# Runs one step with its output in the log (on the terminal with
# --verbose). A quiet failure prints the log's tail and its path.
run_step() {
  if [[ "$VERBOSE" -eq 1 ]]; then
    "$@"
    return
  fi
  if ! "$@" >>"$BUILD_LOG" 2>&1; then
    echo "error: $1 failed:" >&2
    tail -n 20 "$BUILD_LOG" >&2
    echo "full log: $BUILD_LOG" >&2
    exit 1
  fi
}

# Pick the SDK before anything else is printed, so the note (if any) comes
# first. A no-op on a toolchain whose default SDK builds SwiftUI macros.
SDK_CHOICE="$(pick_fallback_sdkroot)" || exit 1
if [[ -n "$SDK_CHOICE" ]]; then
  export SDKROOT="$SDK_CHOICE"
  say "==> using SDKROOT=$SDK_CHOICE"
fi

# The bundled CLI's interpreter: accepted from install.sh (which already
# validated it) via AGENTBOARD_PYTHON3, so both the installed core/ copy
# and this bundled one are pinned to the exact same binary. Resolved the
# same way install.sh does when run standalone (e.g. `build-app.sh
# --no-install` directly, without going through install.sh first).
# Identical to install.sh's find_python3 -- see its comment. Duplicated
# rather than shared so this script keeps working standalone.
find_python3() {
  local candidate resolved
  if [[ -n "${AGENTBOARD_PYTHON3:-}" ]]; then
    if "$AGENTBOARD_PYTHON3" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
      "$AGENTBOARD_PYTHON3" -c 'import sys; print(sys.executable)'
    fi
    return 0
  fi
  for candidate in python3 python3.14 python3.13 python3.12 python3.11; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null || continue
    resolved="$("$candidate" -c 'import sys; print(sys.executable)')"
    if [[ -n "$resolved" ]]; then
      printf '%s\n' "$resolved"
      return 0
    fi
  done
  return 0
}

PYTHON3_BIN="$(find_python3)"
if [[ -z "$PYTHON3_BIN" ]]; then
  echo "error: no python3 >= 3.11 found (tried AGENTBOARD_PYTHON3, python3, python3.11-3.14 on PATH)" >&2
  exit 1
fi
BAKED_AGENTBOARD_HOME="${AGENTBOARD_BAKED_HOME:-}"

# Same shebang/AGENTBOARD_HOME pinning as install.sh's install_core_copy --
# see its comment for why. Duplicated rather than shared so this script
# keeps working standalone.
pin_core_bin() {
  local file="$1" interpreter="$2" baked_home="$3"
  "$interpreter" - "$file" "$interpreter" "$baked_home" <<'PYEOF'
import sys

file_path, interpreter_path, baked_home = sys.argv[1], sys.argv[2], sys.argv[3]
with open(file_path, "r", encoding="utf-8") as fh:
    lines = fh.readlines()
lines[0] = f"#!{interpreter_path}\n"
if baked_home:
    for i, line in enumerate(lines):
        if line.startswith("_BAKED_AGENTBOARD_HOME = "):
            lines[i] = f"_BAKED_AGENTBOARD_HOME = {baked_home!r}\n"
            break
with open(file_path, "w", encoding="utf-8") as fh:
    fh.writelines(lines)
PYEOF
}

say "==> swift build -c release"
BUILD_RC=0
if [[ "$VERBOSE" -eq 1 ]]; then
  (cd "$APP_DIR" && swift build -c release) 2>&1 | tee -a "$BUILD_LOG" || BUILD_RC=$?
else
  (cd "$APP_DIR" && swift build -c release) >>"$BUILD_LOG" 2>&1 || BUILD_RC=$?
fi
if [[ "$BUILD_RC" -ne 0 ]]; then
  if [[ "$VERBOSE" -ne 1 ]]; then
    # The compiler colors its diagnostics even into a file: strip the
    # escapes, keep the error lines (not the caret-diagram copies), dedupe.
    ESC="$(printf '\033')"
    EXCERPT="$(sed "s/${ESC}\[[0-9;]*m//g" "$BUILD_LOG" | grep "error:" | grep -v '^[[:space:]]*|' \
      | cut -c1-300 | awk '!seen[$0]++' | head -n 20)" || EXCERPT=""
    if [[ -z "$EXCERPT" ]]; then
      EXCERPT="$(tail -n 20 "$BUILD_LOG")"
    fi
    printf '%s\n' "$EXCERPT" >&2
  fi
  echo "full log: $BUILD_LOG" >&2
  exit "$BUILD_RC"
fi

BIN_PATH="$APP_DIR/.build/release/AgentBoard"
if [[ ! -x "$BIN_PATH" ]]; then
  echo "error: built executable not found at $BIN_PATH" >&2
  exit 1
fi

say "==> assembling $APP_BUNDLE"
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources/Fixtures"

run_step cp "$BIN_PATH" "$APP_BUNDLE/Contents/MacOS/AgentBoard"
run_step cp "$APP_DIR/Fixtures/snapshot.json" "$APP_BUNDLE/Contents/Resources/Fixtures/snapshot.json"

if [[ -d "$CORE_DIR/bin" && -d "$CORE_DIR/agentboard" ]]; then
  say "==> bundling core/ CLI into Resources/core"
  mkdir -p "$APP_BUNDLE/Contents/Resources/core"
  run_step cp -R "$CORE_DIR/bin" "$APP_BUNDLE/Contents/Resources/core/bin"
  run_step cp -R "$CORE_DIR/agentboard" "$APP_BUNDLE/Contents/Resources/core/agentboard"
  run_step find "$APP_BUNDLE/Contents/Resources/core" -name "__pycache__" -type d -exec rm -rf {} +
  chmod +x "$APP_BUNDLE/Contents/Resources/core/bin/agentboard"
  run_step pin_core_bin "$APP_BUNDLE/Contents/Resources/core/bin/agentboard" "$PYTHON3_BIN" "$BAKED_AGENTBOARD_HOME"
else
  echo "warning: $CORE_DIR/bin or $CORE_DIR/agentboard not found; shipping without a bundled CLI (app will fall back to \$PATH or the agentboardPath setting)" >&2
fi

cat > "$APP_BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>
    <string>AgentBoard</string>
    <key>CFBundleDisplayName</key>
    <string>AgentBoard</string>
    <key>CFBundleIdentifier</key>
    <string>$BUNDLE_ID</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleExecutable</key>
    <string>AgentBoard</string>
    <key>LSMinimumSystemVersion</key>
    <string>14.0</string>
    <key>LSUIElement</key>
    <true/>
    <key>NSSupportsAutomaticTermination</key>
    <false/>
    <key>NSSupportsSuddenTermination</key>
    <false/>
    <key>NSAppleEventsUsageDescription</key>
    <string>AgentBoard's CLI uses AppleScript (osascript) to focus the iTerm2 or Terminal tab running a selected Claude Code or Codex session.</string>
    <key>NSHumanReadableCopyright</key>
    <string>Local-only, not distributed.</string>
</dict>
</plist>
PLIST

say "==> ad-hoc codesign"
run_step codesign --force --deep --sign - "$APP_BUNDLE"

if [[ "$NO_INSTALL" -eq 1 ]]; then
  say "==> built $APP_BUNDLE (--no-install, skipped install)"
  if [[ "$VERBOSE" -ne 1 ]]; then
    echo "built ${APP_BUNDLE#"$ROOT_DIR"/}"
  fi
  exit 0
fi

TARGET_BUNDLE="$INSTALL_DIR/AgentBoard.app"

# Same bundle-ID check and not-a-real-directory refusal install.sh and
# uninstall.sh apply before replacing an existing bundle -- never
# overwrite (or delete) a same-named bundle that isn't actually ours, or
# anything at that path that isn't a plain directory.
APP_REFUSAL="$(_app_bundle_refusal_reason "$TARGET_BUNDLE")"
if [[ -n "$APP_REFUSAL" ]]; then
  echo "error: $APP_REFUSAL" >&2
  exit 1
fi

say "==> installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
rm -rf "$TARGET_BUNDLE"
run_step cp -R "$APP_BUNDLE" "$TARGET_BUNDLE"
say "==> installed $TARGET_BUNDLE"
if [[ "$VERBOSE" -ne 1 ]]; then
  echo "built and installed $TARGET_BUNDLE"
fi

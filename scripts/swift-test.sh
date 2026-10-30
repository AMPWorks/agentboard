#!/usr/bin/env bash
# Runs the app's Swift Testing suite (`swift test` in app/) with whatever
# flags this toolchain needs, quietly: one summary line on success; on
# failure the failing lines plus the path to the full log.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="$ROOT_DIR/app"
LOG_DIR="$APP_DIR/build"
TEST_LOG="$LOG_DIR/swift-test.log"

usage() {
  cat <<USAGE
Usage: $0 [--verbose] [-- <extra swift test args>]

Runs \`swift test\` in app/. Adds -Xswiftc -F/-Xlinker -rpath for the
Command Line Tools' Testing.framework and -plugin-path for their
TestingMacros plugin, each only when this toolchain has them where plain
\`swift test\` doesn't look, and builds against an older installed macOS SDK
when the default one can't expand SwiftUI macros with this toolchain.

  -v, --verbose   stream the full output (also: AGENTBOARD_VERBOSE=1)
  -h, --help      show this help
  -- ARGS         passed to swift test as-is (e.g. -- --filter HelpTests)

A run that executes no Swift Testing test fails, whether Testing.framework
wasn't found or a --filter/--skip matched nothing. Only -- --list-tests
(or -l) skips that check; its output is printed as is.

Output is always written to $TEST_LOG.

Env vars:
  AGENTBOARD_SDKROOT    default|<sdk dir>: skip the SwiftUI-macro SDK check, or force an SDK
  AGENTBOARD_VERBOSE    1: stream the full output
USAGE
}

VERBOSE=0
case "${AGENTBOARD_VERBOSE:-}" in
  ''|0) ;;
  *) VERBOSE=1 ;;
esac
extra=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    -v|--verbose)
      VERBOSE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      extra=("$@")
      break
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage >&2
      exit 1
      ;;
  esac
done

# Identical to build-app.sh's _swiftui_macro_probe -- see its comment.
# Duplicated rather than shared so this script keeps working standalone.
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

# Identical to build-app.sh's pick_fallback_sdkroot -- see its comment.
# Duplicated rather than shared so this script keeps working standalone.
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

# Prints the extra `swift test` arguments this toolchain needs, one per
# line, deciding only by what exists on disk:
# - the Command Line Tools keep Testing.framework in their own
#   Library/Developer/Frameworks, which SwiftPM leaves off the search paths
#   (plain `swift test` then builds, runs zero tests and exits 0). With
#   Xcode selected that directory doesn't exist and nothing is added.
# - some toolchains keep the TestingMacros plugin in host/plugins/testing/
#   rather than host/plugins/, where the compiler looks by default. The
#   path is derived from the compiler's own location, so it is right for
#   the Command Line Tools, Xcode or a swift.org toolchain alike.
swift_test_flags() {
  local dev fw swiftc plugins
  dev="$(xcode-select -p 2>/dev/null)" || dev=""
  if [[ -n "$dev" && -d "$dev/Library/Developer/Frameworks/Testing.framework" ]]; then
    fw="$dev/Library/Developer/Frameworks"
    printf '%s\n' -Xswiftc -F -Xswiftc "$fw" -Xlinker -rpath -Xlinker "$fw"
  fi
  swiftc="$(xcrun --find swiftc 2>/dev/null)" || swiftc=""
  if [[ -n "$swiftc" ]]; then
    plugins="$(cd -P -- "$(dirname "$swiftc")/../lib/swift/host/plugins" 2>/dev/null && pwd -P)" || plugins=""
    if [[ -n "$plugins" && -f "$plugins/testing/libTestingMacros.dylib" && ! -f "$plugins/libTestingMacros.dylib" ]]; then
      printf '%s\n' -Xswiftc -plugin-path -Xswiftc "$plugins/testing"
    fi
  fi
  return 0
}

if ! command -v swift >/dev/null 2>&1; then
  echo "error: swift not found. Install Xcode or the Swift toolchain (e.g. Xcode Command Line Tools) and try again." >&2
  exit 1
fi

SDK_CHOICE="$(pick_fallback_sdkroot)" || exit 1
if [[ -n "$SDK_CHOICE" ]]; then
  export SDKROOT="$SDK_CHOICE"
fi

flags=()
while IFS= read -r line; do
  flags+=("$line")
done < <(swift_test_flags)

# --list-tests (-l) prints the list as its result, so it is never
# summarized or held to the zero-tests guard.
LISTING=0
for arg in ${extra[@]+"${extra[@]}"}; do
  if [[ "$arg" == "--list-tests" || "$arg" == "-l" ]]; then
    LISTING=1
  fi
done

mkdir -p "$LOG_DIR"
RC=0
if [[ "$VERBOSE" -eq 1 || "$LISTING" -eq 1 ]]; then
  (cd "$APP_DIR" && swift test ${flags[@]+"${flags[@]}"} ${extra[@]+"${extra[@]}"}) 2>&1 | tee "$TEST_LOG" || RC=$?
else
  (cd "$APP_DIR" && swift test ${flags[@]+"${flags[@]}"} ${extra[@]+"${extra[@]}"}) >"$TEST_LOG" 2>&1 || RC=$?
fi

# Swift Testing may print ✔/✘ or private-use symbol glyphs and colors its
# output: strip the escapes and never match on the leading glyph.
ESC="$(printf '\033')"
plain_log() {
  sed "s/${ESC}\[[0-9;]*m//g" "$TEST_LOG"
}

if [[ "$RC" -ne 0 ]]; then
  if [[ "$VERBOSE" -ne 1 && "$LISTING" -ne 1 ]]; then
    EXCERPT="$(plain_log | grep -E "error:|recorded an issue|failed after|Test .* failed" \
      | grep -v '^[[:space:]]*|' | awk '!seen[$0]++' | head -n 40)" || EXCERPT=""
    if [[ -z "$EXCERPT" ]]; then
      EXCERPT="$(plain_log | tail -n 40)"
    fi
    printf '%s\n' "$EXCERPT" >&2
  fi
  echo "full log: $TEST_LOG" >&2
  exit "$RC"
fi

if [[ "$LISTING" -eq 1 ]]; then
  exit 0
fi

if ! plain_log | grep -Eq "Test run with [1-9][0-9]* tests?"; then
  echo "error: swift test ran no Swift Testing tests (Testing.framework not found, or a --filter/--skip matched nothing?); full log: $TEST_LOG" >&2
  exit 1
fi

if [[ "$VERBOSE" -ne 1 ]]; then
  plain_log | grep -E "Test run with [0-9]+ tests? .*passed" | tail -n 1 || true
fi

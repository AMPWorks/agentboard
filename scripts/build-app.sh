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
while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-install)
      NO_INSTALL=1
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
      echo "Usage: $0 [--no-install] [--prefix DIR]" >&2
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

if ! command -v swift >/dev/null 2>&1; then
  echo "error: swift not found. Install Xcode or the Swift toolchain (e.g. Xcode Command Line Tools) and try again." >&2
  exit 1
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

echo "==> swift build -c release"
(cd "$APP_DIR" && swift build -c release)

BIN_PATH="$APP_DIR/.build/release/AgentBoard"
if [[ ! -x "$BIN_PATH" ]]; then
  echo "error: built executable not found at $BIN_PATH" >&2
  exit 1
fi

echo "==> assembling $APP_BUNDLE"
rm -rf "$APP_BUNDLE"
mkdir -p "$APP_BUNDLE/Contents/MacOS"
mkdir -p "$APP_BUNDLE/Contents/Resources/Fixtures"

cp "$BIN_PATH" "$APP_BUNDLE/Contents/MacOS/AgentBoard"
cp "$APP_DIR/Fixtures/snapshot.json" "$APP_BUNDLE/Contents/Resources/Fixtures/snapshot.json"

if [[ -d "$CORE_DIR/bin" && -d "$CORE_DIR/agentboard" ]]; then
  echo "==> bundling core/ CLI into Resources/core"
  mkdir -p "$APP_BUNDLE/Contents/Resources/core"
  cp -R "$CORE_DIR/bin" "$APP_BUNDLE/Contents/Resources/core/bin"
  cp -R "$CORE_DIR/agentboard" "$APP_BUNDLE/Contents/Resources/core/agentboard"
  find "$APP_BUNDLE/Contents/Resources/core" -name "__pycache__" -type d -exec rm -rf {} +
  chmod +x "$APP_BUNDLE/Contents/Resources/core/bin/agentboard"
  pin_core_bin "$APP_BUNDLE/Contents/Resources/core/bin/agentboard" "$PYTHON3_BIN" "$BAKED_AGENTBOARD_HOME"
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

echo "==> ad-hoc codesign"
codesign --force --deep --sign - "$APP_BUNDLE"

if [[ "$NO_INSTALL" -eq 1 ]]; then
  echo "==> built $APP_BUNDLE (--no-install, skipped install)"
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

echo "==> installing to $INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
rm -rf "$TARGET_BUNDLE"
cp -R "$APP_BUNDLE" "$TARGET_BUNDLE"
echo "==> installed $TARGET_BUNDLE"

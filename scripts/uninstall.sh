#!/usr/bin/env bash
# Removes everything install.sh added: hooks, the app, and (opt-in) the data dir.
# Restores nothing else (any pre-existing hooks in settings.json/hooks.json are left alone).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REPO_BIN="$ROOT_DIR/core/bin/agentboard"

# See install.sh's identical export for why: no CLI call here, including a
# --dry-run uninstall-hooks preview, should ever leave __pycache__ behind.
export PYTHONDONTWRITEBYTECODE=1

AGENTBOARD_DATA_HOME="${AGENTBOARD_HOME:-$HOME/Library/Application Support/AgentBoard}"

# Normalized (absolute, no trailing slash, .. collapsed) further down,
# right after argument parsing, the same way --prefix is and for the same
# reason -- see install.sh's identical comment. INSTALLED_BIN /
# CORE_STORE_DIR are derived from it there too, after normalizing.
INSTALLED_CORE_DIR="${AGENTBOARD_CORE_DIR:-$HOME/.local/share/agentboard/core}"

# Identical literals to install.sh's -- see its comment. Duplicated rather
# than shared so this script keeps working standalone.
CORE_STORE_MARKER=".agentboard-core-store"
CORE_VERSION_MARKER=".agentboard-core-version"

# Identical to install.sh's AGENTBOARD_BUNDLE_ID -- see its comment.
AGENTBOARD_BUNDLE_ID="io.github.agentboard"

# Every top-level file/dir AgentBoard itself ever creates directly inside
# $AGENTBOARD_HOME (core/agentboard/db.py, hook.py, links.py, cli.py,
# diag.py's agentboard.log and its single rotation agentboard.log.1) --
# see safe_purge_data_home()'s comment for why this list exists.
AGENTBOARD_DATA_FILES=(agentboard.db agentboard.db-wal agentboard.db-shm
  hook-errors.log install-manifest.json config.json
  agentboard.log agentboard.log.1)
AGENTBOARD_DATA_DIRS=(inbox reparse-locks)

NO_HOOKS=0
NO_APP=0
PURGE_DATA=0
DRY_RUN=0
PREFIX=""

usage() {
  cat <<EOF
Usage: $0 [--no-hooks] [--no-app] [--purge-data] [--dry-run] [--prefix DIR]

  --no-hooks     leave Claude Code / Codex hooks in place
  --no-app       leave the installed app in place
  --purge-data   also delete the AgentBoard data dir (agentboard.db, config.json, inbox/)
  --dry-run      show what would change; make no changes
  --prefix DIR   look for the .app bundle under DIR (default: ~/Applications)

Env vars:
  AGENTBOARD_HOME       data dir (default: ~/Library/Application Support/AgentBoard)
  AGENTBOARD_CORE_DIR   installed core/ copy (default: ~/.local/share/agentboard/core)
EOF
}

QUIT_WAIT_SECS=5

# Quit every running AgentBoard pid that is actually running from the exact
# bundle path we're about to remove -- never a same-named app installed
# elsewhere, and never via `osascript ... quit app "AgentBoard"` (addresses
# by name, so it would just as happily quit a copy running from somewhere
# else). Each matching pid gets a direct SIGTERM and up to $QUIT_WAIT_SECS
# to exit before being force-killed.
quit_running_app_if_same_prefix() {
  local target_bundle="$1"
  local target_exe="$target_bundle/Contents/MacOS/AgentBoard"
  local pid comm waited

  for pid in $(pgrep -x AgentBoard 2>/dev/null); do
    comm="$(ps -o comm= -p "$pid" 2>/dev/null)"
    if [[ -z "$comm" || "$comm" != "$target_exe" ]]; then
      continue
    fi

    echo "    AgentBoard (pid $pid) is running from $target_bundle; quitting it before removing the bundle"
    kill -TERM "$pid" 2>/dev/null || true
    waited=0
    while [[ "$waited" -lt "$QUIT_WAIT_SECS" ]] && kill -0 "$pid" 2>/dev/null; do
      sleep 1
      waited=$((waited + 1))
    done
    if kill -0 "$pid" 2>/dev/null; then
      echo "    pid $pid still running after ${QUIT_WAIT_SECS}s; forcing quit"
      kill -KILL "$pid" 2>/dev/null || true
    fi
  done
  return 0
}

# Identical to install.sh's _collapse_dots -- see its comment. Duplicated
# rather than shared so this script keeps working standalone.
_collapse_dots() {
  local p="$1" rest part out=""
  rest="${p#/}"
  while [[ -n "$rest" ]]; do
    part="${rest%%/*}"
    if [[ "$rest" == */* ]]; then
      rest="${rest#*/}"
    else
      rest=""
    fi
    case "$part" in
      ""|.) : ;;
      ..) out="${out%/*}" ;;
      *) out="$out/$part" ;;
    esac
  done
  printf '%s' "${out:-/}"
}

# Identical to install.sh's normalize_path -- see its comment. Duplicated
# rather than shared so this script keeps working standalone.
normalize_path() {
  local p="$1" existing suffix segment
  case "$p" in
    /*) : ;;
    *) p="$PWD/$p" ;;
  esac
  p="$(_collapse_dots "$p")"
  existing="$p"
  suffix=""
  while [[ ! -d "$existing" && "$existing" != "/" ]]; do
    segment="$(basename "$existing")"
    suffix="/$segment$suffix"
    existing="$(dirname "$existing")"
  done
  printf '%s' "$(cd "$existing" && pwd -P)$suffix"
}

# Identical to install.sh's normalize_leaf_path -- see its comment.
# Duplicated rather than shared so this script keeps working standalone.
normalize_leaf_path() {
  local p="$1" dir base
  case "$p" in
    /*) : ;;
    *) p="$PWD/$p" ;;
  esac
  p="$(_collapse_dots "$p")"
  dir="$(dirname "$p")"
  base="$(basename "$p")"
  printf '%s/%s' "$(normalize_path "$dir")" "$base"
}

# Absolute-ize a symlink's raw readlink() result -- identical to
# install.sh's _resolve_link_target(), see its comment.
_resolve_link_target() {
  local link="$1" raw
  raw="$(readlink "$link" 2>/dev/null)" || return 1
  case "$raw" in
    /*) printf '%s' "$raw" ;;
    *) printf '%s' "$(dirname "$link")/$raw" ;;
  esac
}

# Identical to install.sh's _validate_core_store -- see its comment.
_validate_core_store() {
  [[ -d "$CORE_STORE_DIR" && ! -L "$CORE_STORE_DIR" ]] || return 1
  [[ -f "$CORE_STORE_DIR/$CORE_STORE_MARKER" ]]
}

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

# Identical to install.sh's safe_remove_in_store -- see its comment. The
# ONE place this script ever rm -rf's a version dir or the whole store.
safe_remove_in_store() {
  local target="$1" resolved_store resolved_target live_target

  if ! _validate_core_store; then
    echo "warning: $CORE_STORE_DIR isn't a plain, marked AgentBoard version store; refusing to remove $target" >&2
    return 1
  fi
  resolved_store="$(cd "$CORE_STORE_DIR" && pwd -P)"

  if [[ "$target" == "$CORE_STORE_DIR" ]]; then
    rm -rf "$resolved_store"
    return 0
  fi

  if [[ ! -d "$target" ]]; then
    echo "warning: $target does not exist; skipping" >&2
    return 1
  fi
  resolved_target="$(cd "$target" && pwd -P)"
  case "$resolved_target" in
    "$resolved_store"/*) : ;;
    *)
      echo "warning: $target is not inside $CORE_STORE_DIR; refusing to remove it" >&2
      return 1
      ;;
  esac
  if [[ ! -f "$resolved_target/$CORE_VERSION_MARKER" ]]; then
    echo "warning: $target has no version marker; refusing to remove it" >&2
    return 1
  fi
  live_target=""
  if [[ -L "$INSTALLED_CORE_DIR" ]]; then
    live_target="$(_resolve_link_target "$INSTALLED_CORE_DIR")" || live_target=""
  fi
  if [[ "$resolved_target" == "$live_target" ]]; then
    echo "warning: $target is the version core currently points at; refusing to remove it" >&2
    return 1
  fi

  rm -rf "$resolved_target"
}

# Identical to install.sh's _bundle_identifier_matches -- see its
# comment. Duplicated rather than shared so this script keeps working
# standalone.
_bundle_identifier_matches() {
  local bundle="$1" id
  id="$(defaults read "$bundle/Contents/Info" CFBundleIdentifier 2>/dev/null)" || return 1
  [[ "$id" == "$AGENTBOARD_BUNDLE_ID" ]]
}

# True only if $1 is safe for --purge-data to touch at all -- shared by
# the --dry-run preview and the real purge below so they never disagree:
#   - $1 is a real directory, not a symlink;
#   - it resolves to neither "/" nor $HOME itself;
#   - it contains agentboard.db, or every entry in it is one of the
#     known AgentBoard files/dirs above (so a fresh install with no db
#     yet, but e.g. just a config.json, still qualifies).
# Otherwise: warns and returns 1.
_purge_target_is_safe() {
  local dir="$1" resolved home_resolved entry name

  if [[ ! -d "$dir" || -L "$dir" ]]; then
    echo "warning: $dir is not a real directory; refusing to purge it" >&2
    return 1
  fi
  resolved="$(cd "$dir" && pwd -P)"
  home_resolved="$(cd "$HOME" && pwd -P)"
  if [[ "$resolved" == "/" || "$resolved" == "$home_resolved" ]]; then
    echo "warning: $dir resolves to $resolved; refusing to purge it" >&2
    return 1
  fi

  [[ -f "$dir/agentboard.db" ]] && return 0

  shopt -s nullglob dotglob
  for entry in "$dir"/*; do
    name="$(basename "$entry")"
    case " ${AGENTBOARD_DATA_FILES[*]} ${AGENTBOARD_DATA_DIRS[*]} " in
      *" $name "*) : ;;
      *)
        shopt -u nullglob dotglob
        echo "warning: $dir contains files AgentBoard didn't create; refusing to purge it" >&2
        return 1
        ;;
    esac
  done
  shopt -u nullglob dotglob
  return 0
}

# Deletes only entries matching glob $2 directly inside directory $1 (one
# of AGENTBOARD_DATA_DIRS), then rmdir's $1 if that leaves it empty --
# never rm -rf, since AgentBoard isn't the only thing that may have put
# files there: links.py's ingest_inbox only ever writes/reads inbox/*.json
# drop files, and hook.py's _reparse_marker_path only ever writes
# reparse-locks/<tool>-<session_id>.marker. Anything else in $1 (a user's
# own file, say) is left alone, with a warning, and so is $1 itself.
_purge_known_glob() {
  local dir="$1" pattern="$2" entry

  [[ -d "$dir" && ! -L "$dir" ]] || return 0

  shopt -s nullglob
  for entry in "$dir"/$pattern; do
    rm -f "$entry"
  done
  shopt -u nullglob

  if rmdir "$dir" 2>/dev/null; then
    return 0
  fi
  if [[ -d "$dir" ]]; then
    echo "warning: $dir is not empty after removing AgentBoard's own files; left in place" >&2
  fi
  return 1
}

# --purge-data must never rm -rf a whole directory the user pointed
# AGENTBOARD_HOME at -- it could be their home directory, a project, or
# anything else. Removes only the specific files AgentBoard itself creates
# directly inside it (AGENTBOARD_DATA_FILES above) individually, and only
# the specific file shapes it creates inside inbox/ and reparse-locks/
# (_purge_known_glob above) -- never rm -rf either of those two dirs
# wholesale, since a user could have dropped an unrelated file into one.
# Finally rmdir's the top-level directory -- never rm -rf -- and only if
# that leaves it empty.
safe_purge_data_home() {
  local dir="$1" f

  _purge_target_is_safe "$dir" || return 1

  for f in "${AGENTBOARD_DATA_FILES[@]}"; do
    rm -f "$dir/$f"
  done
  _purge_known_glob "$dir/inbox" "*.json"
  _purge_known_glob "$dir/reparse-locks" "*.marker"

  if rmdir "$dir" 2>/dev/null; then
    return 0
  fi
  echo "warning: $dir is not empty after removing known AgentBoard files; left in place" >&2
  return 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-hooks) NO_HOOKS=1; shift ;;
    --no-app) NO_APP=1; shift ;;
    --purge-data) PURGE_DATA=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --prefix)
      if [[ $# -lt 2 ]]; then
        echo "error: --prefix requires a directory argument" >&2
        exit 1
      fi
      PREFIX="$2"
      shift 2
      ;;
    -h|--help) usage; exit 0 ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

# Normalized once -- see install.sh's identical comment.
if [[ -n "$PREFIX" ]]; then
  PREFIX="$(normalize_path "$PREFIX")"
fi

# Same normalization for AGENTBOARD_CORE_DIR -- see install.sh's
# identical comment. INSTALLED_BIN / CORE_STORE_DIR are derived from it
# only now that it's normalized.
INSTALLED_CORE_DIR="$(normalize_leaf_path "$INSTALLED_CORE_DIR")"
INSTALLED_BIN="$INSTALLED_CORE_DIR/bin/agentboard"
CORE_STORE_DIR="${INSTALLED_CORE_DIR}.d"

# Prefer the installed copy (what hooks actually point at; its shebang is
# pinned to a validated interpreter); fall back to the repo checkout if
# it's somehow already gone -- run through find_python3's pick, since the
# checkout shim's own `env python3` shebang may resolve to an interpreter
# too old to import the package (see install.sh's identical reasoning).
AGENTBOARD_CMD=()
if [[ -x "$INSTALLED_BIN" ]]; then
  AGENTBOARD_BIN="$INSTALLED_BIN"
  AGENTBOARD_CMD=("$INSTALLED_BIN")
else
  AGENTBOARD_BIN="$REPO_BIN"
  PYTHON3_BIN="$(find_python3)"
  if [[ -n "$PYTHON3_BIN" && -f "$REPO_BIN" ]]; then
    AGENTBOARD_CMD=("$PYTHON3_BIN" "$REPO_BIN")
  fi
fi

echo "AgentBoard uninstall: $ROOT_DIR"
[[ "$DRY_RUN" -eq 1 ]] && echo "(dry run: no files will be changed)"

# -- 1. Remove hooks ----------------------------------------------------------
#
# A malformed settings.json/hooks.json makes uninstall-hooks print an error
# and exit non-zero for that file, but must never abort the rest of
# uninstall under `set -e` -- the app and the installed core/ copy below
# still have to be removed. The captured rc is only surfaced at the end.

UNINSTALL_HOOKS_RC=0
CORE_REMOVAL_RC=0
APP_REMOVAL_RC=0

if [[ "$NO_HOOKS" -eq 1 ]]; then
  echo "==> leaving hooks in place (--no-hooks)"
elif [[ "${#AGENTBOARD_CMD[@]}" -gt 0 ]]; then
  echo "==> removing hooks"
  HOOK_ARGS=(--claude --codex)
  [[ "$DRY_RUN" -eq 1 ]] && HOOK_ARGS+=(--dry-run)
  "${AGENTBOARD_CMD[@]}" uninstall-hooks "${HOOK_ARGS[@]+"${HOOK_ARGS[@]}"}" || UNINSTALL_HOOKS_RC=$?
else
  echo "==> skipping hook removal ($AGENTBOARD_BIN not found, or no python3 >= 3.11 to run it with)" >&2
fi

# -- 2. Remove the app ---------------------------------------------------------

APP_DIR="$(normalize_path "${PREFIX:-$HOME/Applications}")"
APP_BUNDLE="$APP_DIR/AgentBoard.app"

if [[ "$NO_APP" -eq 1 ]]; then
  echo "==> leaving the app in place (--no-app)"
elif [[ -d "$APP_BUNDLE" ]]; then
  if ! _bundle_identifier_matches "$APP_BUNDLE"; then
    echo "warning: $APP_BUNDLE doesn't look like AgentBoard (CFBundleIdentifier isn't $AGENTBOARD_BUNDLE_ID); leaving it in place" >&2
    APP_REMOVAL_RC=1
  elif [[ "$DRY_RUN" -eq 1 ]]; then
    echo "would remove: $APP_BUNDLE"
  else
    echo "==> removing $APP_BUNDLE"
    quit_running_app_if_same_prefix "$APP_BUNDLE"
    rm -rf "$APP_BUNDLE"
  fi
else
  echo "==> no app found at $APP_BUNDLE"
fi

# -- 3. Remove the installed core/ copy ----------------------------------------
#
# Always removed (not gated behind --purge-data): it's install.sh's own
# managed code copy, not user data -- the db/config/inbox below are.
#
# $INSTALLED_CORE_DIR is normally a symlink into its own dedicated,
# marked $CORE_STORE_DIR ("$INSTALLED_CORE_DIR.d") -- never a glob over
# $INSTALLED_CORE_DIR's parent, which a different install (a different
# AGENTBOARD_CORE_DIR, or a dev checkout) might also share: each store is
# scoped to exactly one link, so removing one install can never touch
# another's live version. Only remove the link if it points into its own
# marked store, then remove that whole store. uninstall.sh supports
# exactly the same two states install.sh does: missing, or our own
# symlink -- anything else (a real directory, including a source
# checkout's own `core/`, or a foreign symlink) is refused outright,
# never removed.

if [[ -L "$INSTALLED_CORE_DIR" ]]; then
  CORE_LINK_TARGET="$(_resolve_link_target "$INSTALLED_CORE_DIR")" || CORE_LINK_TARGET=""
  if [[ -n "$CORE_LINK_TARGET" ]] && _validate_core_store && [[ "$CORE_LINK_TARGET" == "$CORE_STORE_DIR"/* ]]; then
    if [[ "$DRY_RUN" -eq 1 ]]; then
      echo "would remove: $INSTALLED_CORE_DIR"
      echo "would remove: $CORE_STORE_DIR"
    else
      echo "==> removing $INSTALLED_CORE_DIR and $CORE_STORE_DIR"
      rm -f "$INSTALLED_CORE_DIR"
      safe_remove_in_store "$CORE_STORE_DIR"
    fi
  else
    echo "warning: $INSTALLED_CORE_DIR is a symlink to ${CORE_LINK_TARGET:-an unreadable target}, which install.sh did not create; leaving it in place (move it aside, or set AGENTBOARD_CORE_DIR elsewhere)" >&2
    CORE_REMOVAL_RC=1
  fi
elif [[ -e "$INSTALLED_CORE_DIR" ]]; then
  echo "warning: $INSTALLED_CORE_DIR exists and isn't an AgentBoard install install.sh created; leaving it in place (move it aside, or set AGENTBOARD_CORE_DIR elsewhere)" >&2
  CORE_REMOVAL_RC=1
else
  echo "==> no installed core copy found at $INSTALLED_CORE_DIR"
fi

# -- 4. Optionally purge the data dir ------------------------------------------

DATA_PURGE_RC=0

if [[ "$PURGE_DATA" -eq 1 ]]; then
  if [[ "$DRY_RUN" -eq 1 ]]; then
    if _purge_target_is_safe "$AGENTBOARD_DATA_HOME"; then
      echo "would remove: $AGENTBOARD_DATA_HOME"
    else
      DATA_PURGE_RC=1
    fi
  else
    echo "==> removing $AGENTBOARD_DATA_HOME"
    safe_purge_data_home "$AGENTBOARD_DATA_HOME" || DATA_PURGE_RC=1
  fi
else
  echo "==> keeping AgentBoard data dir (pass --purge-data to remove it)"
fi

echo "==> done"
[[ "$DRY_RUN" -eq 1 ]] && echo "(dry run: no files were changed)"
FINAL_RC=0
if [[ "$UNINSTALL_HOOKS_RC" -ne 0 ]]; then
  echo "    note: hook removal reported errors above; fix the invalid file(s) and re-run" >&2
  if [[ "$FINAL_RC" -eq 0 ]]; then
    FINAL_RC="$UNINSTALL_HOOKS_RC"
  fi
fi
if [[ "$CORE_REMOVAL_RC" -ne 0 ]]; then
  echo "    note: core copy removal was refused above; see the warning for why" >&2
  if [[ "$FINAL_RC" -eq 0 ]]; then
    FINAL_RC="$CORE_REMOVAL_RC"
  fi
fi
if [[ "$APP_REMOVAL_RC" -ne 0 ]]; then
  echo "    note: app removal was refused above; see the warning for why" >&2
  if [[ "$FINAL_RC" -eq 0 ]]; then
    FINAL_RC="$APP_REMOVAL_RC"
  fi
fi
if [[ "$DATA_PURGE_RC" -ne 0 ]]; then
  echo "    note: data dir purge was refused above; see the warning for why" >&2
  if [[ "$FINAL_RC" -eq 0 ]]; then
    FINAL_RC="$DATA_PURGE_RC"
  fi
fi
exit "$FINAL_RC"

#!/usr/bin/env bash
# One-shot setup: build+install the app, install hooks, backfill history.
# Safe to re-run (idempotent) to pick up an upgrade.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
CORE_DIR="$ROOT_DIR/core"
APP_DIR="$ROOT_DIR/app"
AGENTBOARD_BIN="$CORE_DIR/bin/agentboard"

# Every CLI invocation below (prereq checks, install-hooks, backfill) must
# never leave __pycache__ behind -- most importantly in this repo checkout
# itself during --dry-run, which runs the CLI straight from $CORE_DIR and
# must make no changes at all.
export PYTHONDONTWRITEBYTECODE=1

# Hooks must never point at this repo checkout -- editing core/ while a
# hook is live must not be able to break it. install.sh copies core/
# into a stable location instead, and that's the path install-hooks
# registers and backfill/launch run from.
#
# The core copy defaults to a path with no spaces -- Claude/Codex both run
# hook commands through a shell, and "Library/Application Support" (the
# data dir's default) splits into multiple shell words if the hook command
# is ever written there unquoted. cli.py also shell-quotes the path it
# writes into hook commands, so a custom AGENTBOARD_CORE_DIR with spaces
# still works, but the default avoids needing that.
#
# Normalized (absolute, no trailing slash, .. collapsed) further down,
# right after argument parsing, the same way --prefix is -- an
# un-normalized relative or trailing-slash AGENTBOARD_CORE_DIR otherwise
# makes the symlink target and the store glob disagree, which looks like
# the live version is not the live version and gets pruned. INSTALLED_BIN
# / CORE_STORE_DIR are derived from it there too, after normalizing, not
# here.
INSTALLED_CORE_DIR="${AGENTBOARD_CORE_DIR:-$HOME/.local/share/agentboard/core}"

# Versions never live loose in $INSTALLED_CORE_DIR's parent (a shared
# directory other installs, or unrelated files, may also live in) --
# they live only in this dedicated per-install sibling, named after the
# link itself, so two installs sharing a parent (e.g. a custom
# AGENTBOARD_CORE_DIR next to the default one) can never collide or glob
# into each other. uninstall.sh duplicates these exact literals (see its
# matching comment).
CORE_STORE_MARKER=".agentboard-core-store"
CORE_VERSION_MARKER=".agentboard-core-version"

# build-app.sh's own Info.plist CFBundleIdentifier (see its BUNDLE_ID) --
# checked before this script ever touches anything at a bundle path, so
# a same-named "AgentBoard.app" that isn't actually ours (a different
# app, or one a user put there deliberately) is never overwritten or
# removed. uninstall.sh duplicates this exact literal.
AGENTBOARD_BUNDLE_ID="io.github.agentboard"

NO_HOOKS=0
NO_APP=0
DRY_RUN=0
PREFIX=""
ENABLE_CODEX_HOOKS=0

usage() {
  cat <<EOF
Usage: $0 [--no-hooks] [--no-app] [--dry-run] [--prefix DIR] [--enable-codex-hooks]

  --no-hooks            skip installing Claude Code / Codex hooks
  --no-app              skip building and installing the menu bar app
  --dry-run             show what would change; makes no changes at all
  --prefix DIR          install the .app bundle under DIR (default: ~/Applications)
  --enable-codex-hooks  opt in: set [features]\\nhooks = true in ~/.codex/config.toml,
                        needed for Codex older than 0.124, or when hooks = false is
                        set (backs up the file first; see docs/REFERENCE.md for what this does)

Env vars:
  AGENTBOARD_HOME       data dir (default: ~/Library/Application Support/AgentBoard)
  AGENTBOARD_CORE_DIR   installed core/ copy (default: ~/.local/share/agentboard/core)
  AGENTBOARD_SDKROOT    default|<sdk dir>: skip the SwiftUI-macro SDK check, or force an SDK for the app build
  AGENTBOARD_VERBOSE    1: show the full swift build output
EOF
}

QUIT_WAIT_SECS=5

# Quit every running AgentBoard pid that is actually running from the exact
# bundle path we're about to replace -- never a same-named app elsewhere,
# and never before the new build has actually succeeded
# (quit_running_app_if_same_prefix is only called after a successful `swift
# build`, further down). Each matching pid is sent SIGTERM directly (never
# `osascript ... quit app "AgentBoard"`, which addresses by name and would
# just as happily quit a copy running from somewhere else) and given up to
# $QUIT_WAIT_SECS to exit before being force-killed.
quit_running_app_if_same_prefix() {
  local target_bundle="$1"
  local target_exe="$target_bundle/Contents/MacOS/AgentBoard"
  local pid comm waited

  for pid in $(pgrep -x AgentBoard 2>/dev/null); do
    comm="$(ps -o comm= -p "$pid" 2>/dev/null)"
    if [[ -z "$comm" || "$comm" != "$target_exe" ]]; then
      # Running, but from a different bundle -- not ours to touch.
      continue
    fi

    echo "==> AgentBoard (pid $pid) is running from $target_bundle; quitting it before replacing the bundle"
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

# True only if $1 (a bundle path) has a Contents/Info.plist whose
# CFBundleIdentifier is exactly $AGENTBOARD_BUNDLE_ID. `defaults read`
# only ever reads the file -- never writes -- so this is safe to call on
# any directory shaped like an app bundle, including one that isn't ours
# at all. False (no error printed; callers decide how to react) if the
# bundle/plist/key doesn't exist or belongs to something else.
_bundle_identifier_matches() {
  local bundle="$1" id
  id="$(defaults read "$bundle/Contents/Info" CFBundleIdentifier 2>/dev/null)" || return 1
  [[ "$id" == "$AGENTBOARD_BUNDLE_ID" ]]
}

# Prints (non-empty) the reason $1 (a target bundle path) must be refused
# rather than replaced: anything there that isn't a real, non-symlink
# directory (a plain file, a symlink, or a dangling symlink), or a real
# directory whose CFBundleIdentifier isn't ours. Prints nothing when $1
# doesn't exist at all, or is already our own bundle -- both safe to
# replace. Checking `-L` before `-e`/`-d` matters: `-d` alone follows a
# symlink and would call a same-named symlink to our own bundle "a
# directory", skipping the refusal and letting a dangling or
# foreign-target symlink get `rm -rf`'d outright.
_app_bundle_refusal_reason() {
  local bundle="$1"
  if [[ -L "$bundle" ]]; then
    echo "$bundle exists and is a symlink, not a plain directory AgentBoard installed; refusing to touch it"
  elif [[ -e "$bundle" ]]; then
    if [[ -d "$bundle" ]]; then
      if ! _bundle_identifier_matches "$bundle"; then
        echo "$bundle exists and doesn't look like AgentBoard (CFBundleIdentifier isn't $AGENTBOARD_BUNDLE_ID); refusing to touch it"
      fi
    else
      echo "$bundle exists and is a file, not a directory AgentBoard installed; refusing to touch it"
    fi
  fi
}

# Lexically collapses "." and ".." components of an absolute path --
# string-only, no filesystem access, so the result is stable regardless of
# whether any component exists yet. Unlike `cd && pwd -P` (which can only
# resolve the longest *existing* prefix, leaving a ".." through a
# not-yet-created directory in the string verbatim), this always collapses
# every ".."/"." -- otherwise the same AGENTBOARD_CORE_DIR can normalize to
# two different strings across runs (once a directory the first run's own
# mkdir -p created starts existing), which disagrees with the symlink
# target/store path recorded on disk and makes a later run refuse it.
# Never resolves symlinks -- normalize_path()/normalize_leaf_path() do
# that separately, only for whatever prefix already exists. A ".." past
# the root collapses to root, matching kernel pathname resolution.
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

# Absolute-ize, lexically collapse "." / "..", strip a trailing slash, and
# resolve symlinks -- without requiring the path to exist (a --prefix for a
# first install usually doesn't yet) and without creating anything, so this
# is identical for --dry-run and a real run. Resolves the longest existing
# leading directory and re-appends whatever doesn't exist yet.
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

# Like normalize_path, but never resolves the final path component itself
# even if it already exists and is a symlink. $INSTALLED_CORE_DIR is
# usually our own symlink; normalize_path's "cd && pwd -P" would resolve
# it straight through to whatever it currently points at, which breaks
# every check below that compares it against the symlink path itself (and
# makes the derived $CORE_STORE_DIR nonsensical). Collapses "." / ".." over
# the whole path first (so the leaf itself can never be a dot component),
# then normalizes everything but the leaf via normalize_path, and
# re-appends the leaf unresolved.
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

# Rewrite the shebang of a staged/bundled bin/agentboard copy to the exact
# interpreter install.sh validated as >=3.11 -- never left as `env python3`,
# which re-resolves PATH at hook/launchd run time and can land on an older
# system python3 (see bin/agentboard's own version guard for when it still
# does, e.g. a manually-launched old copy). Also bakes in AGENTBOARD_HOME
# when a non-default one was used at install time, so the hooks/app and
# backfill agree on one database -- this never changes the registered hook
# command string itself (that would force Codex users to re-trust /hooks).
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

# Picks the interpreter the installed/bundled CLI gets pinned to. A bare
# `python3` first (what docs/REFERENCE.md documents), then versioned names, so a
# Mac whose `python3` on PATH is an older system/Homebrew 3.9 but which
# also has e.g. Homebrew's python3.12 installed just works instead of
# failing the prerequisite check. AGENTBOARD_PYTHON3 (an explicit path)
# wins outright when set. Prints the resolved absolute path, or nothing.
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

TMP_LINK_PATH=""

# Absolute-ize a symlink's raw readlink() result against the directory it
# lives in -- our own symlinks are always written absolute (see
# install_core_copy()), but a foreign one might not be.
_resolve_link_target() {
  local link="$1" raw
  raw="$(readlink "$link" 2>/dev/null)" || return 1
  case "$raw" in
    /*) printf '%s' "$raw" ;;
    *) printf '%s' "$(dirname "$link")/$raw" ;;
  esac
}

# True only when $CORE_STORE_DIR is a real directory (never a symlink --
# a symlinked "store" could point anywhere) that carries our own marker.
# The one place that decides whether a store is ours; everything else
# (safe_remove_in_store, install_core_copy's own refusal check) calls this
# instead of re-deriving the same two checks.
_validate_core_store() {
  [[ -d "$CORE_STORE_DIR" && ! -L "$CORE_STORE_DIR" ]] || return 1
  [[ -f "$CORE_STORE_DIR/$CORE_STORE_MARKER" ]]
}

# The ONE place either script ever rm -rf's a version dir or a whole
# store -- every such deletion in this file goes through it (see
# core/tests/test_install_script.py's audit test for the short,
# independently-reviewed list of unrelated temp-file rm's that don't:
# TMP_LINK_PATH and a staging dir's own __pycache__, neither of which is
# $CORE_STORE_DIR or anything inside it).
#
# Removes $1 only when ALL of:
#   (a) $CORE_STORE_DIR is a real directory, not a symlink;
#   (b) it carries $CORE_STORE_MARKER (both via _validate_core_store);
#   (c) $1 resolves to $CORE_STORE_DIR itself, or strictly inside it;
#   (d) if $1 isn't the store itself: it carries $CORE_VERSION_MARKER,
#       and it is not the version `core` currently points at -- re-read
#       fresh via readlink right here, never from a variable (a signal
#       landing between the swap and a variable being updated was
#       exactly the round-4 bug).
# Otherwise: prints a warning and returns 1, deleting nothing.
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

# Deletes every version in $CORE_STORE_DIR except whatever
# $INSTALLED_CORE_DIR currently resolves to -- re-checked fresh via
# readlink every time this runs (from the EXIT trap, or after a
# successful swap), never from a variable that could go stale if a signal
# lands at an inconvenient time. This is also what cleans up an
# interrupted/half-built staging dir: it's simply never the live target,
# so it's always a deletion candidate. Fails safe: if `core` exists but
# its live target can't be determined, nothing is pruned at all, rather
# than risk treating every entry as deletable.
prune_core_versions() {
  local live_target entry

  _validate_core_store || return 0
  live_target=""
  if [[ -L "$INSTALLED_CORE_DIR" ]]; then
    live_target="$(_resolve_link_target "$INSTALLED_CORE_DIR")" || live_target=""
  fi
  if [[ -z "$live_target" && -e "$INSTALLED_CORE_DIR" ]]; then
    return 0
  fi

  shopt -s nullglob
  for entry in "$CORE_STORE_DIR"/*/; do
    entry="${entry%/}"
    if [[ "$entry" != "$live_target" ]]; then
      # One oddly-shaped entry refusing to be removed must never abort
      # the rest of pruning -- let alone the install/uninstall this runs
      # inside of -- under `set -e`.
      safe_remove_in_store "$entry" || true
    fi
  done
  shopt -u nullglob
  return 0
}

cleanup_core_swap() {
  # Written as `if`, not `[[ ... ]] && rm ...`: under `set -e`, a bare
  # `&&`-as-statement whose left side is false exits the (trap) script
  # with that failure instead of leaving the real exit status alone.
  if [[ -n "$TMP_LINK_PATH" && -L "$TMP_LINK_PATH" ]]; then
    rm -f "$TMP_LINK_PATH"
  fi
  prune_core_versions
}

# Stage a fresh, pinned copy of core/bin + core/agentboard into a new
# versioned directory inside $CORE_STORE_DIR (never loose next to
# $INSTALLED_CORE_DIR -- see its declaration above), then make
# $INSTALLED_CORE_DIR a symlink to it via `mv -fh`, which renames the
# symlink itself over the target without ever following/removing what the
# target currently points at -- so a hook running concurrently always
# resolves either the full old version or the full new one, never a
# missing or half-populated path. `/bin/mv` and `/bin/ln` are called by
# absolute path, not whatever resolves first on PATH: GNU coreutils' `mv`
# (e.g. Homebrew's gnubin ahead of /bin) has no `-h` flag and would
# otherwise fail here.
#
# The staging dir's $CORE_VERSION_MARKER is written immediately after
# `mktemp -d` creates it, before anything is copied into it -- so a
# SIGTERM/Ctrl-C landing anywhere during staging (cp, the __pycache__
# find/rm, chmod, pin_core_bin) still leaves a dir safe_remove_in_store
# recognizes as AgentBoard's own, and prune_core_versions() (run by the
# EXIT trap below, and again at the end of a successful run) cleans it up
# like any other non-live version -- rather than an orphan that every
# later install.sh would refuse to touch, permanently blocking hooks/app/
# backfill from ever running again.
#
# Existing $INSTALLED_CORE_DIR and $CORE_STORE_DIR are classified and
# validated *before* the EXIT trap is ever armed (see below): nothing
# destructive has happened yet at that point, so --help, --dry-run (which
# never calls this function at all), an unknown argument, and every
# refusal here can never trigger a prune. Only once the store is
# confirmed to be either already ours or freshly created by us is the
# trap armed for the rest of this function (staging, the swap, and the
# final cleanup/prune) -- that's what closes the round-4 gap:
# prune_core_versions() always re-reads where `core` points fresh, rather
# than trusting a variable that could go stale if a signal landed between
# the swap and that variable being updated.
#
# install.sh supports exactly two states at $INSTALLED_CORE_DIR:
#   - missing entirely: nothing to do.
#   - a symlink into our own marked $CORE_STORE_DIR: a normal upgrade.
# Anything else -- a real directory (including a source checkout's own
# `core/`), a foreign symlink, or $CORE_STORE_DIR already existing but not
# a plain, marked directory -- is refused outright, touching nothing.
install_core_copy() {
  local core_parent ts staging_dir target

  core_parent="$(dirname "$INSTALLED_CORE_DIR")"
  mkdir -p "$core_parent"

  if [[ -L "$INSTALLED_CORE_DIR" ]]; then
    target="$(_resolve_link_target "$INSTALLED_CORE_DIR")" || target=""
    if [[ -z "$target" ]] || ! _validate_core_store || [[ "$target" != "$CORE_STORE_DIR"/* ]]; then
      echo "error: $INSTALLED_CORE_DIR is a symlink to ${target:-an unreadable target}, which install.sh did not create; refusing to touch it (move it aside, or set AGENTBOARD_CORE_DIR elsewhere)" >&2
      exit 1
    fi
    # Already our current layout -- ordinary upgrade, nothing special.
  elif [[ -e "$INSTALLED_CORE_DIR" ]]; then
    echo "error: $INSTALLED_CORE_DIR exists and isn't an AgentBoard install install.sh created; refusing to touch it (move it aside, or set AGENTBOARD_CORE_DIR elsewhere)" >&2
    exit 1
  fi

  if [[ -e "$CORE_STORE_DIR" || -L "$CORE_STORE_DIR" ]]; then
    if ! _validate_core_store; then
      echo "error: $CORE_STORE_DIR exists and is not an AgentBoard version store (expected a plain directory containing $CORE_STORE_MARKER); refusing to touch it (move it aside, or set AGENTBOARD_CORE_DIR elsewhere)" >&2
      exit 1
    fi
  else
    mkdir -p "$CORE_STORE_DIR"
    : > "$CORE_STORE_DIR/$CORE_STORE_MARKER"
  fi

  # Only now -- the existing core/store confirmed safe, or the store
  # freshly created by us -- is it safe to arm the trap.
  trap cleanup_core_swap EXIT

  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  staging_dir="$(mktemp -d "$CORE_STORE_DIR/${ts}-XXXXXX")"
  # Proof this version dir is AgentBoard's own -- written first, before
  # any copying, see the function comment above.
  date -u +%Y-%m-%dT%H:%M:%SZ > "$staging_dir/$CORE_VERSION_MARKER"
  mkdir -p "$staging_dir/bin" "$staging_dir/agentboard"
  cp -R "$CORE_DIR/bin/." "$staging_dir/bin/"
  cp -R "$CORE_DIR/agentboard/." "$staging_dir/agentboard/"
  find "$staging_dir" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
  chmod +x "$staging_dir/bin/agentboard"
  pin_core_bin "$staging_dir/bin/agentboard" "$PYTHON3_BIN" "$BAKED_AGENTBOARD_HOME"

  TMP_LINK_PATH="$(mktemp -u "$core_parent/.core-link.XXXXXX")"
  /bin/ln -s "$staging_dir" "$TMP_LINK_PATH"
  /bin/mv -fh "$TMP_LINK_PATH" "$INSTALLED_CORE_DIR"
  TMP_LINK_PATH=""

  # Cleans up this version's own interrupted predecessor (if any) and any
  # other leftover non-live version, so a later install always starts
  # from a tidy store.
  prune_core_versions
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-hooks) NO_HOOKS=1; shift ;;
    --no-app) NO_APP=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --enable-codex-hooks) ENABLE_CODEX_HOOKS=1; shift ;;
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

# Normalized once, here, so the dry-run preview and the real install agree
# on the exact same string regardless of how --prefix was spelled (a
# relative path, a trailing slash, or a symlinked ancestor) -- APP_INSTALL_DIR
# below is computed from this and used by both branches.
if [[ -n "$PREFIX" ]]; then
  PREFIX="$(normalize_path "$PREFIX")"
fi

# Same normalization for AGENTBOARD_CORE_DIR, and for the same reason:
# INSTALLED_BIN / CORE_STORE_DIR are derived from it below, only now that
# it's absolute with no trailing slash, so the symlink target this writes
# and the store glob prune_core_versions() reads always agree.
INSTALLED_CORE_DIR="$(normalize_leaf_path "$INSTALLED_CORE_DIR")"
INSTALLED_BIN="$INSTALLED_CORE_DIR/bin/agentboard"
CORE_STORE_DIR="${INSTALLED_CORE_DIR}.d"

echo "AgentBoard install: $ROOT_DIR"
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "(dry run: no files will be changed, including the installed core/ copy)"
fi

# -- 1. Prerequisites --------------------------------------------------------

echo "==> checking prerequisites"

PYTHON3_BIN="$(find_python3)"
if [[ -z "$PYTHON3_BIN" ]]; then
  if [[ -n "${AGENTBOARD_PYTHON3:-}" ]]; then
    echo "error: AGENTBOARD_PYTHON3=$AGENTBOARD_PYTHON3 is not a working python3 >= 3.11" >&2
  elif command -v python3 >/dev/null 2>&1; then
    echo "error: python3 >= 3.11 required, found $(python3 --version 2>&1) at $(command -v python3)" >&2
    echo "       (no python3.11/3.12/3.13/3.14 found on PATH either; install one, e.g. brew install python@3.12," >&2
    echo "        or point AGENTBOARD_PYTHON3 at an interpreter)" >&2
  else
    echo "error: python3 not found (AgentBoard's core requires python3 >= 3.11)" >&2
  fi
  exit 1
fi
echo "    python3: $("$PYTHON3_BIN" --version 2>&1) ($PYTHON3_BIN)"

# Only baked into the installed shim when the caller actually asked for a
# non-default data dir -- otherwise the shim's own fallback (agentboard.db's
# default) already matches, and there's nothing to pin.
BAKED_AGENTBOARD_HOME="${AGENTBOARD_HOME:-}"

if [[ ! -x "$AGENTBOARD_BIN" ]]; then
  chmod +x "$AGENTBOARD_BIN" 2>/dev/null || true
fi
if [[ ! -x "$AGENTBOARD_BIN" ]]; then
  echo "error: $AGENTBOARD_BIN not found or not executable" >&2
  exit 1
fi

HAVE_SWIFT=0
command -v swift >/dev/null 2>&1 && HAVE_SWIFT=1 || true
HAVE_CLT=0
xcode-select -p >/dev/null 2>&1 && HAVE_CLT=1 || true

NO_APP_FORCED=0
if [[ "$NO_APP" -eq 0 ]] && { [[ "$HAVE_SWIFT" -eq 0 ]] || [[ "$HAVE_CLT" -eq 0 ]]; }; then
  echo "    warning: Xcode Command Line Tools / swift not found; no app will be installed" >&2
  NO_APP=1
  NO_APP_FORCED=1
fi
if [[ "$NO_APP" -eq 0 ]]; then
  echo "    swift: $(swift --version 2>&1 | head -1)"
fi

# -- 2. Install a stable, pinned core/ copy at $INSTALLED_CORE_DIR -----------
#
# Gated behind --dry-run (unlike before): a dry run must not modify
# anything, including install.sh's own managed cache directory. The hook
# paths a --dry-run install-hooks preview shows below are still correct --
# they don't depend on the copy's content having been refreshed, only on
# $INSTALLED_CORE_DIR's path, which is fixed regardless.

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "==> would refresh core copy at $INSTALLED_CORE_DIR"
else
  echo "==> installing core copy at $INSTALLED_CORE_DIR"
  install_core_copy
fi

# -- 3. Build + install the app ----------------------------------------------
#
# Always build before touching anything already installed: if the build
# fails, the existing app (if any) must be left running and untouched.
# Only once the build has succeeded do we check whether a *running*
# AgentBoard is the one at this exact install location, and only then quit
# it before swapping the bundle in.

BUILD_APP_SH="$ROOT_DIR/scripts/build-app.sh"
APP_INSTALL_DIR="$(normalize_path "${PREFIX:-$HOME/Applications}")"
TARGET_BUNDLE="$APP_INSTALL_DIR/AgentBoard.app"
APP_INSTALL_RC=0

if [[ "$NO_APP" -eq 1 ]]; then
  if [[ "$NO_APP_FORCED" -eq 1 ]]; then
    echo "==> skipping app build (Xcode Command Line Tools / swift not found; no app installed)"
  else
    echo "==> skipping app build (--no-app)"
  fi
elif [[ ! -x "$BUILD_APP_SH" ]]; then
  echo "==> skipping app build ($BUILD_APP_SH not found or not executable)" >&2
else
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "would run: $BUILD_APP_SH --no-install, then install to $TARGET_BUNDLE"
    echo "  (quitting a currently-running AgentBoard first, only if it is the one at that path)"
    APP_REFUSAL="$(_app_bundle_refusal_reason "$TARGET_BUNDLE")"
    if [[ -n "$APP_REFUSAL" ]]; then
      echo "  ($APP_REFUSAL -- a real run would refuse to touch it)"
    fi
  else
    echo "==> building the app"
    AGENTBOARD_PYTHON3="$PYTHON3_BIN" AGENTBOARD_BAKED_HOME="$BAKED_AGENTBOARD_HOME" \
      "$BUILD_APP_SH" --no-install
    # Never overwrite (or even quit) anything at $TARGET_BUNDLE that isn't
    # a real directory we installed ourselves -- `defaults read` only
    # reads the plist, never writes.
    APP_REFUSAL="$(_app_bundle_refusal_reason "$TARGET_BUNDLE")"
    if [[ -n "$APP_REFUSAL" ]]; then
      echo "warning: $APP_REFUSAL" >&2
      APP_INSTALL_RC=1
    else
      quit_running_app_if_same_prefix "$TARGET_BUNDLE"
      echo "==> installing to $APP_INSTALL_DIR"
      mkdir -p "$APP_INSTALL_DIR"
      rm -rf "$TARGET_BUNDLE"
      cp -R "$APP_DIR/build/AgentBoard.app" "$TARGET_BUNDLE"
      echo "==> installed $TARGET_BUNDLE"
    fi
  fi
fi

# -- 4. Install hooks ---------------------------------------------------------
#
# Always via the installed copy ($INSTALLED_BIN), never the repo
# checkout, so the path registered in settings.json/hooks.json survives
# the repo being edited, moved, or deleted. Re-running this migrates any
# existing agentboard hook entry (e.g. one still pointing at an old repo
# path) to $INSTALLED_BIN in place -- see install-hooks itself.

INSTALL_HOOKS_RC=0

if [[ "$NO_HOOKS" -eq 1 ]]; then
  echo "==> skipping hook install (--no-hooks)"
else
  echo "==> installing hooks"

  HAS_CLAUDE=0
  command -v claude >/dev/null 2>&1 && HAS_CLAUDE=1 || true
  [[ -d "$HOME/.claude" ]] && HAS_CLAUDE=1 || true

  HAS_CODEX=0
  command -v codex >/dev/null 2>&1 && HAS_CODEX=1 || true
  [[ -d "$HOME/.codex" ]] && HAS_CODEX=1 || true

  if [[ "$HAS_CODEX" -eq 1 && "$ENABLE_CODEX_HOOKS" -eq 1 ]]; then
    # The installed shim is pinned to $PYTHON3_BIN; the checkout's own
    # (used only for --dry-run previews) still has an `env python3`
    # shebang, so run it through $PYTHON3_BIN explicitly -- the
    # prerequisite check above may well have picked a python3.1x that a
    # bare `python3` on PATH would not resolve to.
    if [[ "$DRY_RUN" -eq 1 ]]; then
      ENABLE_CMD=("$PYTHON3_BIN" "$AGENTBOARD_BIN")
    else
      ENABLE_CMD=("$INSTALLED_BIN")
    fi
    ENABLE_ARGS=()
    [[ "$DRY_RUN" -eq 1 ]] && ENABLE_ARGS+=(--dry-run)
    # A refusal (unsafe/unparseable config.toml) prints its own message
    # and exits 1 -- caught here, same as the install-hooks calls below,
    # so `set -e` doesn't abort before Claude/Codex hooks and backfill
    # ever run. Surfaced together with INSTALL_HOOKS_RC at the end.
    "${ENABLE_CMD[@]}" enable-codex-hooks "${ENABLE_ARGS[@]+"${ENABLE_ARGS[@]}"}" || INSTALL_HOOKS_RC=$?
  fi

  HOOK_ARGS=()
  [[ "$HAS_CLAUDE" -eq 1 ]] && HOOK_ARGS+=(--claude)
  [[ "$HAS_CODEX" -eq 1 ]] && HOOK_ARGS+=(--codex)
  [[ "$DRY_RUN" -eq 1 ]] && HOOK_ARGS+=(--dry-run)

  if [[ "$HAS_CLAUDE" -eq 0 && "$HAS_CODEX" -eq 0 ]]; then
    echo "    neither Claude Code nor Codex detected; nothing to install"
  elif [[ "$DRY_RUN" -eq 1 ]]; then
    # Nothing has been (re)installed at $INSTALLED_BIN in a dry run, so run
    # the preview via the repo checkout's own copy instead. --bin-path is
    # passed here *and* below, both times as the literal $INSTALLED_BIN
    # string -- never cli.py's own `_bin_path()` default, which resolves
    # `__file__` and would otherwise disagree between a preview (run from
    # the checkout) and a real install (run through the $INSTALLED_CORE_DIR
    # symlink, which `.resolve()` follows to that version's real path) even
    # though both must register the exact same stable command string.
    "$PYTHON3_BIN" "$AGENTBOARD_BIN" install-hooks --bin-path "$INSTALLED_BIN" \
      "${HOOK_ARGS[@]+"${HOOK_ARGS[@]}"}" || INSTALL_HOOKS_RC=$?
  else
    "$INSTALLED_BIN" install-hooks --bin-path "$INSTALLED_BIN" \
      "${HOOK_ARGS[@]+"${HOOK_ARGS[@]}"}" || INSTALL_HOOKS_RC=$?
  fi
fi

# -- 5. Backfill ---------------------------------------------------------------
#
# Still runs even if hook install above reported an error for one tool
# (e.g. malformed settings.json) -- that tool's hooks are skipped, but
# everything else proceeds; the overall failure is surfaced at the end.

echo "==> importing recent session history"
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "would run: $INSTALLED_BIN backfill --days 30"
else
  "$INSTALLED_BIN" backfill --days 30
fi

# Turn the Stop messages already in the events table into retained notes
# (Updates). Idempotent: a second run, or a run after the hook has started
# writing notes itself, imports nothing new. Never fatal to the install.
echo "==> rebuilding retained notes from recorded stop messages"
if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "would run: $INSTALLED_BIN notes --rebuild"
else
  "$INSTALLED_BIN" notes --rebuild || echo "warning: notes --rebuild failed" >&2
fi

# -- 6. Launch -----------------------------------------------------------------

if [[ "$NO_APP" -eq 0 && "$DRY_RUN" -eq 0 && "$APP_INSTALL_RC" -eq 0 ]]; then
  if [[ -d "$TARGET_BUNDLE" ]]; then
    echo "==> launching AgentBoard (enable launch-at-login from its Settings if you want)"
    open "$TARGET_BUNDLE" 2>/dev/null || true
  fi
fi

echo "==> done"
if [[ "$NO_APP_FORCED" -eq 1 ]]; then
  echo "    note: no app was installed (Xcode Command Line Tools / swift not found);"
  echo "    install them and re-run install.sh to add the menu bar app"
fi
[[ "$DRY_RUN" -eq 1 ]] && echo "(dry run: no files were changed)"
FINAL_RC=0
if [[ "$INSTALL_HOOKS_RC" -ne 0 ]]; then
  echo "    note: hook/config setup reported errors above; fix the invalid file(s) and re-run" >&2
  if [[ "$FINAL_RC" -eq 0 ]]; then
    FINAL_RC="$INSTALL_HOOKS_RC"
  fi
fi
if [[ "$APP_INSTALL_RC" -ne 0 ]]; then
  echo "    note: app install was refused above; see the warning for why" >&2
  if [[ "$FINAL_RC" -eq 0 ]]; then
    FINAL_RC="$APP_INSTALL_RC"
  fi
fi
exit "$FINAL_RC"

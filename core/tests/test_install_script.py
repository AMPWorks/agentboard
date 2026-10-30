"""Exercises scripts/install.sh and scripts/uninstall.sh in isolation.

Stubs pgrep/ps/kill/sleep via a shadowed PATH so this never looks at (let
alone touches) a real AgentBoard process on the machine running the tests.
The end-to-end tests run the real scripts against a fresh, throwaway HOME
with --no-app, so they never touch the real ~/.claude, ~/.codex, or
~/Library/Application Support/AgentBoard either.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

if sys.version_info < (3, 11):
    # These tests never import the package, so agentboard/__init__.py's
    # own guard wouldn't fire; they'd instead run the real scripts (which
    # find their own python3.1x) and take minutes to fail confusingly.
    raise unittest.SkipTest(
        f"AgentBoard's tests require Python >= 3.11 (this is {sys.version.split()[0]})"
    )

SCRIPTS_DIR = Path(__file__).resolve().parent.parent.parent / "scripts"
INSTALL_SH = SCRIPTS_DIR / "install.sh"
UNINSTALL_SH = SCRIPTS_DIR / "uninstall.sh"
BUILD_APP_SH = SCRIPTS_DIR / "build-app.sh"
REAL_CORE_DIR = SCRIPTS_DIR.parent / "core"


def _write_executable(path, body):
    path.write_text("#!/bin/bash\n" + body + "\n")
    path.chmod(0o755)


def _extract_function(name, source=INSTALL_SH):
    result = subprocess.run(
        ["sed", "-n", f"/^{name}() {{/,/^}}/p", str(source)],
        capture_output=True, text=True, check=True,
    )
    text = result.stdout
    assert text.strip(), f"{name}() not found in {source}"
    return text


def _extract_function_with_injected_line(name, after_substring, inject, source=INSTALL_SH):
    """_extract_function(), with one extra line spliced in right after the
    first occurrence of after_substring -- used to deterministically
    simulate a signal landing at an exact point too small a window to hit
    reliably by timing alone (see SignalRightAfterMvTests)."""
    text = _extract_function(name, source=source)
    assert after_substring in text, f"{after_substring!r} not found in {name}()"
    return text.replace(after_substring, after_substring + "\n" + inject, 1)


class QuitRunningAppIfSamePrefixTests(unittest.TestCase):
    """quit_running_app_if_same_prefix() must check every pid pgrep -x
    AgentBoard returns, touch only the one(s) whose actual running
    executable path matches the target bundle exactly (never a same-named
    app running from somewhere else), quit a match with a direct `kill
    -TERM` (never `osascript ... quit app` by name), and wait for the real
    pid (not by name) before force-killing it with `kill -KILL`."""

    TARGET_BUNDLE = "/Users/example/Applications/AgentBoard.app"
    OTHER_BUNDLE = "/Applications/AgentBoard.app"

    def _run(self, procs, target_bundle=None):
        """procs: list of {"pid": str, "comm": str, "dies_on_term": bool}."""
        target_bundle = target_bundle or self.TARGET_BUNDLE
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-install-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        bindir = tmp / "bin"
        bindir.mkdir()
        log = tmp / "log.txt"

        if procs:
            pgrep_body = "\n".join(f'echo "{p["pid"]}"' for p in procs) + "\nexit 0"
        else:
            pgrep_body = "exit 1"
        _write_executable(bindir / "pgrep", pgrep_body)

        case_lines = "\n".join(f'    {p["pid"]}) echo "{p["comm"]}" ;;' for p in procs)
        _write_executable(
            bindir / "ps",
            'pid=""\n'
            'prev=""\n'
            'for a in "$@"; do\n'
            '  if [ "$prev" = "-p" ]; then pid="$a"; fi\n'
            '  prev="$a"\n'
            "done\n"
            'case "$pid" in\n'
            f"{case_lines}\n"
            "  *) exit 1 ;;\n"
            "esac",
        )

        dies_on_term = {p["pid"] for p in procs if p.get("dies_on_term")}
        term_cases = "\n".join(f'    {pid}) touch "{tmp}/dead-{pid}" ;;' for pid in dies_on_term)
        _write_executable(
            bindir / "kill",
            f'echo "kill $*" >> "{log}"\n'
            'sig="$1"; pid="$2"\n'
            'case "$sig" in\n'
            "  -0)\n"
            f'    if [ -f "{tmp}/dead-$pid" ]; then exit 1; else exit 0; fi\n'
            "    ;;\n"
            "  -TERM)\n"
            '    case "$pid" in\n'
            f"{term_cases}\n"
            "    esac\n"
            "    exit 0\n"
            "    ;;\n"
            "  -KILL)\n"
            f'    touch "{tmp}/dead-$pid"\n'
            "    exit 0\n"
            "    ;;\n"
            "esac",
        )
        _write_executable(bindir / "sleep", "exit 0")

        func_file = tmp / "func.sh"
        func_file.write_text(
            "enable -n kill\n"  # force PATH lookup instead of bash's kill builtin
            "QUIT_WAIT_SECS=5\n"
            + _extract_function("quit_running_app_if_same_prefix")
            + f'\nquit_running_app_if_same_prefix "{target_bundle}"\n'
        )
        env = dict(os.environ)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        result = subprocess.run(
            ["/bin/bash", str(func_file)], capture_output=True, text=True, env=env,
        )
        calls = log.read_text().splitlines() if log.exists() else []
        return result, calls

    def test_not_running_does_nothing(self):
        result, calls = self._run(procs=[])
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [])

    def test_running_from_a_different_bundle_is_left_alone(self):
        result, calls = self._run(
            procs=[{"pid": "4242", "comm": self.OTHER_BUNDLE + "/Contents/MacOS/AgentBoard"}],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(calls, [], "must not quit an AgentBoard running from elsewhere")

    def test_multiple_pids_only_the_matching_one_is_touched(self):
        result, calls = self._run(
            procs=[
                {
                    "pid": "100",
                    "comm": self.TARGET_BUNDLE + "/Contents/MacOS/AgentBoard",
                    "dies_on_term": True,
                },
                {"pid": "200", "comm": self.OTHER_BUNDLE + "/Contents/MacOS/AgentBoard"},
            ],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kill -TERM 100", calls)
        self.assertFalse(any(" 200" in c for c in calls), calls)

    def test_running_from_target_bundle_quits_via_term_no_force_kill(self):
        result, calls = self._run(
            procs=[
                {
                    "pid": "4242",
                    "comm": self.TARGET_BUNDLE + "/Contents/MacOS/AgentBoard",
                    "dies_on_term": True,
                },
            ],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kill -TERM 4242", calls)
        self.assertFalse(any(c.startswith("kill -KILL") for c in calls), calls)

    def test_still_running_after_wait_falls_back_to_targeted_kill(self):
        result, calls = self._run(
            procs=[
                {"pid": "4242", "comm": self.TARGET_BUNDLE + "/Contents/MacOS/AgentBoard"},
            ],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kill -TERM 4242", calls)
        self.assertIn(
            "kill -KILL 4242", calls, "must force-kill the specific pid, not by process name",
        )


class NormalizePathTests(unittest.TestCase):
    """normalize_path() -- used to resolve --prefix identically in the
    dry-run and real paths -- absolute-izes, strips a trailing slash, and
    resolves symlinks, all without requiring the path to exist or creating
    anything."""

    def _run_normalize(self, arg, cwd=None):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-normalize-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        func_file = tmp / "normalize.sh"
        func_file.write_text(
            "set -euo pipefail\n"
            + _extract_function("_collapse_dots") + "\n"
            + _extract_function("normalize_path") +
            f'\nnormalize_path "{arg}"\n'
        )
        result = subprocess.run(
            ["/bin/bash", str(func_file)], capture_output=True, text=True, cwd=cwd,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_absolute_path_passes_through(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-normalize-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        self.assertEqual(self._run_normalize(str(tmp)), str(tmp))

    def test_trailing_slash_is_stripped(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-normalize-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        self.assertEqual(self._run_normalize(str(tmp) + "/"), str(tmp))

    def test_relative_path_resolved_against_cwd(self):
        cwd = Path(tempfile.mkdtemp(prefix="agentboard-normalize-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(cwd)]))
        out = self._run_normalize("sub/dir", cwd=str(cwd))
        self.assertEqual(out, str(cwd / "sub" / "dir"))

    def test_symlinked_ancestor_is_resolved(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-normalize-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        real_dir = tmp / "real"
        real_dir.mkdir()
        link = tmp / "link"
        link.symlink_to(real_dir)
        out = self._run_normalize(str(link) + "/Applications")
        self.assertEqual(out, str(real_dir / "Applications"))

    def test_nonexistent_path_is_not_created(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-normalize-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        target = tmp / "does" / "not" / "exist"
        out = self._run_normalize(str(target))
        self.assertEqual(out, str(target))
        self.assertFalse(target.exists())


class PythonDontWriteBytecodeTests(unittest.TestCase):
    """Every CLI invocation install.sh/uninstall.sh/build-app.sh make must
    run with PYTHONDONTWRITEBYTECODE=1, so a --dry-run (which runs its
    install-hooks preview straight from the repo checkout) never leaves
    __pycache__ behind there."""

    def test_scripts_export_pythondontwritebytecode(self):
        for script in (INSTALL_SH, UNINSTALL_SH, SCRIPTS_DIR / "build-app.sh"):
            self.assertIn(
                "export PYTHONDONTWRITEBYTECODE=1", script.read_text(), str(script),
            )

    def test_dry_run_install_hooks_preview_inherits_the_env_var(self):
        """A `python3` stub that logs the env var's value on every
        invocation, instead of asserting on the real repo's shared/mutable
        __pycache__ (which a concurrently-running test suite can also
        populate, independent of this script's own behavior)."""
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-pythondontwrite-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()

        bindir = tmp / "bin"
        bindir.mkdir()
        log = tmp / "python3-calls.log"
        _write_executable(
            bindir / "python3",
            f'echo "PYTHONDONTWRITEBYTECODE=${{PYTHONDONTWRITEBYTECODE:-unset}}" >> "{log}"\n'
            f'exec "{sys.executable}" "$@"',
        )

        env = dict(os.environ)
        env["HOME"] = str(fake_home)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR", "PYTHONDONTWRITEBYTECODE"):
            env.pop(var, None)

        result = subprocess.run(
            [str(INSTALL_SH), "--no-app", "--dry-run"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        calls = log.read_text().splitlines() if log.exists() else []
        self.assertTrue(calls, "expected at least one python3 invocation")
        self.assertTrue(
            all(c == "PYTHONDONTWRITEBYTECODE=1" for c in calls), calls,
        )


@unittest.skipUnless(sys.platform == "darwin", "install.sh is macOS-only")
class Python3FallbackTests(unittest.TestCase):
    """install.sh's prerequisite check must not give up just because the
    bare `python3` on PATH is too old: it tries python3.14/3.13/3.12/3.11
    next, and AGENTBOARD_PYTHON3 (an explicit interpreter path) wins
    outright. Exercised in --dry-run with --no-app, whose install-hooks
    preview also has to run under the interpreter that was actually
    chosen (the checkout shim's own shebang is still `env python3`)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentboard-py3-fallback-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.fake_home = self.tmp / "home"
        self.fake_home.mkdir()
        (self.fake_home / ".claude").mkdir()
        self.bindir = self.tmp / "bin"
        self.bindir.mkdir()
        # A `python3` that is "too old": fails the version probe, and
        # reports an old version string for the error message.
        _write_executable(
            self.bindir / "python3",
            'if [ "$1" = "--version" ]; then echo "Python 3.9.6"; exit 0; fi\nexit 1',
        )
        # PATH is restricted to the stub dir plus the system dirs install.sh
        # needs (sed, dirname, defaults, xcode-select) so no python3.1x the
        # machine happens to have elsewhere can be picked up by accident.
        self.env = {
            "HOME": str(self.fake_home),
            "PATH": f"{self.bindir}:/usr/bin:/bin",
            "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        }

    def _run(self, extra_env=None):
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run(
            [str(INSTALL_SH), "--no-app", "--dry-run"],
            capture_output=True, text=True, env=env, timeout=120,
        )

    def test_versioned_python3_is_used_when_bare_python3_is_too_old(self):
        _write_executable(self.bindir / "python3.12", f'exec "{sys.executable}" "$@"')
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"({sys.executable})", result.stdout, result.stdout)
        # The dry-run install-hooks preview ran (under that interpreter) and
        # previewed the Claude hooks diff.
        self.assertIn("hook --tool claude", result.stdout, result.stdout)

    def test_agentboard_python3_override_wins(self):
        result = self._run({"AGENTBOARD_PYTHON3": sys.executable})
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"({sys.executable})", result.stdout, result.stdout)

    def test_no_suitable_interpreter_fails_with_actionable_message(self):
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("python3 >= 3.11 required, found Python 3.9.6", result.stderr)
        self.assertIn("python3.11/3.12", result.stderr, "the error must say which names were tried")
        self.assertIn("AGENTBOARD_PYTHON3", result.stderr, "the error must mention the override")
        self.assertFalse((self.fake_home / ".local").exists())


class PinCoreBinTests(unittest.TestCase):
    """pin_core_bin() rewrites the shebang to the validated interpreter and
    (only when asked) bakes in AGENTBOARD_HOME -- without disturbing
    anything else in the file."""

    def _target_file(self, tmp):
        target = tmp / "agentboard"
        target.write_text(
            "#!/usr/bin/env python3\n"
            "import os\n"
            "_BAKED_AGENTBOARD_HOME = None\n"
            "print('rest of the file')\n"
        )
        return target

    def _run_pin(self, target, interpreter, baked_home):
        tmp = target.parent
        func_file = tmp / "pin.sh"
        func_file.write_text(_extract_function("pin_core_bin") + "\n" +
                              f'pin_core_bin "{target}" "{interpreter}" "{baked_home}"\n')
        result = subprocess.run(["/bin/bash", str(func_file)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_rewrites_shebang_only_when_no_baked_home_requested(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-pin-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        target = self._target_file(tmp)

        self._run_pin(target, sys.executable, "")

        lines = target.read_text().splitlines()
        self.assertEqual(lines[0], f"#!{sys.executable}")
        self.assertIn("_BAKED_AGENTBOARD_HOME = None", lines)
        self.assertIn("print('rest of the file')", lines)

    def test_bakes_in_agentboard_home_when_given(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-pin-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        target = self._target_file(tmp)
        custom_home = "/Users/example/custom agentboard home"

        self._run_pin(target, sys.executable, custom_home)

        text = target.read_text()
        self.assertIn(f"#!{sys.executable}", text.splitlines()[0])
        self.assertIn(f"_BAKED_AGENTBOARD_HOME = {custom_home!r}", text)

    def test_does_not_touch_other_lines(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-pin-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        target = self._target_file(tmp)
        self._run_pin(target, sys.executable, "")
        lines = target.read_text().splitlines()
        self.assertEqual(lines[1], "import os")
        self.assertEqual(lines[3], "print('rest of the file')")


_CORE_SWAP_FUNCTIONS = (
    "pin_core_bin",
    "_resolve_link_target",
    "_validate_core_store",
    "safe_remove_in_store",
    "prune_core_versions",
    "cleanup_core_swap",
    "install_core_copy",
)


class InstallCoreCopyTests(unittest.TestCase):
    """install_core_copy() makes $INSTALLED_CORE_DIR a symlink into a
    dedicated version store ($INSTALLED_CORE_DIR.d), never loose next to
    it, via `mv -fh` (atomic: the symlink is never missing or
    half-written). install.sh supports exactly two states at
    $INSTALLED_CORE_DIR: missing, or our own symlink into our own marked
    store -- anything else (a real directory, a foreign symlink, an
    unmarked or symlinked store) is refused outright, touching nothing.
    No migration from any pre-release layout: those were never
    published, so a real directory there -- including a source
    checkout's own `core/` -- is always refused, never migrated."""

    def _make_core_dir(self, tmp):
        core_dir = tmp / "repo-core"
        (core_dir / "bin").mkdir(parents=True)
        (core_dir / "agentboard").mkdir()
        (core_dir / "bin" / "agentboard").write_text(
            "#!/usr/bin/env python3\n_BAKED_AGENTBOARD_HOME = None\nprint('hi')\n"
        )
        (core_dir / "agentboard" / "__init__.py").write_text("")
        return core_dir

    def _run_install_core_copy(self, tmp, core_dir, installed_core_dir, expect_rc=0):
        func_file = tmp / "install_core_copy.sh"
        func_file.write_text(
            "set -euo pipefail\n"
            f'CORE_DIR="{core_dir}"\n'
            f'PYTHON3_BIN="{sys.executable}"\n'
            'BAKED_AGENTBOARD_HOME=""\n'
            f'INSTALLED_CORE_DIR="{installed_core_dir}"\n'
            f'CORE_STORE_DIR="{installed_core_dir}.d"\n'
            'CORE_STORE_MARKER=".agentboard-core-store"\n'
            'CORE_VERSION_MARKER=".agentboard-core-version"\n'
            'TMP_LINK_PATH=""\n'
            + "\n".join(_extract_function(fn) for fn in _CORE_SWAP_FUNCTIONS) + "\n"
            "install_core_copy\n"
        )
        result = subprocess.run(["/bin/bash", str(func_file)], capture_output=True, text=True)
        if expect_rc is not None:
            self.assertEqual(result.returncode, expect_rc, result.stdout + result.stderr)
        return result

    def test_first_install_creates_symlink_into_store(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-core-swap-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        core_dir = self._make_core_dir(tmp)
        installed = tmp / "share" / "core"
        store = tmp / "share" / "core.d"

        self._run_install_core_copy(tmp, core_dir, installed)

        self.assertTrue(installed.is_symlink())
        target = installed.resolve()
        self.assertEqual(target.parent, store, "versions must live only inside the dedicated store")
        self.assertTrue((installed / "bin" / "agentboard").is_file())
        self.assertTrue((store / ".agentboard-core-store").is_file(), "the store itself must be marked")
        self.assertTrue(
            (target / ".agentboard-core-version").is_file(),
            "every version dir must carry the ownership marker",
        )
        leftovers = [p.name for p in installed.parent.iterdir() if p.name.startswith(".core-")]
        self.assertEqual(leftovers, [], "no temp staging/link artifacts should survive a clean run")

    def test_reinstall_swaps_to_new_version_and_prunes_old(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-core-swap-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        core_dir = self._make_core_dir(tmp)
        installed = tmp / "share" / "core"
        store = tmp / "share" / "core.d"

        self._run_install_core_copy(tmp, core_dir, installed)
        first_target = installed.resolve()

        self._run_install_core_copy(tmp, core_dir, installed)
        second_target = installed.resolve()

        self.assertTrue(installed.is_symlink())
        self.assertNotEqual(first_target, second_target)
        self.assertFalse(first_target.exists(), "the old version must be pruned after the swap")
        versions = [p for p in store.iterdir() if p.is_dir()]
        self.assertEqual(versions, [second_target], versions)

    def test_leftover_marked_version_from_a_prior_interrupted_run_is_pruned(self):
        # Finding 3's other half: a marked, non-live version left behind
        # by some earlier interrupted run (e.g. one the EXIT trap never
        # got to run for, a SIGKILL) must not block a later install --
        # it's just another non-live version, cleaned up by the same
        # prune_core_versions() call every install already makes.
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-core-swap-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        core_dir = self._make_core_dir(tmp)
        installed = tmp / "share" / "core"
        store = tmp / "share" / "core.d"
        store.mkdir(parents=True)
        (store / ".agentboard-core-store").write_text("")
        leftover = store / "20200101T000000Z-stale1"
        leftover.mkdir()
        (leftover / ".agentboard-core-version").write_text("")

        self._run_install_core_copy(tmp, core_dir, installed)

        self.assertTrue(installed.is_symlink())
        self.assertFalse(leftover.exists(), "the stale leftover must be pruned")
        versions = [p for p in store.iterdir() if p.is_dir()]
        self.assertEqual(versions, [installed.resolve()], versions)

    def test_refuses_foreign_symlink(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-core-swap-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        core_dir = self._make_core_dir(tmp)
        installed = tmp / "share" / "core"
        installed.parent.mkdir(parents=True)
        checkout = tmp / "mycheckout"
        checkout.mkdir()
        (checkout / "uncommitted.py").write_text("precious")
        installed.symlink_to(checkout)

        result = self._run_install_core_copy(tmp, core_dir, installed, expect_rc=1)

        self.assertIn("did not create", result.stdout + result.stderr)
        self.assertIn("move it aside, or set AGENTBOARD_CORE_DIR elsewhere", result.stdout + result.stderr)
        self.assertTrue(installed.is_symlink())
        self.assertEqual(installed.resolve(), checkout)
        self.assertTrue((checkout / "uncommitted.py").is_file())

    def test_refuses_foreign_real_directory(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-core-swap-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        core_dir = self._make_core_dir(tmp)
        installed = tmp / "share" / "core"
        installed.mkdir(parents=True)
        (installed / "random-file.txt").write_text("not an agentboard install")

        result = self._run_install_core_copy(tmp, core_dir, installed, expect_rc=1)

        self.assertIn("refusing to touch it", result.stdout + result.stderr)
        self.assertIn("move it aside, or set AGENTBOARD_CORE_DIR elsewhere", result.stdout + result.stderr)
        self.assertTrue(installed.is_dir())
        self.assertFalse(installed.is_symlink())
        self.assertTrue((installed / "random-file.txt").is_file())

    def test_refuses_a_real_directory_shaped_like_the_old_pre_release_layout(self):
        # Round-6 finding 1: the dropped legacy heuristic (an executable
        # bin/agentboard + agentboard/cli.py) used to trigger a migrate-
        # then-delete. Pre-release layouts were never published, so this
        # exact shape -- which also happens to be any source checkout's
        # own core/ -- must now be refused outright, like any other real
        # directory, never migrated.
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-core-swap-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        core_dir = self._make_core_dir(tmp)
        installed = tmp / "share" / "core"
        installed.mkdir(parents=True)
        (installed / "bin").mkdir()
        (installed / "bin" / "agentboard").write_text("#!/bin/sh\n")
        (installed / "bin" / "agentboard").chmod(0o755)
        (installed / "agentboard").mkdir()
        (installed / "agentboard" / "cli.py").write_text("")
        (installed / "agentboard" / "MY_NOTES.py").write_text("wip")
        (installed / "tests").mkdir()
        (installed / "tests" / "test_x.py").write_text("precious uncommitted test")

        result = self._run_install_core_copy(tmp, core_dir, installed, expect_rc=1)

        self.assertIn("refusing to touch it", result.stdout + result.stderr)
        self.assertIn("move it aside, or set AGENTBOARD_CORE_DIR elsewhere", result.stdout + result.stderr)
        self.assertTrue(installed.is_dir())
        self.assertFalse(installed.is_symlink())
        self.assertTrue((installed / "agentboard" / "MY_NOTES.py").is_file())
        self.assertEqual((installed / "agentboard" / "MY_NOTES.py").read_text(), "wip")
        self.assertTrue((installed / "tests" / "test_x.py").is_file())
        self.assertFalse((tmp / "share" / "core.d").exists(), "nothing must be staged either")


def _python3_stub_env(tmp):
    """A `python3` stub on PATH that always execs this exact interpreter,
    so tests are deterministic regardless of what a bare `python3` resolves
    to on the machine running them."""
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    python3_stub = bindir / "python3"
    python3_stub.write_text(f'#!/bin/bash\nexec "{sys.executable}" "$@"\n')
    python3_stub.chmod(0o755)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    return env


def _codex_stub(tmp, version):
    """A `codex` stub next to the python3 stub whose `--version` prints
    `codex-cli <version>`, so the Codex hooks-flag messages do not depend
    on whichever Codex (if any) the machine has. Only for tests whose fake
    HOME already has ~/.codex: install.sh also counts a `codex` on PATH as
    Codex being installed."""
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    stub = bindir / "codex"
    stub.write_text(f'#!/bin/bash\necho "codex-cli {version}"\n')
    stub.chmod(0o755)


@unittest.skipUnless(sys.platform == "darwin", "install.sh/uninstall.sh are macOS-only")
class InstallScriptEndToEndTests(unittest.TestCase):
    """Runs the real install.sh --no-app against a fresh, throwaway HOME
    and checks the result: hook entries registered, the installed shim's
    shebang pinned to the exact validated interpreter, the installed core/
    copy being a symlink to a versioned dir, and the Codex [features] hooks
    warning shown. Never touches the real HOME."""

    def setUp(self):
        # Resolved up front: cli.py's _bin_path() resolves symlinks (so an
        # installed shim's reported path is realpath-clean), and on macOS
        # $TMPDIR lives under a /var -> /private/var symlink. Resolving here
        # keeps the HOME this test hands to install.sh identical to the path
        # the installed hook command ends up pinned to.
        self.tmp = Path(tempfile.mkdtemp(prefix="agentboard-install-e2e-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.fake_home = self.tmp / "home"
        self.fake_home.mkdir()
        (self.fake_home / ".claude").mkdir()
        (self.fake_home / ".codex").mkdir()
        # No [features] hooks = true -- the install should warn about it.
        (self.fake_home / ".codex" / "config.toml").write_text('model = "gpt-5"\n')

        self.env = _python3_stub_env(self.tmp)
        # A Codex old enough to still need [features] hooks = true.
        _codex_stub(self.tmp, "0.123.0")
        self.env["HOME"] = str(self.fake_home)
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR"):
            self.env.pop(var, None)

    def test_install_no_app_against_fresh_home(self):
        result = subprocess.run(
            [str(INSTALL_SH), "--no-app"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        core_dir = self.fake_home / ".local" / "share" / "agentboard" / "core"
        store_dir = self.fake_home / ".local" / "share" / "agentboard" / "core.d"
        installed_bin = core_dir / "bin" / "agentboard"
        self.assertTrue(installed_bin.is_file())
        self.assertTrue(core_dir.is_symlink(), "the installed core/ copy must be a symlink")
        self.assertEqual(core_dir.resolve().parent, store_dir, "versions must live only in core.d/")
        self.assertTrue((store_dir / ".agentboard-core-store").is_file())

        shebang = installed_bin.read_text().splitlines()[0]
        self.assertEqual(shebang, f"#!{sys.executable}", "shebang must be pinned, not `env python3`")

        settings = json.loads((self.fake_home / ".claude" / "settings.json").read_text())
        stop_commands = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
        self.assertTrue(
            any(c == f"{installed_bin} hook --tool claude" for c in stop_commands),
            stop_commands,
        )

        hooks = json.loads((self.fake_home / ".codex" / "hooks.json").read_text())
        stop_commands_codex = [h["command"] for g in hooks["hooks"]["Stop"] for h in g["hooks"]]
        self.assertTrue(any("agentboard hook --tool codex" in c for c in stop_commands_codex))

        self.assertIn("Run /hooks in Codex to trust the new hooks.", result.stdout)
        self.assertIn("hooks = true", result.stdout)
        self.assertIn("config.toml", result.stdout)
        self.assertIn("rebuilt notes: scanned=0 recorded=0", result.stdout)

    def test_dry_run_previews_the_notes_rebuild_after_backfill(self):
        result = subprocess.run(
            [str(INSTALL_SH), "--no-app", "--dry-run"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        lines = result.stdout.splitlines()
        backfill = [line for line in lines if line.endswith(" backfill --days 30")]
        self.assertEqual(len(backfill), 1, result.stdout)
        installed_bin = backfill[0][len("would run: "):-len(" backfill --days 30")]
        self.assertIn(f"would run: {installed_bin} notes --rebuild", lines)
        self.assertLess(
            lines.index(backfill[0]), lines.index(f"would run: {installed_bin} notes --rebuild")
        )

    def test_real_run_notes_rebuild_is_non_fatal(self):
        source = INSTALL_SH.read_text()
        rebuild_lines = [
            line.strip() for line in source.splitlines()
            if "notes --rebuild" in line and "would run" not in line
            and not line.lstrip().startswith("#")
        ]
        self.assertEqual(
            rebuild_lines,
            ['"$INSTALLED_BIN" notes --rebuild || echo "warning: notes --rebuild failed" >&2'],
        )

    def test_dry_run_leaves_everything_untouched(self):
        result = subprocess.run(
            [str(INSTALL_SH), "--no-app", "--dry-run"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse((self.fake_home / ".local").exists(), "dry run must not create the core copy")
        self.assertFalse((self.fake_home / ".claude" / "settings.json").exists())
        self.assertFalse((self.fake_home / ".codex" / "hooks.json").exists())

    def test_reinstall_swaps_core_symlink_and_keeps_hook_command_stable(self):
        first = subprocess.run(
            [str(INSTALL_SH), "--no-app"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        core_dir = self.fake_home / ".local" / "share" / "agentboard" / "core"
        first_version = core_dir.resolve()
        settings_before = json.loads((self.fake_home / ".claude" / "settings.json").read_text())
        codex_hooks_path = self.fake_home / ".codex" / "hooks.json"
        codex_before = codex_hooks_path.read_bytes()

        second = subprocess.run(
            [str(INSTALL_SH), "--no-app"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        second_version = core_dir.resolve()
        settings_after = json.loads((self.fake_home / ".claude" / "settings.json").read_text())

        store_dir = self.fake_home / ".local" / "share" / "agentboard" / "core.d"
        self.assertTrue(core_dir.is_symlink())
        self.assertNotEqual(first_version, second_version, "re-running install.sh must swap the version")
        self.assertFalse(first_version.exists(), "the old version must be pruned")
        versions = [p for p in store_dir.iterdir() if p.is_dir()]
        self.assertEqual(versions, [second_version], versions)
        # The registered hook command string must be byte-identical across
        # the reinstall -- it always names the stable core/ symlink path,
        # never a specific versioned directory -- so Codex users are never
        # asked to re-trust /hooks just because install.sh re-ran.
        self.assertEqual(settings_before["hooks"]["Stop"], settings_after["hooks"]["Stop"])
        self.assertEqual(settings_before["hooks"], settings_after["hooks"])
        # The async entries, too: an unchanged hooks.json is what keeps
        # Codex from skipping AgentBoard's hooks until they are re-trusted.
        self.assertEqual(codex_hooks_path.read_bytes(), codex_before)
        self.assertNotIn("trust them again", second.stdout)
        pre_tool = [h for g in settings_after["hooks"]["PreToolUse"] for h in g["hooks"]
                    if "agentboard hook --tool claude" in h["command"]]
        self.assertEqual([h.get("async") for h in pre_tool], [True])


@unittest.skipUnless(sys.platform == "darwin", "install.sh/uninstall.sh are macOS-only")
class InstallThenUninstallEndToEndTests(unittest.TestCase):
    """Runs install.sh --no-app, fires the registered hook command for
    real, then runs uninstall.sh --no-app and checks everything install.sh
    created (hooks, the core symlink, the whole core.d/ version store) is
    gone."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentboard-install-uninstall-e2e-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.fake_home = self.tmp / "home"
        self.fake_home.mkdir()
        (self.fake_home / ".claude").mkdir()
        (self.fake_home / ".codex").mkdir()
        (self.fake_home / ".codex" / "config.toml").write_text('model = "gpt-5"\n')

        self.env = _python3_stub_env(self.tmp)
        self.env["HOME"] = str(self.fake_home)
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR"):
            self.env.pop(var, None)

    def test_full_cycle_fires_hook_then_removes_everything(self):
        install_result = subprocess.run(
            [str(INSTALL_SH), "--no-app"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self.assertEqual(install_result.returncode, 0, install_result.stdout + install_result.stderr)

        core_dir = self.fake_home / ".local" / "share" / "agentboard" / "core"
        installed_bin = core_dir / "bin" / "agentboard"
        self.assertTrue(core_dir.is_symlink())

        # Fire the exact hook command Claude Code would run, via the exact
        # installed path registered in settings.json.
        payload = json.dumps({
            "session_id": "e2e-s1", "cwd": str(self.tmp), "hook_event_name": "SessionStart",
        })
        hook_result = subprocess.run(
            [str(installed_bin), "hook", "--tool", "claude"],
            input=payload, capture_output=True, text=True, env=self.env, timeout=30,
        )
        self.assertEqual(hook_result.returncode, 0, hook_result.stdout + hook_result.stderr)

        snapshot_result = subprocess.run(
            [str(installed_bin), "snapshot", "--json", "--all"],
            capture_output=True, text=True, env=self.env, timeout=30,
        )
        self.assertEqual(snapshot_result.returncode, 0, snapshot_result.stdout + snapshot_result.stderr)
        session_ids = [s["session_id"] for s in json.loads(snapshot_result.stdout)["sessions"]]
        self.assertIn("e2e-s1", session_ids, "the hook must have recorded the session")

        uninstall_result = subprocess.run(
            [str(UNINSTALL_SH), "--no-app"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self.assertEqual(uninstall_result.returncode, 0, uninstall_result.stdout + uninstall_result.stderr)

        self.assertFalse(core_dir.is_symlink())
        self.assertFalse(core_dir.exists())
        store_dir = self.fake_home / ".local" / "share" / "agentboard" / "core.d"
        self.assertFalse(store_dir.exists(), "the whole version store must be removed")
        # install.sh created settings.json/hooks.json from nothing; removing
        # agentboard's own entries should leave nothing else in them, so
        # uninstall.sh deletes the files outright.
        self.assertFalse((self.fake_home / ".claude" / "settings.json").exists())
        self.assertFalse((self.fake_home / ".codex" / "hooks.json").exists())


@unittest.skipUnless(sys.platform == "darwin", "uninstall.sh is macOS-only")
class UninstallWithMalformedConfigEndToEndTests(unittest.TestCase):
    """A malformed settings.json must not abort the rest of uninstall.sh
    under `set -e`: the core copy and the app still get removed, Codex's
    hooks.json (unaffected) still gets processed, and the script exits
    non-zero only at the very end, as a summary."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentboard-uninstall-malformed-e2e-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.fake_home = self.tmp / "home"
        self.fake_home.mkdir()
        (self.fake_home / ".claude").mkdir()
        (self.fake_home / ".codex").mkdir()
        (self.fake_home / ".codex" / "config.toml").write_text('model = "gpt-5"\n')

        self.env = _python3_stub_env(self.tmp)
        self.env["HOME"] = str(self.fake_home)
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR"):
            self.env.pop(var, None)

        install_result = subprocess.run(
            [str(INSTALL_SH), "--no-app"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        assert install_result.returncode == 0, install_result.stdout + install_result.stderr

    def test_malformed_settings_json_does_not_abort_core_removal(self):
        settings_path = self.fake_home / ".claude" / "settings.json"
        malformed = '{\n  "model": "opus",\n  "hooks": {,}\n'
        settings_path.write_text(malformed)

        core_dir = self.fake_home / ".local" / "share" / "agentboard" / "core"
        self.assertTrue(core_dir.is_symlink())

        result = subprocess.run(
            [str(UNINSTALL_SH), "--no-app"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )

        self.assertNotEqual(result.returncode, 0, "must surface the failure")
        self.assertIn("not valid JSON", result.stdout + result.stderr)

        # The core copy and the app step still ran despite the hooks failure.
        self.assertFalse(core_dir.exists())
        store_dir = self.fake_home / ".local" / "share" / "agentboard" / "core.d"
        self.assertFalse(store_dir.exists(), "the whole version store must be removed")

        # The malformed file itself must be left byte-for-byte untouched.
        self.assertEqual(settings_path.read_text(), malformed)

        # Codex's hooks.json, unaffected by Claude's malformed file, must
        # still have been processed (agentboard uninstalled from it).
        hooks_path = self.fake_home / ".codex" / "hooks.json"
        if hooks_path.is_file():
            hooks = json.loads(hooks_path.read_text())
            all_commands = [
                h["command"]
                for groups in hooks.get("hooks", {}).values()
                for g in groups
                for h in g["hooks"]
            ]
            self.assertFalse(any("agentboard hook" in c for c in all_commands), all_commands)


@unittest.skipUnless(sys.platform == "darwin", "install.sh is macOS-only")
class MvAndLnPinnedToAbsolutePathTests(unittest.TestCase):
    """install_core_copy() must call /bin/mv and /bin/ln by absolute path,
    never whatever resolves first on PATH -- GNU coreutils' `mv` (e.g.
    Homebrew's gnubin ahead of /bin, a common setup) has no `-h` flag and
    would otherwise reject the atomic swap outright."""

    def _env_with_hostile_mv(self, tmp, fake_home):
        env = _python3_stub_env(tmp)
        env["HOME"] = str(fake_home)
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR"):
            env.pop(var, None)
        bindir = tmp / "hostile-bin"
        bindir.mkdir(exist_ok=True)
        # A real GNU mv would say "invalid option -- 'h'"; this stub just
        # fails hard, which is enough to prove install.sh never reaches it.
        _write_executable(bindir / "mv", 'echo "mv: illegal option -- h" >&2\nexit 1')
        env["PATH"] = f"{bindir}:{env['PATH']}"
        return env

    def test_fresh_install_succeeds_despite_hostile_mv_on_path(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-gnu-mv-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()
        env = self._env_with_hostile_mv(tmp, fake_home)

        result = subprocess.run(
            [str(INSTALL_SH), "--no-app"], capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        core_dir = fake_home / ".local" / "share" / "agentboard" / "core"
        self.assertTrue(core_dir.is_symlink())
        self.assertTrue((core_dir / "bin" / "agentboard").is_file())

    def test_reinstall_over_existing_install_succeeds_despite_hostile_mv_on_path(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-gnu-mv-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()

        clean_env = _python3_stub_env(tmp)
        clean_env["HOME"] = str(fake_home)
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR"):
            clean_env.pop(var, None)
        first = subprocess.run(
            [str(INSTALL_SH), "--no-app"], capture_output=True, text=True, env=clean_env, timeout=120,
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)

        hostile_env = self._env_with_hostile_mv(tmp, fake_home)
        second = subprocess.run(
            [str(INSTALL_SH), "--no-app"], capture_output=True, text=True, env=hostile_env, timeout=120,
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        core_dir = fake_home / ".local" / "share" / "agentboard" / "core"
        self.assertTrue(core_dir.is_symlink())
        self.assertTrue((core_dir / "bin" / "agentboard").is_file())


@unittest.skipUnless(sys.platform == "darwin", "install.sh is macOS-only")
class AtomicCoreSwapFailureEndToEndTests(unittest.TestCase):
    """Whatever goes wrong during a *re*install's version swap -- a failed
    command at any step, or the script being signaled outright -- `core`
    must keep resolving to a complete, working version afterward, and a
    hook fired through it must still record an event.

    This is what prune_core_versions() always re-reading `readlink
    "$INSTALLED_CORE_DIR"` fresh (rather than trusting a variable that
    could go stale) is for: trusting a variable was exactly the bug -- the
    EXIT trap could delete the version `core` now points at if a signal
    landed between the swap and that variable being updated. See
    SignalRightAfterMvTests below for the precise repro of that gap."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentboard-swap-failure-e2e-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.fake_home = self.tmp / "home"
        self.fake_home.mkdir()
        (self.fake_home / ".claude").mkdir()

        self.env = _python3_stub_env(self.tmp)
        self.env["HOME"] = str(self.fake_home)
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR"):
            self.env.pop(var, None)

        first = subprocess.run(
            [str(INSTALL_SH), "--no-app"],
            capture_output=True, text=True, env=dict(self.env), timeout=120,
        )
        assert first.returncode == 0, first.stdout + first.stderr

        self.core_dir = self.fake_home / ".local" / "share" / "agentboard" / "core"
        assert self.core_dir.is_symlink()
        self.original_version = self.core_dir.resolve()

    def _fire_hook_and_assert_recorded(self, session_id):
        installed_bin = self.core_dir / "bin" / "agentboard"
        self.assertTrue(self.core_dir.is_symlink(), "core must still resolve to a version")
        self.assertTrue(installed_bin.is_file(), "core must point at a complete version")
        payload = json.dumps({
            "session_id": session_id, "cwd": str(self.tmp), "hook_event_name": "SessionStart",
        })
        hook_result = subprocess.run(
            [str(installed_bin), "hook", "--tool", "claude"],
            input=payload, capture_output=True, text=True, env=self.env, timeout=30,
        )
        self.assertEqual(hook_result.returncode, 0, hook_result.stdout + hook_result.stderr)
        snapshot_result = subprocess.run(
            [str(installed_bin), "snapshot", "--json", "--all"],
            capture_output=True, text=True, env=self.env, timeout=30,
        )
        self.assertEqual(snapshot_result.returncode, 0, snapshot_result.stdout + snapshot_result.stderr)
        session_ids = [s["session_id"] for s in json.loads(snapshot_result.stdout)["sessions"]]
        self.assertIn(session_id, session_ids, "a fired hook must record an event")

    def test_failure_during_staging_leaves_original_version_intact(self):
        bindir = self.tmp / "fail-chmod-bin"
        bindir.mkdir()
        _write_executable(bindir / "chmod", 'echo "chmod: injected failure" >&2\nexit 1')
        env = dict(self.env)
        env["PATH"] = f"{bindir}:{env['PATH']}"

        result = subprocess.run(
            [str(INSTALL_SH), "--no-app"], capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            self.core_dir.resolve(), self.original_version,
            "core must still point at the original version",
        )
        self._fire_hook_and_assert_recorded("staging-failure-s1")

    def test_failure_at_ln_leaves_original_version_intact(self):
        # Can't intercept /bin/ln directly (that's the point of pinning
        # it) -- instead make the real /bin/ln -s genuinely fail by
        # revoking write permission on its target directory right as the
        # (PATH-resolved) mktemp call for TMP_LINK_PATH returns, which is
        # immediately before the real ln -s runs.
        bindir = self.tmp / "fail-ln-bin"
        bindir.mkdir()
        _write_executable(
            bindir / "mktemp",
            'out="$(/usr/bin/mktemp "$@")"\n'
            'rc=$?\n'
            'if [ "$1" = "-u" ]; then chmod 555 "$(dirname "$out")"; fi\n'
            'echo "$out"\n'
            'exit "$rc"',
        )
        env = dict(self.env)
        env["PATH"] = f"{bindir}:{env['PATH']}"

        try:
            result = subprocess.run(
                [str(INSTALL_SH), "--no-app"], capture_output=True, text=True, env=env, timeout=120,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.core_dir.resolve(), self.original_version)
            self._fire_hook_and_assert_recorded("ln-failure-s1")
        finally:
            os.chmod(self.core_dir.parent, 0o755)

    def test_failure_at_mv_leaves_original_version_intact(self):
        # ln -s (a brand new name) still succeeds in the same directory;
        # only replacing the uchg-flagged existing `core` entry fails --
        # the real-world shape of "the rename itself fails" without
        # needing to intercept /bin/mv.
        subprocess.run(["chflags", "-h", "uchg", str(self.core_dir)], check=True, timeout=10)
        try:
            result = subprocess.run(
                [str(INSTALL_SH), "--no-app"], capture_output=True, text=True, env=self.env, timeout=120,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(self.core_dir.resolve(), self.original_version)
            self._fire_hook_and_assert_recorded("mv-failure-s1")
        finally:
            subprocess.run(["chflags", "-h", "nouchg", str(self.core_dir)], timeout=10)

    def test_failure_after_swap_still_leaves_new_version_working(self):
        old_version = str(self.original_version)
        bindir = self.tmp / "fail-rm-bin"
        bindir.mkdir()
        _write_executable(
            bindir / "rm",
            'for a in "$@"; do\n'
            f'  if [ "$a" = "{old_version}" ]; then\n'
            '    echo "rm: injected failure cleaning up the old version" >&2\n'
            "    exit 1\n"
            "  fi\n"
            "done\n"
            'exec /bin/rm "$@"',
        )
        env = dict(self.env)
        env["PATH"] = f"{bindir}:{env['PATH']}"

        result = subprocess.run(
            [str(INSTALL_SH), "--no-app"], capture_output=True, text=True, env=env, timeout=120,
        )
        # The swap itself must still have happened even though cleaning up
        # the old version afterward failed.
        self.assertTrue(self.core_dir.is_symlink())
        new_version = self.core_dir.resolve()
        self.assertNotEqual(new_version, self.original_version, "the swap must still have happened")
        self._fire_hook_and_assert_recorded("after-mv-failure-s1")

    def _run_with_signal(self, sig_name):
        # Can't target install.sh's pid directly from Python (we only
        # learn it after launching, and need the stub to act mid-run) --
        # instead have the (PATH-resolved) mktemp stub, invoked from deep
        # inside install_core_copy, walk its own ppid chain until it finds
        # the install.sh process itself, then signal that one.
        bindir = self.tmp / f"signal-{sig_name.lower()}-bin"
        bindir.mkdir()
        _write_executable(
            bindir / "mktemp",
            'out="$(/usr/bin/mktemp "$@")"\n'
            'rc=$?\n'
            'if [ "$1" = "-u" ]; then\n'
            '  pid=$$\n'
            '  for _hop in 1 2 3 4 5; do\n'
            '    pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d " ")"\n'
            '    [ -z "$pid" ] && break\n'
            '    cmd="$(ps -o command= -p "$pid" 2>/dev/null)"\n'
            '    case "$cmd" in\n'
            '      *install.sh*) kill "-$AGENTBOARD_TEST_SIGNAL" "$pid" 2>/dev/null || true; break ;;\n'
            "    esac\n"
            "  done\n"
            "fi\n"
            'echo "$out"\n'
            'exit "$rc"',
        )
        env = dict(self.env)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        env["AGENTBOARD_TEST_SIGNAL"] = sig_name

        return subprocess.run(
            [str(INSTALL_SH), "--no-app"],
            capture_output=True, text=True, env=env, timeout=120,
        )

    def test_sigterm_during_swap_leaves_a_working_core(self):
        result = self._run_with_signal("TERM")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.core_dir.resolve(), self.original_version)
        self._fire_hook_and_assert_recorded("sigterm-s1")

    def test_sighup_during_swap_leaves_a_working_core(self):
        result = self._run_with_signal("HUP")
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.core_dir.resolve(), self.original_version)
        self._fire_hook_and_assert_recorded("sighup-s1")


class SignalRightAfterMvTests(unittest.TestCase):
    """The precise round-4 finding: a signal landing exactly between
    `/bin/mv -fh` completing and the next line running must not make the
    EXIT trap (or prune_core_versions()) delete the version `core` now
    points at. The real window is far too small to hit reliably by
    timing, so -- exactly as the round-4 review's own repro did -- the
    signal is injected deterministically by inserting `kill -TERM $$` as
    the literal next line after the real `mv -fh` call, in an extracted
    copy of install_core_copy(). Uses the real repo core/ as CORE_DIR so
    the staged version is a fully working CLI, letting this fire a real
    hook afterward."""

    def test_sigterm_right_after_mv_leaves_core_resolving_and_hook_working(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-sigterm-after-mv-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))

        installed = tmp / "share" / "core"
        store = tmp / "share" / "core.d"
        store.mkdir(parents=True)
        (store / ".agentboard-core-store").write_text("")
        old_version = store / "20200101T000000Z-oldver1"
        (old_version / "bin").mkdir(parents=True)
        (old_version / "agentboard").mkdir()
        (old_version / ".agentboard-core-version").write_text("")
        (old_version / "bin" / "agentboard").write_text("#!/bin/sh\nexit 0\n")
        (old_version / "bin" / "agentboard").chmod(0o755)
        installed.symlink_to(old_version)

        func_file = tmp / "install_core_copy.sh"
        func_file.write_text(
            "set -euo pipefail\n"
            f'CORE_DIR="{REAL_CORE_DIR}"\n'
            f'PYTHON3_BIN="{sys.executable}"\n'
            'BAKED_AGENTBOARD_HOME=""\n'
            f'INSTALLED_CORE_DIR="{installed}"\n'
            f'CORE_STORE_DIR="{store}"\n'
            'CORE_STORE_MARKER=".agentboard-core-store"\n'
            'CORE_VERSION_MARKER=".agentboard-core-version"\n'
            'TMP_LINK_PATH=""\n'
            + "\n".join(
                _extract_function(fn)
                for fn in ("pin_core_bin", "_resolve_link_target",
                           "_validate_core_store", "safe_remove_in_store",
                           "prune_core_versions", "cleanup_core_swap")
            ) + "\n"
            + _extract_function_with_injected_line(
                "install_core_copy",
                '/bin/mv -fh "$TMP_LINK_PATH" "$INSTALLED_CORE_DIR"',
                "kill -TERM $$",
            ) + "\n"
            "install_core_copy\n"
        )

        result = subprocess.run(["/bin/bash", str(func_file)], capture_output=True, text=True)

        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(installed.is_symlink())
        new_version = installed.resolve()
        self.assertNotEqual(new_version, old_version, "the swap must have completed before the signal")
        installed_bin = installed / "bin" / "agentboard"
        self.assertTrue(installed_bin.is_file(), "core must resolve to a complete version")

        payload = json.dumps({
            "session_id": "sigterm-after-mv-s1", "cwd": str(tmp), "hook_event_name": "SessionStart",
        })
        # The staged shim is pinned to sys.executable, but HOME and
        # AGENTBOARD_HOME still come from the environment -- point both at
        # this test's own temp dir so the hook can never write into the
        # real ~/Library/Application Support/AgentBoard.
        hook_home = tmp / "home"
        hook_home.mkdir()
        hook_env = dict(os.environ)
        hook_env["PYTHONDONTWRITEBYTECODE"] = "1"
        hook_env["HOME"] = str(hook_home)
        hook_env["AGENTBOARD_HOME"] = str(tmp / "agentboard-home")
        hook_result = subprocess.run(
            [str(installed_bin), "hook", "--tool", "claude"],
            input=payload, capture_output=True, text=True, env=hook_env, timeout=30,
        )
        self.assertEqual(hook_result.returncode, 0, hook_result.stdout + hook_result.stderr)
        self.assertTrue(
            (tmp / "agentboard-home" / "agentboard.db").is_file(),
            "the hook must have recorded into this test's own data dir",
        )


@unittest.skipUnless(sys.platform == "darwin", "install.sh is macOS-only")
class InterruptedStagingThenReinstallTests(unittest.TestCase):
    """Finding 3: the staging dir's version marker is now written
    immediately after `mktemp -d`, before any copying -- so a SIGTERM
    landing during cp, the __pycache__ find/rm, or chmod still leaves a
    dir safe_remove_in_store recognizes as AgentBoard's own (rather than
    an unmarked orphan every later install.sh would refuse to touch,
    forever blocking hooks/app/backfill from running again). A plain
    re-run after each must succeed end to end, with hooks installed and
    no half-staged leftover surviving in the store."""

    # Walks up the real install.sh process's own ppid chain (the stub is
    # invoked as a *child* of it) and signals it -- same technique as
    # AtomicCoreSwapFailureEndToEndTests._run_with_signal above, since the
    # signal must land inside install.sh itself, not just the stub.
    _PS_WALK_AND_SIGNAL = (
        'pid=$$\n'
        'for _hop in 1 2 3 4 5; do\n'
        '  pid="$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d " ")"\n'
        '  [ -z "$pid" ] && break\n'
        '  cmd="$(ps -o command= -p "$pid" 2>/dev/null)"\n'
        '  case "$cmd" in\n'
        '    *install.sh*) kill -TERM "$pid" 2>/dev/null || true; break ;;\n'
        '  esac\n'
        'done\n'
    )

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentboard-interrupted-staging-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.fake_home = self.tmp / "home"
        self.fake_home.mkdir()
        (self.fake_home / ".claude").mkdir()
        self.env = _python3_stub_env(self.tmp)
        self.env["HOME"] = str(self.fake_home)
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR"):
            self.env.pop(var, None)
        self.core_dir = self.fake_home / ".local" / "share" / "agentboard" / "core"
        self.store_dir = self.fake_home / ".local" / "share" / "agentboard" / "core.d"

    def _env_signaling(self, cmd_name, real_path):
        bindir = self.tmp / f"signal-{cmd_name}-bin"
        bindir.mkdir()
        _write_executable(bindir / cmd_name, self._PS_WALK_AND_SIGNAL + f'exec {real_path} "$@"')
        env = dict(self.env)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        return env

    def _assert_any_leftover_is_marked(self):
        # Self-healing is allowed to have already pruned the half-staged
        # dir within the very same interrupted run (its own EXIT trap
        # re-reads live_target as empty on a first install and prunes
        # everything non-live) -- so either there's nothing left, or
        # whatever is left already carries the marker.
        if not self.store_dir.is_dir():
            return
        for entry in self.store_dir.iterdir():
            if entry.is_dir():
                self.assertTrue(
                    (entry / ".agentboard-core-version").is_file(),
                    f"leftover staging dir {entry} must already carry the version marker",
                )

    def _assert_reinstall_succeeds_with_hooks(self):
        result = subprocess.run(
            [str(INSTALL_SH), "--no-app"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.core_dir.is_symlink())
        installed_bin = self.core_dir / "bin" / "agentboard"
        self.assertTrue(installed_bin.is_file())
        versions = [p for p in self.store_dir.iterdir() if p.is_dir()]
        self.assertEqual(
            versions, [self.core_dir.resolve()], "the half-staged leftover must be pruned",
        )

        settings = json.loads((self.fake_home / ".claude" / "settings.json").read_text())
        stop_commands = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
        self.assertTrue(
            any(c == f"{installed_bin} hook --tool claude" for c in stop_commands), stop_commands,
        )

    def _run_interrupted_then_reinstall(self, cmd_name, real_path):
        env = self._env_signaling(cmd_name, real_path)
        result = subprocess.run(
            [str(INSTALL_SH), "--no-app"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self._assert_any_leftover_is_marked()
        self._assert_reinstall_succeeds_with_hooks()

    def test_sigterm_during_cp_then_reinstall_succeeds(self):
        self._run_interrupted_then_reinstall("cp", "/bin/cp")

    def test_sigterm_during_find_pycache_cleanup_then_reinstall_succeeds(self):
        self._run_interrupted_then_reinstall("find", "/usr/bin/find")

    def test_sigterm_during_chmod_then_reinstall_succeeds(self):
        self._run_interrupted_then_reinstall("chmod", "/bin/chmod")


@unittest.skipUnless(sys.platform == "darwin", "install.sh is macOS-only")
class EnableCodexHooksRefusalDoesNotAbortInstallTests(unittest.TestCase):
    """A config.toml shape _patch_codex_hooks_feature() refuses (e.g.
    `features` already an inline table) must not abort the whole install
    under `set -e` -- Claude hooks, Codex hooks.json, and the core copy
    must still get installed, with the failure only surfaced (and exiting
    non-zero) at the end -- same pattern as a malformed settings.json."""

    def test_refusal_prints_message_but_hooks_and_core_still_install(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-enable-codex-refusal-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()
        (fake_home / ".codex").mkdir()
        (fake_home / ".codex" / "config.toml").write_text("features = { web = [1, 2] }\n")

        env = _python3_stub_env(tmp)
        # On a Codex that enables hooks by default there is nothing to patch.
        _codex_stub(tmp, "0.123.0")
        env["HOME"] = str(fake_home)
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR"):
            env.pop(var, None)

        result = subprocess.run(
            [str(INSTALL_SH), "--no-app", "--enable-codex-hooks"],
            capture_output=True, text=True, env=env, timeout=120,
        )

        self.assertNotEqual(result.returncode, 0, "the refusal must be surfaced")
        output = result.stdout + result.stderr
        self.assertIn("error:", output)

        settings_path = fake_home / ".claude" / "settings.json"
        self.assertTrue(settings_path.is_file(), "Claude hooks must still be installed")
        settings = json.loads(settings_path.read_text())
        self.assertIn("Stop", settings["hooks"])

        hooks_path = fake_home / ".codex" / "hooks.json"
        self.assertTrue(hooks_path.is_file(), "Codex hooks.json must still be installed")

        # The refused edit never writes to config.toml at all.
        self.assertEqual(
            (fake_home / ".codex" / "config.toml").read_text(), "features = { web = [1, 2] }\n",
        )

        core_dir = fake_home / ".local" / "share" / "agentboard" / "core"
        self.assertTrue(core_dir.is_symlink(), "the core copy (and so backfill) must still have run")


@unittest.skipUnless(sys.platform == "darwin", "install.sh/uninstall.sh are macOS-only")
class TwoIndependentInstallsShareParentTests(unittest.TestCase):
    """Two installs (distinct AGENTBOARD_CORE_DIR values) sharing a parent
    directory -- e.g. /p/opt/a and /p/opt/b, or the default core next to a
    dev install named "dev" -- must be fully independent: each install's
    versions live only in its own <AGENTBOARD_CORE_DIR>.d store, never in
    anything shared or glob-matched, so uninstalling one can never touch
    the other's live version."""

    def test_uninstalling_one_install_leaves_the_other_fully_working(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-two-installs-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()

        opt_dir = tmp / "opt"
        opt_dir.mkdir()
        core_a = opt_dir / "a"
        core_b = opt_dir / "b"

        base_env = _python3_stub_env(tmp)
        base_env["HOME"] = str(fake_home)
        base_env.pop("AGENTBOARD_HOME", None)

        env_a = dict(base_env)
        env_a["AGENTBOARD_CORE_DIR"] = str(core_a)
        env_a["AGENTBOARD_HOME"] = str(tmp / "data-a")
        env_b = dict(base_env)
        env_b["AGENTBOARD_CORE_DIR"] = str(core_b)
        env_b["AGENTBOARD_HOME"] = str(tmp / "data-b")

        for env in (env_a, env_b):
            result = subprocess.run(
                [str(INSTALL_SH), "--no-app", "--no-hooks"],
                capture_output=True, text=True, env=env, timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        self.assertTrue(core_a.is_symlink())
        self.assertTrue(core_b.is_symlink())
        b_version_before = core_b.resolve()
        b_bin_before = (core_b / "bin" / "agentboard").read_bytes()

        uninstall_a = subprocess.run(
            [str(UNINSTALL_SH), "--no-app", "--no-hooks"],
            capture_output=True, text=True, env=env_a, timeout=120,
        )
        self.assertEqual(uninstall_a.returncode, 0, uninstall_a.stdout + uninstall_a.stderr)

        # a: fully removed.
        self.assertFalse(core_a.exists())
        self.assertFalse(Path(str(core_a) + ".d").exists())

        # b: completely unaffected.
        self.assertTrue(core_b.is_symlink())
        self.assertEqual(core_b.resolve(), b_version_before)
        self.assertTrue((core_b / "bin" / "agentboard").is_file())
        self.assertEqual((core_b / "bin" / "agentboard").read_bytes(), b_bin_before)

        snapshot_result = subprocess.run(
            [str(core_b / "bin" / "agentboard"), "snapshot", "--json"],
            capture_output=True, text=True, env=env_b, timeout=30,
        )
        self.assertEqual(snapshot_result.returncode, 0, snapshot_result.stdout + snapshot_result.stderr)


@unittest.skipUnless(sys.platform == "darwin", "uninstall.sh is macOS-only")
class UninstallRefusesForeignCoreSymlinkTests(unittest.TestCase):
    """If `core` is a symlink uninstall.sh didn't create (no ownership
    marker at its target), it must refuse to touch it -- never rm -rf a
    developer's own symlink to a live checkout, say."""

    def test_foreign_symlink_target_is_left_untouched(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-foreign-symlink-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()

        checkout_core = tmp / "mycheckout" / "core"
        checkout_core.mkdir(parents=True)
        (checkout_core / "uncommitted.txt").write_text("precious uncommitted work")

        installed_core_dir = fake_home / ".local" / "share" / "agentboard" / "core"
        installed_core_dir.parent.mkdir(parents=True)
        installed_core_dir.symlink_to(checkout_core)

        env = _python3_stub_env(tmp)
        env["HOME"] = str(fake_home)
        env.pop("AGENTBOARD_CORE_DIR", None)
        env.pop("AGENTBOARD_HOME", None)

        result = subprocess.run(
            [str(UNINSTALL_SH), "--no-app"], capture_output=True, text=True, env=env, timeout=120,
        )

        self.assertNotEqual(result.returncode, 0, "must surface the refusal")
        self.assertIn("did not create", result.stdout + result.stderr)
        self.assertTrue(installed_core_dir.is_symlink())
        self.assertEqual(installed_core_dir.resolve(), checkout_core.resolve())
        self.assertTrue((checkout_core / "uncommitted.txt").is_file())
        self.assertEqual(
            (checkout_core / "uncommitted.txt").read_text(), "precious uncommitted work",
        )


@unittest.skipUnless(sys.platform == "darwin", "install.sh is macOS-only")
class InstallRefusesForeignCoreSymlinkEndToEndTests(unittest.TestCase):
    """The round-4 finding at the full install.sh level: a foreign symlink
    to a dev checkout (with an uncommitted file) must make install.sh
    refuse outright, never touching the checkout."""

    def test_foreign_symlink_to_dev_checkout_survives_install(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-install-foreign-symlink-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()

        checkout_core = tmp / "mycheckout" / "core"
        checkout_core.mkdir(parents=True)
        (checkout_core / "uncommitted.py").write_text("precious uncommitted work")

        installed_core_dir = fake_home / ".local" / "share" / "agentboard" / "core"
        installed_core_dir.parent.mkdir(parents=True)
        installed_core_dir.symlink_to(checkout_core)

        env = _python3_stub_env(tmp)
        env["HOME"] = str(fake_home)
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR"):
            env.pop(var, None)

        result = subprocess.run(
            [str(INSTALL_SH), "--no-app"], capture_output=True, text=True, env=env, timeout=120,
        )

        self.assertNotEqual(result.returncode, 0, "must surface the refusal")
        self.assertIn("did not create", result.stdout + result.stderr)
        self.assertTrue(installed_core_dir.is_symlink())
        self.assertEqual(installed_core_dir.resolve(), checkout_core.resolve())
        self.assertTrue((checkout_core / "uncommitted.py").is_file())
        self.assertEqual((checkout_core / "uncommitted.py").read_text(), "precious uncommitted work")
        # Nothing was staged anywhere either.
        self.assertFalse((fake_home / ".local" / "share" / "agentboard" / "core.d").exists())


@unittest.skipUnless(sys.platform == "darwin", "install.sh is macOS-only")
class PreReleaseLayoutsAreRefusedNotMigratedEndToEndTests(unittest.TestCase):
    """Decision: drop all migration from pre-release layouts -- they were
    never published. install.sh supports exactly two states at
    $INSTALLED_CORE_DIR: nothing, or our own symlink into our own marked
    store. A real directory from the old pre-versioning layout, and a
    symlink into the old round-2/3 loose-sibling layout, must both be
    refused outright end to end, with the existing content untouched and
    nothing staged -- never migrated."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentboard-no-migration-e2e-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.fake_home = self.tmp / "home"
        self.fake_home.mkdir()
        (self.fake_home / ".claude").mkdir()
        self.env = _python3_stub_env(self.tmp)
        self.env["HOME"] = str(self.fake_home)
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR"):
            self.env.pop(var, None)
        self.installed_core_dir = self.fake_home / ".local" / "share" / "agentboard" / "core"
        self.installed_core_dir.parent.mkdir(parents=True)

    def _assert_refused_and_untouched(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(
            "move it aside, or set AGENTBOARD_CORE_DIR elsewhere", result.stdout + result.stderr,
        )
        self.assertFalse(
            (self.fake_home / ".local" / "share" / "agentboard" / "core.d").exists(),
            "nothing must be staged either",
        )

    def test_refuses_pre_versioning_real_directory(self):
        # The very first (round 1) layout: core/ copied straight in, no
        # versioning, no marker at all.
        (self.installed_core_dir / "bin").mkdir(parents=True)
        (self.installed_core_dir / "bin" / "agentboard").write_text("#!/bin/sh\n")
        (self.installed_core_dir / "bin" / "agentboard").chmod(0o755)
        (self.installed_core_dir / "agentboard").mkdir()
        (self.installed_core_dir / "agentboard" / "cli.py").write_text("")
        (self.installed_core_dir / "agentboard" / "uncommitted.py").write_text("precious")

        result = subprocess.run(
            [str(INSTALL_SH), "--no-app", "--no-hooks"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self._assert_refused_and_untouched(result)
        self.assertFalse(self.installed_core_dir.is_symlink())
        self.assertTrue((self.installed_core_dir / "agentboard" / "uncommitted.py").is_file())
        self.assertEqual(
            (self.installed_core_dir / "agentboard" / "uncommitted.py").read_text(), "precious",
        )

    def test_refuses_round2_round3_loose_sibling_layout(self):
        # The round-2/3 layout: a versioned sibling named core-<ts>-<id>,
        # carrying the per-version marker, living loose next to `core`.
        loose_version = self.installed_core_dir.parent / "core-20250101T000000Z-abc123"
        loose_version.mkdir()
        (loose_version / ".agentboard-core-version").write_text("")
        (loose_version / "marker-of-mine.txt").write_text("don't touch")
        self.installed_core_dir.symlink_to(loose_version)

        result = subprocess.run(
            [str(INSTALL_SH), "--no-app", "--no-hooks"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self._assert_refused_and_untouched(result)
        self.assertTrue(self.installed_core_dir.is_symlink())
        self.assertEqual(self.installed_core_dir.resolve(), loose_version)
        self.assertTrue(loose_version.exists(), "the old loose version must survive, never removed")
        self.assertTrue((loose_version / "marker-of-mine.txt").is_file())


class RmGoesThroughTheSingleGuardAuditTests(unittest.TestCase):
    """Every `rm` in install.sh/uninstall.sh must either live inside
    safe_remove_in_store() (the one guarded deletion primitive) or be one
    of a short, explicitly reviewed list of unrelated temp-file/resource
    cleanups that can't go through it (it only guards $CORE_STORE_DIR):

    install.sh:
      - `rm -f "$TMP_LINK_PATH"` -- our own not-yet-swapped temp symlink
        name (mktemp -u'd moments earlier; never anything else's).
      - the `find ... -exec rm -rf {} +` pycache cleanup -- inside a
        staging dir we just created this instant, before it's linked
        anywhere.
      - `rm -rf "$TARGET_BUNDLE"` -- the app bundle, gated immediately
        above by _app_bundle_refusal_reason() (a real directory whose
        CFBundleIdentifier == io.github.agentboard, read via `defaults
        read` -- never written) as well as the exact --prefix +
        "AgentBoard.app" path and quit_running_app_if_same_prefix; not
        the core-store invariant.

    uninstall.sh:
      - `rm -rf "$APP_BUNDLE"` -- same app-bundle reasoning as above.
      - `rm -f "$INSTALLED_CORE_DIR"` -- unlinking the symlink dirent
        itself (never its target) once the branch above has already
        confirmed it points into our own marked store.
      - `rm -f "$dir/$f"` (safe_purge_data_home) -- one of the specific,
        named AgentBoard-created files (AGENTBOARD_DATA_FILES), deleted
        individually only after _purge_target_is_safe() has confirmed
        the directory is a real directory, is neither "/" nor $HOME, and
        contains nothing else.
      - `rm -f "$entry"` (_purge_known_glob) -- only an entry matching
        the exact glob (inbox/*.json, reparse-locks/*.marker or usage/*.json)
        AgentBoard itself ever creates in that one named dir; the dir
        itself is only ever rmdir'd (never rm -rf), and only once left
        empty.
    """

    GUARD_FUNCTION = "safe_remove_in_store"

    # (?<!\w) / (?!\w) so this never matches "rm" as a substring of a
    # longer identifier (normalize, confirm, safe_remove_in_store, ...).
    RM_INVOCATION = re.compile(r'(?<!\w)rm(?:\s+-\S+)*\s+\S')

    INSTALL_SH_EXCEPTIONS = {
        'rm -f "$TMP_LINK_PATH"',
        'find "$staging_dir" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true',
        'rm -rf "$TARGET_BUNDLE"',
    }
    UNINSTALL_SH_EXCEPTIONS = {
        'rm -rf "$APP_BUNDLE"',
        'rm -f "$INSTALLED_CORE_DIR"',
        'rm -f "$dir/$f"',
        'rm -f "$entry"',
    }

    def _guard_function_line_range(self, lines):
        start = end = None
        for i, line in enumerate(lines):
            if line.startswith(f"{self.GUARD_FUNCTION}() {{"):
                start = i
            elif start is not None and line == "}":
                end = i
                break
        self.assertIsNotNone(start, f"{self.GUARD_FUNCTION}() not found")
        self.assertIsNotNone(end, f"{self.GUARD_FUNCTION}() has no closing brace")
        return start, end

    def _rm_lines_outside_guard(self, path):
        lines = path.read_text().splitlines()
        guard_start, guard_end = self._guard_function_line_range(lines)
        found = []
        for i, line in enumerate(lines):
            if guard_start <= i <= guard_end:
                continue
            if line.strip().startswith("#"):
                continue
            if self.RM_INVOCATION.search(line):
                found.append(line.strip())
        return found

    def test_install_sh_every_rm_is_guarded_or_an_explicit_exception(self):
        found = self._rm_lines_outside_guard(INSTALL_SH)
        self.assertTrue(found, "sanity check: expected to find the known exceptions")
        unexpected = [line for line in found if line not in self.INSTALL_SH_EXCEPTIONS]
        self.assertEqual(unexpected, [], f"unreviewed rm outside {self.GUARD_FUNCTION}(): {unexpected}")

    def test_uninstall_sh_every_rm_is_guarded_or_an_explicit_exception(self):
        found = self._rm_lines_outside_guard(UNINSTALL_SH)
        self.assertTrue(found, "sanity check: expected to find the known exceptions")
        unexpected = [line for line in found if line not in self.UNINSTALL_SH_EXCEPTIONS]
        self.assertEqual(unexpected, [], f"unreviewed rm outside {self.GUARD_FUNCTION}(): {unexpected}")

    def test_both_scripts_define_the_single_guard_function(self):
        for path in (INSTALL_SH, UNINSTALL_SH):
            text = path.read_text()
            self.assertIn(f"{self.GUARD_FUNCTION}() {{", text, path)


@unittest.skipUnless(sys.platform == "darwin", "install.sh is macOS-only")
class ForeignStoreNeverPrunedByNonInstallPathsTests(unittest.TestCase):
    """The round-5 High finding: a foreign core.d (one install.sh never
    created) must survive --dry-run, --help, an unknown argument, and a
    refusal -- none of these may ever call install_core_copy, so the EXIT
    trap (armed only inside it, after the store is validated/created)
    must never even exist for them, let alone prune anything."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agentboard-foreign-store-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))
        self.fake_home = self.tmp / "home"
        self.fake_home.mkdir()
        (self.fake_home / ".claude").mkdir()
        self.env = _python3_stub_env(self.tmp)
        self.env["HOME"] = str(self.fake_home)
        self.proj = self.fake_home / "proj"
        self.proj_store = self.fake_home / "proj.d"
        (self.proj_store / "src").mkdir(parents=True)
        (self.proj_store / "src" / "main.c").write_text("code")
        self.env["AGENTBOARD_CORE_DIR"] = str(self.proj)
        self.env.pop("AGENTBOARD_HOME", None)

    def _assert_store_untouched(self):
        self.assertTrue(self.proj_store.is_dir())
        self.assertTrue((self.proj_store / "src" / "main.c").is_file())
        self.assertEqual((self.proj_store / "src" / "main.c").read_text(), "code")

    def test_dry_run_leaves_foreign_store_untouched(self):
        result = subprocess.run(
            [str(INSTALL_SH), "--no-app", "--dry-run"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._assert_store_untouched()

    def test_help_leaves_foreign_store_untouched(self):
        result = subprocess.run(
            [str(INSTALL_SH), "--help"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self._assert_store_untouched()

    def test_unknown_argument_leaves_foreign_store_untouched(self):
        result = subprocess.run(
            [str(INSTALL_SH), "--this-is-not-a-real-flag"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self.assertNotEqual(result.returncode, 0)
        self._assert_store_untouched()

    def test_refusal_leaves_foreign_store_untouched(self):
        # $INSTALLED_CORE_DIR itself is also foreign here (a real
        # directory that doesn't look like an AgentBoard install), so
        # install_core_copy refuses before ever reaching the store.
        (self.proj).mkdir(parents=True)
        (self.proj / "unrelated.txt").write_text("not an agentboard install")

        result = subprocess.run(
            [str(INSTALL_SH), "--no-app"],
            capture_output=True, text=True, env=self.env, timeout=120,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("refusing to touch it", result.stdout + result.stderr)
        self._assert_store_untouched()
        self.assertTrue((self.proj / "unrelated.txt").is_file())


@unittest.skipUnless(sys.platform == "darwin", "install.sh is macOS-only")
class InstallRefusesSymlinkedCoreStoreTests(unittest.TestCase):
    """If $CORE_STORE_DIR (core.d) itself is a symlink -- not install.sh's
    own real, marked directory -- install must refuse before mkdir -p /
    writing the marker into whatever it points at, per the round-5 fix:
    'install should also refuse an existing unmarked or symlinked
    $CORE_STORE_DIR before mkdir -p/writing the marker into it.'"""

    def test_symlinked_store_is_refused_and_elsewhere_is_untouched(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-symlinked-store-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()

        elsewhere = tmp / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "precious.txt").write_text("do not touch")

        installed_core_dir = fake_home / ".local" / "share" / "agentboard" / "core"
        store_dir = Path(str(installed_core_dir) + ".d")
        store_dir.parent.mkdir(parents=True)
        store_dir.symlink_to(elsewhere)

        env = _python3_stub_env(tmp)
        env["HOME"] = str(fake_home)
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR"):
            env.pop(var, None)

        result = subprocess.run(
            [str(INSTALL_SH), "--no-app"], capture_output=True, text=True, env=env, timeout=120,
        )

        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(installed_core_dir.exists(), "nothing should have been linked either")
        self.assertTrue(store_dir.is_symlink())
        self.assertEqual(store_dir.resolve(), elsewhere.resolve())
        self.assertTrue((elsewhere / "precious.txt").is_file())
        self.assertEqual((elsewhere / "precious.txt").read_text(), "do not touch")


@unittest.skipUnless(sys.platform == "darwin", "uninstall.sh is macOS-only")
class UninstallRefusesForeignRealDirectoryTests(unittest.TestCase):
    """uninstall.sh supports exactly the same two states install.sh does:
    missing, or our own symlink -- any real directory at
    $AGENTBOARD_CORE_DIR must be refused outright, never removed, both at
    a custom path and at the default."""

    def _assert_refused(self, env, target_dir):
        result = subprocess.run(
            [str(UNINSTALL_SH), "--no-app"], capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertNotEqual(result.returncode, 0, "must surface the refusal")
        self.assertIn("leaving it in place", result.stdout + result.stderr)
        self.assertIn("move it aside, or set AGENTBOARD_CORE_DIR elsewhere", result.stdout + result.stderr)
        self.assertTrue(target_dir.is_dir())
        self.assertTrue((target_dir / "src" / "a.c").is_file())
        self.assertEqual((target_dir / "src" / "a.c").read_text(), "code")

    def test_foreign_real_directory_at_custom_path_is_refused(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-foreign-realdir-custom-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()

        proj = tmp / "work" / "proj"
        (proj / "src").mkdir(parents=True)
        (proj / "src" / "a.c").write_text("code")

        env = _python3_stub_env(tmp)
        env["HOME"] = str(fake_home)
        env["AGENTBOARD_CORE_DIR"] = str(proj)
        env.pop("AGENTBOARD_HOME", None)

        self._assert_refused(env, proj)

    def test_foreign_real_directory_at_default_path_is_refused(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-foreign-realdir-default-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()

        default_core = fake_home / ".local" / "share" / "agentboard" / "core"
        (default_core / "src").mkdir(parents=True)
        (default_core / "src" / "a.c").write_text("code")

        env = _python3_stub_env(tmp)
        env["HOME"] = str(fake_home)
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR"):
            env.pop(var, None)

        self._assert_refused(env, default_core)


@unittest.skipUnless(sys.platform == "darwin", "install.sh is macOS-only")
class AgentBoardCoreDirNormalizationEndToEndTests(unittest.TestCase):
    """A relative or trailing-slash AGENTBOARD_CORE_DIR must be normalized
    the same way --prefix is (absolute, trailing slash stripped) before
    INSTALLED_BIN/CORE_STORE_DIR are derived from it -- otherwise the
    symlink target and the store glob disagree and prune_core_versions()
    deletes the version it just installed. Each case must still end in a
    working install with hooks registered and a working hook."""

    def _run_and_assert_working(self, env, expected_core_dir):
        result = subprocess.run(
            [str(INSTALL_SH), "--no-app"], capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        self.assertTrue(expected_core_dir.is_symlink())
        self.assertTrue((expected_core_dir / "bin" / "agentboard").is_file())

        settings_path = Path(env["HOME"]) / ".claude" / "settings.json"
        self.assertTrue(settings_path.is_file())
        settings = json.loads(settings_path.read_text())
        stop_commands = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
        installed_bin = expected_core_dir / "bin" / "agentboard"
        self.assertTrue(
            any(c == f"{installed_bin} hook --tool claude" for c in stop_commands), stop_commands,
        )

        hook_result = subprocess.run(
            [str(installed_bin), "hook", "--tool", "claude"],
            input=json.dumps({
                "session_id": "normalize-s1", "cwd": str(expected_core_dir), "hook_event_name": "SessionStart",
            }),
            capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertEqual(hook_result.returncode, 0, hook_result.stdout + hook_result.stderr)

    def test_relative_agentboard_core_dir(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-core-dir-relative-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()
        workdir = tmp / "somewhere"
        workdir.mkdir()

        env = _python3_stub_env(tmp)
        env["HOME"] = str(fake_home)
        env["AGENTBOARD_CORE_DIR"] = "rel/core"
        env.pop("AGENTBOARD_HOME", None)

        result = subprocess.run(
            [str(INSTALL_SH), "--no-app"],
            capture_output=True, text=True, env=env, timeout=120, cwd=str(workdir),
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        expected_core_dir = workdir / "rel" / "core"
        self.assertTrue(expected_core_dir.is_symlink())
        self.assertTrue((expected_core_dir / "bin" / "agentboard").is_file())
        settings_path = fake_home / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        stop_commands = [h["command"] for g in settings["hooks"]["Stop"] for h in g["hooks"]]
        installed_bin = expected_core_dir / "bin" / "agentboard"
        self.assertTrue(
            any(c == f"{installed_bin} hook --tool claude" for c in stop_commands), stop_commands,
        )
        hook_result = subprocess.run(
            [str(installed_bin), "hook", "--tool", "claude"],
            input=json.dumps({
                "session_id": "normalize-rel-s1", "cwd": str(workdir), "hook_event_name": "SessionStart",
            }),
            capture_output=True, text=True, env=env, timeout=30,
        )
        self.assertEqual(hook_result.returncode, 0, hook_result.stdout + hook_result.stderr)

    def test_trailing_slash_agentboard_core_dir(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-core-dir-trailing-slash-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()

        env = _python3_stub_env(tmp)
        env["HOME"] = str(fake_home)
        env["AGENTBOARD_CORE_DIR"] = str(fake_home / "core") + "/"
        env.pop("AGENTBOARD_HOME", None)

        self._run_and_assert_working(env, fake_home / "core")

    def test_dotdot_through_a_not_yet_existing_dir_survives_reinstall_and_uninstall(self):
        # Round-6 open gap: a ".." through a directory that doesn't exist
        # yet (so cd+pwd -P can't resolve it) must still be lexically
        # collapsed, not left in the string -- otherwise a *second* run
        # (after the first run's own mkdir -p makes "nope" exist) resolves
        # to a different-looking path and refuses the install it just
        # made.
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-core-dir-dotdot-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()

        env = _python3_stub_env(tmp)
        env["HOME"] = str(fake_home)
        env["AGENTBOARD_CORE_DIR"] = f"{fake_home}/nope/../c2"
        env.pop("AGENTBOARD_HOME", None)

        expected_core_dir = fake_home / "c2"
        first = subprocess.run(
            [str(INSTALL_SH), "--no-app"], capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        self.assertTrue(expected_core_dir.is_symlink())
        first_version = expected_core_dir.resolve()

        second = subprocess.run(
            [str(INSTALL_SH), "--no-app"], capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertTrue(expected_core_dir.is_symlink())
        self.assertNotEqual(
            expected_core_dir.resolve(), first_version, "the second run must see this as a normal upgrade",
        )

        uninstall_result = subprocess.run(
            [str(UNINSTALL_SH), "--no-app"], capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(uninstall_result.returncode, 0, uninstall_result.stdout + uninstall_result.stderr)
        self.assertFalse(expected_core_dir.exists())
        self.assertFalse(Path(str(expected_core_dir) + ".d").exists())


def _fake_plist(bundle, bundle_id):
    """Writes a minimal Contents/Info.plist under bundle with the given
    CFBundleIdentifier, without needing a real app build."""
    contents = bundle / "Contents"
    contents.mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n<dict>\n'
        "    <key>CFBundleIdentifier</key>\n"
        f"    <string>{bundle_id}</string>\n"
        "</dict>\n</plist>\n"
    )


class BundleIdentifierMatchesTests(unittest.TestCase):
    """_bundle_identifier_matches() -- shared by install.sh and
    uninstall.sh -- must say yes only for io.github.agentboard, reading
    the plist with `defaults read` (never writing anything), and say no
    (not error) for a missing key, a missing plist, or a missing bundle
    entirely."""

    def _run(self, bundle):
        tmp = bundle.parent
        func_file = tmp / "check.sh"
        func_file.write_text(
            "set -euo pipefail\n"
            'AGENTBOARD_BUNDLE_ID="io.github.agentboard"\n'
            + _extract_function("_bundle_identifier_matches") + "\n"
            f'if _bundle_identifier_matches "{bundle}"; then echo MATCH; else echo NOMATCH; fi\n'
        )
        result = subprocess.run(["/bin/bash", str(func_file)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def test_matching_bundle_identifier(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-bundle-id-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        bundle = tmp / "AgentBoard.app"
        _fake_plist(bundle, "io.github.agentboard")
        self.assertEqual(self._run(bundle), "MATCH")

    def test_foreign_bundle_identifier(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-bundle-id-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        bundle = tmp / "AgentBoard.app"
        _fake_plist(bundle, "com.example.notagentboard")
        self.assertEqual(self._run(bundle), "NOMATCH")

    def test_missing_bundle(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-bundle-id-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        self.assertEqual(self._run(tmp / "NoSuchApp.app"), "NOMATCH")


class AppBundleRefusalReasonTests(unittest.TestCase):
    """_app_bundle_refusal_reason() -- shared by install.sh and (via its
    own duplicate) build-app.sh -- round-6 finding 5: anything at the
    target path that isn't a real, non-symlink directory (a plain file,
    a symlink to a real bundle elsewhere, or a dangling symlink) must be
    refused, exactly like a real directory with the wrong
    CFBundleIdentifier. Prints nothing (safe to replace) only when the
    path is missing entirely, or is already our own matching bundle."""

    def _run(self, bundle):
        tmp = bundle.parent
        func_file = tmp / "check.sh"
        func_file.write_text(
            "set -euo pipefail\n"
            'AGENTBOARD_BUNDLE_ID="io.github.agentboard"\n'
            + _extract_function("_bundle_identifier_matches") + "\n"
            + _extract_function("_app_bundle_refusal_reason") + "\n"
            f'_app_bundle_refusal_reason "{bundle}"\n'
        )
        result = subprocess.run(["/bin/bash", str(func_file)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.strip()

    def test_missing_bundle_is_not_refused(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-app-refusal-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        self.assertEqual(self._run(tmp / "NoSuchApp.app"), "")

    def test_matching_bundle_is_not_refused(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-app-refusal-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        bundle = tmp / "AgentBoard.app"
        _fake_plist(bundle, "io.github.agentboard")
        self.assertEqual(self._run(bundle), "")

    def test_foreign_directory_is_refused(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-app-refusal-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        bundle = tmp / "AgentBoard.app"
        _fake_plist(bundle, "com.example.notagentboard")
        self.assertIn("doesn't look like AgentBoard", self._run(bundle))

    def test_plain_file_is_refused(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-app-refusal-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        bundle = tmp / "AgentBoard.app"
        bundle.write_text("precious")
        self.assertIn("is a file", self._run(bundle))

    def test_symlink_to_a_real_directory_elsewhere_is_refused(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-app-refusal-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        elsewhere = tmp / "elsewhere.app"
        elsewhere.mkdir()
        bundle = tmp / "AgentBoard.app"
        bundle.symlink_to(elsewhere)
        self.assertIn("is a symlink", self._run(bundle))

    def test_dangling_symlink_is_refused(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-app-refusal-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        bundle = tmp / "AgentBoard.app"
        bundle.symlink_to(tmp / "does-not-exist.app")
        self.assertIn("is a symlink", self._run(bundle))


@unittest.skipUnless(sys.platform == "darwin", "uninstall.sh is macOS-only")
class UninstallLeavesForeignAppBundleAloneTests(unittest.TestCase):
    """uninstall.sh must refuse to touch a same-named AgentBoard.app whose
    CFBundleIdentifier isn't io.github.agentboard."""

    def test_foreign_bundle_survives_uninstall(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-uninstall-foreign-app-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()
        apps_dir = tmp / "apps"
        apps_dir.mkdir()
        bundle = apps_dir / "AgentBoard.app"
        _fake_plist(bundle, "com.example.notagentboard")
        (bundle / "Contents" / "marker.txt").write_text("not agentboard")

        env = _python3_stub_env(tmp)
        env["HOME"] = str(fake_home)
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR"):
            env.pop(var, None)

        result = subprocess.run(
            [str(UNINSTALL_SH), "--no-hooks", "--prefix", str(apps_dir)],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertNotEqual(result.returncode, 0, "the refusal must be surfaced")
        self.assertIn("doesn't look like AgentBoard", result.stdout + result.stderr)
        self.assertTrue(bundle.is_dir())
        self.assertTrue((bundle / "Contents" / "marker.txt").is_file())
        self.assertEqual((bundle / "Contents" / "marker.txt").read_text(), "not agentboard")


@unittest.skipUnless(sys.platform == "darwin", "install.sh is macOS-only")
class InstallLeavesForeignAppBundleAloneTests(unittest.TestCase):
    """install.sh must refuse to overwrite (or even quit) a same-named
    AgentBoard.app whose CFBundleIdentifier isn't io.github.agentboard --
    the one test here that needs a real `swift build` (via build-app.sh),
    bounded by a generous timeout; empirically well under a minute. On a
    toolchain whose default SDK can't expand SwiftUI macros, build-app.sh's
    SDK fallback (pick_fallback_sdkroot) is what lets that build succeed;
    its probe adds about ten seconds on a cold module cache."""

    def test_foreign_bundle_survives_install(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-install-foreign-app-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()
        apps_dir = tmp / "apps"
        apps_dir.mkdir()
        bundle = apps_dir / "AgentBoard.app"
        _fake_plist(bundle, "com.example.notagentboard")
        (bundle / "Contents" / "marker.txt").write_text("not agentboard")

        bindir = tmp / "guard-bin"
        bindir.mkdir()
        for cmd in ("open", "osascript", "pkill", "kill"):
            _write_executable(bindir / cmd, f'echo "GUARD TRIPPED: {cmd} ($*)" >&2\nexit 99')

        env = _python3_stub_env(tmp)
        env["HOME"] = str(fake_home)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR"):
            env.pop(var, None)

        result = subprocess.run(
            [str(INSTALL_SH), "--no-hooks", "--prefix", str(apps_dir)],
            capture_output=True, text=True, env=env, timeout=300,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("doesn't look like AgentBoard", result.stdout + result.stderr)
        self.assertTrue(bundle.is_dir())
        self.assertTrue((bundle / "Contents" / "marker.txt").is_file())
        self.assertEqual((bundle / "Contents" / "marker.txt").read_text(), "not agentboard")

    def test_a_file_at_the_target_path_survives_install(self):
        # Round-6 finding 5 exact repro: a plain file (not a directory)
        # at $PREFIX/AgentBoard.app -- the old `[[ -d ... ]] &&` guard
        # skipped the refusal entirely for anything that wasn't a
        # directory, so this used to get `rm -rf`'d and replaced.
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-install-foreign-file-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()
        apps_dir = tmp / "apps"
        apps_dir.mkdir()
        bundle = apps_dir / "AgentBoard.app"
        bundle.write_text("precious")

        bindir = tmp / "guard-bin"
        bindir.mkdir()
        for cmd in ("open", "osascript", "pkill", "kill"):
            _write_executable(bindir / cmd, f'echo "GUARD TRIPPED: {cmd} ($*)" >&2\nexit 99')

        env = _python3_stub_env(tmp)
        env["HOME"] = str(fake_home)
        env["PATH"] = f"{bindir}:{env['PATH']}"
        for var in ("AGENTBOARD_HOME", "AGENTBOARD_CORE_DIR"):
            env.pop(var, None)

        result = subprocess.run(
            [str(INSTALL_SH), "--no-hooks", "--prefix", str(apps_dir)],
            capture_output=True, text=True, env=env, timeout=300,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("is a file", result.stdout + result.stderr)
        self.assertFalse(bundle.is_dir())
        self.assertTrue(bundle.is_file())
        self.assertEqual(bundle.read_text(), "precious")


@unittest.skipUnless(sys.platform == "darwin", "build-app.sh is macOS-only")
class BuildAppRefusesForeignBundleEndToEndTests(unittest.TestCase):
    """Round-6 finding 6: build-app.sh's standalone install step (without
    --no-install) must apply the same bundle-ID check and
    not-a-real-directory refusal install.sh and uninstall.sh do, before
    replacing an existing bundle. Needs a real `swift build` (with the SDK
    fallback where the toolchain needs it), same as
    InstallLeavesForeignAppBundleAloneTests above."""

    def test_foreign_bundle_survives_standalone_install(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-buildapp-foreign-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        prefix = tmp / "apps"
        prefix.mkdir()
        bundle = prefix / "AgentBoard.app"
        _fake_plist(bundle, "com.other.app")
        (bundle / "Contents" / "data.txt").write_text("a data file")

        env = _python3_stub_env(tmp)

        result = subprocess.run(
            [str(BUILD_APP_SH), "--prefix", str(prefix)],
            capture_output=True, text=True, env=env, timeout=300,
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("doesn't look like AgentBoard", result.stdout + result.stderr)
        self.assertTrue(bundle.is_dir())
        self.assertTrue((bundle / "Contents" / "data.txt").is_file())
        self.assertEqual((bundle / "Contents" / "data.txt").read_text(), "a data file")


@unittest.skipUnless(sys.platform == "darwin", "uninstall.sh is macOS-only")
class PurgeDataSafetyTests(unittest.TestCase):
    """--purge-data must never rm -rf a whole directory the user pointed
    AGENTBOARD_HOME at -- only the specific files/dirs AgentBoard itself
    creates, individually, and only when the directory is a real
    directory containing nothing else (and isn't "/" or $HOME), then
    rmdir (never rm -rf) if that leaves it empty."""

    def _env(self, tmp, agentboard_home):
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()
        env = _python3_stub_env(tmp)
        env["HOME"] = str(fake_home)
        env["AGENTBOARD_HOME"] = str(agentboard_home)
        env.pop("AGENTBOARD_CORE_DIR", None)
        return env

    def test_purge_removes_only_known_files_and_the_now_empty_directory(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-purge-happy-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        data_home = tmp / "data"
        data_home.mkdir()
        (data_home / "agentboard.db").write_text("db")
        (data_home / "agentboard.db-wal").write_text("wal")
        (data_home / "agentboard.db-shm").write_text("shm")
        (data_home / "hook-errors.log").write_text("log")
        (data_home / "agentboard.log").write_text("diag log")
        (data_home / "agentboard.log.1").write_text("rotated diag log")
        (data_home / "install-manifest.json").write_text("{}")
        (data_home / "config.json").write_text("{}")
        (data_home / "inbox").mkdir()
        (data_home / "inbox" / "x.json").write_text("{}")
        (data_home / "reparse-locks").mkdir()
        (data_home / "reparse-locks" / "claude-s1.marker").write_text("")
        (data_home / "usage").mkdir()
        (data_home / "usage" / "codex.json").write_text("{}")

        env = self._env(tmp, data_home)
        result = subprocess.run(
            [str(UNINSTALL_SH), "--no-app", "--no-hooks", "--purge-data"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(data_home.exists(), "the now-empty data dir must be removed too")

    def test_purge_data_files_list_names_the_diag_log_and_its_rotation(self):
        # diag.py owns these names; uninstall.sh must list both or
        # --purge-data refuses a data dir that has ever been logged to.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from agentboard import diag

        text = UNINSTALL_SH.read_text()
        start = text.index("AGENTBOARD_DATA_FILES=(")
        listed = text[start:text.index(")", start)].split("(", 1)[1].split()
        self.assertIn(diag.LOG_FILENAME, listed)
        self.assertIn(diag.ROTATED_FILENAME, listed)

    def test_purge_leaves_a_preexisting_non_pattern_file_in_inbox_and_reparse_locks_alone(self):
        # Round-6 finding 4: inbox/ and reparse-locks/ must never be
        # rm -rf'd wholesale -- only entries matching the exact shapes
        # AgentBoard itself creates (links.py's ingest_inbox *.json,
        # hook.py's _reparse_marker_path *.marker) are removed. A user's
        # own file in either survives, and so does the directory (and the
        # data dir around it).
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-purge-user-file-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        data_home = tmp / "docs"
        data_home.mkdir()
        (data_home / "agentboard.db").write_text("db")
        (data_home / "inbox").mkdir()
        (data_home / "inbox" / "m1").write_text("the user's own file")
        (data_home / "inbox" / "drop.json").write_text("{}")
        (data_home / "reparse-locks").mkdir()
        (data_home / "reparse-locks" / "not-ours.txt").write_text("also the user's")
        (data_home / "reparse-locks" / "claude-s1.marker").write_text("")

        env = self._env(tmp, data_home)
        result = subprocess.run(
            [str(UNINSTALL_SH), "--no-app", "--no-hooks", "--purge-data"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertNotEqual(result.returncode, 0, "the refusal must be surfaced")
        # AgentBoard's own entries are gone...
        self.assertFalse((data_home / "inbox" / "drop.json").exists())
        self.assertFalse((data_home / "reparse-locks" / "claude-s1.marker").exists())
        self.assertFalse((data_home / "agentboard.db").exists())
        # ...but the user's own files, and everything around them, survive.
        self.assertTrue((data_home / "inbox" / "m1").is_file())
        self.assertEqual((data_home / "inbox" / "m1").read_text(), "the user's own file")
        self.assertTrue((data_home / "reparse-locks" / "not-ours.txt").is_file())
        self.assertTrue(data_home.is_dir())

    def test_purge_without_agentboard_db_and_with_unrelated_files_is_fully_refused(self):
        # No agentboard.db at all -- the "only known AgentBoard files"
        # branch is the only way to qualify, and an unrelated file there
        # fails it outright, so nothing is deleted, not even an attempt.
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-purge-unrelated-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        data_home = tmp / "data"
        data_home.mkdir()
        (data_home / "my-other-project.sqlite").write_text("unrelated")
        (data_home / "notes.txt").write_text("do not delete me")

        env = self._env(tmp, data_home)
        result = subprocess.run(
            [str(UNINSTALL_SH), "--no-app", "--no-hooks", "--purge-data"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertNotEqual(result.returncode, 0, "the refusal must be surfaced")
        self.assertIn("refusing to purge", result.stdout + result.stderr)
        self.assertTrue(data_home.is_dir())
        self.assertTrue((data_home / "my-other-project.sqlite").is_file())
        self.assertEqual((data_home / "my-other-project.sqlite").read_text(), "unrelated")
        self.assertTrue((data_home / "notes.txt").is_file())
        self.assertEqual((data_home / "notes.txt").read_text(), "do not delete me")

    def test_purge_with_agentboard_db_present_keeps_unrelated_files_and_the_dir(self):
        # agentboard.db present is enough to qualify on its own (per the
        # "contains agentboard.db OR only known files" rule), so the
        # known files are deleted individually -- but an unrelated file
        # elsewhere in the same directory still makes the final rmdir
        # fail, leaving the directory (and that unrelated file) in place.
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-purge-mixed-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        data_home = tmp / "data"
        data_home.mkdir()
        (data_home / "agentboard.db").write_text("db")
        (data_home / "my-other-project.sqlite").write_text("unrelated")
        (data_home / "notes.txt").write_text("do not delete me")

        env = self._env(tmp, data_home)
        result = subprocess.run(
            [str(UNINSTALL_SH), "--no-app", "--no-hooks", "--purge-data"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertNotEqual(result.returncode, 0, "the refusal must be surfaced")
        self.assertIn("not empty after removing known AgentBoard files", result.stdout + result.stderr)
        self.assertTrue(data_home.is_dir(), "the directory itself must survive (rmdir only if empty)")
        self.assertTrue((data_home / "my-other-project.sqlite").is_file())
        self.assertEqual((data_home / "my-other-project.sqlite").read_text(), "unrelated")
        self.assertTrue((data_home / "notes.txt").is_file())
        self.assertEqual((data_home / "notes.txt").read_text(), "do not delete me")

    def test_purge_with_agentboard_home_equal_to_home_is_refused(self):
        tmp = Path(tempfile.mkdtemp(prefix="agentboard-purge-is-home-test-")).resolve()
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
        fake_home = tmp / "home"
        fake_home.mkdir()
        (fake_home / ".claude").mkdir()
        (fake_home / "important-file.txt").write_text("do not delete me either")

        env = _python3_stub_env(tmp)
        env["HOME"] = str(fake_home)
        env["AGENTBOARD_HOME"] = str(fake_home)
        env.pop("AGENTBOARD_CORE_DIR", None)

        result = subprocess.run(
            [str(UNINSTALL_SH), "--no-app", "--no-hooks", "--purge-data"],
            capture_output=True, text=True, env=env, timeout=120,
        )
        self.assertNotEqual(result.returncode, 0, "the refusal must be surfaced")
        self.assertIn("refusing to purge", result.stdout + result.stderr)
        self.assertTrue(fake_home.is_dir())
        self.assertTrue((fake_home / "important-file.txt").is_file())
        self.assertTrue((fake_home / ".claude").is_dir(), "uninstall's own setup must survive too")

SWIFT_TEST_SH = SCRIPTS_DIR / "swift-test.sh"
_SDK_HELPER_SCRIPTS = (BUILD_APP_SH, SWIFT_TEST_SH)
_SDK_HELPERS = ("_swiftui_macro_probe", "pick_fallback_sdkroot")
_SDK_ENV_VARS = ("SDKROOT", "AGENTBOARD_SDKROOT", "AGENTBOARD_VERBOSE",
                 "AGENTBOARD_SDK_PROBE_TIMEOUT", "DEVELOPER_DIR")
_SWIFTUI_MACRO_ERROR = (
    "probe.swift:3:6: error: external macro implementation type "
    "'SwiftUIMacros.StateMacro' could not be found for macro 'State()'; "
    "plugin for module 'SwiftUIMacros' not found"
)
_PASSING_RUN = (
    "Building for debugging...\n"
    "Build complete!\n"
    "◇ Test run started.\n"
    "✔ Test ok() passed after 0.001 seconds.\n"
    "✔ Test run with 3 tests in 2 suites passed after 0.01 seconds.\n"
)
_FAILING_RUN = (
    "Building for debugging...\n"
    "✔ Test ok() passed after 0.001 seconds.\n"
    "✘ Test bad() recorded an issue at T.swift:4:21: Expectation failed: two() == 3\n"
    "✘ Test bad() failed after 0.001 seconds with 1 issue.\n"
    "✘ Test run with 2 tests in 1 suite failed after 0.001 seconds with 1 issue.\n"
)


class _FakeToolchain:
    """A throwaway developer dir plus stub xcrun/swiftc/swift/xcode-select/
    codesign/sw_vers on PATH, so the SDK-fallback and swift-test helpers can
    be exercised without the real toolchain. Layouts:

    - "clt": <tmp>/CommandLineTools with SDKs/ and usr/ (Command Line Tools);
    - "xcode": <tmp>/Xcode.app/Contents/Developer, SDKs under
      Platforms/MacOSX.platform/Developer/SDKs, compiler in the default
      toolchain.

    sdks are the SDK dir names to create, each with a VERSION file (read by
    the stub `xcrun --show-sdk-version`); aliases map a symlink name to one
    of them; default is the name `xcrun --show-sdk-path` reports. working
    and other_error list SDK names whose probe passes, or fails with a
    module-cache error instead of the SwiftUIMacros one. The stub swiftc
    logs each probe's SDKROOT ("<default>" when unset) to probe-calls."""

    def __init__(self, tmp, *, layout="clt", sdks=("MacOSX27.0.sdk", "MacOSX26.5.sdk"),
                 aliases=None, default="MacOSX.sdk", working=(), other_error=(),
                 testing_framework=False, plugin=None, sdks_dir=None):
        self.tmp = tmp
        self.state = tmp / "tc-state"
        self.state.mkdir()
        self.bin = tmp / "tc-bin"
        self.bin.mkdir()
        if layout == "clt":
            self.dev = tmp / "CommandLineTools"
            usr = self.dev / "usr"
            default_sdks_dir = self.dev / "SDKs"
        else:
            self.dev = tmp / "Xcode.app" / "Contents" / "Developer"
            usr = self.dev / "Toolchains" / "XcodeDefault.xctoolchain" / "usr"
            default_sdks_dir = self.dev / "Platforms" / "MacOSX.platform" / "Developer" / "SDKs"
        self.sdks_dir = sdks_dir or default_sdks_dir
        self.sdks_dir.mkdir(parents=True)
        for name in sdks:
            (self.sdks_dir / name).mkdir()
            version = name[len("MacOSX"):-len(".sdk")] or "27.0"
            (self.sdks_dir / name / "VERSION").write_text(version + "\n")
        if aliases is None:
            aliases = {"MacOSX.sdk": "MacOSX27.0.sdk", "MacOSX27.sdk": "MacOSX27.0.sdk",
                       "MacOSX26.sdk": "MacOSX26.5.sdk"}
        for alias, target in aliases.items():
            if (self.sdks_dir / target).exists():
                (self.sdks_dir / alias).symlink_to(target)
        (self.state / "default_sdk").write_text(str(self.sdks_dir / default) + "\n")
        (self.state / "dev_dir").write_text(str(self.dev) + "\n")
        (self.state / "working").write_text("".join(self.sdk(n) + "\n" for n in working))
        (self.state / "other_error").write_text("".join(self.sdk(n) + "\n" for n in other_error))
        if testing_framework:
            (self.dev / "Library" / "Developer" / "Frameworks" / "Testing.framework").mkdir(parents=True)
        self.plugins = usr / "lib" / "swift" / "host" / "plugins"
        self.plugins.mkdir(parents=True)
        if plugin == "testing":
            (self.plugins / "testing").mkdir()
            (self.plugins / "testing" / "libTestingMacros.dylib").write_text("")
        elif plugin == "default":
            (self.plugins / "libTestingMacros.dylib").write_text("")
        elif plugin == "both":
            (self.plugins / "testing").mkdir()
            (self.plugins / "testing" / "libTestingMacros.dylib").write_text("")
            (self.plugins / "libTestingMacros.dylib").write_text("")
        (usr / "bin").mkdir(parents=True)
        self.swiftc = usr / "bin" / "swiftc"
        self.tmpdir = tmp / "tmpdir"
        self.tmpdir.mkdir()
        self._write_stubs()

    def sdk(self, name):
        return os.path.realpath(self.sdks_dir / name)

    def _write_stubs(self):
        s = self.state
        _write_executable(self.swiftc, f'''S="{s}"
sdk="${{SDKROOT:-}}"
printf '%s\\n' "${{sdk:-<default>}}" >> "$S/probe-calls"
[[ -n "$sdk" ]] || sdk="$(cat "$S/default_sdk")"
real="$(cd -P -- "$sdk" 2>/dev/null && pwd -P)"
if [[ -f "$S/sleep" ]]; then exec sleep "$(cat "$S/sleep")"; fi
if grep -qxF -- "$real" "$S/working"; then exit 0; fi
if grep -qxF -- "$real" "$S/other_error"; then
  echo "<unknown>:0: error: unable to open output file '/m/ModuleCache/x.pcm': Operation not permitted" >&2
  exit 1
fi
echo "{_SWIFTUI_MACRO_ERROR}" >&2
exit 1''')
        _write_executable(self.bin / "xcrun", f'''S="{s}"
if [[ "$1" == "swiftc" ]]; then
  if [[ -f "$S/xcrun_swiftc_fail" ]]; then
    echo 'xcrun: error: unable to find utility "swiftc", not a developer tool or in PATH' >&2
    exit 69
  fi
  shift
  exec "{self.swiftc}" "$@"
fi
if [[ "$1" == "--find" && "$2" == "swiftc" ]]; then echo "{self.swiftc}"; exit 0; fi
if [[ "$1" == "--sdk" && "$2" == "macosx" && "$3" == "--show-sdk-path" ]]; then
  [[ ! -f "$S/xcrun_fail" ]] || exit 1
  cat "$S/default_sdk"
  exit 0
fi
if [[ "$1" == "--sdk" && "$3" == "--show-sdk-version" ]]; then
  [[ -f "$2/VERSION" ]] || exit 1
  cat "$2/VERSION"
  exit 0
fi
echo "xcrun stub: unexpected $*" >&2
exit 64''')
        _write_executable(self.bin / "xcode-select", f'[[ "$1" == "-p" ]] && cat "{s}/dev_dir"')
        _write_executable(self.bin / "swift", f'''S="{s}"
printf '%s\\n' "$@" > "$S/swift-argv"
printf '%s\\n' "${{SDKROOT-<unset>}}" > "$S/swift-sdkroot"
case "$1" in
  build)
    echo "Building for production..."
    if [[ -f "$S/swift_build_fail" ]]; then
      echo "/x/Foo.swift:1:1: error: something broke"
      echo "/x/Foo.swift:1:1: error: something broke"
      echo "error: Build failed"
      exit 1
    fi
    mkdir -p .build/release
    printf '#!/bin/bash\\n' > .build/release/AgentBoard
    chmod +x .build/release/AgentBoard
    echo "Build complete!"
    ;;
  test)
    [[ ! -f "$S/test-output" ]] || cat "$S/test-output"
    exit "$(cat "$S/test-rc" 2>/dev/null || echo 0)"
    ;;
esac''')
        _write_executable(self.bin / "codesign",
                          'echo "AgentBoard.app: replacing existing signature" >&2')
        _write_executable(self.bin / "sw_vers", f'touch "{s}/sw_vers-called"\nexit 99')
        # Every sleep started under this toolchain (the probe watchdog's, the
        # hanging swiftc's) carries this test's temp dir in its argv[0], so
        # leftover_processes() can find it after the caller has exited. Its
        # argument goes to sleep-calls, so a test can see the watchdog tick.
        _write_executable(self.bin / "sleep", f'printf "%s\\n" "$*" >> "{s}/sleep-calls"\n'
                          f'exec -a "{self.tmp}/marked-sleep" /bin/sleep "$@"')

    def flag(self, name, content=""):
        (self.state / name).write_text(content)

    def env(self, **extra):
        env = _python3_stub_env(self.tmp)
        for var in _SDK_ENV_VARS:
            env.pop(var, None)
        env["PATH"] = f"{self.bin}:{env['PATH']}"
        env["TMPDIR"] = str(self.tmpdir)
        env["HOME"] = str(self.tmp / "home")
        env.update(extra)
        return env

    def probe_calls(self):
        path = self.state / "probe-calls"
        return path.read_text().splitlines() if path.exists() else []

    def sleep_calls(self):
        path = self.state / "sleep-calls"
        return path.read_text().splitlines() if path.exists() else []

    def swift_argv(self):
        return (self.state / "swift-argv").read_text().splitlines()

    def swift_sdkroot(self):
        return (self.state / "swift-sdkroot").read_text().strip()

    def sw_vers_called(self):
        return (self.state / "sw_vers-called").exists()

    def leftover_probe_dirs(self):
        return sorted(p.name for p in self.tmpdir.glob("agentboard-sdk-probe.*"))

    def leftover_processes(self):
        """Live processes whose command line names this test's temp dir:
        the probe-loop script's subshells (the watchdog), the stub
        xcrun/swiftc, and every marked sleep."""
        r = subprocess.run(["pgrep", "-fl", re.escape(str(self.tmp))],
                           capture_output=True, text=True)
        return r.stdout.splitlines()

    def probe_loop_script(self, script, n, capture=True):
        """A /bin/bash script calling script's own _swiftui_macro_probe n
        times and printing one "rc<TAB>reason" line per call. With capture,
        each call goes through $(...) as the callers do (a watchdog holding
        that pipe would stall it); without, it runs in the script's own shell,
        where bash 3.2 reports a killed job ("Terminated: 15") on `wait`'s
        stderr, which a command substitution never does."""
        out = self.tmp / "probe-out"
        call = ('out="$(_swiftui_macro_probe "")" || rc=$?' if capture else
                f'_swiftui_macro_probe "" > "{out}" || rc=$?; out="$(cat "{out}")"')
        body = "set -euo pipefail\n" + _extract_function("_swiftui_macro_probe", source=script)
        body += (f'i=0\nwhile [[ "$i" -lt {int(n)} ]]; do\n'
                 f'  rc=0; {call}\n'
                 '  printf "%s\\t%s\\n" "$rc" "$out"\n'
                 '  i=$((i + 1))\ndone\n')
        func_file = self.tmp / "run-probe-loop.sh"
        func_file.write_text(body)
        return func_file

    def run_probe_loop(self, script, n, capture=True, **extra_env):
        func_file = self.probe_loop_script(script, n, capture)
        return subprocess.run(["/bin/bash", str(func_file)], capture_output=True, text=True,
                              env=self.env(**extra_env), cwd=self.tmp, timeout=120)

    def run_pick(self, script, cwd=None, **extra_env):
        """pick_fallback_sdkroot from script's own copy, under /bin/bash
        (3.2 on macOS) with set -euo pipefail like the scripts, captured
        through $(...) like the callers do."""
        body = "set -euo pipefail\n"
        body += "".join(_extract_function(n, source=script) for n in _SDK_HELPERS)
        body += 'rc=0\nout="$(pick_fallback_sdkroot)" || rc=$?\nprintf "%s" "$out"\nexit "$rc"\n'
        func_file = self.tmp / "run-pick.sh"
        func_file.write_text(body)
        return subprocess.run(["/bin/bash", str(func_file)], capture_output=True, text=True,
                              env=self.env(**extra_env), cwd=cwd or self.tmp, timeout=60)

    def make_checkout(self):
        """A temp checkout with copies of both scripts, the fixture and a
        minimal core/, so a run never touches the real app/.build."""
        co = self.tmp / "checkout"
        (co / "scripts").mkdir(parents=True)
        for script in _SDK_HELPER_SCRIPTS:
            dest = co / "scripts" / script.name
            dest.write_text(script.read_text())
            dest.chmod(0o755)
        (co / "app" / "Fixtures").mkdir(parents=True)
        (co / "app" / "Fixtures" / "snapshot.json").write_text("{}\n")
        (co / "core" / "bin").mkdir(parents=True)
        (co / "core" / "agentboard").mkdir()
        (co / "core" / "bin" / "agentboard").write_text(
            "#!/usr/bin/env python3\n_BAKED_AGENTBOARD_HOME = None\n")
        (co / "core" / "agentboard" / "__init__.py").write_text("")
        return co

    def run_script(self, co, name, *args, merge=False, **extra_env):
        return subprocess.run(
            [str(co / "scripts" / name), *args], env=self.env(**extra_env), cwd=self.tmp,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge else subprocess.PIPE,
            text=True, timeout=60,
        )


def _tmpdir(test, prefix):
    tmp = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    test.addCleanup(lambda: subprocess.run(["rm", "-rf", str(tmp)]))
    return tmp


def _clt27(test, **kw):
    """This Mac's case: MacOSX.sdk -> MacOSX27.0.sdk fails the probe, the
    older 26.5 and 15.4 SDKs pass."""
    kw.setdefault("sdks", ("MacOSX27.0.sdk", "MacOSX26.5.sdk", "MacOSX15.4.sdk"))
    kw.setdefault("working", ("MacOSX26.5.sdk", "MacOSX15.4.sdk"))
    return _FakeToolchain(_tmpdir(test, "agentboard-sdk-test-"), **kw)


def _one_line(text):
    lines = text.splitlines()
    assert len(lines) == 1, f"expected exactly one line, got {lines!r}"
    return lines[0]


@unittest.skipUnless(sys.platform == "darwin", "build-app.sh/swift-test.sh are macOS-only")
class PickFallbackSdkrootTests(unittest.TestCase):
    """pick_fallback_sdkroot (and _swiftui_macro_probe), run against each
    script's own copy with stub xcrun/swiftc, never the real toolchain."""

    def test_default_sdk_works_is_a_silent_noop(self):
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"),
                                    working=("MacOSX27.0.sdk",))
                r = tc.run_pick(script)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
                self.assertEqual(tc.probe_calls(), ["<default>"])
                self.assertEqual(tc.leftover_probe_dirs(), [])

    def test_falls_back_to_the_older_sdk_with_one_note(self):
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"),
                                    working=("MacOSX26.5.sdk",))
                r = tc.run_pick(script)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, tc.sdk("MacOSX26.5.sdk"))
                note = _one_line(r.stderr)
                self.assertTrue(note.startswith("note: the default macOS SDK (27.0) can't expand"),
                                note)
                self.assertIn("building with MacOSX26.5.sdk (26.5)", note)
                self.assertIn("AGENTBOARD_SDKROOT=default", note)
                self.assertEqual(tc.leftover_probe_dirs(), [])

    def test_picks_the_newest_working_sdk_below_the_default(self):
        sdks = ("MacOSX28.0.sdk", "MacOSX27.0.sdk", "MacOSX26.10.sdk", "MacOSX26.5.sdk",
                "MacOSX15.4.sdk")
        aliases = {"MacOSX.sdk": "MacOSX27.0.sdk", "MacOSX27.sdk": "MacOSX27.0.sdk",
                   "MacOSX26.sdk": "MacOSX26.10.sdk", "MacOSX15.sdk": "MacOSX15.4.sdk"}
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"),
                                    sdks=sdks, aliases=aliases,
                                    working=("MacOSX28.0.sdk", "MacOSX15.4.sdk"))
                r = tc.run_pick(script)
                self.assertEqual(r.stdout, tc.sdk("MacOSX15.4.sdk"))
                self.assertIn("building with MacOSX15.4.sdk (15.4)", _one_line(r.stderr))
                # Newer SDKs are never tried, aliases are probed once, and
                # 26.10 sorts above 26.5.
                self.assertEqual(tc.probe_calls(), [
                    "<default>", tc.sdk("MacOSX26.10.sdk"), tc.sdk("MacOSX26.5.sdk"),
                    tc.sdk("MacOSX15.4.sdk"),
                ])

    def test_patch_version_breaks_the_tie(self):
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"),
                                    sdks=("MacOSX27.0.sdk", "MacOSX26.5.sdk", "MacOSX26.5.1.sdk"),
                                    working=("MacOSX26.5.sdk", "MacOSX26.5.1.sdk"))
                r = tc.run_pick(script)
                self.assertEqual(r.stdout, tc.sdk("MacOSX26.5.1.sdk"))
                self.assertIn("(26.5.1)", r.stderr)

    def test_sdks_dir_with_a_space_and_a_dot(self):
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tmp = _tmpdir(self, "agentboard-sdk-test-")
                tc = _FakeToolchain(tmp,
                                    sdks_dir=tmp / "Dev 1.2" / "SDKs", working=("MacOSX26.5.sdk",))
                r = tc.run_pick(script)
                self.assertEqual(r.stdout, str(tmp / "Dev 1.2" / "SDKs" / "MacOSX26.5.sdk"))
                self.assertEqual(r.returncode, 0)

    def test_no_working_sdk_warns_once_and_uses_the_default(self):
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"))
                r = tc.run_pick(script)
                self.assertEqual((r.returncode, r.stdout), (0, ""))
                warning = _one_line(r.stderr)
                self.assertTrue(warning.startswith("warning: the default macOS SDK (27.0)"),
                                warning)
                self.assertIn(str(tc.sdks_dir), warning)

    def test_other_probe_errors_are_inconclusive_not_a_fallback(self):
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"),
                                    other_error=("MacOSX27.0.sdk",), working=("MacOSX26.5.sdk",))
                r = tc.run_pick(script)
                self.assertEqual((r.returncode, r.stdout), (0, ""))
                note = _one_line(r.stderr)
                self.assertTrue(note.startswith("note: could not check the macOS SDK"), note)
                self.assertIn("unable to open output file", note)
                self.assertTrue(note.endswith("; using the default"), note)
                self.assertEqual(tc.probe_calls(), ["<default>"])

    def test_an_inconclusive_older_sdk_is_skipped_not_picked(self):
        # The default fails with the macro error, 26.5's probe fails for an
        # unrelated reason (inconclusive), 15.4 works: 26.5 is skipped like
        # an SDK that lacks the plugin, and 15.4 is used.
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tc = _clt27(self, other_error=("MacOSX26.5.sdk",), working=("MacOSX15.4.sdk",))
                r = tc.run_pick(script)
                self.assertEqual((r.returncode, r.stdout), (0, tc.sdk("MacOSX15.4.sdk")))
                self.assertIn("building with MacOSX15.4.sdk (15.4)", _one_line(r.stderr))
                self.assertEqual(tc.probe_calls(), [
                    "<default>", tc.sdk("MacOSX26.5.sdk"), tc.sdk("MacOSX15.4.sdk"),
                ])

    def test_xcrun_unable_to_run_swiftc_is_inconclusive(self):
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"),
                                    working=("MacOSX26.5.sdk",))
                tc.flag("xcrun_swiftc_fail")
                r = tc.run_pick(script)
                self.assertEqual((r.returncode, r.stdout), (0, ""))
                self.assertIn("unable to find utility", _one_line(r.stderr))

    def test_agentboard_sdkroot_default_skips_the_probe(self):
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"),
                                    working=("MacOSX26.5.sdk",))
                r = tc.run_pick(script, AGENTBOARD_SDKROOT="default")
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
                self.assertEqual(tc.probe_calls(), [])

    def test_agentboard_sdkroot_dir_is_used_as_its_real_path(self):
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"))
                r = tc.run_pick(script, AGENTBOARD_SDKROOT=str(tc.sdks_dir / "MacOSX26.sdk"))
                self.assertEqual((r.returncode, r.stdout, r.stderr),
                                 (0, tc.sdk("MacOSX26.5.sdk"), ""))
                self.assertEqual(tc.probe_calls(), [])

    def test_relative_agentboard_sdkroot_becomes_absolute(self):
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"))
                r = tc.run_pick(script, cwd=tc.dev, AGENTBOARD_SDKROOT="SDKs/MacOSX26.5.sdk")
                self.assertEqual((r.returncode, r.stdout), (0, tc.sdk("MacOSX26.5.sdk")))

    def test_bad_agentboard_sdkroot_is_an_error(self):
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"))
                r = tc.run_pick(script, AGENTBOARD_SDKROOT="/nonexistent/MacOSX.sdk")
                self.assertNotEqual(r.returncode, 0)
                self.assertEqual(r.stdout, "")
                self.assertEqual(_one_line(r.stderr), "error: AGENTBOARD_SDKROOT="
                                 "/nonexistent/MacOSX.sdk is not an SDK directory")

    def test_preset_sdkroot_is_respected(self):
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"),
                                    working=("MacOSX26.5.sdk",))
                r = tc.run_pick(script, SDKROOT=tc.sdk("MacOSX27.0.sdk"))
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
                self.assertEqual(tc.probe_calls(), [])

    def test_no_toolchain_is_silent(self):
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"))
                tc.flag("xcrun_fail")
                r = tc.run_pick(script)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
                self.assertEqual(tc.probe_calls(), [])

    def test_probe_timeout_is_bounded_quiet_and_no_fallback(self):
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"),
                                    working=("MacOSX26.5.sdk",))
                tc.flag("sleep", "30")
                started = time.monotonic()
                r = tc.run_pick(script, AGENTBOARD_SDK_PROBE_TIMEOUT="1")
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 5,
                                "the watchdog must bound the probe and not hold the pipe")
                self.assertEqual((r.returncode, r.stdout), (0, ""))
                note = _one_line(r.stderr)
                self.assertIn("timed out after 1s", note)
                self.assertNotIn("Terminated", r.stderr)
                self.assertEqual(tc.probe_calls(), ["<default>"])
                self.assertEqual(tc.leftover_probe_dirs(), [])


@unittest.skipUnless(sys.platform == "darwin", "build-app.sh/swift-test.sh are macOS-only")
class SdkProbeWatchdogTests(unittest.TestCase):
    """_swiftui_macro_probe's bash 3.2 watchdog, run many times through
    $(...) under /bin/bash: it must not stall the caller, print anything on
    stderr, or leave a process behind once its `sleep 1` tick runs out."""

    # Plain (script, capture) pairs: each test opens its own subTest, so one
    # failing case is reported once and the remaining cases still run.
    def _cases(self):
        return [(script, capture) for script in _SDK_HELPER_SCRIPTS for capture in (True, False)]

    def _assert_no_process_left(self, tc, within=3):
        # A watchdog killed mid-tick may orphan one `sleep 1` by design; give
        # it a moment, but nothing may outlive that (the old trap-based
        # watchdog could leave its `sleep <limit>` for the full limit).
        deadline = time.monotonic() + within
        while tc.leftover_processes() and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertEqual(tc.leftover_processes(), [])

    def test_fast_probes_leave_no_process_and_nothing_on_stderr(self):
        for script, capture in self._cases():
            with self.subTest(script=script.name, capture=capture):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"))
                tc.flag("xcrun_swiftc_fail")
                started = time.monotonic()
                r = tc.run_probe_loop(script, 20, capture, AGENTBOARD_SDK_PROBE_TIMEOUT="97")
                elapsed = time.monotonic() - started
                self.assertEqual(r.returncode, 0, r.stderr)
                # No "Terminated: 15" from reaping the killed watchdog.
                self.assertEqual(r.stderr, "")
                rcs = [line.split("\t")[0] for line in r.stdout.splitlines()]
                self.assertEqual(rcs, ["2"] * 20)
                # A watchdog writing into the $(...) pipe would hold each call
                # open for its 1 s tick.
                self.assertLess(elapsed, 10, "the watchdog must not hold the caller's pipe")
                # The caller reaps its watchdog before returning: right after the
                # run, at most orphaned `sleep 1` ticks remain.
                self.assertEqual([p for p in tc.leftover_processes() if "marked-sleep" not in p],
                                 [])
                self._assert_no_process_left(tc)
                self.assertEqual(tc.leftover_probe_dirs(), [])

    def test_timed_out_probes_kill_the_stub_and_leave_nothing(self):
        for script, capture in self._cases():
            with self.subTest(script=script.name, capture=capture):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"))
                tc.flag("sleep", "30")  # the stub swiftc outlives the 1 s limit
                started = time.monotonic()
                r = tc.run_probe_loop(script, 3, capture, AGENTBOARD_SDK_PROBE_TIMEOUT="1")
                elapsed = time.monotonic() - started
                self.assertEqual(r.returncode, 0, r.stderr)
                # No "Terminated: 15" from reaping the killed stub.
                self.assertEqual(r.stderr, "")
                self.assertEqual(r.stdout.splitlines(), ["2\ttimed out after 1s"] * 3)
                self.assertLess(elapsed, 10)
                self._assert_no_process_left(tc)
                self.assertEqual(tc.leftover_probe_dirs(), [])

    def test_watchdog_leaves_once_a_stub_outliving_its_caller_exits(self):
        # The caller is killed mid-probe, so only the watchdog's own check can
        # end it: it must leave within a tick of the stub exiting, not stay
        # for the 97 s limit.
        for script, capture in self._cases():
            with self.subTest(script=script.name, capture=capture):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"))
                tc.flag("sleep", "2")
                loop = tc.probe_loop_script(script, 1, capture)
                proc = subprocess.Popen(["/bin/bash", str(loop)],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                        env=tc.env(AGENTBOARD_SDK_PROBE_TIMEOUT="97"), cwd=tc.tmp)
                time.sleep(0.5)
                proc.kill()
                proc.wait()
                self.assertTrue(tc.leftover_processes(), "the stub should still be running")
                self._assert_no_process_left(tc, within=5)

    def test_a_timeout_that_is_not_a_positive_integer_means_the_default(self):
        # A 1.5 s probe must neither time out at once (1.5 and 0 used to)
        # nor run without a watchdog (abc used to kill it before its first
        # tick): the 60 s default applies, so the watchdog ticks and the
        # probe finishes on its own.
        for script in _SDK_HELPER_SCRIPTS:
            for value in ("abc", "1.5", "0", "08", ""):
                with self.subTest(script=script.name, value=value):
                    tc = _FakeToolchain(_tmpdir(self, "agentboard-sdk-test-"))
                    tc.flag("sleep", "1.5")
                    r = tc.run_probe_loop(script, 1, AGENTBOARD_SDK_PROBE_TIMEOUT=value)
                    self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "0\t\n", ""))
                    self.assertIn("1", tc.sleep_calls(), "the watchdog never ticked")
                    self._assert_no_process_left(tc)


class SdkHelpersAreIdenticalTests(unittest.TestCase):
    """build-app.sh and swift-test.sh each carry their own copy of the SDK
    helpers (so each works standalone); the copies must not drift."""

    def test_copies_match(self):
        for name in _SDK_HELPERS:
            with self.subTest(name=name):
                self.assertEqual(_extract_function(name, source=BUILD_APP_SH),
                                 _extract_function(name, source=SWIFT_TEST_SH))

    def test_install_and_uninstall_carry_no_copy(self):
        # install.sh builds only through build-app.sh, uninstall.sh never builds.
        for script in (INSTALL_SH, UNINSTALL_SH):
            for name in _SDK_HELPERS:
                self.assertNotIn(f"{name}() {{", script.read_text())


@unittest.skipUnless(sys.platform == "darwin", "swift-test.sh is macOS-only")
class SwiftTestFlagsTests(unittest.TestCase):
    """swift_test_flags adds each flag group only when its directory exists
    and plain `swift test` wouldn't find it."""

    def _flags(self, tc):
        body = "set -euo pipefail\n" + _extract_function("swift_test_flags", source=SWIFT_TEST_SH)
        body += "swift_test_flags\n"
        func_file = tc.tmp / "run-flags.sh"
        func_file.write_text(body)
        r = subprocess.run(["/bin/bash", str(func_file)], capture_output=True, text=True,
                           env=tc.env(), timeout=30)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        return r.stdout.splitlines()

    def test_both_groups_when_both_dirs_exist(self):
        tc = _FakeToolchain(_tmpdir(self, "agentboard-flags-test-"), testing_framework=True,
                            plugin="testing")
        fw = str(tc.dev / "Library" / "Developer" / "Frameworks")
        self.assertEqual(self._flags(tc), [
            "-Xswiftc", "-F", "-Xswiftc", fw, "-Xlinker", "-rpath", "-Xlinker", fw,
            "-Xswiftc", "-plugin-path", "-Xswiftc", str(tc.plugins / "testing"),
        ])

    def test_nothing_without_them(self):
        tc = _FakeToolchain(_tmpdir(self, "agentboard-flags-test-"))
        self.assertEqual(self._flags(tc), [])

    def test_no_plugin_path_when_the_default_dir_has_the_plugin(self):
        tc = _FakeToolchain(_tmpdir(self, "agentboard-flags-test-"), plugin="both")
        self.assertEqual(self._flags(tc), [])


@unittest.skipUnless(sys.platform == "darwin", "build-app.sh/swift-test.sh are macOS-only")
class ToolchainLayoutCompatibilityTests(unittest.TestCase):
    """The approval condition: the same scripts must behave on a macOS 26
    toolchain whose default SDK works, on a full-Xcode layout, and on a
    Command Line Tools 27 layout whose default SDK can't expand SwiftUI
    macros -- deciding only by probing, never by OS or Swift version."""

    def _assert_no_version_branching(self, tc):
        self.assertFalse(tc.sw_vers_called(), "sw_vers must never be consulted")

    def _swift_test(self, tc, co, *args, **env):
        tc.flag("test-output", _PASSING_RUN)
        return tc.run_script(co, "swift-test.sh", *args, **env)

    def test_a_macos26_default_sdk_works(self):
        tc = _FakeToolchain(_tmpdir(self, "agentboard-layout-test-"), sdks=("MacOSX26.5.sdk",),
                            aliases={"MacOSX.sdk": "MacOSX26.5.sdk"}, working=("MacOSX26.5.sdk",),
                            testing_framework=True)
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                (tc.state / "probe-calls").unlink(missing_ok=True)
                r = tc.run_pick(script)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
                self.assertEqual(tc.probe_calls(), ["<default>"])
        co = tc.make_checkout()
        r = tc.run_script(co, "build-app.sh", "--no-install", merge=True)
        self.assertEqual((r.returncode, r.stdout), (0, "built app/build/AgentBoard.app\n"))
        self.assertEqual(tc.swift_sdkroot(), "<unset>")
        r = self._swift_test(tc, co)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stderr, "")
        self.assertEqual(tc.swift_sdkroot(), "<unset>")
        self._assert_no_version_branching(tc)

    def test_b_macos26_clt_layout_flag_variants(self):
        variants = {
            "frameworks and plugins/testing": dict(testing_framework=True, plugin="testing"),
            "frameworks only": dict(testing_framework=True, plugin="default"),
            "neither": dict(),
        }
        for label, kw in variants.items():
            with self.subTest(variant=label):
                tc = _FakeToolchain(_tmpdir(self, "agentboard-layout-test-"),
                                    sdks=("MacOSX26.5.sdk",),
                                    aliases={"MacOSX.sdk": "MacOSX26.5.sdk"},
                                    working=("MacOSX26.5.sdk",), **kw)
                co = tc.make_checkout()
                r = self._swift_test(tc, co)
                self.assertEqual((r.returncode, r.stderr), (0, ""))
                fw = str(tc.dev / "Library" / "Developer" / "Frameworks")
                expected = ["test"]
                if kw.get("testing_framework"):
                    expected += ["-Xswiftc", "-F", "-Xswiftc", fw, "-Xlinker", "-rpath",
                                 "-Xlinker", fw]
                if kw.get("plugin") == "testing":
                    expected += ["-Xswiftc", "-plugin-path", "-Xswiftc",
                                 str(tc.plugins / "testing")]
                self.assertEqual(tc.swift_argv(), expected)
                self.assertEqual(tc.swift_sdkroot(), "<unset>")
                self._assert_no_version_branching(tc)

    def test_c_full_xcode_layout_is_a_noop(self):
        tc = _FakeToolchain(_tmpdir(self, "agentboard-layout-test-"), layout="xcode",
                            sdks=("MacOSX.sdk",), aliases={"MacOSX27.sdk": "MacOSX.sdk"},
                            working=("MacOSX.sdk",), plugin="default")
        self.assertFalse((tc.dev / "Library" / "Developer" / "Frameworks").exists())
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                r = tc.run_pick(script)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, "", ""))
        co = tc.make_checkout()
        r = self._swift_test(tc, co)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(tc.swift_argv(), ["test"])
        self.assertEqual(tc.swift_sdkroot(), "<unset>")
        r = tc.run_script(co, "build-app.sh", "--no-install", merge=True)
        self.assertEqual((r.returncode, r.stdout), (0, "built app/build/AgentBoard.app\n"))
        self.assertEqual(tc.swift_sdkroot(), "<unset>")
        self._assert_no_version_branching(tc)

    def test_c_xcode_never_borrows_a_command_line_tools_sdk(self):
        tmp = _tmpdir(self, "agentboard-layout-test-")
        clt_sdks = tmp / "CommandLineTools" / "SDKs"
        (clt_sdks / "MacOSX26.5.sdk").mkdir(parents=True)
        (clt_sdks / "MacOSX26.5.sdk" / "VERSION").write_text("26.5\n")
        tc = _FakeToolchain(tmp, layout="xcode", sdks=("MacOSX.sdk",),
                            aliases={"MacOSX27.sdk": "MacOSX.sdk"}, plugin="default")
        (tc.state / "working").write_text(os.path.realpath(clt_sdks / "MacOSX26.5.sdk") + "\n")
        for script in _SDK_HELPER_SCRIPTS:
            with self.subTest(script=script.name):
                (tc.state / "probe-calls").unlink(missing_ok=True)
                r = tc.run_pick(script)
                self.assertEqual((r.returncode, r.stdout), (0, ""))
                self.assertTrue(_one_line(r.stderr).startswith("warning: the default macOS SDK (27.0)"))
                self.assertEqual(tc.probe_calls(), ["<default>"])
        self._assert_no_version_branching(tc)

    def test_d_clt27_falls_back_and_exports_sdkroot(self):
        tc = _clt27(self, testing_framework=True, plugin="testing")
        co = tc.make_checkout()
        r = self._swift_test(tc, co)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("building with MacOSX26.5.sdk (26.5)", _one_line(r.stderr))
        self.assertEqual(tc.swift_sdkroot(), tc.sdk("MacOSX26.5.sdk"))
        self.assertIn("-plugin-path", tc.swift_argv())
        r = tc.run_script(co, "build-app.sh", "--no-install", merge=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("building with MacOSX26.5.sdk", lines[0])
        self.assertEqual(lines[1], "built app/build/AgentBoard.app")
        self.assertEqual(tc.swift_sdkroot(), tc.sdk("MacOSX26.5.sdk"))
        self._assert_no_version_branching(tc)

    def test_scripts_never_read_os_or_swift_versions(self):
        for script in _SDK_HELPER_SCRIPTS:
            text = script.read_text()
            for needle in ("sw_vers", "ProductVersion", "swift --version"):
                self.assertNotIn(needle, text, f"{script.name} must decide by probing only")


@unittest.skipUnless(sys.platform == "darwin", "swift-test.sh is macOS-only")
class SwiftTestScriptQuietTests(unittest.TestCase):
    """swift-test.sh follows the quiet-runner rule: one summary line on
    success; the failing lines plus the log path on failure."""

    def setUp(self):
        self.tc = _FakeToolchain(_tmpdir(self, "agentboard-swifttest-test-"),
                                 working=("MacOSX27.0.sdk",), testing_framework=True,
                                 plugin="testing")
        self.co = self.tc.make_checkout()
        self.log = self.co / "app" / "build" / "swift-test.log"

    def _run(self, output, rc=0, *args, **env):
        self.tc.flag("test-output", output)
        self.tc.flag("test-rc", str(rc))
        return self.tc.run_script(self.co, "swift-test.sh", *args, **env)

    def test_success_prints_only_the_summary(self):
        r = self._run(_PASSING_RUN)
        self.assertEqual((r.returncode, r.stderr), (0, ""))
        self.assertEqual(r.stdout,
                         "✔ Test run with 3 tests in 2 suites passed after 0.01 seconds.\n")
        self.assertEqual(self.log.read_text(), _PASSING_RUN)

    def test_private_use_glyphs_are_recognized(self):
        glyph = "\U000F0000"
        r = self._run(_PASSING_RUN.replace("✔", glyph))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("Test run with 3 tests", _one_line(r.stdout))
        r = self._run(_FAILING_RUN.replace("✘", glyph), 1)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("recorded an issue", r.stderr)

    def test_singular_summary(self):
        r = self._run("✔ Test run with 1 test in 1 suite passed after 0.01 seconds.\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, "✔ Test run with 1 test in 1 suite passed after 0.01 seconds.\n")

    def test_ansi_colors_are_stripped(self):
        r = self._run("\x1b[1;32m✔\x1b[0m Test run with 3 tests in 2 suites "
                      "\x1b[1mpassed\x1b[0m after 0.01 seconds.\n")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertNotIn("\x1b", r.stdout)
        self.assertIn("Test run with 3 tests in 2 suites passed", r.stdout)

    def test_failure_prints_failing_lines_and_the_log_path(self):
        r = self._run(_FAILING_RUN, 1)
        self.assertEqual(r.returncode, 1)
        self.assertEqual(r.stdout, "")
        lines = r.stderr.splitlines()
        self.assertIn("recorded an issue at T.swift:4:21", lines[0])
        self.assertIn("Test bad() failed after", lines[1])
        self.assertIn("Test run with 2 tests in 1 suite failed", lines[2])
        self.assertEqual(lines[-1], f"full log: {self.log}")
        self.assertNotIn("Test ok() passed", r.stderr)
        self.assertTrue(self.log.is_file())

    def test_zero_tests_is_a_failure(self):
        r = self._run("Building for debugging...\nBuild complete!\n")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("error: swift test ran no Swift Testing tests", r.stderr)
        self.assertIn(str(self.log), r.stderr)

    def test_a_filter_matching_nothing_is_named_in_the_zero_tests_error(self):
        r = self._run("Building for debugging...\nBuild complete!\n", 0,
                      "--", "--filter", "NoSuchTestXyz")
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(_one_line(r.stderr),
                         "error: swift test ran no Swift Testing tests (Testing.framework not "
                         "found, or a --filter/--skip matched nothing?); full log: "
                         f"{self.log}")

    def test_list_tests_skips_the_zero_tests_guard(self):
        for flag in ("--list-tests", "-l"):
            with self.subTest(flag=flag):
                r = self._run("AgentBoardTests.HelpTests/x()\n", 0, "--", flag)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("HelpTests/x()", r.stdout)
    def test_xcode_layout_runs_plain_swift_test(self):
        tc = _FakeToolchain(_tmpdir(self, "agentboard-swifttest-test-"), layout="xcode",
                            sdks=("MacOSX.sdk",), aliases={}, working=("MacOSX.sdk",),
                            plugin="default")
        co = tc.make_checkout()
        tc.flag("test-output", _PASSING_RUN)
        r = tc.run_script(co, "swift-test.sh")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(tc.swift_argv(), ["test"])

    def test_verbose_streams_everything(self):
        for how in ({"AGENTBOARD_VERBOSE": "1"}, {"args": ("--verbose",)}, {"args": ("-v",)}):
            with self.subTest(how=how):
                how = dict(how)
                args = how.pop("args", ())
                r = self._run(_PASSING_RUN, 0, *args, **how)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(r.stdout, _PASSING_RUN)

    def test_passthrough_args_flags_and_fallback_sdkroot(self):
        tc = _clt27(self, testing_framework=True, plugin="testing")
        co = tc.make_checkout()
        tc.flag("test-output", _PASSING_RUN)
        r = tc.run_script(co, "swift-test.sh", "--", "--filter", "HelpTests")
        self.assertEqual(r.returncode, 0, r.stderr)
        argv = tc.swift_argv()
        self.assertEqual(argv[0], "test")
        self.assertEqual(argv[-2:], ["--filter", "HelpTests"])
        self.assertIn("-plugin-path", argv)
        self.assertIn("-rpath", argv)
        self.assertEqual(tc.swift_sdkroot(), tc.sdk("MacOSX26.5.sdk"))

    def test_help(self):
        r = self.tc.run_script(self.co, "swift-test.sh", "--help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("Usage:", r.stdout)
        # It says what the zero-tests guard catches and what skips it.
        self.assertIn("a --filter/--skip matched nothing", r.stdout)
        self.assertIn("Only -- --list-tests\n(or -l) skips that check", r.stdout)
        self.assertFalse((self.tc.state / "swift-argv").exists())


@unittest.skipUnless(sys.platform == "darwin", "build-app.sh is macOS-only")
class BuildAppQuietTests(unittest.TestCase):
    """build-app.sh follows the quiet-runner rule: everything goes to
    app/build/build-app.log, the terminal gets one summary line (after the
    SDK note when a fallback is used), or the error lines plus the log path."""

    def _setup(self, tc):
        co = tc.make_checkout()
        return co, co / "app" / "build" / "build-app.log"

    def test_no_install_success_is_one_line(self):
        tc = _FakeToolchain(_tmpdir(self, "agentboard-buildapp-quiet-"), working=("MacOSX27.0.sdk",))
        co, log = self._setup(tc)
        r = tc.run_script(co, "build-app.sh", "--no-install", merge=True)
        self.assertEqual((r.returncode, r.stdout), (0, "built app/build/AgentBoard.app\n"))
        text = log.read_text()
        self.assertIn("replacing existing signature", text)
        self.assertIn("Build complete!", text)
        self.assertIn("==> ad-hoc codesign", text)
        self.assertTrue((co / "app" / "build" / "AgentBoard.app" / "Contents" / "Info.plist").is_file())

    def test_fallback_note_plus_one_line(self):
        tc = _clt27(self)
        co, _ = self._setup(tc)
        r = tc.run_script(co, "build-app.sh", "--no-install", merge=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        lines = r.stdout.splitlines()
        self.assertEqual(len(lines), 2, lines)
        self.assertTrue(lines[0].startswith("note: the default macOS SDK (27.0)"), lines[0])
        self.assertEqual(lines[1], "built app/build/AgentBoard.app")

    def test_build_failure_prints_errors_and_the_log_path(self):
        tc = _FakeToolchain(_tmpdir(self, "agentboard-buildapp-quiet-"), working=("MacOSX27.0.sdk",))
        co, log = self._setup(tc)
        tc.flag("swift_build_fail")
        r = tc.run_script(co, "build-app.sh", "--no-install", merge=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(r.stdout.splitlines(), [
            "/x/Foo.swift:1:1: error: something broke",
            "error: Build failed",
            f"full log: {log}",
        ])

    def test_verbose_shows_the_steps(self):
        for how in ({"args": ("--verbose",)}, {"AGENTBOARD_VERBOSE": "1"}):
            with self.subTest(how=how):
                how = dict(how)
                args = how.pop("args", ())
                tc = _FakeToolchain(_tmpdir(self, "agentboard-buildapp-quiet-"),
                                    working=("MacOSX27.0.sdk",))
                co, log = self._setup(tc)
                r = tc.run_script(co, "build-app.sh", "--no-install", *args, merge=True, **how)
                self.assertEqual(r.returncode, 0, r.stdout)
                for line in ("==> swift build -c release", "Build complete!", "==> ad-hoc codesign",
                             "replacing existing signature", "==> built "):
                    self.assertIn(line, r.stdout)
                self.assertIn("Build complete!", log.read_text())

    def test_refusal_message_still_reaches_the_terminal(self):
        tc = _FakeToolchain(_tmpdir(self, "agentboard-buildapp-quiet-"), working=("MacOSX27.0.sdk",))
        co, _ = self._setup(tc)
        prefix = tc.tmp / "apps"
        (prefix / "AgentBoard.app").mkdir(parents=True)
        (prefix / "AgentBoard.app" / "Contents").mkdir()
        r = tc.run_script(co, "build-app.sh", "--prefix", str(prefix), merge=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("doesn't look like AgentBoard", r.stdout)
        self.assertNotIn("built and installed", r.stdout)

    def test_install_success_is_one_line(self):
        tc = _FakeToolchain(_tmpdir(self, "agentboard-buildapp-quiet-"), working=("MacOSX27.0.sdk",))
        co, _ = self._setup(tc)
        prefix = tc.tmp / "apps"
        r = tc.run_script(co, "build-app.sh", "--prefix", str(prefix), merge=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertEqual(r.stdout, f"built and installed {prefix}/AgentBoard.app\n")
        self.assertTrue((prefix / "AgentBoard.app" / "Contents" / "Info.plist").is_file())


if __name__ == "__main__":
    unittest.main()

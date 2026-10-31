"""Shared test setup: isolates every test behind a fake HOME/AGENTBOARD_HOME."""

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agentboard import db  # noqa: E402
from agentboard import spaces  # noqa: E402


class AgentBoardTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="agentboard-test-")
        self.tmp_path = Path(self._tmp)
        self.agentboard_home = self.tmp_path / "agentboard-home"
        self.fake_home = self.tmp_path / "fake-home"
        self.agentboard_home.mkdir()
        self.fake_home.mkdir()
        (self.fake_home / ".claude").mkdir()
        (self.fake_home / ".codex").mkdir()

        # Also strip any session-identifying env vars this very agent may have
        # inherited from its own real Claude Code / Codex invocation, so
        # session-resolution tests are deterministic rather than accidentally
        # falling through to real-world values.
        scrubbed_keys = [
            "AGENTBOARD_HOME", "HOME",
            "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "CODEX_THREAD_ID",
        ]
        self._old_env = {key: os.environ.get(key) for key in scrubbed_keys}
        for key in scrubbed_keys:
            os.environ.pop(key, None)
        os.environ["AGENTBOARD_HOME"] = str(self.agentboard_home)
        os.environ["HOME"] = str(self.fake_home)

        # No test may read the real window list, process list or Dock defaults:
        # `agentboard focus` tests inject fakes, and anything that forgets
        # to fails loudly here instead of probing this Mac.
        for name in ("window_states", "_run_defaults", "_run_ps"):
            patcher = mock.patch.object(spaces, name, side_effect=AssertionError(f"unmocked {name}"))
            patcher.start()
            self.addCleanup(patcher.stop)

        self.conn = db.connect()
        self.addCleanup(self.conn.close)

    def tearDown(self):
        for key, value in self._old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(self._tmp, ignore_errors=True)

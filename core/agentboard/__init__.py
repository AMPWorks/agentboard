"""AgentBoard core: local dashboard state for Claude Code and Codex CLI sessions."""

import sys

# Checked here, at the top of the package, so every entry point (the CLI,
# the tests, a stray `python3 -c "import agentboard"`) fails with one clear
# sentence under an old interpreter instead of a TypeError from the first
# `Path | None` annotation it meets. The hook fast path in bin/agentboard
# checks the version itself, before importing anything, and exits 0.
if sys.version_info < (3, 11):
    raise ImportError(
        "AgentBoard requires Python >= 3.11; this is "
        f"{sys.version.split()[0]} ({sys.executable}). Run it with a newer "
        "interpreter, e.g. python3.12."
    )

__version__ = "0.1.0"


"""Lazy ``run_agent`` resolver shared by the runtime submodules. ``_ra()``
resolves ``run_agent`` at call time (not import time) so tests patching
``run_agent.X`` keep intercepting and no runtime submodule needs a hard
module-level dependency on ``run_agent`` (circular-import seam)."""

from __future__ import annotations


def _ra():
    """Lazy ``run_agent`` reference for test-patch routing."""
    import run_agent
    return run_agent

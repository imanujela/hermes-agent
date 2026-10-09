"""Shared CLI bootstrap for Hermes skill scripts (UTF-8 stdio, locale-agnostic).

skill scripts run as standalone subprocess CLIs, often under LC_ALL=C where
the default stdio codec is not UTF-8. Every script used to carry its own
copy of the reconfigure preamble; setup_cli() is now the single copy.

Divergence note (union of the two pre-existing variants):
  * pdf copies: reconfigured BOTH stdout and stderr, errors=strict (default),
    exceptions suppressed via hermes_suppress.suppressed(logger, ...).
  * pptx copies: reconfigured stdout ONLY, errors="replace", guarded by
    hasattr(stream, "reconfigure").
setup_cli() takes the union: both streams, hasattr guard, errors="replace",
failures logged at debug level and never propagated. No copy touched
LC_ALL/env vars (LC_ALL=C appears only in test harnesses as the condition
the scripts must survive), so setup_cli() deliberately mutates no env.
"""
from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)


def setup_cli() -> None:
    """Make stdout/stderr UTF-8-safe. Call first thing in main(); never raises."""
    for stream in (sys.stdout, sys.stderr):
        try:
            if hasattr(stream, "reconfigure"):
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - bootstrap must never crash the CLI
            logger.debug("stdio reconfigure failed for %r", stream, exc_info=True)

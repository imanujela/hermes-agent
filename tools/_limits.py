"""Timeout tiers for ``tools/`` — one taxonomy, defined in hermes_tiers.

This module is a thin re-export so existing ``from tools._limits import
PROCESS`` call sites keep working. Edit the tiers (and read the rules of the
road) in ``hermes_tiers`` — the single owner shared with hermes_cli.
"""

from hermes_tiers import BUILD, FAST, INSTALL, NETWORK, PROCESS  # noqa: F401

__all__ = ["FAST", "PROCESS", "NETWORK", "BUILD", "INSTALL"]

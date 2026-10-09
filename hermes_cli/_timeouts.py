"""Timeout tiers for ``hermes_cli`` — one taxonomy, defined in hermes_tiers.

This module is a thin re-export so existing ``from hermes_cli._timeouts import
FAST`` call sites keep working. Edit the tiers (and read the rules of the
road) in ``hermes_tiers`` — the single owner shared with tools/.
"""

from hermes_tiers import BUILD, FAST, INSTALL, NETWORK, PROCESS  # noqa: F401

__all__ = ["FAST", "NETWORK", "BUILD", "INSTALL", "PROCESS"]

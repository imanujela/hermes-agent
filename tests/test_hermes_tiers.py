"""Tests for hermes_tiers and its thin re-export shims.

Covers: integer typing of the five tiers, parity between hermes_tiers and
the hermes_cli._timeouts / tools._limits re-exports, strict ordering, and
__all__ correctness on both shims.
"""

import hermes_tiers
import hermes_cli._timeouts as _timeouts
import tools._limits as _limits

TIER_NAMES = ["FAST", "PROCESS", "NETWORK", "BUILD", "INSTALL"]


def test_all_tiers_are_integers():
    """All five tier values are integers (not booleans, not floats)."""
    for name in TIER_NAMES:
        value = getattr(hermes_tiers, name)
        assert type(value) is int, f"{name} is {type(value).__name__}, expected int"


def test_hermes_cli_timeouts_matches_hermes_tiers():
    """hermes_cli._timeouts re-exports the same values as hermes_tiers."""
    for name in TIER_NAMES:
        assert getattr(_timeouts, name) == getattr(hermes_tiers, name), (
            f"{name} mismatch: {_timeouts=} vs {hermes_tiers=}"
        )


def test_tools_limits_matches_hermes_tiers():
    """tools._limits re-exports the same values as hermes_tiers."""
    for name in TIER_NAMES:
        assert getattr(_limits, name) == getattr(hermes_tiers, name), (
            f"{name} mismatch: {_limits=} vs {hermes_tiers=}"
        )


def test_strict_ordering():
    """FAST < PROCESS < NETWORK < BUILD < INSTALL."""
    assert hermes_tiers.FAST < hermes_tiers.PROCESS
    assert hermes_tiers.PROCESS < hermes_tiers.NETWORK
    assert hermes_tiers.NETWORK < hermes_tiers.BUILD
    assert hermes_tiers.BUILD < hermes_tiers.INSTALL


def test_hermes_tiers_all():
    """hermes_tiers.__all__ lists exactly the five tier names."""
    assert sorted(hermes_tiers.__all__) == sorted(TIER_NAMES)


def test_hermes_cli_timeouts_all():
    """hermes_cli._timeouts.__all__ lists exactly the five tier names."""
    assert sorted(_timeouts.__all__) == sorted(TIER_NAMES)


def test_tools_limits_all():
    """tools._limits.__all__ lists exactly the five tier names."""
    assert sorted(_limits.__all__) == sorted(TIER_NAMES)

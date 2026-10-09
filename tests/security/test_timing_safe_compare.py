"""Interface tests for the single timing-safe string-compare owner (architecture report #5).

Seam under test: hermes_cli.auth_compare — the one module that answers
"is this string equal to that one in constant time?" across all three
divergent precedents discovered in the codebase:

1. **webhook** — non-ASCII tolerant: ``compare_digest`` raises ``TypeError``
   on non-ASCII ``str``, and attacker-controlled headers may contain any byte.
   Existing wrapper encodes to UTF-8 bytes before ``compare_digest``.

2. **pairing** — pre-hashes + lowercases: the pairing code is hashed with a
   salt before comparison, and request-id comparison lowercases both sides.
   The hash normalizes length; lowercasing makes hex ids case-insensitive.

3. **dashboard** — bare ``compare_digest``: the docstring in
   ``DashboardAuthProvider.verify_token`` tells implementers to use
   ``hmac.compare_digest`` directly, which raises on non-ASCII.

The unified seam ``timing_safe_eq`` handles all three by:
- Lowercasing both inputs (case-insensitive).
- Hashing both with SHA-256 (normalises length → fixed-width hex digests,
  avoiding the length-leak of bare ``compare_digest`` and the ``TypeError``
  on non-ASCII ``str``).
- Comparing the hex digests with ``hmac.compare_digest`` (truly constant-time
  on equal-length ASCII strings).
"""

import pytest

from hermes_cli.auth_compare import timing_safe_eq


# ---- Basic equality / inequality ----

def test_equal_ascii_strings_match():
    assert timing_safe_eq("hello", "hello") is True


def test_different_ascii_strings_differ():
    assert timing_safe_eq("hello", "world") is False


def test_empty_strings_match():
    assert timing_safe_eq("", "") is True


def test_empty_vs_nonempty_differ():
    assert timing_safe_eq("", "a") is False
    assert timing_safe_eq("a", "") is False


def test_single_char_match():
    assert timing_safe_eq("a", "a") is True


def test_single_char_mismatch():
    assert timing_safe_eq("a", "b") is False


# ---- Case-insensitivity ----

def test_case_insensitive_match():
    assert timing_safe_eq("Hello", "hello") is True
    assert timing_safe_eq("HELLO", "hello") is True
    assert timing_safe_eq("hello", "HELLO") is True


def test_case_insensitive_mismatch():
    assert timing_safe_eq("Hello", "world") is False


def test_mixed_case_hex_ids():
    """Mirrors pairing's approve_request: entry_id vs request_id comparison."""
    assert timing_safe_eq("A1B2C3D4E5F6G7H8", "a1b2c3d4e5f6g7h8") is True
    assert timing_safe_eq("A1B2C3D4E5F6G7H8", "a1b2c3d4e5f6g7h9") is False


# ---- Non-ASCII tolerance ----

def test_non_ascii_equal():
    """compare_digest raises TypeError on non-ASCII str; timing_safe_eq must not."""
    assert timing_safe_eq("héllo", "héllo") is True


def test_non_ascii_inequal():
    assert timing_safe_eq("héllo", "hëllo") is False


def test_non_ascii_vs_ascii():
    assert timing_safe_eq("héllo", "hello") is False


def test_emoji_equal():
    assert timing_safe_eq("🔑secret", "🔑secret") is True


def test_emoji_inequal():
    assert timing_safe_eq("🔑secret", "🔑other") is False


def test_surrogate_pairs():
    """Non-BMP characters (4-byte UTF-8) must not crash."""
    assert timing_safe_eq("𝕳𝖊𝖑𝖑𝖔", "𝕳𝖊𝖑𝖑𝖔") is True
    assert timing_safe_eq("𝕳𝖊𝖑𝖑𝖔", "𝕳𝖊𝖑𝖑𝖝") is False


def test_mixed_ascii_non_ascii():
    assert timing_safe_eq("café", "CAFÉ") is True  # case-insensitive + non-ASCII
    assert timing_safe_eq("café", "CAFÈ") is False


# ---- Length-leak prevention (hash normalises length) ----

def test_different_lengths_both_nonempty():
    assert timing_safe_eq("a", "aa") is False
    assert timing_safe_eq("aa", "a") is False


def test_very_different_lengths():
    assert timing_safe_eq("x", "x" * 1000) is False
    assert timing_safe_eq("x" * 1000, "x") is False


# ---- Type robustness ----

def test_none_inputs_treated_as_empty():
    assert timing_safe_eq(None, None) is True
    assert timing_safe_eq(None, "") is True
    assert timing_safe_eq(None, "a") is False
    assert timing_safe_eq("a", None) is False


def test_non_string_inputs():
    """Non-string inputs that str() cleanly should still work."""
    assert timing_safe_eq(123, 123) is True
    assert timing_safe_eq(123, 456) is False


def test_bytes_inputs():
    assert timing_safe_eq(b"hello", b"hello") is True
    assert timing_safe_eq(b"hello", b"world") is False


# ---- Consistency: same inputs always same result ----

@pytest.mark.parametrize("a,b,expected", [
    ("", "", True),
    ("a", "a", True),
    ("a", "b", False),
    ("Hello", "hello", True),
    ("héllo", "héllo", True),
    ("héllo", "hëllo", False),
    ("🔑", "🔑", True),
    ("A1B2", "a1b2", True),
    ("A1B2", "a1b3", False),
    (None, None, True),
    (None, "x", False),
    ("x", None, False),
])
def test_parametrized(a, b, expected):
    assert timing_safe_eq(a, b) is expected

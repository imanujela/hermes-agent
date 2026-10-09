"""One owner for the question: are these two strings equal in constant time?

The rule (architecture report #5, discovered independently at three call sites):
timing-safe string comparison must handle three divergent concerns that each
site patched in isolation:

1. **Non-ASCII** (webhook ``_hmac_str_equal``): ``hmac.compare_digest`` raises
   ``TypeError`` on a ``str`` containing non-ASCII characters, and
   attacker-controlled headers may carry any byte.  The webhook wrapper
   encoded to UTF-8 bytes before calling ``compare_digest``.

2. **Case-insensitivity + pre-hashing** (pairing ``approve_code`` /
   ``approve_request``): the pairing code is hashed with a salt before
   comparison, and request-id comparison lowercases both sides.  Hashing
   normalises length; lowercasing makes hex ids case-insensitive.

3. **Bare ``compare_digest``** (dashboard ``verify_token`` docstring): tells
   implementers to use ``hmac.compare_digest`` directly, which raises on
   non-ASCII and leaks length on unequal-length strings.

The unified seam ``timing_safe_eq`` handles all three by:

- Lowercasing both inputs (case-insensitive).
- Hashing both with SHA-256 (normalises length → fixed-width 64-char hex
  digests, avoiding both the length-leak of bare ``compare_digest`` and the
  ``TypeError`` on non-ASCII ``str`` — hex digests are pure ASCII).
- Comparing the hex digests with ``hmac.compare_digest`` (truly constant-time
  on equal-length ASCII byte strings).

Callers keep their own hashing/salting for storage (pairing's salted hash is
a *storage* concern, not a comparison concern); this seam owns the *comparison*
rule, so a new edge case (encoding, normalisation, length) is fixed in one place.

    from hermes_cli.auth_compare import timing_safe_eq
    if not timing_safe_eq(provided_secret, expected_secret):
        reject()
"""

from __future__ import annotations

import hashlib
import hmac

__all__ = ["timing_safe_eq"]


def timing_safe_eq(a: str, b: str) -> bool:
    """Constant-time, case-insensitive, non-ASCII-safe string equality.

    Both inputs are lowercased, SHA-256-hashed, and the resulting hex digests
    are compared with :func:`hmac.compare_digest`.  This gives:

    - **Non-ASCII safe**: hex digests are pure ASCII, so ``compare_digest``
      never raises ``TypeError``.
    - **Length-leak resistant**: both digests are always 64 hex chars, so
      ``compare_digest`` runs on equal-length inputs regardless of the
      original string lengths.
    - **Case-insensitive**: ``.lower()`` before hashing normalises case.

    ``None`` and non-``str`` inputs are coerced to ``str`` (``None`` → ``""``).
    """
    sa = "" if a is None else str(a)
    sb = "" if b is None else str(b)
    ha = hashlib.sha256(sa.lower().encode("utf-8")).hexdigest()
    hb = hashlib.sha256(sb.lower().encode("utf-8")).hexdigest()
    return hmac.compare_digest(ha, hb)

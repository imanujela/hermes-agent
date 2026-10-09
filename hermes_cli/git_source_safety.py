"""One owner for the question: is this string safe to hand to ``git``?

The rule (RC3 security wave, discovered independently at three call sites):
a source is UNSAFE when it could be parsed as a git option (leading ``-``,
including the bare ``-``/``--``) or names one of git's external transport
helpers (``ext::``/``fd::`` and any ``name::`` scheme — these EXECUTE a
command). Everything else that git itself accepts (https, ssh, scp-style,
git-daemon, github shorthand, trailing ``.git`` names) is safe to attempt.

Callers keep their own error types; this interface owns the rule, so a new
transport scheme or option-injection class is fixed in exactly one place:

    reason = reject_reason(source)      # None | "option-injection" | "external-transport" | "empty"
    url = canonical_git_url(source)     # github.com/o/r -> https://github.com/o/r; else unchanged

Ordering constraint for argv builders: always pass the source after ``--``
end-of-options separator (all in-repo git call sites now do).
"""

from __future__ import annotations

import re

# ``name::`` transport helpers (ext::, fd::, …) exec commands; only conservative
# URL-ish characters precede the scheme marker, matching git's own parsing shape.
_EXTERNAL_TRANSPORT_RE = re.compile(r"^[A-Za-z0-9._+-]+::")
_GITHUB_SHORTHAND_RE = re.compile(r"^github\.com/[\w.-]+/[\w.-]+/?$")

__all__ = ["reject_reason", "canonical_git_url"]


def reject_reason(source: str) -> str | None:
    """Return the unsafe class for *source*, or ``None`` when it is safe to attempt."""
    s = (source or "").strip()
    if not s:
        return "empty"
    if s.startswith("-"):
        return "option-injection"
    if _EXTERNAL_TRANSPORT_RE.match(s):
        return "external-transport"
    return None


def canonical_git_url(source: str) -> str:
    """Expand the ``github.com/<owner>/<repo>`` shorthand to an https URL; leave
    every other accepted source byte-identical (callers still pass it after ``--``)."""
    s = (source or "").strip()
    if _GITHUB_SHORTHAND_RE.match(s):
        return f"https://{s.rstrip('/')}"
    return source

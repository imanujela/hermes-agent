"""Regression lock for the dashboard fs sensitive-path mirror (HIGH#3 fix).

``hermes_cli.web_routers.files._is_sensitive_path`` mirrors the two canonical
agent-side guards — ``agent.file_safety.get_read_block_error`` and the
``HOME_CREDENTIAL_DIRS`` component-sequence match — so the Files tab can never
lag behind them (issue #57505 exfil surface). This file is a pure unit test:
it constructs ``Path`` objects and asserts the guard's verdict directly. No
HTTP, no app, no routes.

Two kinds of cases matter:

* BLOCKED cases pin every channel the guard is supposed to cover: the local
  basename denylists, the credential-dir sequence match (including its
  case-insensitivity and every entry of ``HOME_CREDENTIAL_DIRS``), and the
  delegation to ``get_read_block_error`` itself (the ``skills/.hub`` probe is
  blocked ONLY by that delegation — no dashboard denylist would catch it).
* ALLOWED cases pin the precision: a similarly-named directory
  (``.sshmirror``) must NOT false-positive, because the sequence match is on
  exact path components.

A failing BLOCKED case means the mirror has rotted — treat it as a real bug in
the guard, do not weaken the test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent import file_safety
from hermes_cli.web_routers import files as web_files

_is_sensitive = web_files._is_sensitive_path


# ---------------------------------------------------------------------------
# Blocked: every channel the mirror is supposed to cover
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        # Loose credential basenames in HOME (no protective directory).
        str(Path.home() / ".ssh" / "id_rsa"),
        str(Path.home() / ".netrc"),
        str(Path.home() / ".npmrc"),
        # Credential-dir trees anywhere on disk (HOME_CREDENTIAL_DIRS).
        "/x/.aws/credentials",
        "/x/.kube/config",          # basename 'config' is NOT denylisted -> dir match
        "/x/.docker/config.json",
        "/p/.config/gh/hosts.yml",  # two-component sequence ('.config', 'gh')
        # Case-insensitive component match (case-variant Windows-style path).
        "C:/HOME/U/.SSH/id_ed25519",
        # Same, with a neutral basename so ONLY the lower-cased dir match can
        # block it — pins case-insensitivity of the sequence check itself.
        "C:/HOME/U/.SSH/store.dat",
    ],
    ids=[
        "ssh-id_rsa", "home-netrc", "home-npmrc",
        "aws-credentials", "kube-config", "docker-config", "gh-hosts",
        "windows-case-ssh-key", "windows-case-ssh-neutral",
    ],
)
def test_credential_paths_are_blocked(raw: str) -> None:
    assert _is_sensitive(Path(raw)) is True


@pytest.mark.parametrize("rel", file_safety.HOME_CREDENTIAL_DIRS)
def test_every_home_credential_dir_tree_is_blocked(rel: str) -> None:
    """Full sweep: no entry of HOME_CREDENTIAL_DIRS may silently drop out of
    the dashboard mirror. Neutral basename forces the dir-sequence channel."""
    p = Path("/anchor", *[part for part in rel.split("/") if part], "store.bin")
    assert _is_sensitive(p) is True


def test_hermes_home_auth_json_blocked(tmp_path, monkeypatch) -> None:
    """HERMES_HOME control-file shape, with a monkeypatched home so the case
    is deterministic instead of env-dependent. ``_hermes_dirs`` is what
    ``get_read_block_error`` consults to find credential stores."""
    fake_home = (tmp_path / "hermes-home").resolve()
    monkeypatch.setattr(file_safety, "_hermes_dirs", lambda: [fake_home])
    assert _is_sensitive(fake_home / "auth.json") is True


def test_file_safety_delegation_probe(tmp_path, monkeypatch) -> None:
    """Anti-rot probe for the delegation itself: ``<HERMES_HOME>/skills/.hub``
    is blocked ONLY by ``get_read_block_error`` — it is in none of the
    dashboard-local denylists. If this fails, the mirror stopped consulting
    the canonical agent-side guard."""
    fake_home = (tmp_path / "hermes-home").resolve()
    monkeypatch.setattr(file_safety, "_hermes_dirs", lambda: [fake_home])
    assert _is_sensitive(fake_home / "skills" / ".hub" / "bundle.md") is True


# ---------------------------------------------------------------------------
# Allowed: precision — similar names must NOT false-positive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "/repo/src/main.py",
        "/tmp/notes.txt",
        "/x/ssh_utils.py",  # contains 'ssh' but no '.ssh' component
        # Directory named SIMILARLY to '.ssh': sequence match is on exact
        # components, so '.sshmirror' must never match '.ssh'.
        "/home/u/backup/.sshmirror/readme.md",
        "/home/u/backup/.sshmirror/config",
    ],
    ids=["repo-src", "tmp-notes", "ssh-utils-py", "sshmirror-readme", "sshmirror-config"],
)
def test_ordinary_paths_are_allowed(raw: str) -> None:
    assert _is_sensitive(Path(raw)) is False


def test_hermes_home_ordinary_file_allowed(tmp_path, monkeypatch) -> None:
    """The denylist under HERMES_HOME must stay scoped: a random file in the
    same fake home is readable, so the guard is not blanket-refusing."""
    fake_home = (tmp_path / "hermes-home").resolve()
    monkeypatch.setattr(file_safety, "_hermes_dirs", lambda: [fake_home])
    assert _is_sensitive(fake_home / "sessions" / "notes.md") is False

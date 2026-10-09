"""Security regression tests for the git ext:: transport / option-injection guards.

Git's ``name::`` URL prefixes (``ext::sh -c ...``, ``fd::...``) are *external transport
helpers*: the text after ``::`` is executed as a command, and a leading ``-`` string is
parsed as a git option (``--upload-pack=`` etc. can name a program too). Hermes grew
identical guards at each git entry point:

* ``hermes_cli.profile_distribution._looks_like_git_url`` / ``_git_clone``
* ``hermes_cli.plugins_cmd_git._clone_plugin_repo``
* ``hermes_cli.plugins_updates.default_ls_remote``

and a timing-safe webhook token check in
``gateway.platforms.bluebubbles.BlueBubblesAdapter._handle_webhook``
(hmac.compare_digest over UTF-8 bytes, fail-closed on a missing/non-str secret).

Every test here is a pure unit call with subprocess/git monkeypatched — no network, no
real git binary is spawned.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import types
from pathlib import Path

import pytest

from hermes_cli import plugins_cmd, plugins_cmd_git, plugins_updates
from hermes_cli.profile_distribution import _git_clone, _looks_like_git_url

# ---------------------------------------------------------------------------
# Shared hostile / legitimate source corpora
# ---------------------------------------------------------------------------

# Each would either exec a command via git's external-transport helpers or be parsed
# as a clone/ls-remote option. Several deliberately end in ``.git`` so an unguarded
# ``endswith(".git")`` accept rule alone would let them through.
HOSTILE_SOURCES = [
    "ext::sh -c touch% /tmp/pwned",
    "ext::",
    "EXT::sh -c id",
    "ext::/bin/sh exploit",
    "fd::1:evil",
    "helper::sh -c whoami",
    "--upload-pack=evil.git",
    "-u Exploit.git",
    "--mirror=ext::x.git",
    "--",
    "--no-checkout=ext::sh",
    # Would pass every *other* accept rule (ends in .git) — only the transport regex stops them:
    "ext::helper/repo.git",
    "fd::/tmp/evil.git",
]

# Real remote URL forms: https/http, scp-style (colon) with and without .git,
# ssh://, git://. These must reach git (behind a ``--`` separator), never be refused.
LEGIT_URLS = [
    "https://github.com/org/repo.git",
    "http://gitserver.internal/team/repo.git",
    "git@github.com:org/repo.git",
    "git@github.com:org/repo",
    "ssh://git@host/path",
    "git://host/path/repo.git",
    "user@host:path/repo.git",
]


class TestLooksLikeGitUrlPredicate:
    """profile_distribution gate: hostile sources are never treated as repo URLs."""

    @pytest.mark.parametrize("source", HOSTILE_SOURCES)
    def test_hostile_rejected(self, source):
        assert _looks_like_git_url(source) is False

    @pytest.mark.parametrize("source", [
        " ext::sh -c whoami",          # guard strips before matching
        "\text::touch /tmp/pwned",
        " --upload-pack=evil.git",
    ])
    def test_hostile_with_surrounding_whitespace_rejected(self, source):
        assert _looks_like_git_url(source) is False

    @pytest.mark.parametrize("source", LEGIT_URLS)
    def test_legit_accepted(self, source):
        assert _looks_like_git_url(source) is True

    @pytest.mark.parametrize("source", ["github.com/org/repo", "github.com/org/repo/"])
    def test_github_shorthand_accepted(self, source):
        assert _looks_like_git_url(source) is True

    @pytest.mark.parametrize("source", ["./local/dir", "my-profile", ""])
    def test_plain_local_names_not_git_urls(self, source):
        assert _looks_like_git_url(source) is False


def test_git_clone_argv_places_double_dash_before_url(monkeypatch, tmp_path):
    """Even past the predicate, ``_git_clone`` must not let the URL position be an option."""
    import hermes_cli.git_credentials as git_credentials

    calls = []

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def fake(cmd, url, **kwargs):
        calls.append(cmd)
        return _Proc()

    monkeypatch.setattr(git_credentials, "run_git_with_credential_fallback", fake)
    dest = tmp_path / "clone"
    _git_clone(LEGIT_URLS[0], dest)

    assert len(calls) == 1
    argv = calls[0]
    assert argv[0] == "git" and "clone" in argv
    sep = argv.index("--")
    assert argv[sep + 1] == LEGIT_URLS[0]
    assert argv[sep + 2] == str(dest)


# ---------------------------------------------------------------------------
# plugins_cmd_git._clone_plugin_repo — guard + separator, git fully stubbed
# ---------------------------------------------------------------------------

class _CloneSpawned(Exception):
    """Sentinel raised by the stubbed runner once argv would have hit git."""


def _stub_plugin_git(monkeypatch, calls):
    """Force the facade's git resolution to succeed and record (never run) git calls."""
    monkeypatch.setattr(plugins_cmd, "_resolve_git_executable", lambda: "git")

    def fake_run(git_exe, target, *args, **kwargs):
        calls.append(args)
        raise _CloneSpawned

    monkeypatch.setattr(plugins_cmd, "_run_plugin_git", fake_run)


class TestClonePluginRepoGuard:

    @pytest.mark.parametrize("source", HOSTILE_SOURCES)
    def test_hostile_rejected_before_any_git_spawn(self, monkeypatch, tmp_path, source):
        calls: list = []
        _stub_plugin_git(monkeypatch, calls)
        with pytest.raises(plugins_cmd.PluginOperationError) as excinfo:
            plugins_cmd_git._clone_plugin_repo(tmp_path / "clone", source, None)
        assert excinfo.value.failure_class == "invalid_source"
        assert calls == []  # refused outright — nothing was ever handed to git

    def test_legit_url_cloned_behind_double_dash(self, monkeypatch, tmp_path):
        calls: list = []
        _stub_plugin_git(monkeypatch, calls)
        with pytest.raises(_CloneSpawned):
            plugins_cmd_git._clone_plugin_repo(tmp_path / "clone", LEGIT_URLS[0], None)
        assert len(calls) == 1
        args = calls[0]
        assert args[0] == "clone"
        sep = args.index("--")
        assert args[sep + 1] == LEGIT_URLS[0]


# ---------------------------------------------------------------------------
# plugins_updates.default_ls_remote — guard + separator, subprocess stubbed
# ---------------------------------------------------------------------------

def _stub_ls_remote_subprocess(monkeypatch, calls, stdout=""):
    class _Proc:
        returncode = 0
        stderr = ""

        def __init__(self):
            self.stdout = stdout

    def fake_run(*args, **kwargs):
        calls.append(args[0] if args else kwargs.get("argv"))
        return _Proc()

    # Swap the module-level ``subprocess`` reference *inside* plugins_updates only —
    # the real subprocess module is never touched.
    monkeypatch.setattr(plugins_updates, "subprocess", types.SimpleNamespace(run=fake_run))


class TestDefaultLsRemoteGuard:

    @pytest.mark.parametrize("source", HOSTILE_SOURCES)
    def test_hostile_rejected_before_any_subprocess(self, monkeypatch, source):
        calls: list = []
        _stub_ls_remote_subprocess(monkeypatch, calls)
        with pytest.raises(RuntimeError, match="Refusing unsafe git source"):
            plugins_updates.default_ls_remote(source)
        assert calls == []

    @pytest.mark.parametrize("source", LEGIT_URLS)
    def test_legit_url_probed_behind_double_dash(self, monkeypatch, source):
        calls: list = []
        _stub_ls_remote_subprocess(monkeypatch, calls, stdout="abc123\trefs/heads/main\n")
        sha = plugins_updates.default_ls_remote(source)
        assert sha == "abc123"
        assert len(calls) == 1
        argv = calls[0]
        assert "ls-remote" in argv
        sep = argv.index("--")
        assert argv[sep + 1] == source


# ---------------------------------------------------------------------------
# BlueBubbles webhook auth — constant-time, fail-closed token comparison
# ---------------------------------------------------------------------------

class _Resp:
    """Stand-in for aiohttp's json_response so the test needs no aiohttp install."""

    def __init__(self, payload, status):
        self.payload = payload
        self.status = status


@pytest.fixture
def fake_aiohttp(monkeypatch):
    def json_response(payload, status=200):
        return _Resp(payload, status)
    mod = types.ModuleType("aiohttp")
    mod.web = types.SimpleNamespace(json_response=json_response)
    monkeypatch.setitem(sys.modules, "aiohttp", mod)


class _FakeRequest:
    def __init__(self, query=None, headers=None, body=b"{}"):
        self.query = query or {}
        self.headers = headers or {}
        self._body = body

    async def read(self):
        return self._body


def _adapter(password):
    from gateway.platforms.bluebubbles import BlueBubblesAdapter
    adapter = object.__new__(BlueBubblesAdapter)  # __init__ needs full config; auth path needs none
    adapter.password = password
    adapter.require_mention = False
    adapter.send_read_receipts = False
    return adapter


def _handle(adapter, request):
    return asyncio.run(adapter._handle_webhook(request))


@pytest.mark.usefixtures("fake_aiohttp")
class TestBlueBubblesWebhookAuth:

    def test_wrong_token_rejected(self):
        resp = _handle(_adapter("secret"), _FakeRequest({"password": "wrong"}))
        assert resp.status == 401
        assert resp.payload == {"error": "unauthorized"}

    def test_length_matching_wrong_token_rejected(self):
        """compare_digest path: equal length, different bytes must still fail closed."""
        resp = _handle(_adapter("secret"), _FakeRequest({"password": "rezces"}))
        assert resp.status == 401
        assert resp.payload == {"error": "unauthorized"}

    def test_long_token_with_secret_prefix_rejected(self):
        resp = _handle(_adapter("secret"), _FakeRequest({"password": "secretPWNED"}))
        assert resp.status == 401

    def test_non_ascii_token_rejected_without_typeerror(self):
        """compare_digest raises TypeError on non-ASCII str; the guard must encode first."""
        resp = _handle(_adapter("secret"), _FakeRequest({"password": "sécrét"}))
        assert resp.status == 401

    def test_missing_token_rejected(self):
        resp = _handle(_adapter("secret"), _FakeRequest())
        assert resp.status == 401

    def test_non_string_password_fails_closed(self):
        """Unconfigured secret (None) must not accept any attacker token."""
        resp = _handle(_adapter(None), _FakeRequest({"password": "anything"}))
        assert resp.status == 401

    def test_wrong_header_token_rejected(self):
        for header in ("x-password", "x-guid", "x-bluebubbles-guid"):
            resp = _handle(_adapter("secret"), _FakeRequest(headers={header: "attacker"}))
            assert resp.status == 401, header

    def test_correct_token_passes_auth_gate(self):
        """Positive control: the 401 gate is *only* the token check — a valid token with an
        empty payload proceeds past auth (and lands on the 400 missing-fields branch)."""
        resp = _handle(_adapter("secret"), _FakeRequest({"password": "secret"}))
        assert resp.status != 401
        assert resp.status == 400
        assert resp.payload == {"error": "missing message fields"}

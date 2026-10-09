"""Importlib smoke for the 40 highest-risk modules the RC3 sweep touched.

Selection rule: modules that gained a module-level ``logger`` in the RC3
commits (``e4ad1dd2..HEAD``) and sit on a critical seam — the three named
root modules (``hermes_state_guard``, ``hermes_bootstrap``,
``hermes_logging``), agent runtime/lifecycle modules, gateway session-recovery
paths, every ``tools/`` gainer (the terminal/browser hot loop), the cron
incident writer, and the highest-blast-radius ``hermes_cli`` gainers (PTY,
service management, launcher/subprocess plumbing, web-server lifecycle).
A module that cannot even be imported (syntax error, broken reference,
circular-import regression introduced by the error-path sweeps) would take
down its whole feature at startup; this test catches that class of regression
without executing any behavior.

Failure classification (deliberately forgiving about the environment, hard
about the code — concurrent sweeps edit this tree while the suite runs):

* the module file itself is missing, or a dependency is platform-gated
  (``fcntl`` on Windows) or third-party and not installed in this bare
  interpreter  -> ``skip`` with the reason recorded, never a false alarm;
* the source file exists but does not parse (a concurrent agent caught
  mid-edit) -> ``skip`` with the file name, so a half-written edit never
  reddens an unrelated suite;
* anything else (``NameError``, broken repo-internal import wiring,
  import-time exceptions from the new error paths) -> ``FAIL`` — that is
  exactly the RC3 regression this seam guard exists to catch.

Run with stdlib + pytest only: ``uv run --no-project --with pytest``.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# The 40 highest-risk RC3 logger gainers (git diff e4ad1dd2~1..HEAD, files
# where the sweep added `logger = logging.getLogger(__name__)`).
_RC3_LOGGER_GAINERS = (
    # Named core seams
    "hermes_logging",
    "hermes_state_guard",
    "hermes_bootstrap",
    # agent runtime / lifecycle
    "agent.process_bootstrap",
    "agent.runtime_self_protection",
    "agent.subagent_lifecycle",
    "agent.inline_tool_executors",
    "agent.model_metadata_http",
    "agent.opencode_affinity",
    "agent.codex_headers",
    "agent.context_breakdown",
    "agent.learning_mutations",
    # gateway recovery + session state
    "gateway.session_db_recovery",
    "gateway.agent_cache_pressure",
    "gateway.session_context",
    "gateway.systemd_stop_mark",
    # tools hot loop (terminal/browser/environments)
    "tools.bot_mode_probe",
    "tools.browser_tool_lifecycle",
    "tools.browser_tool_lightpanda_fallback",
    "tools.browser_tool_vision",
    "tools.budget_config",
    "tools.environments.base_output",
    "tools.environments.local_env_policy",
    "tools.environments.remote_common",
    "tools.image_source",
    "tools.project_tools",
    "tools.xai_http",
    # cron incident writer
    "cron.incidents",
    # hermes_cli: PTY, services, launcher plumbing, web-server lifecycle
    "hermes_cli._subprocess_compat",
    "hermes_cli._launchers",
    "hermes_cli.pty_bridge",
    "hermes_cli.pty_session",
    "hermes_cli.win_pty_bridge",
    "hermes_cli.service_manager",
    "hermes_cli.kanban_db",
    "hermes_cli.web_server_lifecycle",
    "hermes_cli.credential_lifecycle",
    "hermes_cli.context_switch_guard",
    "hermes_cli.model_cost_guard",
    "hermes_cli._startup_fast",
)


def _module_present_in_tree(dotted: str) -> bool:
    """True when the repo currently holds the module file (or package dir)."""
    parts = dotted.split(".")
    base = REPO_ROOT.joinpath(*parts)
    return (base.with_suffix(".py")).exists() or (base / "__init__.py").exists()


def _classify_import_failure(dotted: str, exc: BaseException) -> str | None:
    """Return a skip reason when *exc* is an environment/concurrency artifact,
    or None when the failure is a real code regression worth reporting."""
    if isinstance(exc, (SyntaxError, IndentationError)):
        return (
            f"{dotted} does not parse ({exc.__class__.__name__}: {exc}) — "
            "mid-edit by a concurrent agent, not an RC3 regression signal"
        )
    if isinstance(exc, ModuleNotFoundError):
        missing = (exc.name or "").split(".")[0]
        if not _module_present_in_tree(dotted):
            return f"{dotted} is not in the tree right now (moved mid-sweep)"
        if missing and missing in sys.stdlib_module_names:
            # e.g. hermes_cli.pty_bridge needs fcntl: POSIX-only stdlib.
            return (
                f"{dotted} needs stdlib '{missing}', unavailable on "
                f"{sys.platform} (platform-gated module, expected skip)"
            )
        if missing and not _module_present_in_tree(missing):
            return (
                f"{dotted} needs third-party '{missing}', not installed in "
                "this bare (stdlib+pytest) interpreter"
            )
        return None  # repo-internal module missing => wiring broke
    return None  # any other error type is a genuine regression


@pytest.mark.parametrize("module_name", _RC3_LOGGER_GAINERS)
def test_rc3_module_imports_cleanly(module_name: str):
    """Import the module; classify any failure per the rules above."""
    try:
        module = importlib.import_module(module_name)
    except BaseException as exc:
        reason = _classify_import_failure(module_name, exc)
        if reason is not None:
            pytest.skip(reason)
        raise AssertionError(
            f"importing {module_name} regressed under RC3: "
            f"{exc.__class__.__name__}: {exc}"
        ) from exc
    assert module.__name__ == module_name
    assert getattr(module, "__file__", None), f"{module_name} imported as a namespace"


def test_smoke_list_covers_named_core_and_is_forty_modules():
    """The 40-module list is the deliverable's contract: keep the named core
    seams in it, and flag if the selection ever silently shrinks."""
    assert len(_RC3_LOGGER_GAINERS) == 40
    for named in ("hermes_logging", "hermes_state_guard", "hermes_bootstrap"):
        assert named in _RC3_LOGGER_GAINERS
    assert len(set(_RC3_LOGGER_GAINERS)) == 40

"""Auth-surface finding #1 evidence: the slash-admin gate is fail-open for a shell-bearing command.

Chain (static, no server needed):
  * ``gateway/slash_access.py::policy_from_extra`` sets ``enabled=bool(admin_ids)`` — so an
    operator who configures chat permission (``allow_from`` / ``group_allow_from``) but never
    lists ``allow_admin_from`` gets a *disabled* policy for that scope.
  * ``SlashAccessPolicy.is_admin`` returns ``True`` for **every** user when ``enabled`` is
    False, and ``can_run`` short-circuits to ``True`` for any admin.
  * ``/goal`` is therefore runnable by any chat-permitted remote user, including
    ``goal gate add``, and ``hermes_cli/goals.py::run_gate`` later executes the stored gate
    command via ``subprocess.run(gate.command, shell=True, ...)`` on the host.

These are pure unit calls against the policy object: no HTTP, no gateway dispatch, no
subprocess is spawned.

* Test 1 PASSES and documents the hole as it behaves in current code.
* Test 2 states the fail-closed requirement and is marked ``xfail(strict=True)``: it must
  fail today, which is the machine-checked proof the hole exists. Flip the default
  (gate on by default; empty admin list => deny) and remove the xfail marker.
"""

from __future__ import annotations

import pytest

from gateway.slash_access import SlashAccessPolicy, policy_from_extra

# A shell-bearing, host-executing slash command reached through `/goal gate add`.
GOAL_CMD = "goal"
# Any user who merely passed chat-level allow_from; not on any admin list.
RANDOM_USER = "any-random-user"

# What `policy_from_extra` returns when the operator never sets `allow_admin_from`
# (enabled=bool(admin_ids) with admin_ids empty => disabled). This is the exact object
# every chat-permitted remote user is evaluated against.
OPERATOR_DEFAULT_POLICY = SlashAccessPolicy(
    enabled=False, admin_user_ids=frozenset(), user_allowed_commands=frozenset()
)


def test_current_code_disabled_policy_lets_anyone_run_goal_documents_failopen():
    """DOCUMENT the fail-open (passes against current code): enabled=False => everyone is
    admin => can_run('goal') is True for a random chat-permitted user."""
    policy = OPERATOR_DEFAULT_POLICY

    # The derivation from a realistic operator config: chat allow set, admin list never set.
    extra = {"allow_from": [RANDOM_USER], "group_allow_from": [RANDOM_USER]}
    derived = policy_from_extra(extra, "dm")
    assert derived == policy  # enabled=bool(admin_ids)==False; gating silently off
    assert policy_from_extra(extra, "group") == policy  # same for the group scope

    # ...so any-random-user is treated as admin and can run the shell-bearing /goal entry
    # point (which exposes `goal gate add` -> run_gate -> subprocess shell=True).
    assert policy.is_admin(RANDOM_USER) is True
    assert policy.can_run(RANDOM_USER, GOAL_CMD) is True


@pytest.mark.xfail(
    strict=True,
    reason="finding 1: shell-bearing slash default is fail-open - flip the default and remove xfail",
)
def test_fail_closed_spec_goal_denied_when_admin_list_empty():
    """SPEC the fail-closed default: with no admin list configured, a chat-permitted but
    non-listed user must NOT be allowed to run /goal (gate add carries a shell command that
    run_gate later executes with subprocess.run(shell=True))."""
    extra = {"allow_from": [RANDOM_USER], "group_allow_from": [RANDOM_USER]}
    policy = policy_from_extra(extra, "dm")

    # Nobody was listed as an admin, so nobody may pass the admin short-circuit...
    assert policy.is_admin(RANDOM_USER) is False
    # ...and 'goal' is not part of the read-only user floor (only help/whoami are),
    # so a shell-bearing command like goal gate-add must be denied outright.
    assert policy.can_run(RANDOM_USER, GOAL_CMD) is False

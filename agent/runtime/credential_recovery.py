
"""Credential-pool recovery: auth/rate-limit failover, entitlement 403 handling,
and primary-runtime restore after a forced rotation. Split from
``agent.agent_runtime_helpers``."""

from __future__ import annotations
import contextlib
import time
from datetime import datetime
from typing import Any, Optional
from agent.credential_pool import STATUS_EXHAUSTED, credential_pool_entry_serves_endpoint, credential_pool_matches_provider, resolve_runtime_pool_key
from agent.error_classifier import FailoverReason
import logging
from agent.runtime._runtime_ref import _ra
from agent.runtime.transport_clients import _build_anthropic_client_from_runtime
from hermes_suppress import suppressed
logger = logging.getLogger(__name__)


# Cap same-entry OAuth refreshes on a persistent auth failure, else a single-entry pool re-mints forever.
_MAX_AUTH_REFRESH_ATTEMPTS = 2


def sync_credential_pool_entry_id(agent) -> None:
    """Rebind ``agent._credential_pool_entry_id`` from the current pool + key. OAuth refreshes
    can replace the token before recovery runs, so the key alone cannot attribute a failure;
    the stable entry ID can. Cleared when no pool is bound."""
    pool = getattr(agent, "_credential_pool", None)
    try:
        agent._credential_pool_entry_id = (
            pool.entry_id_for_api_key(getattr(agent, "api_key", None)) if pool is not None else None
        )
    except Exception:
        agent._credential_pool_entry_id = None


_STATUS_TO_FAILOVER_REASON = {
    402: FailoverReason.billing, 429: FailoverReason.rate_limit, 401: FailoverReason.auth,
    403: FailoverReason.auth,
}


_USAGE_LIMIT_REASON_TOKENS = ("usage_limit_reached", "gousagelimit")


_USAGE_LIMIT_MESSAGE_TOKENS = ("usage limit reached", "usage limit has been reached")


def _failed_credential_identity(agent, pool) -> tuple[Optional[str], Optional[str]]:
    """``(api_key_hint, credential_id)`` of the key actually dispatched, not ``pool.current()``:
    the shared pointer often points at a different healthy entry, and marking it exhausted
    can take the whole pool offline from one 429."""
    api_key_hint = getattr(agent, "api_key", None) or None
    raw_id = getattr(agent, "_credential_pool_entry_id", None)
    credential_id = raw_id if isinstance(raw_id, str) and raw_id else None
    if not api_key_hint:
        cur = pool.current()
        if cur:
            api_key_hint = getattr(cur, "runtime_api_key", None)
            if not credential_id:
                current_id = getattr(cur, "id", None)
                if isinstance(current_id, str) and current_id:
                    credential_id = current_id
    return api_key_hint, credential_id


def _is_entitlement_403(agent, status_code, error_context) -> bool:
    """Entitlement 403s look like auth failures but refresh cannot fix them. Any xai-oauth 403
    is entitlement EXCEPT xAI's stale-token signals (``[WKE=unauthenticated:...]``,
    "could not be validated"), which must stay refreshable."""
    if agent._is_entitlement_failure(error_context, status_code):
        return True
    if status_code != 403:
        return False
    haystack = " ".join(
        # Subscription/entitlement 403s look like auth failures on the wire but refresh cannot fix them —
        # the OAuth token is already valid, the account simply lacks the entitlement. Without this guard,
        # the refresh path keeps minting fresh tokens against the same unsubscribed account and the main
        # agent loop spins re-issuing the same 403 until the user Ctrl+C's. Defense-in-depth for #26847:
        # xAI's backend has been seen to 403 standard SuperGrok subscribers with bodies that don't match the
        # existing entitlement keyword set in ``_is_entitlement_failure``. Any 403 against ``xai-oauth`` is
        # treated as entitlement here so the refresh loop can't spin in those cases either. Exception
        # (#29344): xAI's ``[WKE=unauthenticated:...]`` suffix and the ``OAuth2 access token could not be
        # validated`` phrasing are xAI's authoritative "this is a stale token, not entitlement" signal. When
        # either fires we must NOT apply the catch-all override — refresh is the recoverable path for these
        # bodies, and blanket-classifying them as entitlement was the bug that left long-running TUI
        # sessions stuck on stale tokens until the user exited and reopened.
        str(error_context.get(k) or "").lower()
        for k in ("message", "reason", "code", "error")
        if isinstance(error_context, dict)
    )
    if "oauth authentication is currently not allowed for this organization" in haystack:
        return True
    provider = agent.provider or ""
    if provider == "anthropic" and getattr(agent, "api_mode", "") == "anthropic_messages":
        return True
    if provider == "xai-oauth":
        return not (
            "[wke=unauthenticated:" in haystack
            or "oauth2 access token could not be validated" in haystack
        )
    return False


def _recover_auth_failure(agent, pool, *, status_code, has_retried_429, error_context, api_key_hint, credential_id, rotate_and_swap):
    if _is_entitlement_403(agent, status_code, error_context):
        _ra().logger.info(
            "Credential %s — entitlement-shaped 403 from %s; "
            "skipping pool refresh (account lacks subscription, "
            "not a transient auth failure).", status_code if status_code is not None else "auth",
            agent.provider or "provider",
        )
        return False, has_retried_429
    # Refresh the entry that supplied the failing key, not current(): refreshing a healthy entry
    # burns its single-use refresh token for a failure it never had.
    refresh_kwargs = {"api_key_hint": api_key_hint}
    if credential_id:
        refresh_kwargs["credential_id"] = credential_id
    refreshed = pool.try_refresh_matching(**refresh_kwargs)
    if refreshed is None:
        # Refresh failed; rotate (the failed entry is already marked exhausted).
        return (True, False) if rotate_and_swap(401, "auth refresh failed") else (False, has_retried_429)
    # try_refresh_matching() reports success even when upstream keeps rejecting; cap same-entry
    # refreshes so a single-entry pool falls through to fallback.
    refreshed_id = getattr(refreshed, "id", None)
    if refreshed_id is not None:
        if getattr(agent, "_auth_pool_refresh_counts", None) is None:
            agent._auth_pool_refresh_counts = {}
        refresh_counts = agent._auth_pool_refresh_counts
        refresh_key = (agent.provider, refreshed_id)
        refresh_counts[refresh_key] = refresh_counts.get(refresh_key, 0) + 1
        if refresh_counts[refresh_key] > _MAX_AUTH_REFRESH_ATTEMPTS:
            _ra().logger.warning(
                "Credential auth failure persists after %s refreshes for "
                "pool entry %s — treating as unrecoverable and allowing "
                "fallback to activate.", refresh_counts[refresh_key] - 1, refreshed_id,
            )
            return False, has_retried_429
    _ra().logger.info("Credential auth failure — refreshed pool entry %s", getattr(refreshed, 'id', '?'))
    if agent._swap_credential(refreshed) is False:
        return False, has_retried_429
    return True, has_retried_429


def _recover_rate_limit(pool, *, has_retried_429, error_context, api_key_hint, credential_id, rotate_and_swap):
    # Already-exhausted credential: rotate immediately. Avoids the "cancel-between-429s" trap where
    # the local has_retried_429 resets per prompt and retries forever.
    current_entry = None
    if credential_id:
        current_entry = next((e for e in pool.entries() if e.id == credential_id), None)
    if api_key_hint:
        current_entry = current_entry or next(
            (e for e in pool.entries() if e.runtime_api_key == api_key_hint), None
        )
    if current_entry is None:
        current_entry = pool.current()
    current_last_status = getattr(current_entry, "last_status", None) if current_entry else None
    if current_last_status == STATUS_EXHAUSTED:
        _ra().logger.info(
            "Credential already exhausted (last_status=%s) — rotating immediately instead of retrying",
            current_last_status,
        )
        return (True, False) if rotate_and_swap(429, "rate limit, pre-exhausted") else (False, True)
    usage_limit_reached = False
    if error_context:
        context_reason = str(error_context.get("reason") or "").lower()
        context_message = str(error_context.get("message") or "").lower()
        usage_limit_reached = any(t in context_reason for t in _USAGE_LIMIT_REASON_TOKENS) or any(
            t in context_message for t in _USAGE_LIMIT_MESSAGE_TOKENS
        )
    if not has_retried_429 and not usage_limit_reached:
        return False, True
    return (True, False) if rotate_and_swap(429, "rate limit") else (False, True)


def recover_with_credential_pool(
    agent, *, status_code: Optional[int], has_retried_429: bool,
    classified_reason: Optional[FailoverReason] = None,
    error_context: Optional[dict[str, Any]] = None, billing_unverified: bool = False,
) -> tuple[bool, bool]:
    """Attempt credential recovery via pool rotation; returns (recovered, has_retried_429).
    Rate limits: retry once, then rotate. Billing: rotate immediately. Auth: refresh before
    rotating. ``classified_reason`` beats raw HTTP codes (e.g. Anthropic 400 "out of extra
    usage"); ``billing_unverified`` gives the entry a short cooldown, not the one-hour bench."""
    pool = agent._credential_pool
    if pool is None:
        return False, has_retried_429
    # The pool belongs to the PRIMARY provider: acting on fallback errors would corrupt its state
    # and reset base_url to the primary endpoint. Empty pool provider means unscoped; empty agent
    # provider is a mismatch (swap would leave provider="" model="").
    # Defensive guard: if a fallback provider is active and its provider name doesn't match the pool's
    # provider, the pool belongs to the PRIMARY provider. Mutating it based on fallback errors would corrupt
    # the primary's credential state (see #33088) and, via _swap_credential, overwrite the agent's base_url
    # back to the primary's endpoint — every subsequent request then goes to the wrong host and 404s (see
    # #33163). The pool should only act when the agent is still on the same provider that seeded the pool.
    current_provider = (getattr(agent, "provider", "") or "").strip().lower()
    pool_provider = (getattr(pool, "provider", "") or "").strip().lower()
    if pool_provider and not credential_pool_matches_provider(
        pool, current_provider, base_url=getattr(agent, "base_url", None)
    ):
        # Same fail-closed boundary predicate as runtime binding.
        _ra().logger.warning(
            "Credential pool provider mismatch: pool=%s, agent=%s — "
            "skipping pool mutation to avoid cross-provider contamination",
            pool_provider, current_provider,
        )
        return False, has_retried_429
    api_key_hint, credential_id = _failed_credential_identity(agent, pool)
    effective_reason = classified_reason
    if effective_reason is None:
        effective_reason = _STATUS_TO_FAILOVER_REASON.get(status_code)

    def _rotate_and_swap(default_status: int, label: str) -> bool:
        """Rotate away from the failed credential; True when a new entry was swapped in."""
        rotate_status = status_code if status_code is not None else default_status
        kwargs = {
            "status_code": rotate_status,
            "error_context": error_context,
            "api_key_hint": api_key_hint,
        }
        if credential_id:
            kwargs["credential_id"] = credential_id
        # Pass classified semantics, not just the status: a billing 403 and an edge-throttle 403
        # need opposite cooldowns.
        if effective_reason is not None:
            failure_reason = effective_reason.value
            if effective_reason == FailoverReason.billing and billing_unverified:
                # Ambiguous billing body: size the cooldown as transient, not a 1-hour bench.
                from agent.credential_pool import FAILURE_REASON_BILLING_UNVERIFIED
                failure_reason = FAILURE_REASON_BILLING_UNVERIFIED
            kwargs["failure_reason"] = failure_reason
        model = getattr(agent, "model", None)
        if isinstance(model, str) and model.strip():
            kwargs["model"] = model
        next_entry = pool.mark_exhausted_and_rotate(**kwargs)
        if next_entry is None:
            return False
        if not credential_pool_entry_serves_endpoint(next_entry, getattr(agent, "base_url", None)):
            # Mixed same-provider pool (#68237): the entry serves another endpoint and _swap_credential
            # would rebind this session to it. Treat as no recovery, like a rotation that yields nothing.
            _ra().logger.info(
                "Credential %s (%s) — pool entry %s serves another endpoint; not swapping",
                rotate_status, label, getattr(next_entry, "id", "?"),
            )
            return False
        _ra().logger.info(
            "Credential %s (%s) — rotated to pool entry %s",
            rotate_status, label, getattr(next_entry, "id", "?"),
        )
        swapped = agent._swap_credential(next_entry) is not False
        benched = next((e for e in pool.entries() if e.id == credential_id), None) if credential_id else None
        if (
            swapped
            and benched is not None
            and benched.priority < getattr(next_entry, "priority", benched.priority)
            and not getattr(agent, "_credential_pool_revert_id", None)
            and effective_reason in (FailoverReason.rate_limit, FailoverReason.billing)
        ):
            # A quota bench (429/402) lifts when the window reopens, and a fresh session's
            # select() would go straight back to this entry; arm the per-turn hook so the live
            # session does too (#114501). Only when the benched entry OUTRANKS the one we rotated
            # to: a session that was already on the fallback (preferred benched elsewhere) and
            # rotates UP once the preferred window reopened must not be pulled back down when
            # the fallback's cooldown lifts. Keep the FIRST benched entry across chained
            # rotations — it is the preferred one. Auth benches are not windows; they stay.
            agent._credential_pool_revert_id = credential_id
        return swapped
    if effective_reason == FailoverReason.upstream_rate_limit:
        # Upstream (e.g. DeepSeek behind OpenRouter) is throttling the aggregator; the credential is
        # healthy. Do not rotate/exhaust; let fallback switch models.
        upstream = (error_context or {}).get("upstream_provider") if error_context else None
        if upstream:
            _ra().logger.info(
                "Upstream provider %s rate-limited via aggregator — skipping "
                "credential rotation, deferring to fallback chain", upstream,
            )
        else:
            _ra().logger.info(
                "Upstream aggregator 429 (provider unknown) — skipping "
                "credential rotation, deferring to fallback chain"
            )
        return False, has_retried_429
    if effective_reason == FailoverReason.billing:
        # A separate pool instance may have resolved runtime credentials, leaving no ``current_id``;
        # match the key that failed, not a different account.
        return (True, False) if _rotate_and_swap(402, "billing") else (False, has_retried_429)
    if effective_reason == FailoverReason.rate_limit:
        return _recover_rate_limit(
            pool, has_retried_429=has_retried_429, error_context=error_context,
            api_key_hint=api_key_hint, credential_id=credential_id, rotate_and_swap=_rotate_and_swap,
        )
    if effective_reason == FailoverReason.model_entitlement:
        # The pool benches (credential, model) only and hands back the next entry that is not
        # benched for this model; None once every entry rejected it, so the caller falls
        # through to the single-credential handling in _mark_entitlement_rejected_model (#71970).
        return _rotate_and_swap(400, "model entitlement"), has_retried_429
    if effective_reason == FailoverReason.auth:
        return _recover_auth_failure(
            agent, pool, status_code=status_code, has_retried_429=has_retried_429,
            error_context=error_context, api_key_hint=api_key_hint, credential_id=credential_id,
            rotate_and_swap=_rotate_and_swap,
        )
    return False, has_retried_429


def _apply_primary_runtime_fields(agent, rt: dict[str, Any]) -> None:
    """Copy the identity/transport fields of a ``_primary_runtime`` snapshot onto ``agent``
    (shared by transport recovery and turn-start restore; the caller rebuilds the client)."""
    agent.model = rt["model"]
    agent.provider = rt["provider"]
    agent.requested_provider = rt.get("requested_provider", agent.provider)
    agent.base_url = rt["base_url"]           # setter updates _base_url_lower
    from hermes_cli.providers import is_actual_route
    agent.api_mode = "chat_completions" if is_actual_route(agent.provider, agent.base_url) else rt["api_mode"]
    if hasattr(agent, "_transport_cache"):
        agent._transport_cache.clear()
    agent.api_key = rt["api_key"]
    agent._reasoning_echo_flag = rt.get("reasoning_echo_flag", False)
    agent.request_overrides = dict(rt.get("request_overrides") or {})
    agent._client_kwargs = dict(rt["client_kwargs"])


def _rebuild_primary_client(agent, rt: dict[str, Any], *, reason: str) -> None:
    """Rebuild the primary client from a ``_primary_runtime`` snapshot (MoA facade / native Anthropic / OpenAI wire)."""
    if (agent.provider or "").strip().lower() == "moa":
        # MoA has empty client_kwargs; rebuild via the shared facade factory so the
        # reference_callback relay survives recovery.
        from agent.moa_loop import build_moa_facade
        agent.client = build_moa_facade(agent, agent.model)
        # MoA is a virtual chat-completions provider. It never has real OpenAI client kwargs; restoring it
        # after a fallback must recreate the facade, not call OpenAI() with an empty api_key. Use the shared
        # factory so the restored facade keeps the reference_callback relay wired at init — a bare
        # MoAClient() would silently stop emitting moa.reference/moa.aggregating display events (#53802).
        agent._anthropic_client = None
    elif agent.provider == "bedrock" and agent.api_mode in ("anthropic_messages", "bedrock_converse"):
        from agent.bedrock_adapter import bind_bedrock_runtime
        bind_bedrock_runtime(agent, agent.base_url, agent.api_mode)
    elif agent.api_mode == "anthropic_messages":
        _build_anthropic_client_from_runtime(agent, rt)
    else:
        agent.client = agent._create_openai_client(dict(rt["client_kwargs"]), reason=reason, shared=True)


def try_recover_primary_transport(
    agent, api_error: Exception, *, retry_count: int, max_retries: int,
) -> bool:
    """Rebuild the primary client once and retry after ``max_retries`` exhaust on a transient
    transport error. Skipped for aggregators (OpenRouter, Nous) that manage retries server-side."""
    error_type = type(api_error).__name__
    if agent._fallback_activated or error_type not in _TRANSIENT_TRANSPORT_ERRORS or agent._is_openrouter_url():
        return False
    # Portal OpenAI-wire traffic rides aggregator retry infra (skip), but Portal Claude on native
    # Messages holds a local Anthropic client that needs the rebuild.
    if (
        (agent.provider or "").strip().lower() in {"nous", "nous-portal", "nousresearch"}
        and getattr(agent, "api_mode", None) != "anthropic_messages"
    ):
        return False
    try:
        # Never hard-close the shared client here: stale streaming workers may still be unwinding on
        # the old pool; _retire_shared_openai_client defers FD release to GC.
        # Retire the existing client to release stale connections. #70773: never hard-close the shared
        # client here — this runs on the conversation-loop thread while workers from stale-killed streaming
        # attempts may still be unwinding their SSL BIOs on the old pool. ``_retire_shared_openai_client``
        # shuts the sockets down (FD-safe from any thread) and defers the FD release to GC, which cannot
        # complete until every borrowing thread has unwound.
        if getattr(agent, "client", None) is not None:
            with contextlib.suppress(Exception):
                agent._retire_shared_openai_client(agent.client, reason="primary_recovery")
        rt = agent._primary_runtime
        _apply_primary_runtime_fields(agent, rt)
        _rebuild_primary_client(agent, rt, reason="primary_recovery")
        wait_time = min(3 + retry_count, 8)
        agent._vprint(
            f"{agent.log_prefix}🔁 Transient {error_type} on {agent.provider} — "
            f"rebuilt client, waiting {wait_time}s before one last primary attempt.", force=True, diagnostic=True,
        )
        time.sleep(wait_time)
        return True
    except Exception as e:
        logger.warning("Primary transport recovery failed: %s", e)
        return False


def _primary_reset_gate_blocks(agent, rt, primary_provider, primary_runtime_base_url, matches_primary, load_primary_pool):
    """Reset-aware gate: skip a guaranteed-to-fail restore while the primary pool reports a
    future reset; fails open on any error/None. Returns ``(blocked, prefetched_pool, prefetched)``
    so the rebind step reuses the loaded pool (one auth.json read at most)."""
    prefetched_pool, prefetched = None, False
    with suppressed(logger, "Reset-aware restore gate failed; falling back to per-turn retry"):
        pool = getattr(agent, "_credential_pool", None)
        if not matches_primary(pool):
            prefetched_pool = pool = load_primary_pool()
            prefetched = True
        primary_model = str(rt.get("model") or "").strip()
        next_at = getattr(pool, "next_available_at", lambda **_kwargs: None)(model=primary_model or None)
        if next_at is not None and next_at > time.time():
            if not getattr(agent, "_restore_wait_logged", False):
                agent._restore_wait_logged = True
                logger.info(
                    "Primary %s rate-limited until %s; staying on fallback "
                    "%s/%s until the reset elapses", primary_provider or "?",
                    datetime.fromtimestamp(next_at).isoformat(timespec="seconds"), agent.provider,
                    agent.model,
                )
            return True, prefetched_pool, prefetched
    return False, prefetched_pool, prefetched


def _restore_runtime_capabilities(agent, rt: dict[str, Any]) -> None:
    # ``capabilities`` is the legacy key from the initial capability propagation patch.
    raw = rt["runtime_capabilities"] if "runtime_capabilities" in rt else rt.get("capabilities")
    if isinstance(raw, dict):
        agent.runtime_capabilities = dict(raw)
    elif "runtime_capabilities" in rt:
        logger.warning("Ignoring malformed runtime capabilities snapshot")


def _rebind_primary_credential_pool(agent, primary_provider, primary_model, matches_primary, load_primary_pool, prefetched_pool, prefetched) -> None:
    """Rebind and re-select the primary credential pool after a fallback turn. A cross-provider
    fallback attaches its own pool, which would trip the provider-mismatch guard on the next
    401/429: reload the primary pool, else clear it. The snapshot api_key may be stale after
    rotation; re-select the pool's best entry, keeping the snapshot key when none is usable."""
    pool = getattr(agent, "_credential_pool", None)
    pool_provider = str(getattr(pool, "provider", "") or "").strip().lower()
    if pool is not None and pool_provider and not matches_primary(pool):
        agent._credential_pool = None
        agent._credential_pool_entry_id = None
        try:
            # Reuse the pool the reset-aware gate already loaded (avoids a second auth.json read).
            agent._credential_pool = prefetched_pool if prefetched else load_primary_pool()
        except Exception as exc:
            logger.warning(
                "Restore could not reload primary credential pool for %s: %s", primary_provider, exc
            )
    agent._credential_pool_entry_id = None
    pool = getattr(agent, "_credential_pool", None)
    entry = pool.select(model=primary_model or None) if pool is not None and pool.has_available(model=primary_model or None) else None
    if entry is None or not (getattr(entry, "runtime_api_key", None) or getattr(entry, "access_token", "")):
        return
    if matches_primary(entry):
        # _swap_credential rebuilds the client and reapplies base-url-scoped headers.
        # ``_swap_credential`` rebuilds the OpenAI/Anthropic client, reapplies base-url-scoped headers, and
        # carries the accumulated base_url / OAuth-detection fixes (#33163).
        agent._swap_credential(entry)
        logger.info(
            "Restore re-selected pool entry %s (%s)",
            getattr(entry, "id", "?"), getattr(entry, "label", "?"),
        )
    else:
        logger.info(
            "Restore skipped pool entry %s (%s): provider %s does not match primary provider %s",
            getattr(entry, "id", "?"), getattr(entry, "label", "?"),
            str(getattr(entry, "provider", "") or "").strip().lower() or "?",
            primary_provider or "?",
        )


def _revert_credential_rotation(agent) -> None:
    """Move a live session back onto the credential a quota bench rotated it off, once the bench
    lifts. New sessions already do this through ``select()``; without it a long-lived (gateway)
    session keeps billing the fallback for its whole life (#114501). Credential-only: the
    model/base_url/compressor restore stays gated on ``_fallback_activated``."""
    revert_id = getattr(agent, "_credential_pool_revert_id", None)
    if not revert_id:
        return
    pool = getattr(agent, "_credential_pool", None)
    if pool is None or getattr(agent, "_credential_pool_entry_id", None) == revert_id:
        agent._credential_pool_revert_id = None
        return
    try:
        entry = pool.reclaim(revert_id, model=getattr(agent, "model", None))
    except Exception as exc:
        logger.warning("Credential revert check failed: %s", exc)
        return
    if entry is None:
        return  # still cooling down; check again next turn
    if agent._swap_credential(entry) is not False:
        logger.info(
            "Credential %s (%s) available again — reverted pool rotation",
            getattr(entry, "id", "?"), getattr(entry, "label", "?"),
        )
    agent._credential_pool_revert_id = None


def _primary_quota_reopened_early(agent, primary_provider, primary_model, matches_primary, load_primary_pool) -> bool:
    """True when a Codex quota window that the primary pool still has benched has reopened.

    A Codex 429 benches the entry, and arms ``_rate_limited_until``, until a ``resets_at`` that can be
    days out (weekly window). The window can reopen sooner (redeemed reset, top-up, plan change); the
    pool's throttled usage probe notices, but only on ``select()``, which a session pinned to its
    fallback never reaches. Fails closed: any doubt leaves both cooldowns in force.

    At most one check per probe interval per agent: the probe caches its verdict and nothing clears
    it on the next 429, so a "restored" answer the endpoint got wrong would otherwise restore,
    fail, and fall back again on every turn of the cached window. Checks in between could only
    replay that cached verdict, so they return before loading the pool.
    """
    if primary_provider != "openai-codex":
        return False
    from hermes_cli.auth_codex import CODEX_QUOTA_PROBE_MIN_INTERVAL_SECONDS
    now = time.monotonic()
    if now - getattr(agent, "_codex_reopen_checked_at", float("-inf")) < CODEX_QUOTA_PROBE_MIN_INTERVAL_SECONDS:
        return False
    agent._codex_reopen_checked_at = now
    try:
        pool = getattr(agent, "_credential_pool", None)
        if pool is None or not matches_primary(pool):
            pool = load_primary_pool()
        if pool is None:
            return False
        model = primary_model or None
        benched_until = pool.next_available_at(model=model)
        if benched_until is None or benched_until <= time.time():
            return False
        return pool.lift_reopened_cooldowns(model=model)
    except Exception:
        logger.debug("Early quota-reopen check failed; keeping the cooldown", exc_info=True)
        return False


def restore_primary_runtime(agent) -> bool:
    """Restore the primary runtime at the start of a new turn so fallback stays turn-scoped
    (long-lived CLI agents and the gateway's cached agents)."""
    if not agent._fallback_activated:
        # Reset the index even without activation: a failed _try_activate_fallback() can strand
        # _fallback_index past the chain end and silently block future fallbacks (#20465).
        agent._fallback_index = 0
        _revert_credential_rotation(agent)
        return False
    rt = agent._primary_runtime
    primary_provider = str((rt or {}).get("provider") or "").strip().lower()
    primary_model = str((rt or {}).get("model") or "").strip()
    from agent.fallback_cooldown import _is_entitlement_rejected
    from hermes_cli.chat_catalog import is_known_non_chat_model
    if primary_model and (
        _is_entitlement_rejected(agent, primary_provider, primary_model)
        or is_known_non_chat_model(primary_model)
    ):
        # Unentitled (#106475) or already known non-chat: restoring would announce a recovery
        # that was never verified and re-fail every turn. Stay on the fallback.
        return False
    primary_runtime_base_url = str((rt or {}).get("base_url") or "")

    def _matches_primary(candidate) -> bool:
        return credential_pool_matches_provider(candidate, primary_provider, base_url=primary_runtime_base_url)

    def _load_primary_pool():
        """Load the primary provider's pool; None when absent or provider-mismatched."""
        from agent.credential_pool import load_pool
        key = resolve_runtime_pool_key(primary_provider, primary_runtime_base_url)
        loaded = load_pool(key) if key else None
        return loaded if loaded is not None and _matches_primary(loaded) else None
    if _primary_quota_reopened_early(agent, primary_provider, primary_model, _matches_primary, _load_primary_pool):
        agent._rate_limited_until = 0
    if getattr(agent, "_rate_limited_until", 0) > time.monotonic():
        return False  # primary still in rate-limit cooldown, stay on fallback
    blocked, prefetched_pool, prefetched = _primary_reset_gate_blocks(
        agent, rt, primary_provider, primary_runtime_base_url, _matches_primary, _load_primary_pool
    )
    if blocked:
        return False
    agent._restore_wait_logged = False
    fallback_route = getattr(agent, "_provider_fallback_route", None)
    if not (isinstance(fallback_route, (list, tuple)) and len(fallback_route) == 2):
        fallback_route = (getattr(agent, "model", ""), getattr(agent, "provider", ""))
    previous_model, previous_provider = (str(v or "unknown") for v in fallback_route)
    provider_fallback_active = bool(getattr(agent, "_provider_fallback_active", False))
    try:
        from agent.route_binding import reinstall_primary_runtime
        reinstall_primary_runtime(
            agent, rt, primary_provider, primary_model, _matches_primary, _load_primary_pool, prefetched_pool, prefetched,
        )
        logger.info("Primary runtime restored for new turn: %s (%s)", agent.model, agent.provider)
        agent._provider_fallback_active = False
        agent._provider_fallback_route = None
        if provider_fallback_active:
            # Notification surfaces are best-effort and must never undo a successful restore.
            with contextlib.suppress(Exception):
                agent._emit_diagnostic_status(
                    f"✅ Primary model restored: {agent.model} via {agent.provider}; "
                    f"fallback {previous_model} via {previous_provider} is no longer active."
                )
        return True
    except Exception as e:
        logger.warning("Failed to restore primary runtime: %s", e)
        return False


# Transient transport failures worth one more attempt with a rebuilt client / connection pool.
_TRANSIENT_TRANSPORT_ERRORS = frozenset({
    "ReadTimeout", "ConnectTimeout", "PoolTimeout", "ConnectError", "ReadError", "RemoteProtocolError",
    "APIConnectionError", "APITimeoutError",
})

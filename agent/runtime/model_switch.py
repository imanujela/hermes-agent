
"""Model switching: destination resolution, client rebuild, runtime field swap,
snapshot/rollback and billing-route persistence around ``switch_model``. Split
from ``agent.agent_runtime_helpers``."""

from __future__ import annotations
import contextlib
import re
from typing import Any
from hermes_cli.timeouts import get_provider_request_timeout
import logging
from agent.runtime.credential_recovery import sync_credential_pool_entry_id
from hermes_suppress import suppressed
logger = logging.getLogger(__name__)


def _apply_switched_provider_request_overrides(agent, new_provider):
    """Re-derive the switched-to provider's ``request_overrides`` (custom_providers ``extra_body``).
    Matches by provider key, base_url AND model (same rule as
    ``agent_init._merge_custom_provider_extra_body``) so a different model at the same endpoint
    never inherits another's ``extra_body``. Stale ``extra_body`` cleared; ``service_tier``/``speed`` kept."""
    from agent.agent_init import _custom_provider_extra_body_for_agent
    # Prefer the init-time cache (agent._custom_providers); reload only if absent.
    custom_providers = getattr(agent, "_custom_providers", None)
    if custom_providers is None:
        try:
            from hermes_cli.config import load_config, get_compatible_custom_providers
            custom_providers = get_compatible_custom_providers(load_config())
        except Exception:
            custom_providers = []
    new_extra_body = _custom_provider_extra_body_for_agent(
        provider=new_provider, model=getattr(agent, "model", "") or "",
        base_url=getattr(agent, "base_url", "") or "", custom_providers=custom_providers or [],
    )
    overrides = dict(getattr(agent, "request_overrides", {}) or {})
    overrides.pop("extra_body", None)  # always drop the previous provider's extra_body
    if new_extra_body:
        overrides["extra_body"] = dict(new_extra_body)
    agent.request_overrides = overrides


# Pool reload is part of the switch and must be reversible on rollback, hence the pool fields.
_SWITCH_SNAPSHOT_FIELDS = (
    "model", "provider", "requested_provider", "base_url", "api_mode", "api_key", "client",
    "_anthropic_client", "_anthropic_api_key", "_anthropic_base_url", "_is_anthropic_oauth",
    "_config_context_length", "_reasoning_echo_flag", "runtime_capabilities",
    "_credential_pool", "_credential_pool_entry_id",
    "_codex_reasoning_replay_enabled", "_codex_reasoning_replay_rejected",
)


_MISSING = object()


def _snapshot_switch_state(agent) -> dict[str, Any]:
    """Snapshot every field the swap+rebuild mutates so a failed rebuild rolls back atomically
    (else a new model name + OLD client 400s next turn). The sentinel distinguishes unset from
    None: tests build bare agents via ``__new__`` without all fields."""
    snapshot = {name: getattr(agent, name, _MISSING) for name in _SWITCH_SNAPSHOT_FIELDS}
    # Shallow-copy the dict so mutating the live one doesn't poison the rollback target.
    snapshot["_client_kwargs"] = dict(getattr(agent, "_client_kwargs", {}) or {})
    return snapshot


def _restore_switch_snapshot(agent, snapshot: dict[str, Any]) -> None:
    for name, value in snapshot.items():
        if value is _MISSING:
            continue  # attribute did not exist before the swap; don't fabricate it
        with contextlib.suppress(Exception):
            setattr(agent, name, value)


def _resolve_switch_destination(agent, new_model, new_provider, base_url, api_mode, capabilities, old_norm, new_norm):
    """Resolve ``(api_mode, base_url, destination_capabilities)`` for the switch target."""
    from hermes_cli.providers import determine_api_mode, is_actual_route
    from agent.native_compaction import resolve_native_compaction_capabilities
    from hermes_cli.models import opencode_provider_family
    # Pass model so dual-wire providers (Nous Portal anthropic/* -> Messages) resolve correctly.
    if not api_mode:
        api_mode = determine_api_mode(new_provider, base_url, model=new_model)
    if not base_url and new_norm == "openai":
        # An omitted URL means the provider's canonical direct endpoint.
        base_url = "https://api.openai.com/v1"
    # Same-provider switches may omit base_url (e.g. credential refresh); resolve capabilities from
    # the endpoint the normalization below retains.
    effective_base_url = base_url
    if not effective_base_url and old_norm == new_norm:
        effective_base_url = getattr(agent, "base_url", "")
    if is_actual_route(new_provider, effective_base_url):
        api_mode = "chat_completions"
        if effective_base_url:
            from hermes_cli.auth import normalize_actual_base_url
            base_url = normalize_actual_base_url(effective_base_url)
    destination_capabilities = (
        dict(capabilities)
        if isinstance(capabilities, dict)
        else resolve_native_compaction_capabilities(
            model=new_model, base_url=effective_base_url, provider=new_provider,
            is_codex_backend=new_norm == "openai-codex",
        )
    )
    # Guard against a trailing /v1 on OpenCode base_url reaching the anthropic_messages client
    # (double-/v1 404); model_switch already strips it, direct callers may not.
    if (
        api_mode == "anthropic_messages"
        and opencode_provider_family(new_provider) is not None
        and isinstance(base_url, str)
        and base_url
    ):
        base_url = re.sub(r"/v1/?$", "", base_url)
    return api_mode, base_url, destination_capabilities


def _build_switched_client(agent, new_provider, api_key, base_url, api_mode, new_norm) -> None:
    """Build the client for the switched-to destination (MoA facade / native Anthropic / OpenAI wire)."""
    if new_norm == "moa":
        from agent.moa_loop import bind_moa_runtime
        # MoA speaks only chat.completions via the MoAClient facade; the aggregator's real transport
        # is applied inside the fan-out. The binder pins api_mode so the loop never dispatches
        # client.responses.create against the facade (same pins as agent_init / fallback).
        bind_moa_runtime(agent, agent.model, api_key)
        return
    if new_provider == "bedrock" and api_mode in ("anthropic_messages", "bedrock_converse"):
        # Non-Mantle Bedrock wires authenticate through boto3, never through the generic
        # Anthropic/OpenAI builders (which would ship the ``aws-sdk`` sentinel as a credential).
        from agent.bedrock_adapter import bind_bedrock_runtime
        bind_bedrock_runtime(agent, base_url or agent.base_url, api_mode)
        return
    if api_mode == "anthropic_messages":
        from agent.anthropic_adapter import build_anthropic_client
        from agent.anthropic_credentials import resolve_anthropic_token, anthropic_route_is_oauth
        # Only fall back to ANTHROPIC_TOKEN for native Anthropic; other anthropic_messages providers
        # must never receive Anthropic credentials.
        is_native_anthropic = new_provider == "anthropic"
        effective_key = api_key or agent.api_key or (
            resolve_anthropic_token(model=getattr(agent, "model", None)) if is_native_anthropic else ""
        ) or ""
        # MiniMax OAuth: per-request callable token provider survives 15-min expiry (rationale in
        # agent_init.py).
        if new_provider == "minimax-oauth" and isinstance(effective_key, str) and effective_key:
            try:
                from hermes_cli.auth import build_minimax_oauth_token_provider
                effective_key = build_minimax_oauth_token_provider()
            except Exception as _mm_exc:
                logger.warning(
                    "MiniMax OAuth: failed to install per-request token provider "
                    "on switch (%s); using static bearer.", _mm_exc,
                )
        agent.api_key = agent._anthropic_api_key = effective_key
        agent._anthropic_base_url = base_url or getattr(agent, "_anthropic_base_url", None)
        agent._anthropic_client = build_anthropic_client(
            effective_key, agent._anthropic_base_url,
            timeout=get_provider_request_timeout(agent.provider, agent.model),
        )
        agent._is_anthropic_oauth = anthropic_route_is_oauth(agent._anthropic_base_url, effective_key, provider=new_provider)
        agent.client = None
        agent._client_kwargs = {}
        return
    effective_base = base_url or agent.base_url
    agent._client_kwargs = {"api_key": api_key or agent.api_key, "base_url": effective_base}
    with suppressed(logger, "custom-provider TLS resolution skipped on switch_model"):
        from hermes_cli.config import (
            apply_custom_provider_tls_to_client_kwargs, get_compatible_custom_providers,
            load_config_readonly,
        )
        # Read live config, not agent._custom_providers, so mid-session ssl_ca_cert / ssl_verify
        # edits are honored.
        # Read custom_providers from live config (not the init-time snapshot on ``agent._custom_providers``)
        # so ssl_ca_cert / ssl_verify edits are honored when switching mid-session, matching the
        # context-length reload below (#15779).
        apply_custom_provider_tls_to_client_kwargs(
            agent._client_kwargs, str(effective_base or ""),
            get_compatible_custom_providers(load_config_readonly()),
        )
    timeout = get_provider_request_timeout(agent.provider, agent.model)
    if timeout is not None:
        agent._client_kwargs["timeout"] = timeout
    # Reapply provider headers (OpenRouter HTTP-Referer/X-Title) lost when _client_kwargs was
    # rebuilt; otherwise attribution shows "Unknown".
    agent._apply_client_headers_for_base_url(effective_base)
    agent.client = agent._create_openai_client(dict(agent._client_kwargs), reason="switch_model", shared=True)


def _swap_switch_runtime(agent, new_model, new_provider, api_key, base_url, api_mode, old_provider, old_norm, new_norm) -> None:
    """Swap identity/transport fields, reload the pool, rebuild the client (rolled back by the caller on error)."""
    # Clear the per-config override so the new model's context window is re-resolved.
    agent._config_context_length = None
    agent.model = new_model
    agent.provider = agent.requested_provider = new_provider
    # Re-read reasoning_echo so the flag reflects the new primary model (see _reasoning_echo_opt_in).
    agent._reasoning_echo_flag = agent._read_reasoning_echo_from_config()
    # Empty base_url while the provider changes means upstream resolution failed; falling back to
    # the old provider's URL pairs the wrong host and persists via _primary_runtime. Fail loud.
    # Same-provider re-select (credential refresh) may keep the URL.
    if base_url:
        agent.base_url = base_url
    elif old_norm != new_norm:
        raise ValueError(
            f"switch_model: no base_url resolved for provider "
            f"'{new_provider}' (switching from '{old_provider}'); "
            "refusing to keep the previous provider's endpoint"
        )
    agent.api_mode = api_mode
    # New api_mode may need a different transport.
    if hasattr(agent, "_transport_cache"):
        agent._transport_cache.clear()
    from agent.turn_recovery import reset_codex_reasoning_replay
    reset_codex_reasoning_replay(agent)
    if api_key:
        agent.api_key = api_key
    # Reload the credential pool on provider change: a pool with a mismatched provider makes
    # recover_with_credential_pool short-circuit. Reload failure is non-fatal.
    if old_norm != new_norm or getattr(agent, "_credential_pool", None) is None:
        # A pool bound to the old provider is worse than none: the recovery guard rejects it.
        agent._credential_pool = None
        agent._credential_pool_entry_id = None
        try:
            from agent.credential_pool import load_pool
            agent._credential_pool = load_pool(new_provider)
        except Exception as _pool_exc:
            logger.warning(
                "switch_model: credential pool reload failed for %s (%s); "
                "continuing without pool rotation this turn", new_provider, _pool_exc,
            )
    _build_switched_client(agent, new_provider, api_key, base_url, api_mode, new_norm)
    sync_credential_pool_entry_id(agent)


def _resolve_switch_context_length(agent, snapshot):
    """Resolve the destination context length (LM Studio preload first); returns ``(custom_providers, effective_len)``."""
    custom_providers = None
    try:
        from hermes_cli.config import (
            get_compatible_custom_providers, get_custom_provider_context_length, load_config
        )
        from agent.agent_init import config_context_length_for_runtime
        switch_cfg = load_config()
        custom_providers = get_compatible_custom_providers(switch_cfg)
        # The durable ``model.context_length`` pin is re-read from live config (never carried over
        # blindly, never simply dropped): the destination IS the configured default route -> keep the
        # ceiling; it is some other route -> the scoping inside returns None. Same precedence as
        # construction, where the pin outranks custom_providers metadata (#116467).
        intent = config_context_length_for_runtime(agent, switch_cfg)
        if intent is None:
            intent = get_custom_provider_context_length(
                model=agent.model, base_url=agent.base_url, custom_providers=custom_providers
            )
    except Exception:
        intent = None
    from agent.agent_init import set_config_context_length
    set_config_context_length(agent, intent)
    runtime_len = None
    if hasattr(agent, "_ensure_lmstudio_runtime_loaded"):
        try:
            runtime_len = agent._ensure_lmstudio_runtime_loaded(intent)
        except Exception:
            _restore_switch_snapshot(agent, snapshot)
            raise
    if hasattr(agent, "_lmstudio_load_was_unverified") and agent._lmstudio_load_was_unverified(runtime_len):
        logger.warning(
            "LM Studio model activation was rejected or completed without a "
            "verifiable active context length during model switch; continuing "
            "with configured context"
        )
    effective = intent
    if hasattr(agent, "_effective_lmstudio_context_length"):
        effective = agent._effective_lmstudio_context_length(intent, runtime_len)
    return custom_providers, effective


def _update_switch_compressor(agent, custom_providers, effective_context_length, snapshot) -> None:
    """Point the context compressor at the new model (rolls back the switch on failure)."""
    from agent.model_metadata import get_model_context_length
    if custom_providers is None:
        try:
            from hermes_cli.config import get_compatible_custom_providers, load_config
            custom_providers = get_compatible_custom_providers(load_config())
        except Exception:
            custom_providers = None
    # agent.api_key may be a callable (Azure Foundry Entra ID); get_model_context_length expects a
    # string for live probes, so coerce defensively.
    ctx_api_key = agent.api_key if isinstance(agent.api_key, str) else ""
    try:
        new_context_length = get_model_context_length(
            agent.model, base_url=agent.base_url, api_key=ctx_api_key, provider=agent.provider,
            config_context_length=effective_context_length, custom_providers=custom_providers,
        )
        agent.context_compressor.update_model(
            model=agent.model,
            context_length=new_context_length,
            base_url=agent.base_url,
            api_key=agent.api_key,  # context_compressor forwards to call_llm; callable preserved
            provider=agent.provider,
            api_mode=agent.api_mode,
        )
    except Exception:
        _restore_switch_snapshot(agent, snapshot)
        raise
    # Outside the rollback guard: a probe hiccup must not undo a good switch. Eager, so the aux
    # clamp lands before the first compaction on the new window, not after it (#114707).
    from agent.conversation_compression import revalidate_compression_feasibility
    revalidate_compression_feasibility(agent)


def _build_primary_runtime_snapshot(agent, api_mode) -> dict[str, Any]:
    """The ``_primary_runtime`` record that persists a switch across turns."""
    cc = getattr(agent, "context_compressor", None) or None
    rt = {
        "model": agent.model,
        "provider": agent.provider,
        "requested_provider": agent.requested_provider,
        "base_url": agent.base_url,
        "api_mode": agent.api_mode,
        "api_key": getattr(agent, "api_key", ""),
        "client_kwargs": dict(agent._client_kwargs),
        "use_prompt_caching": agent._use_prompt_caching,
        "use_native_cache_layout": agent._use_native_cache_layout,
        "reasoning_config": dict(agent.reasoning_config) if getattr(agent, "reasoning_config", None) else None,
        "reasoning_echo_flag": getattr(agent, "_reasoning_echo_flag", False),
        # Overrides must travel with the switched-to identity or a later recovery/restore resurrects
        # PRE-switch overrides from the stale init snapshot.
        # See #75091.
        "request_overrides": dict(getattr(agent, "request_overrides", {}) or {}),
        "runtime_capabilities": dict(getattr(agent, "runtime_capabilities", {}) or {}),
        "compressor_model": getattr(cc, "model", agent.model),
        "compressor_base_url": getattr(cc, "base_url", agent.base_url),
        "compressor_api_key": getattr(cc, "api_key", ""),
        "compressor_provider": getattr(cc, "provider", agent.provider),
        "compressor_context_length": cc.context_length if cc else 0,
        "compressor_api_mode": getattr(cc, "api_mode", agent.api_mode),
        "compressor_threshold_tokens": cc.threshold_tokens if cc else 0,
    }
    if api_mode == "anthropic_messages":
        rt.update({
            "anthropic_api_key": agent._anthropic_api_key,
            "anthropic_base_url": agent._anthropic_base_url,
            "is_anthropic_oauth": agent._is_anthropic_oauth,
        })
    return rt


def _finish_switch(agent, new_provider, old_norm, new_norm) -> None:
    """Post-switch bookkeeping: fallback reset/prune, request_overrides, billing route."""
    agent._fallback_activated = False
    agent._provider_fallback_active = False
    agent._provider_fallback_route = None
    agent._fallback_index = 0
    agent._credential_pool_revert_id = None
    # On a deliberate provider swap, prune fallback entries targeting the OLD or NEW primary;
    # otherwise a failed turn silently re-activates the provider the user just rejected.
    fallback_chain = list(getattr(agent, "_fallback_chain", []) or [])
    if old_norm and new_norm and old_norm != new_norm:
        fallback_chain = [
            entry for entry in fallback_chain
            if (entry.get("provider") or "").strip().lower() not in {old_norm, new_norm}
        ]
    agent._fallback_chain = fallback_chain
    agent._fallback_model = fallback_chain[0] if fallback_chain else None
    # Apply the switched-to provider's request_overrides (custom_providers extra_body).
    with suppressed(logger, "switch_model: request_overrides re-derivation failed"):
        _apply_switched_provider_request_overrides(agent, new_provider)


def _persist_switch_billing_route(agent) -> None:
    """Persist the billing route so dashboard Model cards show the post-switch provider."""
    # _session_db / session_id may be unset (tests, bare agents).
    session_db = getattr(agent, "_session_db", None)
    session_id = getattr(agent, "session_id", None)
    if session_db is None or not session_id:
        return
    try:
        session_db.update_session_billing_route(
            session_id, provider=agent.provider, base_url=agent.base_url,
            billing_mode=getattr(agent, "api_mode", None),
        )
    except Exception:
        logger.warning("Failed to persist billing route after model switch", exc_info=True)


def switch_model(
    agent, new_model, new_provider, api_key='', base_url='', api_mode='', capabilities=None
):
    """Switch the model/provider in-place for a live agent (rebuild clients, caching flags,
    compressor). Mirrors ``_try_activate_fallback()`` but also updates ``_primary_runtime`` so
    the change persists across turns. A failed swap/rebuild rolls back to the pre-switch
    snapshot and re-raises (callers catch)."""
    old_model = agent.model
    old_provider = agent.provider
    # ── Reload credential pool for the new provider (issue #52727) ── Without this,
    # ``recover_with_credential_pool`` sees a ``pool.provider != agent.provider`` mismatch and
    # short-circuits, leaving the new provider with no rotation/recovery on 401/429 and burning the original
    # pool's entries. Only reload when the provider actually changed (or the pool was missing) —
    # re-selecting the same provider must not churn the pool reference. A reload failure is logged +
    # swallowed: the switch itself must still complete.
    old_norm = (old_provider or "").strip().lower()
    new_norm = (new_provider or "").strip().lower()
    api_mode, base_url, destination_capabilities = _resolve_switch_destination(
        agent, new_model, new_provider, base_url, api_mode, capabilities, old_norm, new_norm
    )
    snapshot = _snapshot_switch_state(agent)
    try:
        _swap_switch_runtime(
            agent, new_model, new_provider, api_key, base_url, api_mode, old_provider, old_norm, new_norm
        )
    except Exception:
        _restore_switch_snapshot(agent, snapshot)
        raise
    custom_providers, effective_context_length = _resolve_switch_context_length(agent, snapshot)
    # Refresh the custom-provider snapshot from the config just loaded so the prompt_caching lookup
    # sees flags added to config.yaml after session start.
    if custom_providers is not None:
        agent._custom_providers = custom_providers
    agent._use_prompt_caching, agent._use_native_cache_layout = agent._anthropic_prompt_cache_policy(
        provider=new_provider, base_url=agent.base_url, api_mode=api_mode, model=new_model
    )
    if hasattr(agent, "context_compressor") and agent.context_compressor:
        _update_switch_compressor(agent, custom_providers, effective_context_length, snapshot)
    # Re-read the per-model reasoning_effort override so it applies immediately (per-model > global;
    # YAML False = disabled).
    try:
        from hermes_constants import resolve_reasoning_config
        from hermes_cli.config import load_config as _sm_load_config
        agent.reasoning_config = resolve_reasoning_config(_sm_load_config() or {}, agent.model)
        logger.info(
            "switch_model: reasoning_config resolved for %s: %s", agent.model, agent.reasoning_config
        )
    except Exception as _reasoning_err:
        logger.debug("switch_model: could not re-resolve reasoning_config: %s", _reasoning_err)
    # Invalidate the cached system prompt so it rebuilds next turn.
    agent._cached_system_prompt = None
    # Publish the destination capability map only after every runtime setup above has succeeded.
    # Failed switches must leave the old map intact.
    agent.runtime_capabilities = destination_capabilities
    # Reset the cross-turn stale-call circuit breaker; otherwise the latched streak keeps
    # short-circuiting the freshly selected healthy provider.
    from agent.chat_completion_helpers import _reset_stale_streak
    _reset_stale_streak(agent)
    agent._primary_runtime = _build_primary_runtime_snapshot(agent, api_mode)
    _finish_switch(agent, new_provider, old_norm, new_norm)
    logger.info(
        "Model switched in-place: %s (%s) -> %s (%s)",
        old_model, old_provider, new_model, new_provider,
    )
    _persist_switch_billing_route(agent)

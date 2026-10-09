
"""Transport clients & socket plumbing: OpenAI/Anthropic/Gemini/Copilot client
construction, API-error extraction/debug dumps, and httpx pool socket discovery
(force-close / drain helpers; formerly the transport_sockets utility surface). Split from ``agent.agent_runtime_helpers``."""

from __future__ import annotations
import contextlib
import copy
import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from hermes_cli.timeouts import get_provider_request_timeout
from agent.credential_pool import _parse_absolute_timestamp
from agent.retry_utils import parse_retry_after_seconds, reset_delay_from_message
from utils import base_url_host_matches, env_var_enabled, atomic_json_write
import logging
from agent.runtime._runtime_ref import _ra
logger = logging.getLogger(__name__)


def _build_anthropic_client_from_runtime(agent, rt: dict[str, Any]) -> None:
    """Rebuild the native Anthropic client from a ``_primary_runtime`` snapshot."""
    from agent.anthropic_adapter import build_anthropic_client
    agent._anthropic_api_key = rt["anthropic_api_key"]
    agent._anthropic_base_url = rt["anthropic_base_url"]
    agent._anthropic_client = build_anthropic_client(
        rt["anthropic_api_key"], rt["anthropic_base_url"],
        timeout=get_provider_request_timeout(agent.provider, agent.model),
    )
    agent._is_anthropic_oauth = rt["is_anthropic_oauth"]
    agent.client = None


def _api_error_debug_info(error: Exception) -> dict[str, Any]:
    info: dict[str, Any] = {"type": type(error).__name__, "message": str(error)}
    info.update({
        k: v for k in ("status_code", "request_id", "code", "param", "type", "body")
        if (v := getattr(error, k, None)) is not None
    })
    response_obj = getattr(error, "response", None)
    if response_obj is not None:
        try:
            info["response_status"] = getattr(response_obj, "status_code", None)
            info["response_text"] = response_obj.text
        except Exception as e:
            _ra().logger.debug("Could not extract error response details: %s", e)
    return info


def dump_api_request_debug(
    agent, api_kwargs: dict[str, Any], *, reason: str, error: Optional[Exception] = None
) -> Optional[Path]:
    """Dump the request body from api_kwargs (minus transport keys) for debugging provider 4xx failures."""
    try:
        body = {k: v for k, v in copy.deepcopy(api_kwargs).items() if v is not None and k != "timeout"}
        api_key = None
        # anthropic_messages keeps its SDK client on ``_anthropic_client`` (``client`` is None):
        # read the key from there so the dump does not say "Bearer None" (#24293).
        anthropic = agent.api_mode == "anthropic_messages"
        try:
            live = getattr(agent, "_anthropic_client", None) if anthropic else agent.client
            api_key = getattr(live, "api_key", None) or getattr(live, "auth_token", None)
        except Exception as e:
            _ra().logger.debug("Could not extract API key for debug dump: %s", e)
        endpoint = {"codex_responses": "/responses", "anthropic_messages": "/messages"}.get(
            agent.api_mode, "/chat/completions"
        )
        dump_payload: dict[str, Any] = {
            "timestamp": datetime.now().isoformat(), "session_id": agent.session_id, "reason": reason,
            "request": {
                "method": "POST", "url": f"{agent.base_url.rstrip('/')}{endpoint}",
                "headers": {
                    "Authorization": f"Bearer {agent._mask_api_key_for_logs(api_key)}",
                    "Content-Type": "application/json",
                },
                "body": body,
            },
        }
        if error is not None:
            dump_payload["error"] = _api_error_debug_info(error)
        # Sanitize the session ID (may come from an untrusted X-Hermes-Session-Id header) so a
        # "../"-shaped ID cannot write outside logs_dir.
        from agent.session_persistence import _safe_session_filename_component
        safe_sid = _safe_session_filename_component(agent.session_id)
        dump_file = agent.logs_dir / f"request_dump_{safe_sid}_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.json"
        # Redact secrets first: this fires unconditionally on API errors and captures the full
        # request body, so context-embedded secrets would otherwise land in cleartext on disk.
        from agent.redact import redact_sensitive_text
        _serialized = json.dumps(dump_payload, ensure_ascii=False, indent=2, default=str)
        _redacted_payload = json.loads(redact_sensitive_text(_serialized, force=True))
        atomic_json_write(dump_file, _redacted_payload, default=str)
        agent._vprint(f"{agent.log_prefix}🧾 Request debug dump written to: {dump_file}")
        if env_var_enabled("HERMES_DUMP_REQUEST_STDOUT"):
            print(json.dumps(_redacted_payload, ensure_ascii=False, indent=2, default=str))
        return dump_file
    except Exception as dump_error:
        if agent.verbose_logging:
            logger.warning("Failed to dump API request debug payload: %s", dump_error)
        return None


def _provider_supplied_client(agent, client_kwargs: dict) -> Any | None:
    """Ask the registered ProviderProfile for a custom client, if any. Resolves by provider name,
    then by ``base_url`` prefix so a URL-only runtime (``acp://…``) still reaches its profile.
    A profile that raises is logged and skipped: a third-party plugin must not be able to take
    the turn down, it can only fail to provide a client."""
    try:
        from providers import get_provider_profile
    except Exception:
        return None
    profile = None
    provider_name = (getattr(agent, "provider", "") or "").strip()
    if provider_name:
        try:
            profile = get_provider_profile(provider_name)
        except Exception:
            profile = None
    if profile is None:
        base_url = str(client_kwargs.get("base_url", "") or "").strip()
        if base_url:
            profile = _profile_for_base_url(base_url)
    if profile is None:
        return None
    try:
        return profile.create_client(**client_kwargs)
    except Exception:
        _ra().logger.warning(
            "Provider profile %r failed to create a client; falling back to the standard client path",
            getattr(profile, "name", provider_name) or "?", exc_info=True,
        )
        return None


def _profile_for_base_url(base_url: str) -> Any | None:
    """Registered profile whose own base_url is a prefix of ``base_url`` (provider name did not
    resolve). Prefix, not equality: the replaced copilot-acp branch keyed on
    ``startswith("acp://copilot")``, so a path or user override under the same root must resolve."""
    try:
        from providers import list_providers
        candidates = list_providers()
    except Exception:
        return None
    target = base_url.rstrip("/").lower()
    for candidate in candidates or []:
        own = str(getattr(candidate, "base_url", "") or "").rstrip("/").lower()
        if own and (target == own or target.startswith(own + "/")):
            return candidate
    return None


def _ensure_copilot_headers(client_kwargs: dict) -> None:
    """Defense-in-depth: recovery/restore rebuild from a snapshot without re-running header
    wiring; a missing Copilot-Integration-Id causes model_not_available_for_integrator 400s.
    Only ADD missing keys, never override."""
    try:
        if base_url_host_matches(str(client_kwargs.get("base_url", "")), "githubcopilot.com"):
            from hermes_cli.models import copilot_default_headers
            existing = dict(client_kwargs.get("default_headers") or {})
            existing_lower = {k.lower() for k in existing}
            for hk, hv in copilot_default_headers().items():
                if hk.lower() not in existing_lower:
                    existing[hk] = hv
            client_kwargs["default_headers"] = existing
    except Exception:
        _ra().logger.debug("Copilot default-header guard skipped", exc_info=True)


def _gemini_native_client(agent, client_kwargs: dict, httpx_verify, *, reason: str, shared: bool):
    """Native Gemini client when the base_url is the Gemini API, else None."""
    from agent.gemini_native_adapter import GeminiNativeClient, is_native_gemini_base_url
    base_url = str(client_kwargs.get("base_url", "") or "")
    if not is_native_gemini_base_url(base_url):
        return None
    safe_kwargs = {
        k: v for k, v in client_kwargs.items()
        if k in {"api_key", "base_url", "default_headers", "timeout", "http_client"}
    }
    if "http_client" not in safe_kwargs:
        keepalive_http = agent._build_keepalive_http_client(base_url, verify=httpx_verify)
        if keepalive_http is not None:
            safe_kwargs["http_client"] = keepalive_http
    client = GeminiNativeClient(**safe_kwargs)
    _ra().logger.info(
        "Gemini native client created (%s, shared=%s) %s", reason, shared, agent._client_log_context()
    )
    return client


def create_openai_client(agent, client_kwargs: dict, *, reason: str, shared: bool) -> Any:
    from agent.auxiliary_client import (
        _to_openai_base_url,
        _validate_base_url,
        _validate_proxy_env_urls,
    )
    from agent.ssl_verify import resolve_httpx_verify
    # Treat client_kwargs as read-only: callers pass agent._client_kwargs, and in-place mutation
    # leaks into later requests (a torn-down httpx transport got reused).
    # Callers pass agent._client_kwargs (or shallow copies of it) in; any in-place mutation leaks back into
    # the stored dict and is reused on subsequent requests. #10933 hit this by injecting an httpx.Client
    # transport that was torn down after the first request, so the next request wrapped a closed transport
    # and raised "Cannot send a request, as the client has been closed" on every retry. The revert resolved
    # that specific path; this copy locks the contract so future transport/keepalive work can't reintroduce
    # the same class of bug.
    client_kwargs = dict(client_kwargs)
    if client_kwargs.get("base_url"):
        client_kwargs["base_url"] = _to_openai_base_url(client_kwargs["base_url"])
    try:
        from providers import get_provider_profile

        profile = get_provider_profile(getattr(agent, "provider", ""))
        if profile is not None:
            for key, value in profile.build_client_kwargs_extras(
                base_url=client_kwargs.get("base_url", "")
            ).items():
                client_kwargs.setdefault(key, value)
    except Exception:
        _ra().logger.debug("Provider client-kwargs hook skipped", exc_info=True)
    # The MoA virtual provider has no OpenAI wire endpoint; the facade *is* the client. Rebuild the
    # facade, never a native client (TypeError; relay re-wire).
    # Rebuilding a native OpenAI client while agent.provider == "moa" (client replacement, stream-retry pool
    # cleanup, credential rotation, fallback+restore) drops the facade: the next primary call either raises
    # a `_moa_prepared_request` TypeError (#78382) or, when _client_kwargs carry an unrelated relay
    # base_url, leaks the request to a foreign gateway. Rebuild the facade instead (build_moa_facade also
    # re-wires the reference relay, see #53802).
    if (getattr(agent, "provider", "") or "").strip().lower() == "moa":
        from agent.moa_loop import build_moa_facade
        return build_moa_facade(agent, getattr(agent, "model", None) or "default")
    ssl_ca_cert = client_kwargs.pop("ssl_ca_cert", None)
    ssl_verify_cfg = client_kwargs.pop("ssl_verify", None)
    httpx_verify = resolve_httpx_verify(
        ca_bundle=ssl_ca_cert, ssl_verify=ssl_verify_cfg,
        base_url=str(client_kwargs.get("base_url", "")),
    )
    _validate_proxy_env_urls()
    _validate_base_url(client_kwargs.get("base_url"))
    # Provider-supplied client (registration seam): a provider whose wire protocol is not
    # OpenAI-over-HTTP supplies its own client from ProviderProfile.create_client(). Consulted
    # before the built-in ladder so a profile registered from ~/.hermes/plugins/ or a pip entry
    # point can ship a transport without editing this function (what makes an out-of-tree ACP
    # provider possible). None (the default) falls through, so existing providers are unaffected.
    provider_client = _provider_supplied_client(agent, client_kwargs)
    if provider_client is not None:
        _ra().logger.info(
            "%s client created from provider profile (%s, shared=%s) %s",
            agent.provider, reason, shared, agent._client_log_context(),
        )
        return provider_client
    from agent.auxiliary_client import _GEMINI_NATIVE_PROVIDER_NAMES
    if agent.provider in _GEMINI_NATIVE_PROVIDER_NAMES:
        client = _gemini_native_client(agent, client_kwargs, httpx_verify, reason=reason, shared=shared)
        if client is not None:
            return client
    # TCP keepalives so dead provider connections are detected (~60s) instead of hanging in
    # CLOSE-WAIT. Injected into the local copy only, so each client gets its own httpx.Client;
    # pinned by tests/agent/test_create_openai_client_reuse.py. What IS shared across those per-client wrappers is the
    # connection pool: ``build_keepalive_http_client`` mounts a process-shared ``HTTPTransport``
    # behind a per-client view whose ``close()`` is a no-op for the pool, so a closed wrapper
    # never takes a sibling's (or the successor's) connections with it
    # (tests/agent/test_shared_http_transport.py).
    # Without this, a peer that drops mid-stream leaves the socket in a state where epoll_wait never fires,
    # ``httpx`` read timeout may not trigger, and the agent hangs until manually killed. Probes after 30s
    # idle, retry every 10s, give up after 3 → dead peer detected within ~60s. Safety against #10933: the
    # ``client_kwargs = dict(client_kwargs)`` above means this injection only lands in the local per-call
    # copy, never back into ``agent._client_kwargs``. Each ``_create_openai_client`` invocation therefore
    # gets its OWN fresh ``httpx.Client`` whose lifetime is tied to the OpenAI client it is passed to. When
    # the OpenAI client is closed (rebuild, teardown, credential rotation), the paired ``httpx.Client``
    # closes with it, and the next call constructs a fresh one — no stale closed transport can be reused.
    # Bedrock Mantle: the ``aws-sdk`` placeholder is a sentinel for IAM-chain auth, not a bearer token.
    # Every rebuild from bare ``{api_key, base_url}`` kwargs (switch_model, fallback restore, credential
    # rotation, request-scoped clients) must reinstall the SigV4 http_client or Mantle answers 401.
    if "bedrock-mantle." in str(client_kwargs.get("base_url") or ""):
        from agent.bedrock_adapter import configure_bedrock_openai_client_kwargs
        timeout = client_kwargs.get("timeout")
        configure_bedrock_openai_client_kwargs(
            client_kwargs, timeout=timeout if isinstance(timeout, (int, float)) else None,
        )
    if "http_client" not in client_kwargs:
        keepalive_http = agent._build_keepalive_http_client(client_kwargs.get("base_url", ""), verify=httpx_verify)
        if keepalive_http is not None:
            client_kwargs["http_client"] = keepalive_http
    # Retries belong to the outer conversation loop (honors Retry-After); SDK retries would
    # double-retry inside it. auxiliary_client keeps SDK retries as it isn't wrapped.
    # Delegate all rate-limit / 5xx retry to hermes's outer conversation loop, which honors Retry-After and
    # applies adaptive/jittered backoff. The OpenAI SDK default (max_retries=2) uses its own 1-2s backoff
    # that ignores Retry-After and double-retries inside our loop — the same deadlock the Anthropic clients
    # hit (#26293). This is the single chokepoint every primary OpenAI/aggregator client passes through
    # (init, switch_model, recovery, restore, request-scoped); auxiliary_client builds its own clients and
    # keeps SDK retries because it is NOT wrapped by the conversation loop.
    client_kwargs.setdefault("max_retries", 0)
    _ensure_copilot_headers(client_kwargs)
    # All primary construction and recovery paths must identify Hermes to the official Codex
    # endpoint, including snapshots with custom header overrides.
    from agent.codex_headers import apply_required_codex_headers
    apply_required_codex_headers(
        client_kwargs, access_token=client_kwargs.get("api_key", ""),
        base_url=str(client_kwargs.get("base_url", "")),
    )
    # ``process_bootstrap.OpenAI`` is a lazy SDK proxy; resolved at call time so tests can patch it.
    from agent import process_bootstrap
    client = process_bootstrap.OpenAI(**client_kwargs)
    # Routing proxies name the deployment they served in a response header (#54864).
    from agent.served_model import install_served_model_capture
    install_served_model_capture(agent, client)
    _ra().logger.info("OpenAI client created (%s, shared=%s) %s", reason, shared, agent._client_log_context())
    return client


def _iter_httpx_pools_with_owner(http_client: Any):
    """Yield ``(pool, owner)`` pairs reachable from an httpx client, including mounted transports:
    keepalive and proxy configs put live connections on ``client._mounts``, which a
    ``_transport``-only walk misses.

    ``owner`` is ``None`` for a pool this client owns outright, or the ``_SharedTransport`` view
    id when the pool is process-shared with other clients
    (``process_bootstrap.build_keepalive_http_client``). Callers must then touch only the
    in-flight requests stamped with that owner.

    Walking the default transport alone makes ``force_close_tcp_sockets`` return 0 while a stream is still
    mid-recv — the interrupt logs success and the provider keeps burning the slot (#72975).
    """
    seen_pools: set[int] = set()
    try:
        transports = [getattr(http_client, "_transport", None)]
        transports += list((getattr(http_client, "_mounts", None) or {}).values())
        for transport in transports:
            if transport is None:
                continue
            # Connections live under ``_pool``; a directly mounted HTTPProxy *is* a ConnectionPool,
            # so ``_connections`` may sit on the transport itself.
            pool = getattr(transport, "_pool", None)
            if pool is None and getattr(transport, "_connections", None) is not None:
                pool = transport
            if pool is not None and id(pool) not in seen_pools:
                seen_pools.add(id(pool))
                owner = id(transport) if type(transport).__name__ == "_SharedTransport" else None
                yield pool, owner
    except Exception:
        return


def _iter_httpx_pool_objects(http_client: Any):
    """Yield httpcore pool objects reachable from an httpx client."""
    for pool, _owner in _iter_httpx_pools_with_owner(http_client):
        yield pool


def _connection_candidates(conn: Any):
    """Walk nested wrappers: proxy tunnels (``_connection``) plus httpx/httpcore
    stream envelopes (``_stream``/``_httpcore_stream``: BoundSyncStream →
    ResponseStream → connection byte stream → HTTP11/2 connection)."""
    seen: set[int] = set()
    stack = [conn]
    while stack:
        obj = stack.pop()
        if obj is None or id(obj) in seen:
            continue
        seen.add(id(obj))
        yield obj
        for attr in ("_connection", "_stream", "_httpcore_stream"):
            nxt = getattr(obj, attr, None)
            if nxt is not None:
                stack.append(nxt)


def _socket_from_candidate(candidate: Any):
    """Raw socket behind a connection/stream wrapper yielded by ``_connection_candidates``."""
    stream = getattr(candidate, "_network_stream", None) or getattr(candidate, "_stream", None)
    sock = _socket_from_stream(stream) if stream is not None else None
    return sock if sock is not None else _socket_from_stream(candidate)


def _socket_from_response(response: Any):
    """Raw socket behind an httpx response's network stream (``extensions["network_stream"]``
    first, then ``response.stream``), or None. Callers own their error handling."""
    exts = getattr(response, "extensions", None) or {}
    direct = exts.get("network_stream") if isinstance(exts, dict) else None
    for start in (direct, getattr(response, "stream", None)):
        if start is None:
            continue
        for candidate in _connection_candidates(start):
            sock = _socket_from_candidate(candidate)
            if sock is not None:
                return sock
    return None


def _socket_from_stream(stream: Any):
    """Raw socket behind an httpcore network stream (several backends), or None."""
    sock = getattr(stream, "_sock", None)
    if sock is None and callable(getattr(stream, "get_extra_info", None)):
        with contextlib.suppress(Exception):
            sock = stream.get_extra_info("socket")
    if sock is None:
        sock = getattr(getattr(stream, "stream", None), "_sock", None)
    if sock is None and callable(getattr(getattr(stream, "_stream", None), "extra", None)):
        # anyio-backed streams expose the raw socket through SocketAttribute.raw_socket.
        with contextlib.suppress(Exception):
            from anyio.abc import SocketAttribute
            sock = stream._stream.extra(SocketAttribute.raw_socket)
    return sock


def _iter_pool_sockets(client: Any):
    """Yield raw sockets reachable from an OpenAI/httpx client pool. Defensive over private
    httpcore internals (``conn._connection``, proxy tunnel wrappers) that vary by release; also
    walks mount transports and in-flight ``PoolRequest.connection`` objects (``_connections``
    is empty during checkout)."""
    try:
        # Some SDK wrappers *are* the httpx client; fall through so mount-aware discovery runs.
        http_client = getattr(client, "_client", None)
        pools = list(_iter_httpx_pools_with_owner(client if http_client is None else http_client))
    except Exception:
        return
    if not pools:
        return
    from agent.process_bootstrap import HERMES_TRANSPORT_OWNER_EXT
    seen: set[int] = set()
    for pool, owner in pools:
        # ``is None``, not falsiness: an empty ``_connections`` must still let us walk in-flight ``_requests``.
        raw_conns = getattr(pool, "_connections", None)
        if raw_conns is None:
            raw_conns = getattr(pool, "_pool", None)
        # A process-shared pool carries other clients' idle + in-flight connections: only this
        # client's own in-flight requests (stamped by ``_SharedTransport.handle_request``) may be
        # shut down.
        connections = [] if owner is not None else list(raw_conns or [])
        for pool_req in list(getattr(pool, "_requests", None) or []):
            if owner is not None:
                exts = getattr(getattr(pool_req, "request", None), "extensions", None) or {}
                if exts.get(HERMES_TRANSPORT_OWNER_EXT) != owner:
                    continue
            conn = getattr(pool_req, "connection", None)
            if conn is not None:
                connections.append(conn)
        for conn in connections:
            for candidate in _connection_candidates(conn):
                sock = _socket_from_candidate(candidate)
                if sock is not None and id(sock) not in seen:
                    seen.add(id(sock))
                    yield sock


def _set_reset_from_retry_after(context: dict[str, Any], retry_after: Any) -> None:
    if "reset_at" in context:
        return
    seconds = parse_retry_after_seconds(retry_after)
    if seconds is not None:
        context["reset_at"] = time.time() + seconds


# OpenAI-style relative windows: "6m0s", "1.5s", "20ms", "1h2m3s" (also a bare number of seconds).
_DURATION_COMPONENT_RE = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


_DURATION_UNIT_SECONDS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


# Lowest-priority reset sources, after Retry-After and x-ratelimit-reset: OpenAI's per-bucket
# durations and Anthropic's per-bucket ISO-8601 timestamps. Plain OpenAI/Anthropic 429s often
# carry only these, and without them the retry status never names the reset window.
_VENDOR_RESET_HEADERS = (
    "x-ratelimit-reset-requests", "x-ratelimit-reset-tokens",
    "anthropic-ratelimit-requests-reset", "anthropic-ratelimit-tokens-reset",
)


def _duration_string_seconds(text: str) -> Optional[float]:
    raw = text.strip().lower()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        pass
    parts = _DURATION_COMPONENT_RE.findall(raw)
    if not parts or "".join(n + u for n, u in parts) != raw:
        return None
    return sum(float(n) * _DURATION_UNIT_SECONDS[u] for n, u in parts)


def _set_reset_from_vendor_headers(context: dict[str, Any], headers: Any) -> None:
    for name in _VENDOR_RESET_HEADERS:
        value = headers.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        seconds = _duration_string_seconds(value)
        if seconds is None:
            absolute = _parse_absolute_timestamp(value)
            seconds = None if absolute is None else absolute - time.time()
        if seconds is not None and seconds > 0:
            context["reset_at"] = time.time() + seconds
            return


def extract_api_error_context(error: Exception) -> dict[str, Any]:
    """Extract structured rate-limit details from provider errors."""
    context: dict[str, Any] = {}
    body = getattr(error, "body", None)
    payload = (body.get("error") if isinstance(body.get("error"), dict) else body) if isinstance(body, dict) else None
    if isinstance(payload, dict):
        reason = payload.get("code") or payload.get("type") or payload.get("error")
        if isinstance(reason, str) and reason.strip():
            context["reason"] = reason.strip()
        message = payload.get("message") or payload.get("error_description")
        if not message and isinstance(payload.get("error"), str):
            # xAI uses a top-level string ``error`` beside a structured ``code``.
            message = payload.get("error")
        if isinstance(message, str) and message.strip():
            context["message"] = message.strip()
        reset = next((payload.get(k) for k in ("resets_at", "reset_at") if payload.get(k) not in {None, ""}), None)
        if reset is not None:
            context["reset_at"] = reset
        elif isinstance(payload.get("resets_in_seconds"), (int, float)):
            # Codex/ChatGPT usage-limit bodies carry a relative window beside (or instead of) the epoch.
            context["reset_at"] = time.time() + float(payload["resets_in_seconds"])
        _set_reset_from_retry_after(context, payload.get("retry_after"))
    headers = getattr(getattr(error, "response", None), "headers", None)
    if headers:
        _set_reset_from_retry_after(context, headers)
        ratelimit_reset = headers.get("x-ratelimit-reset")
        if ratelimit_reset and "reset_at" not in context:
            context["reset_at"] = ratelimit_reset
        if "reset_at" not in context:
            _set_reset_from_vendor_headers(context, headers)
    if "message" not in context and str(error).strip():
        context["message"] = str(error).strip()[:500]
    if "reset_at" not in context and isinstance(context.get("message") or "", str):
        delay = reset_delay_from_message(context.get("message") or "")
        if delay is not None:
            context["reset_at"] = time.time() + delay
    return context


def _shutdown_socket(sock: Any) -> None:
    """``shutdown(SHUT_RDWR)`` WITHOUT closing the FD. ``close()`` from a non-owner thread is
    unsafe: the SSL BIO caches the raw FD, the kernel recycles it, and a flushed TLS record lands
    in the wrong file (once clobbered a SQLite header). ``shutdown()`` is FD-safe from any thread.
    Already shut down / not connected / FD invalid are all benign."""
    import socket as _socket
    try:
        # Clear a blocking timeout so a hung SSL_read notices the shutdown. Still no close().
        settimeout = getattr(sock, "settimeout", None)
        if callable(settimeout):
            with contextlib.suppress(OSError):
                settimeout(0)
        sock.shutdown(_socket.SHUT_RDWR)
    except OSError:
        pass


def force_close_tcp_sockets(client: Any) -> int:
    """Abort in-flight TCP I/O on every pool socket via ``_shutdown_socket``. Returns the count
    (logged as ``tcp_force_closed=N``)."""
    # Late-bound compat seam: tests patch agent_runtime_helpers._iter_pool_sockets
    # (test_timeout_transport_drain); resolve through the shim so the patch sticks.
    from agent import agent_runtime_helpers as _compat
    _pool_sockets = getattr(_compat, "_iter_pool_sockets", _iter_pool_sockets)
    shutdown_count = 0
    try:
        for sock in _pool_sockets(client):
            _shutdown_socket(sock)
            shutdown_count += 1
    except Exception as exc:
        _ra().logger.debug("Force-close TCP sockets sweep error: %s", exc)
    return shutdown_count

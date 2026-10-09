"""Tool-call integrity passes for the pre-API sanitizer: invalid-role dropping,
empty/malformed ``tool_calls`` normalization, ``function.name`` coercion, positional
tool_call <-> tool_result pairing, duplicate-id dedup, result-name realignment, and the
global orphan classifier. Split from ``agent.runtime.message_repair`` (DESIGN-IT-TWICE:
split by cohesion); ``sanitize_api_messages`` remains the single entry point and
re-imports these passes."""

from __future__ import annotations
from typing import Any
from agent.message_sanitization import (
    coalesce_tool_call_id, coerce_tool_name, tool_call_id_variants, tool_result_id_variants
)
from agent.runtime._runtime_ref import _ra


def _classify_tool_call_orphans(messages: list[dict[str, Any]]):
    """Classify orphaned tool-call / tool-result pairs; single source of truth for GLOBAL orphan
    detection. Returns ``(surviving_call_ids, result_call_ids, orphaned_results, missing_tool_calls)``;
    every id variant of a tool_call is registered so a result matching any alias survives, and
    ``orphaned_results`` are the actual dicts (filter by ``id(msg)``). ``sanitize_api_messages``
    pairs positionally instead but shares the ``*_id_variants`` alias policy."""
    assistant_call_variants = [
        (tc, variants)
        for msg in messages if msg.get("role") == "assistant"
        for tc in msg.get("tool_calls") or []
        if (variants := tool_call_id_variants(tc))
    ]
    surviving_call_ids: set[str] = set().union(*(v for _, v in assistant_call_variants))
    result_entries = [
        (msg, tool_result_id_variants(msg.get("tool_call_id"))) for msg in messages if msg.get("role") == "tool"
    ]
    result_call_ids: set[str] = set().union(*(v for _, v in result_entries))
    orphaned_results = [msg for msg, v in result_entries if v and not (v & surviving_call_ids)]
    # Orphan result variants are disjoint from every declared call, so they
    # cannot contribute a match. Reuse the union instead of scanning each result.
    missing_tool_calls = [
        tc for tc, v in assistant_call_variants if not (v & result_call_ids)
    ]
    return surviving_call_ids, result_call_ids, orphaned_results, missing_tool_calls


def _drop_invalid_roles(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop messages whose role the API won't accept."""
    valid = _ra().AIAgent._VALID_API_ROLES
    for msg in messages:
        if msg.get("role") not in valid:
            _ra().logger.debug("Pre-call sanitizer: dropping message with invalid role %r", msg.get("role"))
    return [m for m in messages if m.get("role") in valid]


def _drop_empty_tool_calls_arrays(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strict providers 400 on ``tool_calls: []``; normalize on shallow copies so history stays byte-stable."""
    # --- Drop empty / malformed tool_calls arrays on assistant messages --- An assistant message carrying
    # ``tool_calls: []`` (an empty array) — or a non-list value under the key — is semantically identical to
    # an assistant message with no tool calls, but strict OpenAI-compatible providers reject the empty array
    # outright: DeepSeek v4 returns HTTP 400 "Invalid 'messages[N].tool_calls': empty array. Expected an
    # array with minimum length 1, but got an empty array instead." (#58755, follow-up to #56980). Empty
    # arrays reach here from session resume, host-fed histories, or the consecutive-assistant merge in
    # ``repair_message_sequence`` (which preserves a pre-existing ``[]`` on the surviving turn). This is the
    # final pre-API chokepoint, so normalize defensively — and, per the #56980 review, do it HERE on the
    # per-call copy rather than in ``repair_message_sequence``, which would destructively rewrite the
    # persisted trajectory. Shallow-copy the message before dropping the key so stored history (and prompt
    # caching) stays byte-stable.
    normalized: list[dict[str, Any]] = []
    dropped = 0
    for msg in messages:
        if (
            isinstance(msg, dict)
            and msg.get("role") == "assistant"
            # Defense-in-depth: a strict OpenAI-compatible provider (e.g. onerouter / Qwen, DeepSeek v4)
            # rejects an assistant message carrying ``tool_calls: []`` (empty array) with HTTP 400 "Empty
            # tool_calls is not supported in message." The pre-API sanitizer in agent_runtime_helpers drops
            # these, but only on the conversation_loop path — other routes can reach the wire without it.
            # For every request that serializes through this transport (conversation loop and any caller
            # using it), this is the last boundary, so normalize here. Requests built by fully separate
            # payload paths (e.g. some auxiliary clients) never pass through this layer and are out of scope
            # for it. (#58755 follow-up)
            and "tool_calls" in msg
            and not (isinstance(msg["tool_calls"], list) and msg["tool_calls"])
        ):
            msg = {k: v for k, v in msg.items() if k != "tool_calls"}
            dropped += 1
        normalized.append(msg)
    if not dropped:
        return messages
    _ra().logger.debug(
        "Pre-call sanitizer: dropped empty/invalid tool_calls on %d assistant message(s)", dropped
    )
    return normalized


def _repair_invalid_tool_call_names(messages: list[dict[str, Any]]) -> None:
    """Coerce every ``function.name`` to the provider-safe ``^[A-Za-z0-9_-]{1,64}$``. An empty/missing
    name becomes the ``invalid_tool_call`` sentinel (dropping would unpair the anti-priming result the
    dispatch loop keeps for it); an invalid one (``multi_tool_use.parallel``, a shell command a weak
    fallback model put in ``name``) is coerced deterministically, because one such stored turn 400s
    every later request on a strict endpoint and pins the session to the fallback model (#51944).
    Tool calls are rewritten copy-on-write (an SDK object becomes a dict copy) so a shallow per-call
    copy never edits persisted history; tool results follow via ``_realign_tool_result_names``."""
    for msg in messages:
        if msg.get("role") != "assistant":
            continue
        tcs = msg.get("tool_calls") or []
        for idx, tc in enumerate(tcs):
            if isinstance(tc, dict):
                fn = tc.get("function")
                name = fn.get("name") if isinstance(fn, dict) else getattr(fn, "name", None)
            else:
                fn = getattr(tc, "function", None)
                name = getattr(fn, "name", None) if fn else None
            coerced = coerce_tool_name(name)
            if coerced == name:
                continue
            _ra().logger.warning(
                "Pre-call sanitizer: repairing tool_call with invalid function.name %r -> %r (id=%s)",
                (name or "")[:80], coerced, _ra().AIAgent._get_tool_call_id_static(tc),
            )
            if tcs is msg.get("tool_calls"):
                tcs = msg["tool_calls"] = list(tcs)
            if isinstance(tc, dict):
                fn = {**fn, "name": coerced} if isinstance(fn, dict) else {"name": coerced, "arguments": "{}"}
                tcs[idx] = {**tc, "function": fn}
            else:
                args = getattr(fn, "arguments", None) if fn is not None else None
                tcs[idx] = {
                    "id": _ra().AIAgent._get_tool_call_id_static(tc),
                    "type": "function",
                    "function": {"name": coerced, "arguments": args if isinstance(args, str) else "{}"},
                }


def _drop_results_without_ids(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop tool results with a missing/empty tool_call_id. Kept explicit (not left to the
    positional walk) for its own log line and so the final-chokepoint guarantee holds for
    callers skipping ``repair_message_sequence``."""
    kept = [
        m for m in messages
        if not (m.get("role") == "tool" and not (m.get("tool_call_id") or "").strip())
    ]
    if len(kept) != len(messages):
        _ra().logger.debug(
            "Pre-call sanitizer: dropped %d tool result(s) with missing/empty tool_call_id",
            len(messages) - len(kept),
        )
    return kept


def _pair_tool_calls_positionally(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Positional tool_call <-> tool_result pairing: strict providers (DeepSeek v4, Kimi) require
    results IMMEDIATELY after their call. Drops positional orphans, stubs unanswered declared
    ids; matching is alias-aware."""
    # --- Positional tool_call <-> tool_result pairing --- Strict OpenAI-compatible providers (DeepSeek v4,
    # Kimi) enforce the POSITIONAL invariant: an assistant message carrying tool_calls must be IMMEDIATELY
    # followed by tool messages covering every tool_call_id. The previous implementation compared global id
    # sets, which misses the failure mode where a result exists somewhere in the transcript but not in the
    # run right after its call — an interrupted turn or a compression window can displace a result past a
    # user turn. The id then survives in the global result set, so the call looks answered, no stub is
    # injected, and the provider rejects the payload with HTTP 400 "An assistant message with 'tool_calls'
    # must be followed by tool messages responding to each 'tool_call_id' (insufficient tool messages
    # following tool_calls message)". Rewritten as a single rolling walk on the per-call copy (#94704): (a)
    # tool results that do not immediately follow an assistant message declaring their id are dropped
    # (positional orphans — includes results appearing BEFORE their call, which strict providers also
    # reject); (b) declared ids not covered by the immediately-following tool run get a stub result injected
    # at the end of that run, even when a mispositioned result exists elsewhere. Matching is variant-aware
    # (``tool_call_id_variants`` / ``tool_result_id_variants``): a result keyed on ANY alias spelling
    # (``id`` / ``call_id`` / ``response_item_id`` / composite bridge) answers the call, preserving the
    # unified alias policy from #55626/#63000/#93251.
    paired: list[dict[str, Any]] = []
    declared_calls: dict[str, tuple] = {}
    dropped = 0
    stubs = 0

    def _flush_unanswered_stubs() -> None:
        nonlocal stubs
        for key in sorted(declared_calls):
            tc, _variants = declared_calls[key]
            paired.append({
                "role": "tool", "name": _ra().AIAgent._get_tool_call_name_static(tc),
                "content": "[Result unavailable — see context summary above]",
                "tool_call_id": coalesce_tool_call_id(tc) or key,
            })
            stubs += 1
        declared_calls.clear()

    for msg in messages:
        role = msg.get("role")
        if role == "assistant":
            # A new assistant turn closes the previous tool-result run: anything still pending was
            # never answered positionally.
            _flush_unanswered_stubs()
            declared_calls = {}
            for tc in msg.get("tool_calls") or []:
                variants = tool_call_id_variants(tc)
                if variants:
                    # Key on a stable representative of the alias group so a result matching ANY
                    # spelling can consume the call.
                    declared_calls[sorted(variants)[0]] = (tc, variants)
        elif role == "tool":
            result_variants = tool_result_id_variants(msg.get("tool_call_id"))
            matched = next((k for k, (_tc, v) in declared_calls.items() if v & result_variants), None)
            if matched is None:
                dropped += 1
                continue
            # Consume so a duplicate result reusing the id is dropped (strict providers reject duplicates).
            declared_calls.pop(matched, None)
        elif role == "user":
            # A user turn closes the tool-result run; later tool messages are orphans.
            _flush_unanswered_stubs()
        paired.append(msg)
    # The transcript may end right after an unanswered assistant turn.
    _flush_unanswered_stubs()
    if dropped:
        _ra().logger.debug("Pre-call sanitizer: removed %d positionally orphaned tool result(s)", dropped)
    if stubs:
        _ra().logger.debug(
            "Pre-call sanitizer: added %d stub tool result(s) for "
            "positionally unanswered tool call(s)", stubs,
        )
    return paired if (dropped or stubs) else messages


def _dedupe_tool_call_ids(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate tool_call_ids (strict providers 400 on duplicates): collapse duplicates within
    an assistant message, drop results answering no OUTSTANDING call. Tracks outstanding calls
    (not ids ever seen) because llama.cpp reuses one constant id, and whole variant groups so
    alias-keyed results are not deleted."""
    outstanding: dict[str, int] = {}  # every alias of an unanswered call -> its group id
    # 3. Deduplicate tool_call_ids. Strict providers (DeepSeek) reject a payload where the same tool_call_id
    #   appears more than once with HTTP 400 "Duplicate value for 'tool_call_id'" (#58327). Duplicates can
    #   arise from retries, crash/resume glitches, or a compression window that re-emits a tool result. This
    #   is the final pre-API chokepoint, so dedup defensively here even though repair_message_sequence also
    #   consumes matched ids. (a) collapse duplicate tool_calls WITHIN an assistant message (b) drop tool
    #   results that answer no OUTSTANDING tool call (b) tracks outstanding calls rather than every id ever
    #   seen, because ``tool_call_id`` is NOT globally unique in practice: llama.cpp emits a single constant
    #   id for every tool call it ever returns (verified: three separate completions from one server all
    #   carry the same id). A seen-once-drop-forever rule reads the SECOND legitimate tool result of such a
    #   session as a duplicate and deletes it, so from the second tool call onward the model never sees any
    #   result — it announces its next action and the turn dies with the work unfinished. Outstanding-call
    #   semantics keep both protections intact: a re-emitted result still answers no pending call and is
    #   still dropped, while a genuine new call that reuses the id re-arms that id first. Variant-group
    #   tracking: answering or deduping one spelling consumes its siblings too. A Codex/Responses tool_call
    #   registers ``id`` (fc_...), ``call_id`` (call_...), ``response_item_id``, and composite spellings
    #   (#55626/#58168/#63000); tracking only the coalesced id here made a result keyed on any OTHER variant
    #   look like it answered no outstanding call, so this pass deleted the very result step 2's
    #   variant-aware matching had just preserved (issue #93251 — whole parallel batches vanished).
    outstanding_groups: dict[int, frozenset] = {}
    next_group_id = 0
    deduped: list[dict[str, Any]] = []
    removed = 0
    for msg in messages:
        role = msg.get("role")
        if role == "assistant" and msg.get("tool_calls"):
            kept_tcs = []
            for tc in msg.get("tool_calls") or []:
                variants = tool_call_id_variants(tc)
                if variants and variants & outstanding.keys():
                    removed += 1
                    continue
                if variants:
                    group_id = next_group_id
                    next_group_id += 1
                    outstanding_groups[group_id] = variants
                    for variant in variants:
                        outstanding.setdefault(variant, group_id)
                kept_tcs.append(tc)
            if kept_tcs:
                msg = {**msg, "tool_calls": kept_tcs}
            elif len(kept_tcs) != len(msg.get("tool_calls") or []):
                msg = {k: v for k, v in msg.items() if k != "tool_calls"}
        elif role == "tool":
            result_variants = tool_result_id_variants(msg.get("tool_call_id"))
            candidate_groups = {outstanding[v] for v in result_variants if v in outstanding}
            if result_variants and not candidate_groups:
                removed += 1
                continue
            if candidate_groups:
                # Consume EVERY variant of the matched call; ids are re-armed by the next call reusing them.
                # Consume the whole alias group so a SECOND result replaying any sibling spelling falls into
                # the drop branch below — strict providers reject duplicate tool_call_ids with HTTP 400
                # (#58327, #66974). Credit: #55436.
                group_id = min(candidate_groups)
                for variant in outstanding_groups.pop(group_id, frozenset()):
                    if outstanding.get(variant) == group_id:
                        del outstanding[variant]
        deduped.append(msg)
    if not removed:
        return messages
    _ra().logger.debug(
        "Pre-call sanitizer: removed %d duplicate tool_call_id reference(s)", removed
    )
    return deduped


def _realign_tool_result_names(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Align each tool result's wire ``name`` with its call's function name (per-call copy only):
    Google 400s on a mismatch, routine when tool_search bridges via ``tool_call``."""
    # 4. Google matches functionResponse.name against functionCall.name and rejects a mismatch with HTTP 400
    #   "Request contains an invalid argument" (INVALID_ARGUMENT); behind an OpenAI-compatible gateway that
    #   surfaces only as a generic "Provider returned error". When tool_search defers MCP/plugin tools the
    #   model calls the bridge tool ``tool_call``, while ``make_tool_result_message()`` labels the result
    #   with the unwrapped internal tool name (``mcp__github__create_issue``) that dispatch, hooks, logging,
    #   and guardrails need. #72089 fixed exactly this for the native Gemini adapter, which now prefers
    #   ``tool_name_by_call_id`` over the result name; requests that reach Gemini through the
    #   OpenAI-compatible path (OpenRouter, Vertex/LiteLLM proxies, any OpenAI-shaped gateway) skip that
    #   translation entirely and still send the internal name on the wire. Normalizing here rather than in
    #   the OpenAI-compat serializer keeps it provider-agnostic: Gemini reaches Hermes under many model
    #   strings and base URLs, so sniffing for "is this really Google?" is unreliable, and every other
    #   provider either ignores the field or agrees with the call name. Runs on the per-call copy, so the
    #   stored trajectory keeps the real tool name for the session DB and the UI — only the wire payload
    #   changes. A no-op for the native Gemini path, which already resolves the same name. A result whose
    #   assistant call frame is missing entirely never reaches here — pass 1 above drops it as an orphan —
    #   so the only results this pass sees are ones whose call name is knowable.
    call_names: dict[str, str] = {}
    for msg in messages:
        if msg.get("role") == "assistant":
            for tc in msg.get("tool_calls") or []:
                # Strip on insert to match the lookup below so padded ids still pair.
                cid = (_ra().AIAgent._get_tool_call_id_static(tc) or "").strip()
                nm = _ra().AIAgent._get_tool_call_name_static(tc)
                if cid and nm:
                    call_names[cid] = nm
    realigned: list[tuple[str, str]] = []
    aligned: list[dict[str, Any]] = []
    for msg in messages:
        if msg.get("role") == "tool":
            expected = call_names.get((msg.get("tool_call_id") or "").strip())
            current = msg.get("name")
            # Only rewrite a present, disagreeing name; clean transcripts must stay byte-identical for prompt caching.
            if expected and current and current != expected:
                msg = {**msg, "name": expected}
                realigned.append((current, expected))
        aligned.append(msg)
    if not realigned:
        return messages
    _ra().logger.debug(
        "Pre-call sanitizer: realigned %d tool result name(s) with their "
        "tool_call function name (%s)", len(realigned),
        ", ".join(f"{was} -> {now}" for was, now in realigned),
    )
    return aligned

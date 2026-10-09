
"""Message repair & sanitization: malformed tool-call repair, orphaned-result
stripping, role-alternation fixing, think-block stripping, trajectory conversion,
sanitizer-heal accounting and the ack/degenerate/intent heuristics. Split from
``agent.agent_runtime_helpers`` (DESIGN-IT-TWICE: split by cohesion)."""

from __future__ import annotations
import contextlib
import json
import logging
import re
import threading
import time
from typing import Any, Optional
from agent.message_sanitization import (
    _FULL_ARGS_LOG_BOUND, tool_call_id_variants, tool_result_id_variants
)
from agent.message_metadata import (
    TOOL_CALL_UIDS, merge_tool_call_uids, per_occurrence_tool_call_uids, record_absorbed_message)
from agent.prompt_builder import STEER_DISPLAY_KIND, steer_user_row
from agent.tool_dispatch_helpers import _trajectory_normalize_msg, make_tool_result_message
from agent.think_scrubber import THINK_TAG_NAMES
from agent.trajectory import convert_scratchpad_to_think
from agent.message_metadata import MERGED_TURN_PREFIX
from agent.turn_context import drop_stale_api_content
from agent.agent_runtime_helpers_placeholders import _INTERRUPTED_PLACEHOLDER
from agent.runtime._runtime_ref import _ra
from agent.runtime.tool_call_integrity import (  # re-export: callers/tests unchanged
    _classify_tool_call_orphans,  # noqa: F401
    _dedupe_tool_call_ids,
    _drop_empty_tool_calls_arrays,
    _drop_invalid_roles,
    _drop_results_without_ids,
    _pair_tool_calls_positionally,
    _realign_tool_result_names,
    _repair_invalid_tool_call_names,
)
logger = logging.getLogger(__name__)


_TOOL_CALL_TAG_NAMES = ("tool_call", "tool_calls", "tool_result", "function_call", "function_calls")


# Optional XML namespace prefix: some models serialize native tool calls as <ns:function_calls>.
_NS_PREFIX = r"(?:[\w.-]+:)?"


_REASONING_BLOCK_PATTERNS = tuple(
    re.compile(rf"<{name}>.*?</{name}>", re.DOTALL | re.IGNORECASE) for name in THINK_TAG_NAMES
)


_TOOL_CALL_BLOCK_PATTERNS = tuple(
    re.compile(rf"<{_NS_PREFIX}{name}\b[^>]*>.*?</{_NS_PREFIX}{name}>", re.DOTALL | re.IGNORECASE)
    for name in _TOOL_CALL_TAG_NAMES
)


# Named <function name=...> blocks; boundary- and name-gated (see _THINK_STRIP_PATTERNS note).
_NAMED_FUNCTION_BLOCK_PATTERN = re.compile(
    r'(?:(?<=^)|(?<=[\n\r.!?:]))[ \t]*'
    r'<function\b[^>]*\bname\s*=[^>]*>'
    r'(?:(?:(?!</function>).)*)</function>', re.DOTALL | re.IGNORECASE,
)


_UNTERMINATED_REASONING_BLOCK_PATTERN = re.compile(
    rf'(?:^|\n)[ \t]*<(?:{"|".join(THINK_TAG_NAMES)})\b[^>]*>.*$', re.DOTALL | re.IGNORECASE
)


_ORPHAN_REASONING_TAG_PATTERN = re.compile(
    rf'</?(?:{"|".join(THINK_TAG_NAMES)})>\s*', re.IGNORECASE
)


_STRAY_TOOL_CALL_CLOSER_PATTERN = re.compile(
    rf'</(?:{_NS_PREFIX}(?:{"|".join(_TOOL_CALL_TAG_NAMES)}|function))>\s*', re.IGNORECASE
)


# An unclosed tool call is unrecoverable (#101899), so drop its remaining block.
# Stray argument tags only identify fragment lines, not the rest of the text
# (#102303). Require a line-start tag (optionally glued to a bare tool name,
# process_manage<arg_key>) or a line-ending closer (wait</arg_value>) so inline
# prose mentions and subsequent prose survive.
_UNTERMINATED_TOOL_CALL_PATTERN = re.compile(
    rf'(?:^|\n)[ \t]*<{_NS_PREFIX}(?:{"|".join(_TOOL_CALL_TAG_NAMES)})\b[^>]*>.*$'
    r'|(?:^|\n)[ \t]*[\w.:-]*</?arg_(?:key|value)\b[^\n]*'
    r'|(?:^|\n)[^\n<]*</arg_(?:key|value)>[ \t\r]*(?=\n|$)',
    re.DOTALL | re.IGNORECASE,
)


AGENT_RUNTIME_POST_HOOK_TOOL_NAMES = frozenset({
    "todo_list", "session_search", "memory", "clarify", "read_terminal", "desktop_preview",
    "drive_preview", "annotate_preview", "read_window_below", "manage_connections", "manage_catalog", "setup_mcp",
    "gui_tour",
    "delegate_task",
})


_TRAJECTORY_SYSTEM_PROMPT = (
    "You are a function calling AI model. You are provided with function signatures within <tools> </tools> XML tags. "
    "You may call one or more functions to assist with the user query. If available tools are not relevant in assisting "
    "with user query, just respond in natural conversational language. Don't make assumptions about what values to plug "
    "into functions. After calling & executing the functions, you will be provided with function results within "
    "<tool_response> </tool_response> XML tags. Here are the available tools:\n"
    "<tools>\n{tools}\n</tools>\n"
    "For each function call return a JSON object, with the following pydantic model json schema for each:\n"
    "{{'title': 'FunctionCall', 'type': 'object', 'properties': {{'name': {{'title': 'Name', 'type': 'string'}}, "
    "'arguments': {{'title': 'Arguments', 'type': 'object'}}}}, 'required': ['name', 'arguments']}}\n"
    "Each function call should be enclosed within <tool_call> </tool_call> XML tags.\n"
    "Example:\n<tool_call>\n{{'name': <function-name>,'arguments': <args-dict>}}\n</tool_call>"
)


def _trajectory_gpt_prefix(msg: dict[str, Any]) -> str:
    """Leading ``<think>`` block from native reasoning tokens, if any."""
    if msg.get("reasoning") and msg["reasoning"].strip():
        return f"<think>\n{msg['reasoning']}\n</think>\n"
    return ""


def _with_think_block(content: str) -> str:
    """Every gpt turn gets a <think> block (empty if none) for a consistent training format."""
    return content if "<think>" in content else "<think>\n</think>\n" + content


def _trajectory_tool_call_turn(msg: dict[str, Any]) -> str:
    content = _trajectory_gpt_prefix(msg)
    if msg.get("content") and msg["content"].strip():
        # <REASONING_SCRATCHPAD> -> <think> (model reasons via XML when native thinking is off)
        content += convert_scratchpad_to_think(msg["content"]) + "\n"
    for tool_call in msg["tool_calls"]:
        if not tool_call or not isinstance(tool_call, dict):
            continue
        raw_args = tool_call["function"]["arguments"]
        # Arguments were validated during conversation; degrade to {} rather than abort.
        try:
            arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            logger.warning("Unexpected invalid JSON in trajectory conversion: %s", raw_args[:100])
            arguments = {}
        tool_call_json = {"name": tool_call["function"]["name"], "arguments": arguments}
        content += f"<tool_call>\n{json.dumps(tool_call_json, ensure_ascii=False)}\n</tool_call>\n"
    return _with_think_block(content).rstrip()


def _trajectory_tool_responses(msg: dict[str, Any], messages: list[dict[str, Any]], start: int) -> tuple[list[str], int]:
    """Collect the ``<tool_response>`` blocks for the tool run starting at ``start``; returns ``(blocks, next_index)``."""
    tool_responses = []
    j = start
    while j < len(messages) and messages[j]["role"] == "tool":
        tool_msg = messages[j]
        tool_content = tool_msg["content"]
        try:  # pretty-print tool content if it looks like JSON
            if tool_content.strip().startswith(("{", "[")):
                tool_content = json.loads(tool_content)
        except (json.JSONDecodeError, AttributeError):
            pass
        tool_index = len(tool_responses)
        tool_name = (
            msg["tool_calls"][tool_index]["function"]["name"]
            if tool_index < len(msg["tool_calls"])
            else "unknown"
        )
        payload = json.dumps(
            {"tool_call_id": tool_msg.get("tool_call_id", ""), "name": tool_name, "content": tool_content},
            ensure_ascii=False,
        )
        tool_responses.append(f"<tool_response>\n{payload}\n</tool_response>")
        j += 1
    return tool_responses, j


def convert_to_trajectory_format(agent, messages: list[dict[str, Any]], user_query: str, completed: bool) -> list[dict[str, Any]]:
    """Convert internal message history to trajectory format for saving."""
    # Trajectories are text-only: swap image-bearing tool messages for their text_summary so ~1MB
    # base64 blobs are not embedded.
    messages = [_trajectory_normalize_msg(m) for m in messages]
    trajectory = [
        {"from": "system", "value": _TRAJECTORY_SYSTEM_PROMPT.format(tools=agent._format_tools_for_system_message())},
        {"from": "human", "value": user_query},
    ]
    # Skip messages[0] (already added). Prefill is injected at API-call time only, so no offset adjustment is needed.
    i = 1
    while i < len(messages):
        msg = messages[i]
        if msg["role"] == "assistant":
            if msg.get("tool_calls"):
                trajectory.append({"from": "gpt", "value": _trajectory_tool_call_turn(msg)})
                tool_responses, j = _trajectory_tool_responses(msg, messages, i + 1)
                if tool_responses:
                    trajectory.append({"from": "tool", "value": "\n".join(tool_responses)})
                    i = j - 1  # skip the tool messages just processed
            else:
                content = _trajectory_gpt_prefix(msg) + convert_scratchpad_to_think(msg["content"] or "")
                trajectory.append({"from": "gpt", "value": _with_think_block(content).strip()})
        elif msg["role"] == "user":
            trajectory.append({"from": "human", "value": msg["content"]})
        i += 1
    return trajectory


def _prepend_corruption_marker(tool_msg: dict, marker: str) -> None:
    existing = tool_msg.get("content")
    if isinstance(existing, str) and existing.startswith(marker):
        return
    if not isinstance(existing, (str, type(None))):
        try:
            existing = json.dumps(existing)
        except TypeError:
            existing = str(existing)
    tool_msg["content"] = f"{marker}\n{existing}" if existing else marker
    # The tool result was rewritten in place; a stamped dict's persisted row is now stale.
    from agent.context_compressor import _DB_PERSISTED_MARKER
    tool_msg.pop(_DB_PERSISTED_MARKER, None)


def _find_tool_result(messages: list, start: int, tool_call: dict) -> Optional[dict]:
    """The tool result answering ``tool_call`` in the run starting at ``start``, if any."""
    for candidate in messages[start:]:
        if not isinstance(candidate, dict) or candidate.get("role") != "tool":
            return None
        if tool_result_id_variants(candidate.get("tool_call_id")) & tool_call_id_variants(tool_call):
            return candidate
    return None


def _cursor_skip_prefix(messages: list, cursor: Optional[dict]) -> int:
    """Length of the ``is``-identical prefix already validated on the previous call."""
    prev_prefix = cursor.get("prefix") if cursor is not None else None
    start = 0
    if isinstance(prev_prefix, list):
        while start < min(len(prev_prefix), len(messages)) and messages[start] is prev_prefix[start]:
            start += 1
    return start


def sanitize_tool_call_arguments(
    messages: list, *, logger=None, session_id: str | None = None, cursor: Optional[dict] = None
) -> int:
    """Repair corrupted assistant tool-call argument JSON in-place.
    ``cursor["prefix"]`` holds strong refs (not ``id()``: address reuse aliases) to the
    messages validated last call; the ``is``-identical prefix is skipped. Safe because only
    the surrogate sanitizers mutate live dicts; every other path replaces dicts, breaking identity.

    Safety argument for skipping: a message in the matched prefix was fully scanned before — every tool_call
    argument was either already valid JSON or was rewritten to ``"{}"`` (valid). The only code paths that
    mutate ``function["arguments"]`` on live history dicts between calls are the surrogate / non-ASCII
    sanitizers, which substitute characters *inside* JSON string values and cannot invalidate JSON syntax.
    Compression, repair, undo, and steer paths replace or reorder message dicts, which breaks the identity
    match and forces a re-scan. Holding strong references (the objects themselves, not ``id()``s) makes
    address reuse aliasing (#50372-style) impossible.
    """
    log = logger or logging.getLogger(__name__)
    if not isinstance(messages, list):
        return 0
    from agent.context_compressor import _DB_PERSISTED_MARKER
    repaired = 0
    marker = _ra().AIAgent._TOOL_CALL_ARGUMENTS_CORRUPTION_MARKER
    message_index = _cursor_skip_prefix(messages, cursor)
    while message_index < len(messages):
        msg = messages[message_index]
        tool_calls = msg.get("tool_calls") if isinstance(msg, dict) and msg.get("role") == "assistant" else None
        if not isinstance(tool_calls, list) or not tool_calls:
            message_index += 1
            continue
        insert_at = message_index + 1
        for tool_call in tool_calls:
            function = tool_call.get("function") if isinstance(tool_call, dict) else None
            if not isinstance(function, dict):
                continue
            arguments = function.get("arguments")
            if arguments is None or (isinstance(arguments, str) and not arguments.strip()):
                function["arguments"] = "{}"
                msg.pop(_DB_PERSISTED_MARKER, None)
                continue
            if not isinstance(arguments, str):
                continue
            with contextlib.suppress(json.JSONDecodeError):
                json.loads(arguments)
                continue
            # Canonical ``call_id || id`` precedence so scan and stub share the id the pipeline
            # uses; bare ``id`` misses Codex call_id results and orphans a stub.
            # Keying on bare ``id`` here would fail to find a result built with ``call_id`` (Codex Responses
            # format) and insert a duplicate stub that itself becomes an orphan (#58168).
            tool_call_id = _ra().AIAgent._get_tool_call_id_static(tool_call) or None
            function_name = function.get("name", "?")
            # Log the FULL (bounded) argument string: we are about to overwrite the only copy, which
            # may hold real user content from a truncated write_file/patch.
            log.warning(
                "Corrupted tool_call arguments repaired before request "
                "(session=%s, message_index=%s, tool_call_id=%s, function=%s, "
                "original_arguments=%r)", session_id or "-", message_index, tool_call_id or "-",
                function_name, arguments[:_FULL_ARGS_LOG_BOUND],
            )
            function["arguments"] = "{}"
            # The persisted row for a stamped dict still holds the corrupted args; pop the
            # marker so the flush rewrites it (the repaired args are what the wire saw).
            msg.pop(_DB_PERSISTED_MARKER, None)
            existing_tool_msg = _find_tool_result(messages, message_index + 1, tool_call)
            if existing_tool_msg is None:
                messages.insert(
                    insert_at,
                    make_tool_result_message(function_name if function_name != "?" else "", marker, tool_call_id),
                )
                insert_at += 1
            else:
                _prepend_corruption_marker(existing_tool_msg, marker)
            repaired += 1
        message_index += 1
    if cursor is not None:
        # Strong refs to the objects validated this call; any divergence (compression, undo, repair,
        # steer) forces a re-scan from that index.
        cursor["prefix"] = messages[:]
    return repaired


# Session-scoped in-flight registry for note_turn_start: the gateway caches agents per routing key
# while the transcript is keyed by session_id, so two agent objects can run concurrent turns on one
# session unseen by per-agent state.
_INFLIGHT_TURNS_BY_SESSION: dict[str, tuple[str, float]] = {}


_INFLIGHT_TURNS_LOCK = threading.Lock()


def note_turn_start(agent, turn_id: str):
    """Tripwire: warn when a turn starts while a previous turn of the same agent or session
    (on another agent object) has not finished its persist. Does not prevent the overlap; it
    names both turn ids so the dispatch route that bypassed the busy guard is findable in logs.
    Returns the previous in-flight turn_id on overlap, else None; takes the slot either way."""
    prev = getattr(agent, "_inflight_turn_id", None)
    prev_started = getattr(agent, "_inflight_turn_started", 0.0)
    agent._inflight_turn_id = turn_id
    agent._inflight_turn_started = time.time()
    overlap = None
    if prev and prev != turn_id:
        logger.warning(
            "turn %s starting while turn %s (started %.0fs ago) has not "
            "completed its turn-end persist (session=%s) — concurrent turns "
            "on one session; transcript writes may interleave", turn_id, prev,
            time.time() - prev_started if prev_started else -1.0, getattr(agent, "session_id", None) or "-",
        )
        overlap = prev
    # Cross-agent leg: same session_id in flight under another agent object (busy guard is keyed by
    # routing key and cannot see it). Persist-disabled forks share the parent's session_id but never
    # write, so they must not register or pop here (note_turn_persisted skips them symmetrically).
    session_id = getattr(agent, "session_id", None)
    if session_id and not getattr(agent, "_persist_disabled", False):
        now = time.time()
        with _INFLIGHT_TURNS_LOCK:
            entry = _INFLIGHT_TURNS_BY_SESSION.get(session_id)
            _INFLIGHT_TURNS_BY_SESSION[session_id] = (turn_id, now)
        # Record the session id registered under: compression can rotate agent.session_id mid-turn
        # and persist must pop the slot actually held.
        agent._inflight_turn_session_id = session_id
        if entry and entry[0] not in (turn_id, prev):
            logger.warning(
                "turn %s starting while turn %s (started %.0fs ago) is still "
                "in flight on session %s under a different agent object — "
                "two routing keys are mapped to one session_id; concurrent "
                "turns on one session; transcript writes may interleave", turn_id, entry[0],
                now - entry[1] if entry[1] else -1.0, session_id,
            )
            overlap = overlap or entry[0]
    return overlap


def note_turn_persisted(agent):
    """Clear the in-flight marker at turn-end persist (see note_turn_start). Unconditional by
    design: on a real overlap the first persist clears the second slot, so the tripwire
    under-reports rather than double-reports."""
    agent._inflight_turn_id = None
    # Persist-disabled forks never registered a slot; popping here would steal the live parent
    # turn's slot (symmetric with note_turn_start).
    if not getattr(agent, "_persist_disabled", False):
        session_id = getattr(agent, "_inflight_turn_session_id", None) or getattr(agent, "session_id", None)
        if session_id:
            with _INFLIGHT_TURNS_LOCK:
                _INFLIGHT_TURNS_BY_SESSION.pop(session_id, None)
    agent._inflight_turn_session_id = None


def _is_codex_interim(m: dict) -> bool:
    """Codex Responses interim turn: carries its own continuation state, replayed verbatim."""
    return bool(
        m.get("codex_reasoning_items")
        or m.get("codex_message_items")
        or m.get("finish_reason") == "incomplete"
    )


def _merge_assistant_into(prev: dict, msg: dict) -> bool:
    """Fold consecutive assistant *msg* into *prev* (union tool_calls, concat text). Returns whether *msg*'s
    text survives: multimodal (list) content is never joined."""
    from agent.context_compressor import _DB_PERSISTED_MARKER

    prev_calls = list(prev.get("tool_calls") or [])
    new_calls = list(msg.get("tool_calls") or [])
    calls_changed = False
    if new_calls:
        prev["tool_calls"] = prev_calls + new_calls
        # The absorbed turn's calls keep the per-occurrence ids they were persisted with.
        if isinstance(extra := msg.get(TOOL_CALL_UIDS), dict) and extra:
            prev[TOOL_CALL_UIDS] = merge_tool_call_uids(
                per_occurrence_tool_call_uids(
                    own if isinstance(own := prev.get(TOOL_CALL_UIDS), dict) else {}, prev_calls),
                per_occurrence_tool_call_uids(extra, new_calls))
        calls_changed = True
    elif prev_calls:
        prev["tool_calls"] = prev_calls
    else:
        # Drop a stale ``tool_calls: []`` at the source: strict providers (DeepSeek v4, Kimi) 400 on
        # it and it persists into replayed history.
        # Neither turn carries tool calls, but the surviving turn may still carry a stale ``tool_calls: []``
        # from the earlier message. An empty array is semantically "no tool calls", yet strict
        # OpenAI-compatible providers (DeepSeek v4, Moonshot/Kimi) reject it with HTTP 400 ("Invalid
        # 'messages[N].tool_calls': empty array..."). Drop the key HERE, at the source:
        # ``sanitize_api_messages`` only fixes the per-call wire copy, so a ``[]`` left on the repaired turn
        # survives in the live/persisted trajectory returned to callers (gateway/WebUI transcripts, session
        # resume, subagents, cron) and is replayed on the next turn — which is how #58755 kept reproducing
        # after the chokepoint fix (#77921). Popping is non-destructive: an empty array carries no
        # information.
        calls_changed = "tool_calls" in prev
        prev.pop("tool_calls", None)
    # Concatenate plain-text content only; leave multimodal (list) content alone.
    prev_content = prev.get("content")
    new_content = msg.get("content")
    content_rewritten = False
    text_kept = not new_content  # nothing to lose
    if isinstance(prev_content, str) and isinstance(new_content, str):
        text_kept = True
        joined = "\n".join(p for p in (prev_content.strip(), new_content.strip()) if p)
        prev["content"] = joined
        # A falsy new_content leaves ``joined`` == prev_content; that is not a rewrite.
        # "") strips to nothing and ``joined`` collapses back to ``prev_content`` unchanged -- that must NOT
        # count as a rewrite (wz-heng, #78063 review).
        content_rewritten = joined != prev_content
    elif not prev_content and new_content is not None:
        prev["content"] = new_content
        content_rewritten = new_content != prev_content
        text_kept = True
    # Carry reasoning_content from the later turn only if the earlier lacks it (strict thinking
    # providers need one on the merged tool-call turn).
    reasoning_carried = False
    if not prev.get("reasoning_content") and msg.get("reasoning_content"):
        prev["reasoning_content"] = msg["reasoning_content"]
        reasoning_carried = True
    # A stale ``api_content`` sidecar overrides ``content`` at API-build time and would replay
    # pre-merge bytes; drop it only when content actually changed.
    # ``prev`` may carry an ``api_content`` sidecar (the exact bytes previously sent to the API, e.g. a
    # sanitize-divergence stamp — see ``_flush_messages_to_session_db``) from BEFORE this merge. The sidecar
    # takes priority over ``content`` at API-build time (``conversation_loop``'s ``api_messages`` build
    # substitutes it back in for role ``assistant``), so leaving it in place while ``prev["content"]``
    # changes would silently replay the pre-merge bytes and discard everything this merge just concatenated
    # on — the same stale-field-survives-the-merge shape as the ``tool_calls`` gap above, just for a
    # different field. Only drop it when the merge actually changed the resulting value (e.g. the later
    # turn's content is ``None``, or either side is multimodal/list — both branches skip the reassignment
    # and ``prev["content"]`` is untouched; a falsy ``new_content`` that strips to nothing also leaves
    # ``joined`` equal to the original ``prev_content``): in those cases the sidecar is still the exact
    # bytes previously sent for the UNCHANGED content, and dropping it would break the prompt-cache replay
    # invariant for no reason (wz-heng, #78063 review).
    if content_rewritten:
        drop_stale_api_content(prev)
    # The persist marker asserts the whole row is durable (content, tool_calls, reasoning sidecar), so
    # any merged field stales it; pop it or the flush scan identity-skips the merged dict and the DB
    # keeps the pre-merge row. The caller recomputes the flush cursor for the surviving sequence.
    if content_rewritten or calls_changed or reasoning_carried:
        prev.pop(_DB_PERSISTED_MARKER, None)
    return text_kept


def _remember_absorbed_row(survivor: dict[str, Any], dropped: dict[str, Any], *, folded: bool) -> None:
    """Retire *dropped*'s row ids onto *survivor*; record its uid as a merge witness only when *folded* (its
    text survives). An empty incoming turn still merges; stamping an empty list would change a message that
    absorbed nothing. A dropped id that equals the survivor's own live id (the display-marker merge adopts
    the plain row's id, #94486) is not an absorbed row: the survivor IS that row."""
    from agent.conversation_compression_archive import OWN_ROW, RETIRED_DURABLE_ROWS, UNNAMED_DURABLE_ROWS

    own_id = survivor.get("_row_id")
    ids = []
    row_id = dropped.get("_row_id")
    if isinstance(row_id, int) and not isinstance(row_id, bool) and row_id > 0 and row_id != own_id:
        ids.append(row_id)
    for older in dropped.get("_absorbed_row_ids") or ():
        if isinstance(older, int) and not isinstance(older, bool) and older > 0 and older not in ids:
            ids.append(older)
    if ids:
        absorbed = survivor.setdefault("_absorbed_row_ids", [])
        for row_id in ids:
            if row_id not in absorbed:
                absorbed.append(row_id)
    # The rows the retired dict counted are behind the survivor now.
    if dropped.get(UNNAMED_DURABLE_ROWS):
        survivor[UNNAMED_DURABLE_ROWS] = int(survivor.get(UNNAMED_DURABLE_ROWS) or 0) + int(dropped[UNNAMED_DURABLE_ROWS])
    # Its own row is behind the survivor now too.
    if dropped.get(RETIRED_DURABLE_ROWS):
        survivor.setdefault(RETIRED_DURABLE_ROWS, []).extend(
            {k: v for k, v in row.items() if k != OWN_ROW} for row in dropped[RETIRED_DURABLE_ROWS])
    # The uid witness claims the dropped dict's TEXT lives on in the survivor: only a fold earns it. A
    # superseded row (``folded=False``) is retired like any absorbed row but its content is discarded.
    if folded:
        record_absorbed_message(survivor, dropped)


def _count_unnamed_row(survivor: dict[str, Any], retired: dict[str, Any]) -> None:
    """On a reload without row ids nothing names *retired*'s durable row once the repair takes the dict
    out of the list, so *survivor* counts it. A dict that counts rows was loaded too: a merge may have
    popped its marker since. Call before the merge rewrites the survivor: *retired*'s own fields are
    recorded so the commit can name its row."""
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from agent.conversation_compression_archive import (
        OWN_ROW, RETIRED_DURABLE_ROWS, UNNAMED_DURABLE_ROWS, retired_row_payload)

    def loaded(message: dict[str, Any]) -> bool:
        return bool(message.get(_DB_PERSISTED_MARKER) or message.get(UNNAMED_DURABLE_ROWS))

    if loaded(survivor) and loaded(retired) and not isinstance(retired.get("_row_id"), int):
        survivor[UNNAMED_DURABLE_ROWS] = int(survivor.get(UNNAMED_DURABLE_ROWS) or 0) + 1
        # A previously folded dict already records its original row. The transfer in
        # _remember_absorbed_row retires that record; its synthetic text/calls never existed in storage.
        if not any(row.get(OWN_ROW) for row in retired.get(RETIRED_DURABLE_ROWS) or ()):
            survivor.setdefault(RETIRED_DURABLE_ROWS, []).append(retired_row_payload(retired))


def _remember_own_row(survivor: dict[str, Any]) -> None:
    """An assistant fold rewrites *survivor*'s text, so on a reload without row ids its own row no longer
    matches it by content. Record its loaded fields first, once."""
    from agent.context_compressor import _DB_PERSISTED_MARKER
    from agent.conversation_compression_archive import OWN_ROW, RETIRED_DURABLE_ROWS, retired_row_payload

    recorded = survivor.get(RETIRED_DURABLE_ROWS) or ()
    if (survivor.get(_DB_PERSISTED_MARKER) and not isinstance(survivor.get("_row_id"), int)
            and not any(isinstance(row, dict) and row.get(OWN_ROW) for row in recorded)):
        survivor.setdefault(RETIRED_DURABLE_ROWS, []).append({**retired_row_payload(survivor), OWN_ROW: True})


def _retire_dropped_row(kept: list[dict], dropped: dict[str, Any], leading: list[dict]) -> None:
    """A durable row the repair drops was still handed to the caller, so the survivor before it stands
    for it. Left unnamed, an in-place compaction takes it for a row another surface appended and
    re-sequences it behind the running turn. A drop with no survivor before it waits in *leading*."""
    if not kept:
        leading.append(dropped)
    elif isinstance(kept[-1], dict):
        _count_unnamed_row(kept[-1], dropped)
        _remember_absorbed_row(kept[-1], dropped, folded=False)


def _retire_leading_drops(kept: list[dict], leading: list[dict]) -> None:
    """Rows dropped ahead of the first survivor sit right before its own row, so it stands for them."""
    for dropped in leading if kept and isinstance(kept[0], dict) else ():
        _count_unnamed_row(kept[0], dropped)
        _remember_absorbed_row(kept[0], dropped, folded=False)


def _merge_consecutive_assistants(messages: list[dict]) -> tuple[list[dict], int]:
    """Pass 0: merge consecutive assistant turns (codex interims exempt)."""
    repairs = 0
    collapsed: list[dict] = []
    for msg in messages:
        prev = collapsed[-1] if collapsed and isinstance(collapsed[-1], dict) else None
        if (
            prev is not None and prev.get("role") == "assistant"
            and isinstance(msg, dict) and msg.get("role") == "assistant"
            and not _is_codex_interim(msg) and not _is_codex_interim(prev)
        ):
            # A provisional verification candidate is superseded, not unioned.
            if prev.get("finish_reason") in {"verification_required", "verify_hook_continue"}:
                _count_unnamed_row(msg, prev)
                _remember_absorbed_row(msg, prev, folded=False)
                collapsed[-1] = msg
            else:
                _count_unnamed_row(prev, msg)
                _remember_own_row(prev)
                _remember_absorbed_row(prev, msg, folded=_merge_assistant_into(prev, msg))
            repairs += 1
            continue
        collapsed.append(msg)
    return collapsed, repairs


def _drop_stray_tool_results(messages: list[dict]) -> tuple[list[dict], int]:
    """Pass 1: drop tool results not following a known assistant tool call. Consumes the whole
    alias group (call_id/id/response_item_id/composite) so a duplicate keyed on a sibling
    alias is not replayed to strict providers."""
    repairs = 0
    known_tool_ids: dict[str, int] = {}  # alias -> group id; reset by assistant/user turns
    # Pass 1: drop stray tool messages that don't follow a known assistant tool call. A Responses call can
    # have several equivalent spellings (call_id, id, response_item_id, or a composite ``call|item`` id), so
    # consume the whole alias group when one spelling is matched. Alias expansion lives in
    # ``agent.message_sanitization.tool_call_id_variants`` / ``tool_result_id_variants`` (single policy
    # owner) — which also handles SDK tool_call objects, preserving the #91768 dict-or-object tolerance.
    matched_tool_groups: set = set()
    next_tool_group = 0
    filtered: list[dict] = []
    leading: list[dict] = []
    for msg in messages:
        role = msg.get("role") if isinstance(msg, dict) else None
        if role in ("assistant", "user"):
            # An assistant turn starts a new tool-result run; a user turn closes it (later tool
            # messages are orphans).
            known_tool_ids = {}
            matched_tool_groups = set()
            for tc in (msg.get("tool_calls") or []) if role == "assistant" else ():
                variants = tool_call_id_variants(tc)
                if variants:
                    for tc_id in variants:
                        known_tool_ids.setdefault(tc_id, next_tool_group)
                    next_tool_group += 1
        elif role == "tool":
            result_variants = tool_result_id_variants(msg.get("tool_call_id"))
            candidate_groups = {
                known_tool_ids[tc_id] for tc_id in result_variants
                if tc_id in known_tool_ids and known_tool_ids[tc_id] not in matched_tool_groups
            }
            if result_variants and not candidate_groups:
                _retire_dropped_row(filtered, msg, leading)
                repairs += 1
                continue
            if candidate_groups:
                matched_tool_groups.add(min(candidate_groups))
        filtered.append(msg)
    _retire_leading_drops(filtered, leading)
    return filtered, repairs


def _prune_unanswered_tool_calls(messages: list[dict]) -> tuple[list[dict], int]:
    """Pass 2: prune tool_calls not answered in the IMMEDIATELY following tool run (a displaced
    result masks the per-call stub pass and strict providers 400). Payload-empty turns are
    dropped; codex interims exempt."""
    from agent.context_compressor import _DB_PERSISTED_MARKER

    repairs = 0
    pruned: list[dict] = []
    leading: list[dict] = []
    for i, msg in enumerate(messages):
        if not (
            isinstance(msg, dict) and msg.get("role") == "assistant" and msg.get("tool_calls")
            and not _is_codex_interim(msg)
        ):
            pruned.append(msg)
            continue
        answered: set = set()
        for follower in messages[i + 1:]:
            if not (isinstance(follower, dict) and follower.get("role") == "tool"):
                break
            tid = (follower.get("tool_call_id") or "").strip()
            if tid:
                answered.update(tool_result_id_variants(tid))
        kept_calls = [tc for tc in msg["tool_calls"] if tool_call_id_variants(tc) & answered]
        if len(kept_calls) != len(msg["tool_calls"]):
            repairs += 1
            if not kept_calls and not _msg_has_payload({k: v for k, v in msg.items() if k != "tool_calls"}):
                # Pruned calls were the only payload; drop the turn (empty assistant messages 400).
                _retire_dropped_row(pruned, msg, leading)
                continue
            if kept_calls:
                msg["tool_calls"] = kept_calls
            else:
                msg.pop("tool_calls", None)
            # tool_calls is part of the persisted row; rewriting it on a stamped dict stales the
            # marker, so pop it or the flush scan skips the dict and the DB keeps the old calls.
            msg.pop(_DB_PERSISTED_MARKER, None)
        pruned.append(msg)
    _retire_leading_drops(pruned, leading)
    return pruned, repairs


def _merge_consecutive_users(messages: list[dict]) -> tuple[list[dict], int]:
    """Pass 3: merge consecutive plain-text user messages (no user input lost)."""
    from agent.context_compressor import _DB_PERSISTED_MARKER, split_user_originated_turn
    from agent.conversation_compression_archive import MERGED_DURABLE_ROWS
    from hermes_state import SessionDB

    def _plain_text(content: Any) -> bool:
        # never rewrite the persisted row around undecodable content
        return isinstance(content, str) and not content.startswith(SessionDB._CONTENT_JSON_PREFIX)

    repairs = 0
    merged: list[dict] = []
    for msg in messages:
        prev = merged[-1] if merged and isinstance(merged[-1], dict) else None
        if (
            prev is not None and prev.get("role") == "user"
            and isinstance(msg, dict) and msg.get("role") == "user"
            # A summary carrier followed by a new user row is a deliberate durable shape after
            # retry/rewind; never mutate the persisted carrier (sanitizers merge copies later).
            and split_user_originated_turn(prev)[0] is None
            # A /steer row that ended the previous run is already persisted; merging the next
            # prompt into it would rewrite it in place and re-break replay parity.
            and prev.get("display_kind") != STEER_DISPLAY_KIND
            # Only merge plain-text content; leave multimodal (list or undecodable sentinel) content alone.
            and _plain_text(prev.get("content", "")) and _plain_text(msg.get("content", ""))
        ):
            prev_content, new_content = prev.get("content", ""), msg.get("content", "")
            merged_content = (
                (prev_content + "\n\n" + new_content) if prev_content and new_content else (prev_content or new_content)
            )
            had_api_sidecar = "api_content" in prev
            # Read before the marker is popped below. An unpersisted turn folded in ends the claim:
            # the dict no longer stands for durable rows only.
            if (prev.get(_DB_PERSISTED_MARKER) or prev.get(MERGED_DURABLE_ROWS)) and msg.get(_DB_PERSISTED_MARKER):
                prev[MERGED_DURABLE_ROWS] = int(prev.get(MERGED_DURABLE_ROWS) or 1) + 1
            else:
                prev.pop(MERGED_DURABLE_ROWS, None)
            prev["content"] = merged_content
            # The clean-text persist override must replace only the absorbed turn, never the
            # unanswered text before it; kept across replay passes (an empty turn absorbs too).
            if prev_content:
                prev[MERGED_TURN_PREFIX] = prev_content
            # Merged content invalidates the api_content sidecar; drop it so replay cannot use stale bytes.
            drop_stale_api_content(prev)
            # A display-marker row (e.g. a model-switch marker persisted as role=user on
            # purpose, #48338) merging with a plain user row must not bury the plain row's
            # addressable identity: keeping the marker's display_kind hides the merged pair
            # from every user-turn index used for rewind/submit addressing (display rows are
            # excluded), so the plain row's durable id becomes unresolvable and the client's
            # next prompt.submit fails closed with the input silently dropped (#94486). Keep
            # the pair addressable instead: drop the display classification and carry the
            # plain row's id, retiring the marker's own id onto the absorbed list. display_kind
            # never reaches providers (stripped from every outgoing copy), so the merged
            # turn's wire payload is unchanged. Deliberate scope: when BOTH rows carry
            # display_kind (two consecutive model-switch markers) the pair keeps the first
            # marker's classification and id — no plain row is swallowed there, so no
            # addressable turn is lost.
            if prev.get("display_kind") and not msg.get("display_kind"):
                marker_row_id = prev.get("_row_id")
                prev.pop("display_kind", None)
                if msg.get("_row_id") is not None:
                    if isinstance(marker_row_id, int) and not isinstance(marker_row_id, bool):
                        absorbed_ids = prev.setdefault("_absorbed_row_ids", [])
                        if marker_row_id not in absorbed_ids:
                            absorbed_ids.append(marker_row_id)
                    prev["_row_id"] = msg["_row_id"]
                # display_kind is part of the persisted row; reclassifying stales it even
                # when the merged bytes reproduce the persisted content (empty absorb).
                prev.pop(_DB_PERSISTED_MARKER, None)
            # Pop the persist marker only when the durable row actually changed: a merge that
            # reproduces the persisted bytes (e.g. an empty incoming turn) keeps its stamp.
            if merged_content != prev_content or had_api_sidecar:
                prev.pop(_DB_PERSISTED_MARKER, None)
            _remember_absorbed_row(prev, msg, folded=True)
            repairs += 1
            continue
        merged.append(msg)
    return merged, repairs


_SEQUENCE_REPAIR_PASSES = (
    _merge_consecutive_assistants, _drop_stray_tool_results, _prune_unanswered_tool_calls,
    _merge_consecutive_users,
)


def _normalize_sentinel_encoded_content(messages: list[dict]) -> None:
    """Decode any sentinel-encoded row content in place before the alternation passes run, so an
    image-bearing turn is never merged as text (#125299). A multimodal turn can re-enter the working set
    as its ``\\x00json:[…]`` string (e.g. after a proactive prune re-inserts history, #124102). A body
    that no longer parses stays a sentinel string; ``_merge_consecutive_users`` refuses to weld it."""
    from hermes_state import SessionDB  # lazy: the persistence layer owns the sentinel codec

    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        decoded = SessionDB._decode_content(content)
        if decoded is not content:
            msg["content"] = decoded
            # Keep any `_db_persisted` marker: re-encoding reproduces the stored scalar, and dropping
            # it makes the append-only flush re-append this user turn as a duplicate (#125331).


def repair_message_sequence(agent, messages: list[dict]) -> int:
    """Collapse malformed role-alternation left in the live history; returns repair count.
    Providers require strict alternation after the system message (violations: silent empty
    responses or 400s); this is the pre-call belt for host-fed, resumed or replayed histories.
    Passes in order: decode any sentinel-encoded multimodal rows (so an image is not merged as text);
    merge consecutive assistant turns (BEFORE orphan detection so the merged tool_call-id union is
    known); drop stray tool results; prune unanswered tool_calls; merge consecutive user turns. A user
    turn directly after an assistant turn is valid and left alone.
    """
    if not messages:
        return 0
    _normalize_sentinel_encoded_content(messages)
    repairs = 0
    current = messages
    for repair_pass in _SEQUENCE_REPAIR_PASSES:
        current, made = repair_pass(current)
        repairs += made
    if repairs > 0:
        # Rewrite in place so persistence/return value/DB flush see the repaired sequence.
        messages[:] = current
    return repairs


def repair_message_sequence_with_cursor(agent, messages: list[dict]) -> int:
    """Run :func:`repair_message_sequence` and keep ``_last_flushed_db_idx`` consistent. Repair
    shrinks the list in place; counting identity-preserved survivors of the flushed prefix gives
    the exact new cursor, whereas a ``min()`` clamp would skip unflushed rows (used only without a snapshot)."""
    from agent.context_compressor import _DB_PERSISTED_MARKER

    flush_cursor = getattr(agent, "_last_flushed_db_idx", None)
    flushed_ids = {id(m) for m in messages[:flush_cursor]} if isinstance(flush_cursor, int) and flush_cursor > 0 else None
    stamped_ids = {id(m) for m in messages if isinstance(m, dict) and m.get(_DB_PERSISTED_MARKER)}
    repairs = repair_message_sequence(agent, messages)
    if repairs > 0:
        # A stamped survivor that lost its marker was mutated in place by a merge/prune pass; the
        # bounded flush scan would skip past it inside the identity-matched prefix, so force a
        # full re-scan (same contract as the compressor's _flush_scan_cursor_invalidated).
        if stamped_ids and any(
            id(m) in stamped_ids and not m.get(_DB_PERSISTED_MARKER) for m in messages
        ):
            agent._db_flush_scan_prefix = None
        if hasattr(agent, "_last_flushed_db_idx"):
            if flushed_ids is not None:
                agent._last_flushed_db_idx = sum(1 for m in messages if id(m) in flushed_ids)
            else:
                agent._last_flushed_db_idx = min(agent._last_flushed_db_idx, len(messages))
    return repairs


def _flatten_content_text(content: Any) -> str:
    """Flatten list/dict content (e.g. Anthropic-via-OpenRouter block lists) to text: a raw list
    hitting ``re.sub`` raises TypeError and the loop retries forever. Thinking/reasoning blocks
    are dropped outright; their text key varies per provider."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part if isinstance(part, str) else part.get("text")
            for part in content
            if isinstance(part, str) or (
                isinstance(part, dict)
                and str(part.get("type") or "").strip().lower() not in {"thinking", "reasoning", "redacted_thinking"}
                and isinstance(part.get("text"), str) and part.get("text")
            )
        )
    if isinstance(content, dict):
        return str(content.get("text") or content.get("content") or "")
    return str(content)


# Order matters: closed pairs first (case-insensitive so mixed-case tags don't fall through to the
# unterminated pass and eat trailing content), then tool-call XML blocks, the boundary+name-gated
# <function> block, the unterminated reasoning block, stray orphan reasoning tags, and finally stray
# tool-call CLOSERS only (bare/unterminated <function> is kept: a truncated streaming tail may still
# be valuable, matching OpenClaw's asymmetry).
_THINK_STRIP_PATTERNS = (
    *_REASONING_BLOCK_PATTERNS, *_TOOL_CALL_BLOCK_PATTERNS, _NAMED_FUNCTION_BLOCK_PATTERN,
    _UNTERMINATED_REASONING_BLOCK_PATTERN, _ORPHAN_REASONING_TAG_PATTERN,
    _STRAY_TOOL_CALL_CLOSER_PATTERN, _UNTERMINATED_TOOL_CALL_PATTERN,
)


def strip_think_blocks(agent, content: str) -> str:
    """Remove reasoning/thinking blocks from content, returning only visible text: closed tag
    pairs, unterminated open tags at a block boundary (mirrors ``gateway/stream_consumer.py``),
    stray orphan tags (all case-insensitive variants), and standalone tool-call XML blocks some
    open models emit; ``<function>`` is boundary- and ``name=``-gated so prose mentions survive."""
    content = _flatten_content_text(content) if content else ""
    for pattern in _THINK_STRIP_PATTERNS if content else ():
        content = pattern.sub('', content)
    return content


def _merge_user_content(prev_content: Any, cur_content: Any) -> Any:
    """Merged content for two adjacent user messages (``_UNMERGEABLE`` for unknown shapes):
    string+string joins with a blank line; list sides append as separate blocks."""
    if isinstance(prev_content, str) and isinstance(cur_content, str):
        return prev_content + ("\n\n" if prev_content and cur_content else "") + cur_content
    if isinstance(prev_content, list) and isinstance(cur_content, list):
        return list(prev_content) + list(cur_content)
    if isinstance(prev_content, list) and isinstance(cur_content, str):
        return list(prev_content) + ([{"type": "text", "text": cur_content}] if cur_content else [])
    if isinstance(prev_content, str) and isinstance(cur_content, list):
        return ([{"type": "text", "text": prev_content}] if prev_content else []) + list(cur_content)
    return _UNMERGEABLE


_UNMERGEABLE = object()


def drop_thinking_only_and_merge_users(
    messages: list[dict[str, Any]], *, drop_codex_reasoning_items: bool = True,
    drop_nudge_marker: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Drop thinking-only assistant turns and merge adjacent user messages left behind, on the
    per-call ``api_messages`` copy only (``agent.messages`` is never mutated). Drop-and-merge
    (not stub text) keeps history honest and preserves role alternation.

    ``drop_nudge_marker`` (#67321): user rows equal to the marker — the synthetic Codex
    continuation nudge — are dropped too once the turn has crossed to a non-Codex provider;
    doing it in this pass keeps alternation valid when the nudge sat between dropped
    reasoning-only interims and a tool result rather than next to the user's message."""
    if not messages:
        return messages
    kept = [
        m for m in messages
        if not (drop_nudge_marker is not None and m.get("role") == "user" and m.get("content") == drop_nudge_marker)
        and not _ra().AIAgent._is_thinking_only_assistant(m, drop_codex_reasoning_items=drop_codex_reasoning_items)
    ]
    dropped = len(messages) - len(kept)
    merged: list[dict[str, Any]] = []
    merges = 0
    for m in kept:
        prev = merged[-1] if merged else None
        content = _UNMERGEABLE
        if prev is not None and prev.get("role") == "user" and m.get("role") == "user":
            content = _merge_user_content(prev.get("content", ""), m.get("content", ""))
        if content is _UNMERGEABLE:
            # Not a user pair, or an unknown content shape: append separately (the latter violates
            # alternation, but is safer than raising in a hot path).
            merged.append(m)
        else:
            merged[-1] = {**prev, "content": content}  # copy so caller dicts are never mutated
            merges += 1
    if dropped == 0 and merges == 0:
        return messages
    _ra().logger.debug(
        "Pre-call sanitizer: dropped %d thinking-only assistant turn(s), "
        "merged %d adjacent user message(s)", dropped, merges,
    )
    return merged


_INLINE_REASONING_PATTERNS = tuple(
    re.compile(rf"<{tag}>(.*?)</{tag}>", re.DOTALL | re.IGNORECASE)
    for tag in THINK_TAG_NAMES
)


def extract_reasoning(agent, assistant_message) -> Optional[str]:
    """Reasoning text from ``reasoning`` / ``reasoning_content`` / ``reasoning_details``
    (OpenRouter unified), else inline thinking blocks in the content; None when absent."""
    from agent.message_content import flatten_message_text

    parts: list[str] = []

    def _add(text) -> None:
        text = flatten_message_text(text, sep="")
        if text and text not in parts:
            parts.append(text)
    _add(getattr(assistant_message, "reasoning", None))
    _add(getattr(assistant_message, "reasoning_content", None))
    # reasoning_details: [{"type": "reasoning.summary", "summary": "...", ...}, ...]
    for detail in getattr(assistant_message, "reasoning_details", None) or []:
        if isinstance(detail, dict):
            _add(detail.get('summary') or detail.get('thinking') or detail.get('content') or detail.get('text'))
    # Fall back to reasoning embedded in content only when no structured field was found.
    content = getattr(assistant_message, "content", None)
    if not parts and isinstance(content, list):
        # DeepSeek V4 Pro returns typed content blocks ({"type": "thinking", ...}); dropping them
        # makes the next turn fail with HTTP 400 "thinking must be passed back".
        # Refs #21944.
        for block in content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                # Non-strict OpenAI-compatible backends (Mistral via custom provider)
                # deliver the thinking value as a JSON array, not a string (#106006);
                # flatten first so .strip() never sees a list.
                _add(flatten_message_text(block.get("thinking") or block.get("text") or "", sep="").strip())
    if not parts and isinstance(content, str) and content:
        for pattern in _INLINE_REASONING_PATTERNS:
            for block in pattern.findall(content):
                _add(block.strip())
    return "\n\n".join(parts) if parts else None


def _pre_tool_block_message(agent, function_name, function_args, effective_task_id, tool_call_id, middleware_trace):
    """Plugin pre-tool-call hook verdict: ``(block_message, function_args)``; failures never block."""
    try:
        from hermes_cli.plugins import _dispatch_pre_tool_call_hooks
        block_message, modified_args = _dispatch_pre_tool_call_hooks(
            function_name, function_args, task_id=effective_task_id or "",
            session_id=getattr(agent, "session_id", "") or "", tool_call_id=tool_call_id or "",
            turn_id=getattr(agent, "_current_turn_id", "") or "",
            api_request_id=getattr(agent, "_current_api_request_id", "") or "",
            middleware_trace=list(middleware_trace),
        )
        return block_message, (modified_args if modified_args is not None else function_args)
    except Exception:
        return None, function_args


def invoke_tool(agent, function_name: str, function_args: dict, effective_task_id: str,
                 tool_call_id: Optional[str] = None, messages: list | None = None,
                 pre_tool_block_checked: bool = False,
                 skip_tool_request_middleware: bool = False,
                 tool_request_middleware_trace: Optional[list[dict[str, Any]]] = None,
                 skip_tool_execution_middleware: bool = False) -> str:
    """Invoke a single tool (agent-level or registry-dispatched) and return the result string;
    no display logic. Used by the concurrent path; the sequential path keeps its own inline
    invocation for display."""
    from agent.inline_tool_executors import (
        InlineToolContext, apply_transform_tool_result, emit_terminal_post_tool_call,
        resolve_invoke_tool_executor, tool_hook_ids
    )
    if not isinstance(function_args, dict):
        function_args = {}
    hook_ids = tool_hook_ids(agent, effective_task_id, tool_call_id)
    _tool_middleware_trace = list(tool_request_middleware_trace or [])
    try:
        from hermes_cli.middleware import apply_tool_request_middleware
        if not skip_tool_request_middleware:
            _tool_request_mw = apply_tool_request_middleware(function_name, function_args, **hook_ids)
            function_args = _tool_request_mw.payload
            _tool_middleware_trace = _tool_request_mw.trace
    except Exception as _mw_err:
        logger.debug("tool_request middleware error: %s", _mw_err)
    block_message: Optional[str] = None
    if not pre_tool_block_checked:
        block_message, function_args = _pre_tool_block_message(
            agent, function_name, function_args, effective_task_id, tool_call_id, _tool_middleware_trace
        )
    if block_message is not None:
        result = json.dumps({"error": block_message}, ensure_ascii=False)
        emit_terminal_post_tool_call(
            agent, function_name=function_name, function_args=function_args, result=result,
            effective_task_id=effective_task_id, tool_call_id=tool_call_id, status="blocked",
            error_type="plugin_block", error_message=block_message,
            middleware_trace=_tool_middleware_trace,
        )
        return result
    tool_start_time = time.monotonic()
    inline_executor = resolve_invoke_tool_executor(agent, function_name)
    if inline_executor is not None:
        inline_ctx = InlineToolContext(
            effective_task_id=effective_task_id, tool_call_id=tool_call_id, messages=messages
        )

        def _execute(next_args: dict) -> Any:
            result = inline_executor(agent, next_args, inline_ctx)
            call_args = next_args if isinstance(next_args, dict) else function_args
            duration_ms = int((time.monotonic() - tool_start_time) * 1000)
            emit_terminal_post_tool_call(
                agent, function_name=function_name, function_args=call_args,
                result=result, effective_task_id=effective_task_id, tool_call_id=tool_call_id,
                duration_ms=duration_ms, middleware_trace=_tool_middleware_trace,
            )
            return apply_transform_tool_result(
                agent, function_name=function_name, function_args=call_args, result=result,
                effective_task_id=effective_task_id, tool_call_id=tool_call_id, duration_ms=duration_ms,
            )
    else:
        def _execute(next_args: dict) -> Any:
            dispatch_kwargs = dict(
                tool_call_id=tool_call_id, session_id=agent.session_id or "",
                turn_id=getattr(agent, "_current_turn_id", "") or "",
                api_request_id=getattr(agent, "_current_api_request_id", "") or "",
                enabled_tools=list(agent.valid_tool_names) if agent.valid_tool_names else None,
                skip_pre_tool_call_hook=True, skip_tool_request_middleware=True,
                enabled_toolsets=getattr(agent, "enabled_toolsets", None),
                disabled_toolsets=getattr(agent, "disabled_toolsets", None),
                tool_request_middleware_trace=list(_tool_middleware_trace),
            )
            if skip_tool_execution_middleware:
                dispatch_kwargs["skip_tool_execution_middleware"] = True
            import model_tools
            return model_tools.handle_function_call(function_name, next_args, effective_task_id, **dispatch_kwargs)
    if skip_tool_execution_middleware:
        return _execute(function_args)
    from hermes_cli.middleware import run_tool_execution_middleware
    return run_tool_execution_middleware(
        function_name, function_args,
        lambda next_args: _execute(next_args if isinstance(next_args, dict) else function_args),
        original_args=function_args, **hook_ids,
    )


def repair_tool_call(agent, tool_name: str) -> str | None:
    """Repair a mismatched tool name (case, separators, CamelCase, ``_tool`` suffixes twice so
    ``TodoTool_tool`` reduces fully, then fuzzy match) before aborting. Returns the repaired
    name if in valid_tool_names, else None."""
    from difflib import get_close_matches
    if not tool_name:
        return None
    # VolcEngine api/plan leaks XML attribute fragments into tool_use.name (`terminal"
    # parameter="command" ...`); trim at the first quote/angle bracket. Do NOT split on whitespace:
    # "write file" must reach ``_norm`` -> ``write_file``.
    # `terminal" parameter="command" string="true` `execute_code" parameter="code" string="true`
    # `session_search" parameter="session_id" string="true` We trim at the first unambiguous XML/quote
    # character so the rest of the repair pipeline (lowercase / snake_case / fuzzy match) can resolve the
    # cleaned name to a real tool. Crucially we DO NOT split on whitespace: legitimate inputs like "write
    # file" must keep flowing through ``_norm`` -> ``write_file`` (covered by test_space_to_underscore in
    # tests/agent/test_repair_tool_call_name.py). See #33007.
    for _xml_sep in ('"', "'", "<", ">"):
        _idx = tool_name.find(_xml_sep)
        if _idx > 0:
            tool_name = tool_name[:_idx]
    if not tool_name:
        return None
    _norm = lambda s: s.lower().replace("-", "_").replace(" ", "_")
    _camel_snake = lambda s: re.sub(r"(?<!^)(?=[A-Z])", "_", s).lower()

    def _strip_tool_suffix(s: str) -> str | None:
        lc = s.lower()
        return next((s[: -len(sfx)].rstrip("_-") for sfx in ("_tool", "-tool", "tool") if lc.endswith(sfx)), None)
    # Cheap fast-paths first.
    lowered = tool_name.lower()
    if lowered in agent.valid_tool_names:
        return lowered
    normalized = _norm(tool_name)
    if normalized in agent.valid_tool_names:
        return normalized
    cands: set[str] = {tool_name, lowered, normalized, _camel_snake(tool_name)}
    for _ in range(2):  # strip trailing tool-suffix up to twice (TodoTool_tool needs it)
        extra: set[str] = set()
        for c in cands:
            stripped = _strip_tool_suffix(c)
            if stripped:
                extra.update((stripped, _norm(stripped), _camel_snake(stripped)))
        cands |= extra
    for c in cands:
        if c and c in agent.valid_tool_names:
            return c
    matches = get_close_matches(lowered, agent.valid_tool_names, n=1, cutoff=0.7)
    return matches[0] if matches else None


# Escalate repeated heals once per session window, then stay quiet. Default threshold; tunable via
# ``agent.sanitizer_heal_escalation_threshold`` (<= 0 disables).
# Repeated heals of the same poisoned transcript used to WARNING on every send (#96870).
# ``_EMPTY_HEAL_ESCALATE_AFTER`` is the built-in default; deployments tune it via
# ``agent.sanitizer_heal_escalation_threshold`` in config.yaml (<= 0 disables escalation entirely — WARNINGs
# still fire per window).
_EMPTY_HEAL_ESCALATE_AFTER = 3


_EMPTY_HEAL_WINDOW_S = 600.0


_empty_heal_log_state: dict[str, dict[str, Any]] = {}


_empty_heal_log_lock = threading.Lock()


# Sessions already told ONCE (out-of-band, never in conversation context); kept apart from the
# windowed log state so a new window never re-arms the notice.
# Session keys that already received the one-time user notice. Separate from the windowed log state so a new
# 10-minute window never re-notifies: the user is told ONCE per session, ever (#96870 — out-of-band,
# delivery channel only, never injected into conversation context).
_empty_heal_user_notified: set = set()


# One-shot pending notices keyed by session, drained via ``consume_pending_sanitizer_heal_notice``
# and delivered via the status/warning callback.
_empty_heal_pending_notice: dict[str, str] = {}


def _content_has_payload(content: Any) -> bool:
    if isinstance(content, str):
        return bool(content.strip())
    if not isinstance(content, list):
        return content not in (None, "")
    # Any typed block counts, as long as a text block is not itself blank.
    return any(
        (block.get("type") != "text" or (isinstance(block.get("text"), str) and block["text"].strip()))
        if isinstance(block, dict) else bool(block)
        for block in content
    )


def _msg_has_payload(msg: dict[str, Any]) -> bool:
    """True if ``msg`` carries anything the API treats as non-empty content (text, multimodal
    blocks, tool_calls, reasoning). Role-agnostic counterpart of ``AIAgent._is_thinking_only_assistant``.
    Codex Responses item carriers persist with content:"" by design (text lives in codex_*_items
    and is replayed); treating them as payload keeps the repair from rewriting a designed-empty turn."""
    return _content_has_payload(msg.get("content")) or bool(
        msg.get("tool_calls")
        or (isinstance(msg.get("reasoning_content"), str) and msg["reasoning_content"].strip())
        or msg.get("reasoning")
        or msg.get("reasoning_details")
        or msg.get("codex_message_items")
        or msg.get("codex_reasoning_items")
    )


def fill_empty_non_final_wire_payload(msg: dict[str, Any], *, is_final: bool) -> bool:
    """Write the interrupted placeholder onto an empty non-final wire copy; True when filled.
    Pass the per-call copy only; durable history must not be mutated."""
    if is_final or not isinstance(msg, dict) or msg.get("role") not in ("user", "assistant"):
        return False
    if _msg_has_payload(msg):
        return False
    msg["content"] = _INTERRUPTED_PLACEHOLDER
    return True


def _session_id_for_heal_log() -> str:
    try:
        from hermes_logging import _session_context
        return str(getattr(_session_context, "session_id", None) or "")
    except Exception:
        return ""


def _heal_escalation_threshold() -> int:
    """Escalation threshold from ``agent.sanitizer_heal_escalation_threshold``, else the module default (fail-safe on any read error)."""
    with contextlib.suppress(Exception):
        from hermes_cli.config import load_config_readonly
        raw = (load_config_readonly().get("agent", {}) or {}).get("sanitizer_heal_escalation_threshold")
        if raw is not None:
            return int(raw)
    return _EMPTY_HEAL_ESCALATE_AFTER


def consume_pending_sanitizer_heal_notice() -> Optional[str]:
    """Drain the one-time user notice for the current session (at most one per session lifetime).
    Delivered through the status/warning callback, NEVER appended to the conversation context."""
    key = _session_id_for_heal_log() or "-"
    with _empty_heal_log_lock:
        return _empty_heal_pending_notice.pop(key, None)


def get_sanitizer_heal_stats() -> dict[str, dict[str, Any]]:
    """Read-only per-session sanitiser heal counters (``heal_events``, ``messages_healed``, ``escalated``) for diagnostics."""
    with _empty_heal_log_lock:
        return {
            k: {
                "heal_events": v.get("total_events", v.get("count", 0)),
                "messages_healed": v.get("total_healed", 0),
                "escalated": k in _empty_heal_user_notified,
            }
            for k, v in _empty_heal_log_state.items()
        }


def _log_empty_non_final_heal(healed: int) -> None:
    """WARNING on the first heals in a window, one ERROR at the threshold, then silent. The
    threshold also queues a ONE-TIME out-of-band user notice (drained by
    ``consume_pending_sanitizer_heal_notice``); never re-armed by a new window."""
    # Late-bound compat seam: tests patch agent_runtime_helpers._heal_escalation_threshold
    # (test_sanitiser_escalation); resolve through the shim so the patch sticks.
    from agent import agent_runtime_helpers as _compat
    _escalation_threshold = getattr(_compat, "_heal_escalation_threshold", _heal_escalation_threshold)
    key = _session_id_for_heal_log() or "-"
    threshold = _escalation_threshold()
    now = time.monotonic()
    with _empty_heal_log_lock:
        state = _empty_heal_log_state.get(key)
        if state is None or (now - state["window_start"]) > _EMPTY_HEAL_WINDOW_S:
            prior = state or {}
            state = _empty_heal_log_state[key] = {
                "count": 0, "window_start": now, "escalated": False,
                "total_events": prior.get("total_events", 0), "total_healed": prior.get("total_healed", 0),
            }
        state["count"] += 1
        state["total_events"] = state.get("total_events", 0) + 1
        state["total_healed"] = state.get("total_healed", 0) + healed
        count, total_events, total_healed = state["count"], state["total_events"], state["total_healed"]
        if state["escalated"]:
            return
        escalate = threshold > 0 and count >= threshold
        if escalate:
            state["escalated"] = True
            if key not in _empty_heal_user_notified:
                _empty_heal_user_notified.add(key)
                _empty_heal_pending_notice[key] = (
                    "⚠️ Your session transcript required repeated repair "
                    f"({total_events} heal passes so far). Replies keep "
                    "working, but a corrupted turn is stuck in this "
                    "session's history — run /debug share or `hermes "
                    "doctor` to capture diagnostics, or /new to start a clean session."
                )
    if escalate:
        _ra().logger.error(
            "Pre-call sanitizer: repeated-heal escalation for session %s — "
            "healed %d empty non-final message(s) this send; heal pattern: "
            "%d heal events / %d messages healed this session "
            "(%d in the current session window, threshold %d). The transcript "
            "is being repaired on every send; /new drops the poisoned turns.", key, healed,
            total_events, total_healed, count, threshold,
        )
        return
    _ra().logger.warning(
        "Pre-call sanitizer: healed %d empty non-final message(s) by "
        "substituting placeholder content — an empty-content turn was in "
        "the transcript and would 400 the request ('messages must have "
        "non-empty content' / INVALID_REQUEST_BODY). Self-recovering the "
        "poisoned transcript in memory; no restart needed.", healed,
    )


def repair_empty_non_final_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Substitute a placeholder for empty-content non-final messages on the per-call copy.
    Anthropic/litellm/Bedrock 400 on any empty non-final message and a persisted stub poisons
    every later turn; repairing the wire copy heals the session in memory. Substitution (not
    deletion) keeps role alternation and tool-call pairing intact. The final message is untouched."""
    if not messages or len(messages) < 2:
        return messages
    repaired: list[dict[str, Any]] = []
    healed = 0
    last_idx = len(messages) - 1
    for idx, msg in enumerate(messages):
        # Tool results are checked by their own pairing pass; empty ones are a separate concern.
        if idx != last_idx and isinstance(msg, dict) and msg.get("role") in ("assistant", "user") and not _msg_has_payload(msg):
            # Shallow-copy so stored history / prompt caching stays byte-stable.
            repaired.append({**msg, "content": _INTERRUPTED_PLACEHOLDER})
            healed += 1
        else:
            repaired.append(msg)
    if healed:
        _log_empty_non_final_heal(healed)
        return repaired
    return messages


def sanitize_api_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Fix orphaned tool_call / tool_result pairs before every LLM call; runs unconditionally (not
    gated on the compressor). Order matters: empty non-final messages are healed first so the
    substituted turn participates in the pairing and dedup passes."""
    messages = _drop_invalid_roles(messages)
    messages = repair_empty_non_final_messages(messages)
    messages = _drop_empty_tool_calls_arrays(messages)
    _repair_invalid_tool_call_names(messages)
    messages = _drop_results_without_ids(messages)
    messages = _pair_tool_calls_positionally(messages)
    messages = _dedupe_tool_call_ids(messages)
    return _realign_tool_result_names(messages)


_ACK_FUTURE_RE = re.compile(r"\b(i['’]ll|i will|let me|i can do that|i can help with that)\b")


_ACK_ACTION_MARKERS = (
    "look into", "look at", "inspect", "scan", "check", "analyz", "review", "explore", "read", "open",
    "run", "test", "fix", "debug", "search", "find", "walkthrough", "report back", "summarize",
)


_ACK_WORKSPACE_MARKERS = (
    "directory", "current directory", "current dir", "cwd", "repo", "repository", "codebase",
    "project", "folder", "filesystem", "file tree", "files", "path",
)


# Intermediate-ack detection patterns (compiled once at import, not per-call).
_ACK_ACTION_CLAUSE_RE = re.compile(
    r"(?:^|[.!…—–]\s+|\n+\s*|,\s+then\s+)"
    r"(?:(?P<transition>then)\s+)?"
    r"(?P<action>(?:re)?launching|(?:re)?starting|creating|"
    r"checking|running|writing|opening|reading|inspecting|reviewing|"
    r"testing|debugging|searching|fixing)\b"
)


_ACK_LIST_ITEM_PREFIX_RE = re.compile(r"(?:^|\n)\s*(?:[-*+]|\d+[.)])\s*$")


_ACK_NUMBERED_ITEM_RE = re.compile(r"(?:^|\s)(\d+)[.)](?=\s)")


_ACK_STATUS_NUMBER_RE = re.compile(
    r"\b(?P<label>exit\s+code|attempt|step|retry)\s+"
    r"(?P<number>\d+)[.)](?=\s)"
)


_ACK_QUESTION_RE = re.compile(r"\?(?=\s|$|[\"'’”)\\]])")


_ACK_SENTENCE_BOUNDARY_RE = re.compile(
    r"(?:[.!…](?=\s|$)|\?(?=\s|$|[\"'’”)\\]])|\n+)"
)


_ACK_ALLOWED_TRAILING_ACTION_RE = re.compile(
    r"\s*(?:will\s+report\s+back|"
    r"then\s+(?:launching|relaunching|starting|restarting|creating)\s+"
    r"(?:it|(?:the\s+)?(?:session|worker|agent|job|process))|"
    r"(?:checking|reading|opening|inspecting)\s+(?:the\s+)?(?:log|output))"
    r"\s*[.!…]?\s*$"
)


_ACK_LAUNCH_NOW_TAIL_RE = re.compile(
    r"\s+(?:(?:it|(?:the\s+)?(?:session|worker|agent|job|process)|"
    r"(?:the\s+)?migration\s+file\s+in\s+the\s+repo)\s+)?"
    r"now(?:\s*\([^)]*\))?\s*$"
)


_ACK_RUN_NOW_TAIL_RE = re.compile(
    r"\s+(?:(?:it|(?:the\s+)?(?:(?:repo\s+)?suite|tests?|job|process|"
    r"server|service))\s+)?now(?:\s*\([^)]*\))?\s*$"
)


_ACK_CHECK_NOW_TAIL_RE = re.compile(
    r"\s+(?:the\s+)?(?:log|output|status|service|session|job)\s+"
    r"now(?:\s*\([^)]*\))?\s*$"
)


_ACK_LAUNCH_URL_TAIL_RE = re.compile(
    r"\s+now\s*[—–-]\s*see\s+https?://\S+"
    r"(?:\s+for\s+progress)?\s*$"
)


_ACK_PROVIDER_TAIL_RE = re.compile(
    r"\s+(?:it|(?:the\s+)?(?:session|worker|agent|job|process))\s+"
    r"on\s+copilot(?:\s+via\s+acpx)?\s*$"
)


_ACK_ACTUAL_LOG_TAIL_RE = re.compile(
    r"\s+(?:the\s+)?actual\s+(?:log|output)\s*$"
)


_ACK_HEALTH_CHECK_TAIL_RE = re.compile(
    r"\s+whether\b.*\b(?:healthy|ready|running|available|reachable|working)\s*$"
)


_ACK_LAUNCH_WITH_RE = re.compile(
    r"\s+with\s+(?:(?:corrected|updated|new)\s+"
    r"(?:arguments?|args?|options?|flags?|parameters?)|"
    r"globals?\s+before\s+(?:the\s+)?agent\s+name)\s*$"
)


def looks_like_codex_intermediate_ack(
    agent, user_message: Any, assistant_content: str, messages: list[dict[str, Any]],
    require_workspace: bool = True,
) -> bool:
    """Detect a planning/ack message that should continue instead of ending the turn.
    ``require_workspace=False`` (opt-in for all api_modes) drops the filesystem/repo reference
    requirement; short-content + no-prior-tools guardrails always apply. A response must then
    contain either a first-person future acknowledgement with an action marker or a narrowly
    bounded pronounless action clause with an explicit transition cue; this keeps ordinary
    conversational replies such as "I'll help you brainstorm" from tripping it."""
    if any(isinstance(msg, dict) and msg.get("role") == "tool" for msg in messages):
        return False
    assistant_text = agent._strip_think_blocks(assistant_content or "").strip().lower()
    if not assistant_text or len(assistant_text) > 1200:
        return False
    has_pronounless_action = False
    step_numbers = {
        status_match.group("number")
        for status_match in _ACK_STATUS_NUMBER_RE.finditer(assistant_text)
        if status_match.group("label") == "step"
    }
    has_step_sequence = len(step_numbers) >= 2
    numbering_text = _ACK_STATUS_NUMBER_RE.sub("", assistant_text)
    numbered_markers = {
        match.group(1) for match in _ACK_NUMBERED_ITEM_RE.finditer(numbering_text)
    }
    has_numbered_list = has_step_sequence or bool(numbered_markers)
    if not _ACK_QUESTION_RE.search(assistant_text):
        for action_match in _ACK_ACTION_CLAUSE_RE.finditer(assistant_text):
            action_prefix = assistant_text[: action_match.start("action")]
            if has_numbered_list or _ACK_LIST_ITEM_PREFIX_RE.search(action_prefix):
                continue
            raw_clause_tail = assistant_text[action_match.end() :]
            boundary_match = _ACK_SENTENCE_BOUNDARY_RE.search(raw_clause_tail)
            if boundary_match:
                clause_tail = raw_clause_tail[: boundary_match.start()]
                remaining_text = raw_clause_tail[boundary_match.end() :]
            else:
                clause_tail = raw_clause_tail
                remaining_text = ""
            if remaining_text.strip() and not _ACK_ALLOWED_TRAILING_ACTION_RE.fullmatch(
                remaining_text
            ):
                continue
            action_word = action_match.group("action")
            is_launch_action = action_word in {
                "launching",
                "relaunching",
                "starting",
                "restarting",
                "creating",
            }
            has_transition_cue = bool(
                (is_launch_action and _ACK_LAUNCH_NOW_TAIL_RE.fullmatch(clause_tail))
                or (action_word == "running" and _ACK_RUN_NOW_TAIL_RE.fullmatch(clause_tail))
                or (
                    action_word == "checking"
                    and _ACK_CHECK_NOW_TAIL_RE.fullmatch(clause_tail)
                )
                or (is_launch_action and _ACK_LAUNCH_URL_TAIL_RE.fullmatch(clause_tail))
                or (is_launch_action and _ACK_PROVIDER_TAIL_RE.fullmatch(clause_tail))
                or (
                    action_word in {"checking", "reading", "opening", "inspecting"}
                    and _ACK_ACTUAL_LOG_TAIL_RE.fullmatch(clause_tail)
                )
                or (
                    action_word == "checking"
                    and _ACK_HEALTH_CHECK_TAIL_RE.fullmatch(clause_tail)
                )
                or (is_launch_action and _ACK_LAUNCH_WITH_RE.fullmatch(clause_tail))
            )
            if not has_transition_cue:
                continue
            has_pronounless_action = True
            break
    if not (_ACK_FUTURE_RE.search(assistant_text) or has_pronounless_action):
        return False
    if not has_pronounless_action and not any(
        marker in assistant_text for marker in _ACK_ACTION_MARKERS
    ):
        return False
    # Opted-in (all-api_mode) path: future-ack + action verb + no prior tool call suffices.
    if not require_workspace:
        return True
    # ``user_message`` may be a multi-part content list (vision via the OpenAI-compat server); a
    # list survives ``or ""`` and ``.strip()`` raises, so flatten first.
    from agent.codex_responses_adapter import _summarize_user_message_for_log
    user_text = _summarize_user_message_for_log(user_message).strip().lower()
    return (
        any(marker in user_text for marker in _ACK_WORKSPACE_MARKERS)
        or "~/" in user_text
        or "/" in user_text
        or any(marker in assistant_text for marker in _ACK_WORKSPACE_MARKERS)
    )


# Degenerate-final detector (#103483): after real tool work a text stop whose ENTIRE answer is a
# fragment — a stray wrong-script word ("пар" in an English conversation), a token starting
# mid-punctuation ("?warming up") — is a provider-side collapse, not an answer, yet the loop
# accepted it and the turn reported completed. Shape alone cannot PROVE a collapse, so this is
# deliberately narrower than "short": a terse legitimate answer ("42", "SQLite", "report.csv",
# "€12.50", "你好。", "Done.", ":8080", "да" to a Russian prompt) never matches, English-script
# fragments ("the", "ing") are knowingly not covered, and the re-prompt it triggers asks for the
# same answer again if it was complete. ``turn_finalizer._SENTENCE_END`` encodes a sibling
# "≤ 24 chars, no terminal" heuristic for the finish explainer.
_DEGENERATE_FINAL_MAX_CHARS = 24


_SENTENCE_TERMINALS = (".", "!", "?", "\u3002", "\uff01", "\uff1f")


# Punctuation no answer begins with when a letter follows ("?warming"); "$5", "#123", "-1",
# "/tmp", ".env", "(a)", ":8080", ":)", ";;" all stay answers.
_DEGENERATE_LEADING_PUNCT = "?!,;:)]}"


def looks_like_degenerate_final(text: str, user_message: Any = None) -> bool:
    """Whether a text stop reads as a collapsed fragment rather than a (terse) answer.

    "Wrong script" is judged against the conversation: when the user's own message carries
    non-ASCII letters, a terse non-Latin reply ("是", "Готово") is an answer, not a collapse.
    """
    t = (text or "").strip()
    if not t or len(t) > _DEGENERATE_FINAL_MAX_CHARS or t.endswith(_SENTENCE_TERMINALS):
        return False
    if t[0] in _DEGENERATE_LEADING_PUNCT and len(t) > 1 and t[1].isalpha():
        return True
    if not any(ch.isalpha() for ch in t) or any(ch.isascii() and ch.isalnum() for ch in t):
        return False
    from agent.codex_responses_adapter import _summarize_user_message_for_log
    user_text = _summarize_user_message_for_log(user_message) if user_message else ""
    return not any(ch.isalpha() and not ch.isascii() for ch in user_text)


def tool_results_this_turn(messages: list[dict[str, Any]]) -> int:
    """Tool-result rows after the most recent user row — whether the turn did real tool work.

    ANY user row ends the window, the continuation nudges included: that is what bounds the
    degenerate-final guard to one re-prompt per collapse. Skipping synthetic user rows here
    would turn it into a two-nudge loop.
    """
    count = 0
    for msg in reversed(messages or ()):
        if not isinstance(msg, dict):
            continue
        if msg.get("role") == "user":
            break
        if msg.get("role") == "tool":
            count += 1
    return count


# Narrow "trailing continue-intent" detector for the stall guard (agent.stall_guards): only the
# message TAIL announcing a next action, so mid-sentence "I will" never trips it.
_TRAILING_CONTINUE_INTENT_RE = re.compile(
    r"(?:\blet me now\b|\bi(?:['\u2019])?ll now\b|\bi will now\b"
    r"|\bnow i(?:['\u2019]ll| will)\b|\bnext[,:] i\b)"
    r"[^.!?\n]{0,100}[.:\u2026]?\s*$", re.IGNORECASE,
)


# Content longer than this is a substantive reply, not a dangling ack.
_TRAILING_CONTINUE_INTENT_MAX_CHARS = 400


def trailing_continue_intent(text: str) -> bool:
    """Whether ``text`` is a short reply ENDING on an announced next action (stall-guard re-prompt trigger)."""
    t = (text or "").strip()
    if not t or len(t) > _TRAILING_CONTINUE_INTENT_MAX_CHARS:
        return False
    return bool(_TRAILING_CONTINUE_INTENT_RE.search(t[-160:]))


# Broader tail detector for PROMOTED REASONING only (reasoning-only clean stop with tools offered
# and no tool call). Visible content keeps the narrow ``let me now`` shape above because a real
# reply legitimately says "I'll" mid-text; chain-of-thought that ENDS on a first-person plan
# ("Let me batch the terminal calls and run them in parallel.", "I need to check the log.") is a
# stalled model whose turn would otherwise report "complete" with zero tool calls (#111761).
# Tail-only and anchored on the last sentence, so reasoning that merely mentions a plan before
# stating its answer ("...Let me check. The answer is 42.") still promotes.
# Thai (unsegmented script, so no \b after the trigger, unlike the English group) shares the same
# tail shape: a first-person future-action marker immediately followed by more Thai text, often
# preceded by an em/en dash rather than sentence punctuation (#116495). Trigger glosses, in
# pattern order: "I will give you" / "I will", "next I('ll)" + one of {start,try,check,fix,send,
# do,look}, "please let me" + one of {start,try,check,fix,send,do,look}, "I('ll)" + one of
# {start,try,check,fix,send,do,look,run,fire}.
_PROMOTED_REASONING_PLAN_TAIL_RE = re.compile(
    r"(?:^|[.!?:\u3002\uff01\uff1f\u2014\u2013\n]\s*|\u2026\s*)"
    r"(?:let(?:['\u2019]s| me)\b|i(?:['\u2019]ll| will| need to| should| am going to|['\u2019]m going to)\b"
    r"|next[,:]? i\b|now i(?:['\u2019]ll| will| need to)\b|first[,:]? i(?:['\u2019]ll| will| need to)\b"
    r"|\u0e08\u0e30\u0e43\u0e2b\u0e49\u0e1c\u0e21|\u0e1c\u0e21\u0e08\u0e30"
    r"|\u0e15\u0e48\u0e2d\u0e44\u0e1b(?:\u0e08\u0e30|\u0e1c\u0e21\u0e08\u0e30)"
    r"|\u0e02\u0e2d(?:\u0e40\u0e23\u0e34\u0e48\u0e21|\u0e25\u0e2d\u0e07|\u0e15\u0e23\u0e27\u0e08|\u0e41\u0e01\u0e49|\u0e2a\u0e48\u0e07|\u0e17\u0e33|\u0e14\u0e39)"
    r"|\u0e08\u0e30(?:\u0e40\u0e23\u0e34\u0e48\u0e21|\u0e25\u0e2d\u0e07|\u0e15\u0e23\u0e27\u0e08|\u0e41\u0e01\u0e49|\u0e2a\u0e48\u0e07|\u0e17\u0e33|\u0e14\u0e39|\u0e23\u0e31\u0e19|\u0e22\u0e34\u0e07))"
    r"[^.!?\n\u3002\uff01\uff1f]{0,160}(?:[.:\u2026]+)?\s*$",
    re.IGNORECASE,
)


def promoted_reasoning_announces_action(text: str) -> bool:
    """Whether promoted reasoning ENDS on a first-person plan to act (stall, not an answer).

    No overall length cap: the reasoning block of a stalled model is often 300-1600 chars of
    planning monologue; only the tail decides.
    """
    t = (text or "").strip()
    if not t:
        return False
    return bool(_PROMOTED_REASONING_PLAN_TAIL_RE.search(t[-240:]))


_INTENT_ACK_ON = {"true", "always", "yes", "on"}


_INTENT_ACK_OFF = {"false", "never", "no", "off"}


def intent_ack_continuation_mode(agent) -> str:
    """Intent-ack continuation mode: ``"off"``, ``"codex_only"`` (workspace acks on codex_responses)
    or ``"all"``. Mirrors ``agent.tool_use_enforcement``: ``"auto"`` -> codex_only; true-ish -> all;
    false-ish -> off; ``list`` -> all when a substring matches the active model name, else off."""
    mode = getattr(agent, "_intent_ack_continuation", "auto")
    if mode is True or (isinstance(mode, str) and mode.lower() in _INTENT_ACK_ON):
        return "all"
    if mode is False or (isinstance(mode, str) and mode.lower() in _INTENT_ACK_OFF):
        return "off"
    if isinstance(mode, list):
        model_lower = (agent.model or "").lower()
        return "all" if any(p.lower() in model_lower for p in mode if isinstance(p, str)) else "off"
    # "auto" or any unrecognised value: historical codex-only behavior.
    return "codex_only" if agent.api_mode == "codex_responses" else "off"


def copy_reasoning_content_for_api(agent, source_msg: dict, api_msg: dict) -> None:
    """Forward reasoning fields onto an API replay message; policy lives in ``agent.message_sanitization.apply_reasoning_content_policy``."""
    from agent.message_sanitization import apply_reasoning_content_policy
    apply_reasoning_content_policy(source_msg, api_msg, agent._needs_thinking_reasoning_pad())


def reapply_reasoning_echo_for_provider(agent, api_messages: list) -> int:
    """Re-pad or strip assistant turns' reasoning_content for the CURRENT provider after a
    fallback switch: ``api_messages`` is shaped for the primary; require-side providers
    (DeepSeek/Kimi/MiMo) 400 without the pad, strict ones (Mistral, Cerebras, Groq) 400/422
    with it. Idempotent; returns the number of assistant turns changed.

    * Switching TO a strict provider that rejects the field (Mistral, Cerebras, Groq, SambaNova, …):
    assistant turns built under a reasoning primary carry a ``reasoning_content`` pad (often a single space
    ``" "``), and the strict provider rejects it with HTTP 400/422 ("Extra inputs are not permitted"). This
    is the exact cross-provider fallback bug from #45655 — a DeepSeek primary pads history with ``" "``, the
    request falls back to Mistral, and Mistral 422s on the stale pad.
    """
    from agent.message_sanitization import reapply_reasoning_echo
    return reapply_reasoning_echo(api_messages, agent._needs_thinking_reasoning_pad())


def _requeue_pending_steer(agent, steer_text: str) -> None:
    """Put drained steer text back so the caller's fallback delivers it as a next-turn user message."""
    # Under the lock the slot is read directly: an initialized agent always has both attributes, so a
    # missing ``_pending_steer`` there is a real bug and must fail loud. The lock-less branch only
    # exists for test stubs built via ``object.__new__`` that skipped ``__init__``.
    _lock = getattr(agent, "_pending_steer_lock", None)
    if _lock is not None:
        with _lock:
            if agent._pending_steer:
                agent._pending_steer = agent._pending_steer + "\n" + steer_text
            else:
                agent._pending_steer = steer_text
    else:
        existing = getattr(agent, "_pending_steer", None)
        agent._pending_steer = (existing + "\n" + steer_text) if existing else steer_text


def apply_pending_steer_to_tool_results(agent, messages: list, num_tool_msgs: int) -> None:
    """Persist any pending /steer text as a standalone user message.

    Called at the end of a tool-call batch, before the next API call.

    The steer is emitted as a NEW ``role:"user"`` message appended after the
    last tool result (marker text included), so:

    - the model still sees the self-describing out-of-band marker (same text,
      same provenance semantics);
    - message-role alternation stays legal — ``assistant(tool_calls) → tool →
      user`` is the documented "user jumped in mid-run" pattern that
      ``repair_message_sequence`` deliberately keeps;
    - the appended dict carries no ``_DB_PERSISTED_MARKER`` yet, so the next
      ``_flush_messages_to_session_db`` writes it to the session store — the
      steer text finally becomes part of the durable transcript instead of
      being smeared onto an already-persisted tool row that append-only
      persistence never rewrites (replayed histories then diverge from the
      live request bytes and break the provider prompt cache).
    """
    if num_tool_msgs <= 0 or not messages:
        return
    steer_text = agent._drain_pending_steer()
    if not steer_text:
        return
    # Skip non-tool messages in the tail in case something else is appended at the boundary.
    tail = range(len(messages) - 1, max(len(messages) - num_tool_msgs - 1, -1), -1)
    target = next((messages[j] for j in tail if isinstance(messages[j], dict) and messages[j].get("role") == "tool"), None)
    if target is None:
        # No tool result in this batch (e.g. all skipped by interrupt);
        # requeue so the fallback path delivers it as a normal next-turn
        # user message (which persists like any other user turn).
        _requeue_pending_steer(agent, steer_text)
        return
    messages.append(steer_user_row(steer_text))
    _ra().logger.info(
        "Delivered /steer to agent after tool batch (%d chars) as new user message: %s", len(steer_text),
        steer_text[:120] + ("..." if len(steer_text) > 120 else ""),
    )

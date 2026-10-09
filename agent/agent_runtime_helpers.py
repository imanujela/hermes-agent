"""Assorted AIAgent runtime helpers (message repair/sanitization, credential recovery, primary
runtime restore, prompt-cache policy, client construction, model switching, tool invocation).

COMPAT SHIM: the implementation now lives in the ``agent.runtime`` package, split by
cohesion (message_repair, credential_recovery, prompt_cache_policy, transport_clients,
model_switch; ``_ra`` resolves ``run_agent`` lazily via ``agent.runtime._runtime_ref``).
``from agent.agent_runtime_helpers import X`` keeps working for every name, public or
private — this seam is unchanged by DESIGN-IT-TWICE; new code should import the
submodules' cohesive surface instead of relying on this module's grab-bag.
"""

from __future__ import annotations
import contextlib  # noqa: F401  (attr surface parity with the pre-split module)
import copy  # noqa: F401
import json  # noqa: F401
import logging
import re  # noqa: F401
import threading  # noqa: F401
import time  # noqa: F401  (tests patch agent_runtime_helpers.time.sleep through this attr)
from datetime import datetime  # noqa: F401
from pathlib import Path  # noqa: F401
from typing import Any, Dict, List, Optional, Tuple  # noqa: F401

logger = logging.getLogger(__name__)

from agent.runtime import *  # noqa: F401,F403

__all__ = [
    "_iter_pool_sockets",
    "anthropic_prompt_cache_policy",
    "apply_pending_steer_to_tool_results",
    "blank_cache_policy_stub",
    "convert_to_trajectory_format",
    "copy_reasoning_content_for_api",
    "create_openai_client",
    "drop_thinking_only_and_merge_users",
    "dump_api_request_debug",
    "extract_api_error_context",
    "extract_reasoning",
    "force_close_tcp_sockets",
    "invoke_tool",
    "looks_like_codex_intermediate_ack",
    "plan_cache_sections_for_destination",
    "prompt_caching_disabled_from_config",
    "recover_with_credential_pool",
    "repair_message_sequence",
    "repair_tool_call",
    "restore_primary_runtime",
    "sanitize_api_messages",
    "sanitize_tool_call_arguments",
    "strip_think_blocks",
    "switch_model",
    "try_recover_primary_transport",
]

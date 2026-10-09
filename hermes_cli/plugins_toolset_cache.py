"""Cache-file location for persisted plugin toolset keys (split out of hermes_cli.plugins)."""

from __future__ import annotations

from pathlib import Path

from hermes_constants import get_hermes_home


def _plugin_toolset_keys_cache_path() -> Path:
    return get_hermes_home() / "cache" / "plugin_toolset_keys.json"

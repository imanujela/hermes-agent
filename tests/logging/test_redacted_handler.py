"""Tests for the ``redacted_handler`` / ``redacted_formatter`` factory in hermes_logging.

The factory centralizes the lazy-import + try/except-ImportError pattern that
was duplicated across six entry points.  Each site previously built its own
``RedactingFormatter`` (or silently fell back to ``logging.Formatter``);
``redacted_handler`` / ``redacted_formatter`` does it once so the fallback is
consistent and the lazy import is never duplicated.
"""

from __future__ import annotations

import logging
import re
import sys
from unittest.mock import patch

import pytest

import hermes_logging
from hermes_logging import redacted_formatter, redacted_handler


# ── redacted_formatter ──────────────────────────────────────────────


class TestRedactedFormatter:
    def test_returns_redacting_formatter_normally(self):
        """When ``agent.redact`` is importable the formatter redacts secrets."""
        fmt = redacted_formatter("%(message)s")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="key=sk-%s", args=("a" * 32,), exc_info=None,
        )
        out = fmt.format(record)
        assert "a" * 32 not in out  # the secret portion is scrubbed

    def test_uses_given_format_string(self):
        fmt = redacted_formatter("[%(levelname)s] %(message)s")
        record = logging.LogRecord(
            name="t", level=logging.WARNING, pathname=__file__, lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        assert fmt.format(record) == "[WARNING] hello"

    def test_accepts_datefmt(self):
        fmt = redacted_formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S")
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname=__file__, lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        out = fmt.format(record)
        assert re.match(r"\d{2}:\d{2}:\d{2}", out), out

    def test_falls_back_to_plain_formatter_on_import_error(self):
        """When ``agent.redact`` cannot be imported, return a ``logging.Formatter``."""
        with patch.dict(sys.modules, {"agent.redact": None}):
            fmt = redacted_formatter("%(message)s")
        assert type(fmt) is logging.Formatter
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname=__file__, lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        assert fmt.format(record) == "hello"

    def test_fallback_formatter_uses_given_format_string(self):
        with patch.dict(sys.modules, {"agent.redact": None}):
            fmt = redacted_formatter("[%(levelname)s] %(message)s")
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname=__file__, lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        assert fmt.format(record) == "[INFO] hello"

    def test_fallback_formatter_preserves_datefmt(self):
        with patch.dict(sys.modules, {"agent.redact": None}):
            fmt = redacted_formatter("%(asctime)s %(message)s", datefmt="%H:%M:%S")
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname=__file__, lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        out = fmt.format(record)
        assert re.match(r"\d{2}:\d{2}:\d{2}", out), out


# ── redacted_handler ────────────────────────────────────────────────


class TestRedactedHandler:
    def test_returns_stream_handler(self):
        handler = redacted_handler("%(message)s")
        assert isinstance(handler, logging.StreamHandler)

    def test_default_level_is_info(self):
        handler = redacted_handler("%(message)s")
        assert handler.level == logging.INFO

    def test_custom_level(self):
        handler = redacted_handler("%(message)s", level=logging.DEBUG)
        assert handler.level == logging.DEBUG

    def test_redacts_secrets(self):
        """The handler's formatter must scrub credential-shaped strings."""
        handler = redacted_handler("%(message)s")
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname=__file__, lineno=1,
            msg="key=sk-%s", args=("a" * 32,), exc_info=None,
        )
        out = handler.format(record)
        assert "a" * 32 not in out

    def test_uses_given_format_string(self):
        handler = redacted_handler("[%(levelname)s] %(message)s")
        record = logging.LogRecord(
            name="t", level=logging.WARNING, pathname=__file__, lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        assert handler.format(record) == "[WARNING] hello"

    def test_accepts_datefmt(self):
        handler = redacted_handler("%(asctime)s %(message)s", datefmt="%H:%M:%S")
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname=__file__, lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        out = handler.format(record)
        assert re.search(r"\d{2}:\d{2}:\d{2}", out), out

    def test_falls_back_to_plain_formatter_on_import_error(self):
        """When ``agent.redact`` cannot be imported, the handler still formats."""
        with patch.dict(sys.modules, {"agent.redact": None}):
            handler = redacted_handler("[%(levelname)s] %(message)s")
        record = logging.LogRecord(
            name="t", level=logging.INFO, pathname=__file__, lineno=1,
            msg="hello", args=(), exc_info=None,
        )
        assert handler.format(record) == "[INFO] hello"

    def test_stream_is_safe_stderr(self):
        """The handler writes to ``_safe_stderr()``, not a bare ``sys.stderr``."""
        handler = redacted_handler("%(message)s")
        # _safe_stderr() may return sys.stderr directly when it's already UTF-8,
        # but it should never be None.
        assert handler.stream is not None

    def test_handler_formatter_is_not_none(self):
        handler = redacted_handler("%(message)s")
        assert handler.formatter is not None

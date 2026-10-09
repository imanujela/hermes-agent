"""Behaviour tests for the ``hermes_suppress.suppressed`` seam.

Seam under test (pre-agreed, stated here as the contract): the *public
interface* of ``hermes_suppress.suppressed(logger=None, msg="", *,
level=logging.DEBUG)`` — a context manager that

* swallows any ``Exception`` raised inside the block and logs it on
  ``logger`` at ``level`` with a full traceback (``exc_info=True``);
* does NOT swallow ``BaseException`` (``KeyboardInterrupt`` must pass
  through — the semantics boundary between this helper and a bare
  ``except:``);
* never raises itself, even when the logging call fails (teardown safety);
* defaults to the ``hermes_suppress`` module logger when no logger is given.

Behaviour is observed through the public logging API only — a recording
handler attached to the target logger — never module internals. Run with
stdlib + pytest only: ``uv run --no-project --with pytest``.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import hermes_suppress  # noqa: E402  (module logger identity for slice 5)
from hermes_suppress import suppressed  # noqa: E402


class _Recorder(logging.Handler):
    """Minimal capturing handler that never touches the filesystem."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@contextmanager
def _capturing(logger: logging.Logger):
    """Attach a recorder to ``logger`` with full isolation: propagate off (so
    session root handlers can't double-capture), level forced to DEBUG,
    original handlers restored on exit. Same style as the RC3 suppression
    tests' ``hermes_logger_attached`` fixture."""
    rec = _Recorder()
    saved_handlers = logger.handlers[:]
    saved_propagate = logger.propagate
    saved_level = logger.level
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    logger.addHandler(rec)
    try:
        yield rec
    finally:
        logger.removeHandler(rec)
        for leftover in list(logger.handlers):
            if leftover is not rec and leftover not in saved_handlers:
                logger.removeHandler(leftover)
        logger.handlers = saved_handlers
        logger.propagate = saved_propagate
        logger.level = saved_level


def _make_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.handlers = []
    return logger


def _formatted(record: logging.LogRecord) -> str:
    return logging.Formatter().format(record)


# ── Slice 1: Exception swallowed, logged at DEBUG with traceback ───────────
def test_inner_exception_swallowed_and_logged_with_traceback() -> None:
    """Any ``Exception`` raised inside the block disappears without breaking
    the caller, and one DEBUG record carrying the traceback shows up on the
    supplied logger."""
    logger = _make_logger("test.hermes_suppress.slice1")
    with _capturing(logger) as rec:
        with suppressed(logger, "send welcome"):
            raise RuntimeError("boom from best-effort block")
        # If suppressed() re-raised, we would never get here.
        reached_after = True
    assert reached_after, "suppressed() let the inner Exception escape"
    assert len(rec.records) == 1
    record = rec.records[0]
    assert record.levelno == logging.DEBUG
    assert record.msg == "send welcome"
    assert record.exc_info is not None
    assert isinstance(record.exc_info[1], RuntimeError)
    text = _formatted(record)
    assert "Traceback (most recent call last)" in text
    assert "boom from best-effort block" in text


# ── Slice 2: BaseException is NOT swallowed (semantics boundary) ───────────
def test_base_exception_keyboard_interrupt_passes_through() -> None:
    """``KeyboardInterrupt`` must escape the block untouched — this is the
    boundary that separates ``suppressed()`` (``except Exception``) from a
    bare ``except:``. Nothing is logged for it either."""
    logger = _make_logger("test.hermes_suppress.slice2")
    with _capturing(logger) as rec:
        with pytest.raises(KeyboardInterrupt):
            with suppressed(logger, "cleanup"):
                raise KeyboardInterrupt("user hit ctrl-c")
    assert rec.records == [], "BaseException must not be swallowed-and-logged"


# ── Slice 3: helper never raises even when logging itself fails ────────────
class _ExplodingLogger(logging.Logger):
    """Public-interface stand-in: a logger whose ``log`` call always blows up
    (the RC3 scenario — teardown paths running while the logging stack is
    half-open). Records that the seam actually attempted the log call so the
    test cannot pass vacuously."""

    def __init__(self) -> None:
        super().__init__("test.hermes_suppress.slice3.exploding")
        self.log_attempted = False

    def log(self, level, msg, *args, **kwargs):  # noqa: ANN001, ANN201
        self.log_attempted = True
        raise RuntimeError("the logging machinery is broken too")


def test_helper_never_raises_when_logger_log_itself_raises() -> None:
    """If ``logger.log(...)`` raises, ``suppressed()`` must still swallow the
    original exception and exit the ``with`` block cleanly — it is documented
    as safe in teardown paths."""
    logger = _ExplodingLogger()
    with suppressed(logger, "close session"):
        raise ValueError("original teardown failure")
    # Reaching here at all means neither the ValueError nor the logging
    # RuntimeError escaped. Prove the guard covered the real log path:
    assert logger.log_attempted, "suppressed() never even attempted to log"


# ── Slice 4: custom level keyword is respected ─────────────────────────────
def test_custom_level_keyword_is_respected() -> None:
    """``level=logging.WARNING`` must route the record at WARNING, not the
    DEBUG default — callers guard noisy-but-important best-effort blocks with
    it."""
    logger = _make_logger("test.hermes_suppress.slice4")
    with _capturing(logger) as rec:
        with suppressed(logger, "flush lagging", level=logging.WARNING):
            raise TimeoutError("child did not exit")
    assert len(rec.records) == 1
    record = rec.records[0]
    assert record.levelno == logging.WARNING
    assert record.levelname == "WARNING"
    assert isinstance(record.exc_info[1], TimeoutError)


# ── Slice 5: logger-less default lands on the hermes_suppress module logger ─
def test_no_logger_default_uses_hermes_suppress_module_logger() -> None:
    """``suppressed()`` with no logger must log on the module's own logger
    (``logging.getLogger(hermes_suppress.__name__)``) rather than silently
    dropping the record — best-effort callers that forgot to pass one still
    leave a trace."""
    module_logger = logging.getLogger(hermes_suppress.__name__)
    module_logger.handlers = []
    with _capturing(module_logger) as rec:
        with suppressed(msg="welcome send"):
            raise OSError("socket closed")
    assert len(rec.records) == 1
    record = rec.records[0]
    assert record.name == "hermes_suppress"
    assert record.levelno == logging.DEBUG
    assert isinstance(record.exc_info[1], OSError)

"""Guard the logging suppression seam RC3 added to ``hermes_logging``.

RC3 did not add a ``suppressed()`` helper or new redaction to this module —
it added the module-level ``logger`` (see the "best-effort debug logging in
teardown/config paths" comment) and relies on two suppression seams that are
driven through it:

* ``_quietly()`` — teardown callbacks swallow errors, then report via
  ``logger.debug(..., exc_info=True)``;
* ``_warn_windows_lock_timeout_once()`` — reports a *suppressed*
  concurrent-log-handler lock timeout exactly once per process.

The property under test: debug records with ``exc_info`` must survive a
misconfigured logging environment (setup never called, no handlers, or a
handler whose ``emit`` raises), because teardown paths run while the logging
stack itself is half-open. If these raise, every cleanup path in the RC3
sweep dies with them.
"""

from __future__ import annotations

import logging

import pytest

import hermes_logging


class _Recorder(logging.Handler):
    """Minimal capturing handler that never touches the filesystem."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def hermes_logger_attached(monkeypatch):
    """Attach a recorder to ``hermes_logging.logger`` with full isolation:
    propagate off (so root handlers from conftest can't double-capture),
    level forced to DEBUG, handlers removed on teardown."""
    attached: list[logging.Handler] = []

    def _attach(handler: logging.Handler) -> logging.Handler:
        log = hermes_logging.logger
        monkeypatch.setattr(log, "propagate", False)
        monkeypatch.setattr(log, "level", logging.DEBUG)
        log.addHandler(handler)
        attached.append(handler)
        return handler

    yield _attach

    for handler in attached:
        hermes_logging.logger.removeHandler(handler)


def test_rc3_module_level_logger_exists():
    """The seam RC3 added: a module-level logger safe to call before setup."""
    assert isinstance(hermes_logging.logger, logging.Logger)
    assert hermes_logging.logger is logging.getLogger("hermes_logging")


def test_debug_with_exc_info_does_not_raise_when_unconfigured():
    """setup_logging() never ran, no handlers, propagation off: the record
    simply drops and ``logger.debug(exc_info=True)`` must still return."""
    log = logging.getLogger("test.rc3.unconfigured.child")
    log.handlers.clear()
    log.propagate = False
    try:
        1 / 0
    except ZeroDivisionError:
        log.debug("teardown note with traceback", exc_info=True)  # must not raise
    finally:
        log.propagate = True


def test_debug_with_exc_info_survives_failing_emit_on_stdlib_handler(
    hermes_logger_attached,
):
    """Emit failures inside a *stdlib* handler (here: a formatter that raises
    while ``StreamHandler.emit`` formats) route through ``Handler.handleError``
    and must never reach the caller. This is the contract teardown paths rely
    on when the log backend is broken.

    (A handler subclass that overrides ``emit`` and raises propagates by
    design — ``Logger.callHandlers`` has no try/except — which is exactly why
    ``hermes_logging`` suppresses CLH failures at its own call sites rather
    than letting handler emit die raw; see ``_quietly`` and
    ``_warn_windows_lock_timeout_once`` below.)
    """
    import io

    class _ExplodingFormatter(logging.Formatter):
        def format(self, record):  # noqa: ANN001 - misconfigured formatter
            raise RuntimeError("simulated broken log backend")

    handler = logging.StreamHandler(io.StringIO())
    hermes_logger_attached(handler)
    handler.setFormatter(_ExplodingFormatter())
    try:
        raise ValueError("simulated teardown failure")
    except ValueError:
        for raise_exc in (True, False):
            logging.raiseExceptions = raise_exc
            try:
                hermes_logging.logger.debug(
                    "suppressed teardown error", exc_info=True
                )  # must not raise
            finally:
                logging.raiseExceptions = True


def test_safe_stderr_is_writeable_and_tolerates_unicode():
    """The misconfiguration this guard covers: console encodings that crash on
    non-ASCII records. ``_safe_stderr()`` must always hand back a stream whose
    ``write`` survives Unicode."""
    stream = hermes_logging._safe_stderr()
    assert hasattr(stream, "write")
    stream.write("logging diagnostics with unicode \u00e9 \u2603\n")


def test_line_buffer_piped_stdout_never_crashes_on_broken_streams(monkeypatch):
    """setup_logging() reconfigures piped stdout; a closed/detached stream
    raises ValueError/OSError there — buffering is observability, not
    correctness, so it must be swallowed, not propagate into startup."""

    class _BrokenStream:
        encoding = "utf-8"
        line_buffering = False

        def isatty(self) -> bool:
            return False

        def reconfigure(self, **kwargs) -> None:
            raise ValueError("I/O operation on closed file")

    monkeypatch.setattr(hermes_logging.sys, "stdout", _BrokenStream())
    hermes_logging._line_buffer_piped_stdout()  # must not raise

    class _DetachedStream(_BrokenStream):
        def reconfigure(self, **kwargs) -> None:
            raise OSError(9, "bad file descriptor")

    monkeypatch.setattr(hermes_logging.sys, "stdout", _DetachedStream())
    hermes_logging._line_buffer_piped_stdout()  # must not raise


def test_is_unavailable_log_stream_classification():
    """Teardown-time classification of a lost backing stream (OSError errno 5
    / closed-file ValueError), used to suppress repeat warnings."""
    assert hermes_logging._is_unavailable_log_stream(OSError(5, "I/O error"))
    assert hermes_logging._is_unavailable_log_stream(
        ValueError("I/O operation on closed file")
    )
    assert not hermes_logging._is_unavailable_log_stream(OSError(2, "no such file"))
    assert not hermes_logging._is_unavailable_log_stream(ValueError("other"))
    assert not hermes_logging._is_unavailable_log_stream(None)


def test_quietly_swallows_callback_error_and_logs_debug_with_exc_info(
    hermes_logger_attached,
):
    """``_quietly`` is the suppression helper: the callback error never
    escapes, and the report goes out as a DEBUG record carrying exc_info."""
    recorder = hermes_logger_attached(_Recorder())

    def boom():
        raise RuntimeError("close() blew up")

    hermes_logging._quietly(boom)  # must not raise

    errors = [r for r in recorder.records if r.levelno == logging.DEBUG]
    assert len(errors) == 1, "teardown failure must be reported exactly once"
    assert errors[0].exc_info is not None
    assert errors[0].exc_info[0] is RuntimeError
    assert "_quietly" in errors[0].getMessage()


def test_quietly_passes_through_a_healthy_callback(hermes_logger_attached):
    recorder = hermes_logger_attached(_Recorder())
    calls: list[int] = []
    hermes_logging._quietly(lambda: calls.append(1))
    assert calls == [1]
    assert recorder.records == []


def test_warn_windows_lock_timeout_once_is_one_shot(monkeypatch):
    """The suppressed CLH lock timeout is reported exactly once per process —
    a second call must stay silent or errors.log gets spammed as badly as the
    noise the suppression replaced."""
    monkeypatch.setattr(hermes_logging, "_windows_lock_timeout_warned", False)
    log = logging.getLogger("hermes_logging")
    recorder = _Recorder()
    monkeypatch.setattr(log, "propagate", False)
    monkeypatch.setattr(log, "level", logging.DEBUG)
    log.addHandler(recorder)
    try:
        hermes_logging._warn_windows_lock_timeout_once()
        hermes_logging._warn_windows_lock_timeout_once()
        hermes_logging._warn_windows_lock_timeout_once()
    finally:
        log.removeHandler(recorder)

    warnings = [r for r in recorder.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "lock" in warnings[0].getMessage()

"""The shared swallow-and-log seam for best-effort blocks.

Stdlib-only leaf module by design: anything in the tree can import it with zero
cycle risk. ``hermes_logging.suppressed`` re-exports this implementation — do
not fork the logic again; one convention, one home.

Usage:

    from hermes_suppress import suppressed

    with suppressed(logger, "send welcome"):
        notifier.send(welcome_text)

Any ``Exception`` raised inside is logged at DEBUG (configurable) with a full
traceback and suppressed. The context manager itself never raises — even if the
logging call fails — so it is safe in teardown paths. Blocks with real control
flow in the handler (return, cleanup, re-raise) must stay explicit try/except.
"""

import contextlib
import logging
from typing import Optional

_module_logger = logging.getLogger(__name__)


@contextlib.contextmanager
def suppressed(logger: Optional[logging.Logger] = None, msg: str = "", *fmt_args, level: int = logging.DEBUG):
    """Swallow-and-log the best-effort block: any ``Exception`` raised inside is
    recorded on ``logger`` (module logger of ``hermes_suppress`` when omitted) at
    ``level`` with ``exc_info=True`` and suppressed; never raises itself.

    Extra positional ``fmt_args`` are forwarded as %-style log-record arguments,
    mirroring the inline call verbatim:
    ``with suppressed(logger, "PUSH failed: %s", err):`` replaces
    ``logger.debug("PUSH failed: %s", err, exc_info=True)``."""
    try:
        yield
    except Exception:  # health: allow BLE001 -- the seam's contract: swallow-and-log ANY Exception raised inside
        target = logger if logger is not None else _module_logger
        try:
            target.log(level, msg, *fmt_args, exc_info=True)
        except Exception:  # health: allow BLE001,S110 -- the helper itself must never raise: logging
            pass  # failure inside a teardown path would mask the original swallow's cause

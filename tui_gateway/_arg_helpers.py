"""Pure argument/text helpers shared by the tools/slash split (``methods_tools``).

No globals beyond stdlib: every body works on its arguments alone, so these survive
``method_ctx.bind_module`` as-is. ``methods_tools`` imports them *renamed*
(``str_arg as _str_arg``) and re-exports them: ``bind_module`` skips plain same-name
function imports ("server has its own"), so the rename is the load-bearing part — it
publishes the originals onto server.py's namespace, where the rebound bodies resolve
the underscore names at call time.
"""


def str_arg(params: dict, key: str) -> str:
    """``params[key]`` as a stripped string; missing/None → ``""``."""
    return str(params.get(key) or "").strip()


def joined_output(r) -> str:
    """stdout + stderr of a CompletedProcess, non-empty parts only, newline-joined and stripped."""
    return "\n".join(p for p in (r.stdout or "", r.stderr or "") if p).strip()


def is_snapshot_restore(arg: str) -> bool:
    """True when the leading word of a ``/snapshot`` argument is ``restore``/``rewind``."""
    return (arg.split(maxsplit=1)[0].lower() if arg else "") in {"restore", "rewind"}

"""Ratchet: no bare ``timeout=60`` literal on subprocess calls in product code.

The RC3 error-path sweeps wrote ``subprocess.run(..., timeout=60)`` by hand
across ``tools/``, ``hermes_cli/`` and ``gateway/``. A bare literal there is a
second-class citizen by policy: the timeout of an external command is a tuning
knob (slow installs and cold ``git`` calls need a different number than a
probe did) and, worse, copy-paste of ``60`` hides the fact that different
call sites silently disagree on what a hung child process means. Each site
must import a named constant so the value is declared once and greppable.

Like the other tree-wide guards in this suite (see
``test_managed_runtime_resolution.py``), the property under test is "no call
site anywhere spells it this way" — a statement about the whole codebase that
no runtime seam can observe, so AST is the right tool and only used here.

Scope: calls to ``subprocess.run`` / ``subprocess.check_output`` /
``subprocess.check_call`` (including ``from subprocess import ...`` and
``import subprocess as sp`` forms) whose ``timeout`` keyword is the integer
literal ``60`` exactly. ``timeout=600`` or ``timeout=TWO_MINUTES`` are not
flagged by this rule. Files that do not parse are recorded and skipped — a
concurrent sweep caught mid-edit must not become a false violation here.

This is a hard ratchet: it fails while the migration is still landing, and
the count in the failure message is the work queue.
"""

from __future__ import annotations

import ast
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_SCANNED_DIRS = ("tools", "hermes_cli", "gateway")
_SUBPROCESS_FUNCS = frozenset({"run", "check_output", "check_call"})
_EXCLUDED_DIR_NAMES = frozenset(
    {"__pycache__", ".venv", "venv", "node_modules", ".git", ".worktrees"}
)


def _iter_scanned_files():
    for top in _SCANNED_DIRS:
        base = REPO_ROOT / top
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*.py")):
            if _EXCLUDED_DIR_NAMES.isdisjoint(path.parts):
                yield path


class _BareTimeoutVisitor(ast.NodeVisitor):
    """Collects ``(lineno, col)`` of subprocess calls with timeout == 60 exact.

    ``subprocess_names`` are local names bound to the module
    (``import subprocess``, ``import subprocess as sp``); ``func_names`` maps
    local names bound straight to a function (``from subprocess import run``,
    ``from subprocess import check_call as cc``)."""

    def __init__(self, subprocess_names: set[str], func_names: dict[str, str]):
        self.subprocess_names = subprocess_names
        self.func_names = func_names
        self.hits: list[tuple[int, int]] = []

    def _callee(self, node: ast.Call) -> str | None:
        """Return the subprocess function name this call targets, if any."""
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _SUBPROCESS_FUNCS:
            base = func.value
            if isinstance(base, ast.Name) and base.id in self.subprocess_names:
                return func.attr
            return None
        if isinstance(func, ast.Name):
            return self.func_names.get(func.id)
        return None

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 (ast API)
        if self._callee(node) is not None:
            for kw in node.keywords:
                if kw.arg != "timeout" or not isinstance(kw.value, ast.Constant):
                    continue
                value = kw.value.value
                if isinstance(value, int) and not isinstance(value, bool) \
                        and value == 60:
                    self.hits.append((node.lineno, node.col_offset))
        self.generic_visit(node)


def _subprocess_bindings(tree: ast.Module) -> tuple[set[str], dict[str, str]]:
    """Names in this module that refer to the subprocess module / its funcs."""
    module_names: set[str] = set()
    func_names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "subprocess":
                    module_names.add(alias.asname or "subprocess")
        elif isinstance(node, ast.ImportFrom):
            if node.module == "subprocess":
                for alias in node.names:
                    if alias.name in _SUBPROCESS_FUNCS:
                        func_names[alias.asname or alias.name] = alias.name
                    elif alias.name == "*":
                        func_names.update({name: name for name in _SUBPROCESS_FUNCS})
    return module_names, func_names


def _find_bare_timeout60_sites(source: str) -> list[tuple[int, int]]:
    """Violations in *source* (parse errors raise — caller handles)."""
    tree = ast.parse(source)
    module_names, func_names = _subprocess_bindings(tree)
    visitor = _BareTimeoutVisitor(module_names, func_names)
    visitor.visit(tree)
    return visitor.hits


def _scan_tree() -> tuple[list[str], list[str]]:
    """Return (violations, unparseable files) across the scanned dirs."""
    violations: list[str] = []
    unparseable: list[str] = []
    for path in _iter_scanned_files():
        rel = path.relative_to(REPO_ROOT).as_posix()
        try:
            hits = _find_bare_timeout60_sites(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            unparseable.append(rel)
            continue
        violations.extend(f"{rel}:{lineno}:{col}" for lineno, col in hits)
    return violations, unparseable


# ── meta: the detector itself ───────────────────────────────────────────────


def test_detector_flags_each_call_shape():
    cases = [
        "import subprocess\nsubprocess.run(cmd, timeout=60)\n",
        "import subprocess as sp\nsp.check_output(cmd, timeout=60)\n",
        "from subprocess import check_call\ncheck_call(cmd, timeout=60)\n",
        "import subprocess\nsubprocess.run(cmd, **kwargs, timeout=60)\n",
        "import subprocess\nsubprocess.run(\n    cmd,\n    timeout=60,\n)\n",
    ]
    for source in cases:
        assert _find_bare_timeout60_sites(source), f"missed: {source!r}"


def test_detector_ignores_legal_shapes():
    cases = [
        # different literal (600 != 60)
        "import subprocess\nsubprocess.run(cmd, timeout=600)\n",
        # named constant, the required shape
        "import subprocess\nfrom hermes_constants import CLI_SUBPROCESS_TIMEOUT\nsubprocess.run(cmd, timeout=CLI_SUBPROCESS_TIMEOUT)\n",
        # not a subprocess call
        "proc.run(cmd, timeout=60)\n",
        "other = type('X', (), {})\nother.check_call(cmd, timeout=60)\n",
        # numeric lookalike is not the int literal 60
        "import subprocess\nsubprocess.run(cmd, timeout='60')\n",
        "import subprocess\nsubprocess.run(cmd, timeout=60.0)\n",
    ]
    for source in cases:
        assert not _find_bare_timeout60_sites(source), f"false hit: {source!r}"


# ── the ratchet ─────────────────────────────────────────────────────────────


def test_no_bare_timeout_60_literals_in_subprocess_calls():
    """Every subprocess.run/check_output/check_call in tools/, hermes_cli/
    and gateway/ must take timeout from a named constant, not a literal 60."""
    violations, unparseable = _scan_tree()
    if unparseable:
        # A concurrent sweep caught mid-edit is not a violation, but the ratchet
        # was blind to those files — make the gap visible in every run's output.
        warnings.warn(
            f"timeout ratchet skipped {len(unparseable)} unparseable file(s) "
            f"(mid-edit, re-run once they parse): {unparseable}",
            stacklevel=1,
        )
    if violations:
        shown = "\n".join(f"  {v}" for v in violations[:20])
        more = len(violations) - 20
        tail = f"\n  ... and {more} more" if more > 0 else ""
        note = (
            f"\n  (skipped {len(unparseable)} unparseable files: {unparseable})"
            if unparseable
            else ""
        )
        raise AssertionError(
            f"{len(violations)} subprocess call(s) pass a bare timeout=60 "
            f"literal; hoist the value to a named constant and import it:\n"
            f"{shown}{tail}{note}"
        )

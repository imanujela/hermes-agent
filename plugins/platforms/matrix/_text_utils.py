"""Matrix text/markup helpers: LaTeX math sentinel tokens -> Element ``data-mx-maths`` markup.

Moved out of ``adapter.py`` to keep that module within its line budget; re-imported there as
a re-export so ``adapter._latex_to_tokens`` / ``adapter._tokens_to_mx_maths`` stay resolvable.
"""

from __future__ import annotations

import re
from html import escape as _html_escape

# --- LaTeX math ($...$, $$...$$) -> Element data-mx-maths markup ---
# Element (feature_latex_maths) typesets <div|span data-mx-maths="TEX"> at display time.
# Our sanitizer allowlists tags/attrs, so data-mx-maths cannot pass through HTML
# sanitization directly. Instead, math is swapped for opaque sentinel tokens before
# Markdown conversion (protecting TeX from escaping) and expanded back to math
# markup after sanitization. Tokens are plain printable text with no special
# HTML/Markdown meaning, so both the Markdown converter and the sanitizer
# pass them through verbatim.
_TEX_TOKEN_RE = re.compile(r"HERMESTEX(?:DISPLAY|INLINE)(\d+)HERMESTEXEND")
_TEX_DISPLAY_TOKEN = "HERMESTEXDISPLAY%dHERMESTEXEND"
_TEX_INLINE_TOKEN = "HERMESTEXINLINE%dHERMESTEXEND"


def _latex_to_tokens(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Replace ``$$...$$``/``$...$`` with sentinel tokens.

    Returns the tokenized text plus an ordered ``(tag, tex)`` store, where tag
    is ``div`` for display math and ``span`` for inline math. Dollars that do
    not form a pair (prices, literals) are left untouched.
    """
    if not text or "$" not in text:
        return text, []
    store: list[tuple[str, str]] = []

    def _sub_display(match: re.Match[str]) -> str:
        store.append(("div", match.group(1).strip()))
        return _TEX_DISPLAY_TOKEN % (len(store) - 1)

    def _sub_inline(match: re.Match[str]) -> str:
        store.append(("span", match.group(1).strip()))
        return _TEX_INLINE_TOKEN % (len(store) - 1)

    text = re.sub(r"\$\$([^\n$]+?)\$\$", _sub_display, text)
    text = re.sub(r"(?<![\\$\w])\$([^\n$]+?)\$(?!\w)", _sub_inline, text)
    return text, store


def _tokens_to_mx_maths(html: str, store: list[tuple[str, str]]) -> str:
    """Expand sentinel tokens into ``data-mx-maths`` markup (TeX HTML-escaped)."""

    def _expand(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        if idx >= len(store):
            # Not one of our tokens (user-typed text that collides with the
            # sentinel format) — leave it verbatim.
            return match.group(0)
        tag, tex = store[idx]
        escaped = _html_escape(tex, quote=True)
        return f'<{tag} data-mx-maths="{escaped}">{escaped}</{tag}>'

    return _TEX_TOKEN_RE.sub(_expand, html)

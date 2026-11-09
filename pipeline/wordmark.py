"""Header lockup: the Launch Box arrow and 'Accelerated by the Launch Box'.

The feedback pages and the console are one HTML document each, so the font
and arrow are embedded. A relative font file would not load from those pages.
"""

from __future__ import annotations

import base64
from pathlib import Path

HOME_URL = "https://os.thelaunchbox.com"

_ROOT = Path(__file__).resolve().parent.parent / "launch-box-wordmark"
_FONT = _ROOT / "fonts" / "BebasNeuePro-BoldItalic.otf"
_ARROW = _ROOT / "arrow.svg"
_CSS = _ROOT / "wordmark.css"


def _font_face() -> str:
    if not _FONT.is_file():
        return ""
    encoded = base64.b64encode(_FONT.read_bytes()).decode("ascii")
    return (
        "@font-face {\n"
        '  font-family: "Bebas Neue Pro";\n'
        f'  src: url("data:font/otf;base64,{encoded}") format("opentype");\n'
        "  font-weight: 700;\n"
        "  font-style: italic;\n"
        "  font-display: swap;\n"
        "}\n"
    )


def _css_without_font_face(css: str) -> str:
    start = css.find("@font-face")
    if start < 0:
        return css
    end = css.find("}", start)
    if end < 0:
        return css
    return css[:start] + css[end + 1 :]


def lockup_styles() -> str:
    css = _CSS.read_text(encoding="utf-8") if _CSS.is_file() else ""
    return (
        _font_face()
        + _css_without_font_face(css)
        + "\n.lb-lockup, .lb-lockup:hover { color: var(--color-foreground, #f0f6fc); }\n"
    )


def lockup_html() -> str:
    """The top-left lockup. It links back to the Launch Box OS."""
    if not _ARROW.is_file():
        return f'<a class="lb-lockup" href="{HOME_URL}">The Launch Box</a>'
    encoded = base64.b64encode(_ARROW.read_bytes()).decode("ascii")
    src = "data:image/svg+xml;base64," + encoded
    return (
        f'<a class="lb-lockup" href="{HOME_URL}">'
        f'<img class="lb-lockup__mark" src="{src}" alt="">'
        '<span class="lb-lockup__word">ACCELERATED BY THE LAUNCH BOX</span>'
        "</a>"
    )

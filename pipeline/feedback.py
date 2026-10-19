"""Yes/No links in the roundup, and the page that records why a 'no' happened.

Email clients do not run JavaScript or submit forms, so each button is a link.
Opening the link does not save anything (mail scanners follow links). The page
has the button that writes the verdict. 'No' asks why before it saves.
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pipeline.storage import FeedbackRecord, LocalStore

_TOKEN = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
_MAX_REASON = 2000
_MAX_BODY = 16_384


def avoidance_guidance(rejections: list[FeedbackRecord]) -> str:
    """Standing rules appended to each Claude research prompt."""
    if not rejections:
        return ""
    lines = []
    for record in rejections:
        reason = " ".join(record.reason.split())
        if len(reason) > 400:
            reason = reason[:397].rstrip() + "..."
        outlet = record.outlet or "unknown outlet"
        kind = record.opportunity_type or "other"
        who = record.person_name or "the team"
        lines.append(
            f'- For {who}: "{record.title}" ({outlet}, {kind}). Why: {reason}'
        )
    joined = "\n".join(lines)
    return f"""Standing rules from the team — they already rejected opportunities like these.
Do not suggest the same URLs. Apply each reason:
- If they rejected the outlet, format, quality, or the kind of ask, do not suggest that kind of opportunity again.
- If they rejected it only because it did not fit that spokesperson, you may still suggest it for someone whose expertise matches.

Rejected:
{joined}
"""


def feedback_href(base_url: str, token: str, action: str) -> str:
    return f"{base_url.rstrip('/')}/feedback/{token}/{action}"


@dataclass
class FeedbackHttpResult:
    status: int
    body: str = ""
    location: str = ""


def parse_form_body(raw: bytes) -> dict[str, str]:
    if len(raw) > _MAX_BODY:
        return {}
    parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    reason = parsed.get("reason", [""])[0]
    return {"reason": reason[:_MAX_REASON]}


def handle_feedback_request(
    store: LocalStore,
    method: str,
    path: str,
    form: dict[str, str] | None = None,
) -> FeedbackHttpResult:
    """GET shows the page. POST is the only call that writes a verdict."""
    route = _route(path)
    if route is None:
        return FeedbackHttpResult(
            404,
            _message_page("Link not found", "That feedback link is not valid."),
        )
    token, action = route
    record = store.get_feedback(token)
    if record is None:
        return FeedbackHttpResult(
            404,
            _message_page("Link not found", "That opportunity is no longer on file."),
        )
    if method.upper() == "GET":
        if action == "saved":
            return FeedbackHttpResult(200, _saved_page(record))
        return FeedbackHttpResult(200, _prompt_page(record, action))
    if action not in {"yes", "no"}:
        return FeedbackHttpResult(
            404,
            _message_page("Link not found", "That feedback link is not valid."),
        )
    reason = (form or {}).get("reason", "")
    if action == "no" and len(reason.strip()) < 3:
        return FeedbackHttpResult(
            400,
            _prompt_page(
                record,
                "no",
                error="Say why in a few words so the next roundup can avoid opportunities like this.",
            ),
        )
    saved = store.record_feedback(token, action, reason)
    if saved is None:
        return FeedbackHttpResult(
            404,
            _message_page("Link not found", "That opportunity is no longer on file."),
        )
    return FeedbackHttpResult(303, location=f"/feedback/{token}/saved")


def serve_feedback(store: LocalStore, host: str, port: int) -> None:
    """Block and serve the feedback pages. Ctrl-C stops it."""
    httpd = ThreadingHTTPServer((host, port), _handler(store))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


def _handler(store: LocalStore) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def do_GET(self) -> None:  # noqa: N802
            result = handle_feedback_request(store, "GET", self.path)
            self._send_html(result.status, result.body)

        def do_POST(self) -> None:  # noqa: N802
            result = handle_feedback_request(store, "POST", self.path, _read_form(self))
            if result.location:
                self.send_response(result.status)
                self.send_header("Location", result.location)
                self.end_headers()
                return
            self._send_html(result.status, result.body)

        def _send_html(self, status: int, body: str) -> None:
            payload = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return Handler


def _route(path: str) -> tuple[str, str] | None:
    parsed = urlparse(path)
    parts = [part for part in parsed.path.split("/") if part]
    if parts and parts[0] == "api":
        parts = parts[1:]
    if len(parts) != 3 or parts[0] != "feedback":
        return None
    token, action = parts[1], parts[2]
    if action not in {"yes", "no", "saved"} or not _TOKEN.match(token):
        return None
    return token, action


def _read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length < 0 or length > _MAX_BODY:
        return {}
    return parse_form_body(handler.rfile.read(length))


_THEME_PATH = Path(__file__).with_name("feedback_theme.css")
_LOGO_PATH = (
    Path(__file__).resolve().parent.parent / "assets" / "The Launch Box Logo White (1).png"
)


def _brand_logo() -> str:
    if not _LOGO_PATH.is_file():
        return ""
    encoded = base64.b64encode(_LOGO_PATH.read_bytes()).decode("ascii")
    return "data:image/png;base64," + encoded


def _page(title: str, body: str) -> str:
    """Same chrome as the console: dark Launch Box canvas, wordmark, Manrope."""
    styles = _THEME_PATH.read_text(encoding="utf-8") if _THEME_PATH.is_file() else ""
    logo = _brand_logo()
    mark = (
        f'<a class="brand" href="/"><img class="logo" src="{logo}" alt="The Launch Box"></a>'
        if logo
        else '<a class="brand" href="/">The Launch Box</a>'
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en" class="dark">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{escape(title)} · Earned media</title>\n"
        "<style>\n"
        + styles
        + "</style>\n</head>\n<body>\n"
        '<header class="topbar">\n'
        + mark
        + '\n<span class="product">Earned media</span>\n'
        "</header>\n"
        '<div class="stage">\n<main class="decision">\n'
        + body
        + "\n</main>\n</div>\n</body>\n</html>\n"
    )


def _opportunity_card(record: FeedbackRecord) -> str:
    outlet = escape(record.outlet) if record.outlet else "Unknown outlet"
    kind = escape(record.opportunity_type or "other")
    return (
        '<div class="card">'
        f"<strong>{escape(record.title)}</strong><br>"
        f'<span class="meta">{outlet} · {kind}'
        + (f" · for {escape(record.person_name)}" if record.person_name else "")
        + "</span><br>"
        f'<a href="{escape(record.url)}">{escape(record.url)}</a>'
        "</div>"
    )


def _prompt_page(record: FeedbackRecord, action: str, error: str = "") -> str:
    card = _opportunity_card(record)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""
    if action == "yes":
        body = (
            "<h1>Is this a worthwhile opportunity?</h1>"
            f"{card}"
            f"{error_html}"
            f'<form method="post" action="/feedback/{escape(record.token)}/yes">'
            '<button class="yes" type="submit">Yes</button>'
            "</form>"
            f'<p><a class="quiet" href="/feedback/{escape(record.token)}/no">No — tell us why</a></p>'
        )
        return _page("Worthwhile opportunity?", body)

    body = (
        "<h1>Why isn't this worthwhile?</h1>"
        "<p>This is saved and sent to the next research run so it can avoid opportunities like this.</p>"
        f"{card}"
        f"{error_html}"
        f'<form method="post" action="/feedback/{escape(record.token)}/no">'
        '<label for="reason">Why</label>'
        '<textarea id="reason" name="reason" required minlength="3" '
        'placeholder="Pay-to-play, wrong audience, already pitched, too junior…"></textarea>'
        '<button class="no" type="submit">Save</button>'
        "</form>"
        f'<p><a class="quiet" href="/feedback/{escape(record.token)}/yes">Actually, yes</a></p>'
    )
    return _page("Why isn't this worthwhile?", body)


def _saved_page(record: FeedbackRecord) -> str:
    card = _opportunity_card(record)
    if record.verdict == "no" and record.reason.strip():
        body = (
            "<h1>Saved</h1>"
            "<p>Future roundups will avoid opportunities like this.</p>"
            f"{card}"
            f"<p><strong>Why:</strong> {escape(record.reason)}</p>"
            f'<p><a class="quiet" href="/feedback/{escape(record.token)}/yes">Change this to yes</a></p>'
        )
        return _page("Saved", body)
    if record.verdict == "yes":
        body = (
            "<h1>Saved</h1>"
            "<p>Marked as worthwhile. It will not be used as a reason to filter later searches.</p>"
            f"{card}"
            f'<p><a class="quiet" href="/feedback/{escape(record.token)}/no">Change this to no</a></p>'
        )
        return _page("Saved", body)
    body = (
        "<h1>Is this a worthwhile opportunity?</h1>"
        f"{card}"
        f'<p><a class="btn yes" href="/feedback/{escape(record.token)}/yes">Yes</a> '
        f'<a class="btn no" href="/feedback/{escape(record.token)}/no">No</a></p>'
    )
    return _page("Worthwhile opportunity?", body)


def _message_page(title: str, message: str) -> str:
    return _page(title, f"<h1>{escape(title)}</h1><p>{escape(message)}</p>")

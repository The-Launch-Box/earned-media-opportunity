"""Go, No-Go, and Make it LinkedIn links in the roundup.

Email clients do not run JavaScript or submit forms, so each button is a link.
Opening the link does not save anything (mail scanners follow links). The page
has the button that writes the decision. No-Go asks why before it saves.
Go and Make it LinkedIn queue a draft email; they do not send anything
to an outlet or publish a post.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from pipeline.decisions import decline_from_form, item_number_example
from pipeline.storage import FeedbackRecord, LocalStore
from pipeline.wordmark import lockup_html, lockup_styles

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
            return FeedbackHttpResult(200, _saved_page(record, store))
        return FeedbackHttpResult(200, _prompt_page(record, action))
    if action in {"no", "nogo"}:
        saved_rows, error = decline_from_form(store, record, (form or {}).get("reason", ""))
        if error:
            return FeedbackHttpResult(400, _prompt_page(record, action, error=error))
        if any(row.token == token for row in saved_rows):
            return FeedbackHttpResult(303, location=f"/feedback/{token}/saved")
        return FeedbackHttpResult(200, _batch_saved_page(saved_rows))
    if action in {"go", "linkedin"}:
        blocked = _followup_block(store, record)
        if blocked:
            return FeedbackHttpResult(400, _prompt_page(record, action, error=blocked))
        store.record_feedback(token, "go" if action == "go" else "linkedin")
        from pipeline.followup import enqueue_followup

        enqueue_followup(store, token, action)
        return FeedbackHttpResult(303, location=f"/feedback/{token}/saved")
    if action != "yes":
        return FeedbackHttpResult(
            404,
            _message_page("Link not found", "That feedback link is not valid."),
        )
    saved = store.record_feedback(token, action)
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
    if action not in {"yes", "no", "nogo", "go", "linkedin", "saved"} or not _TOKEN.match(token):
        return None
    return token, action


def _read_form(handler: BaseHTTPRequestHandler) -> dict[str, str]:
    length = int(handler.headers.get("Content-Length") or "0")
    if length < 0 or length > _MAX_BODY:
        return {}
    return parse_form_body(handler.rfile.read(length))


_THEME_PATH = Path(__file__).with_name("feedback_theme.css")


def _page(title: str, body: str) -> str:
    """Same chrome as the console: dark Launch Box canvas, wordmark, Manrope."""
    styles = _THEME_PATH.read_text(encoding="utf-8") if _THEME_PATH.is_file() else ""
    styles = styles + "\n" + lockup_styles()
    mark = lockup_html()
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
    bits = []
    if record.item_number:
        bits.append(f"Item {record.item_number}")
    if record.batch_key:
        bits.append(escape(record.batch_key))
    prefix = f'<span class="meta">{" · ".join(bits)}</span><br>' if bits else ""
    return (
        '<div class="card">'
        f"{prefix}"
        f"<strong>{escape(record.title)}</strong><br>"
        f'<span class="meta">{outlet} · {kind}'
        + (f" · for {escape(record.person_name)}" if record.person_name else "")
        + "</span><br>"
        f'<a href="{escape(record.url)}">{escape(record.url)}</a>'
        "</div>"
    )


def _followup_block(store: LocalStore, record: FeedbackRecord) -> str:
    from pipeline.config import settings
    from pipeline.followup import recipient_for

    if settings.delivery_mode.strip().lower() != "graph":
        return ""
    if recipient_for(store, record).strip():
        return ""
    return "No recipient is saved for this roundup, so the draft cannot be emailed."


def _prompt_page(record: FeedbackRecord, action: str, error: str = "") -> str:
    card = _opportunity_card(record)
    error_html = f'<p class="error">{escape(error)}</p>' if error else ""
    token = escape(record.token)
    if action == "go":
        body = (
            "<h1>Draft a pitch?</h1>"
            "<p>This emails where to send it, a draft to edit, the angle, and why it is "
            "worth the time. Nothing is sent to the outlet.</p>"
            f"{card}"
            f"{error_html}"
            f'<form method="post" action="/feedback/{token}/go">'
            '<button class="go" type="submit">Email the pitch</button>'
            "</form>"
            f'<p><a class="quiet" href="/feedback/{token}/nogo">No-Go instead</a></p>'
        )
        return _page("Draft a pitch?", body)
    if action == "linkedin":
        who = escape(record.person_name or "the spokesperson")
        body = (
            "<h1>Draft a LinkedIn post?</h1>"
            f"<p>This emails a post for {who} to edit. It is not published.</p>"
            f"{card}"
            f"{error_html}"
            f'<form method="post" action="/feedback/{token}/linkedin">'
            '<button class="linkedin" type="submit">Email the draft</button>'
            "</form>"
            f'<p><a class="quiet" href="/feedback/{token}/nogo">No-Go instead</a></p>'
        )
        return _page("Draft a LinkedIn post?", body)
    if action == "yes":
        body = (
            "<h1>Is this a worthwhile opportunity?</h1>"
            f"{card}"
            f"{error_html}"
            f'<form method="post" action="/feedback/{token}/yes">'
            '<button class="yes" type="submit">Yes</button>'
            "</form>"
            f'<p><a class="quiet" href="/feedback/{token}/nogo">No-Go — tell us why</a></p>'
        )
        return _page("Worthwhile opportunity?", body)

    example = escape(item_number_example(record))
    post_action = "nogo" if action == "nogo" else "no"
    body = (
        "<h1>Why is this a no-go?</h1>"
        "<p>The next search reads this reason, so say what to avoid: the outlet, "
        "the format, the audience, or why it does not fit this person. "
        "A no-go without that reason cannot teach the next run.</p>"
        "<p>You can also reply to the roundup. One line per item, the item number, "
        f"a period, and the reason — for example {example}. The batch key in the "
        "subject ties the reply to that email.</p>"
        f"{card}"
        f"{error_html}"
        f'<form method="post" action="/feedback/{token}/{post_action}">'
        '<label for="reason">Why</label>'
        f'<textarea id="reason" name="reason" required minlength="3" '
        f'placeholder="{example}"></textarea>'
        '<button class="no" type="submit">Save</button>'
        "</form>"
        f"{_console_button()}"
        f'<p><a class="quiet" href="/feedback/{token}/go">Actually, draft a pitch</a></p>'
    )
    return _page("Why is this a no-go?", body)


def _saved_page(record: FeedbackRecord, store: LocalStore) -> str:
    card = _opportunity_card(record)
    token = escape(record.token)
    job = store.latest_action(record.token)
    draft_note = ""
    if job is not None and job.kind == "linkedin":
        draft_note = _job_note(store, record, job, "LinkedIn draft")
    elif job is not None and job.kind == "go" and record.verdict != "no":
        draft_note = _job_note(store, record, job, "Pitch")

    if record.verdict == "no" and record.reason.strip():
        extra = f"<p>{draft_note}</p>" if draft_note else ""
        body = (
            "<h1>Saved</h1>"
            "<p>Future roundups will avoid opportunities like this.</p>"
            f"{card}"
            f"<p><strong>Why:</strong> {escape(record.reason)}</p>"
            f"{extra}"
            f"{_console_button()}"
            f'<p><a class="quiet" href="/feedback/{token}/go">Change this to a pitch</a></p>'
        )
        return _page("Saved", body)
    if job is not None and job.status == "error":
        body = (
            "<h1>The draft did not go out</h1>"
            f"<p>{escape(job.error or 'Something went wrong.')}</p>"
            f"{card}"
            f'<p><a class="quiet" href="/feedback/{token}/{escape(job.kind)}">Try again</a></p>'
        )
        return _page("Draft did not go out", body)
    if draft_note:
        body = f"<h1>Saved</h1><p>{draft_note}</p>{card}"
        return _page("Saved", body)
    if record.verdict == "yes":
        body = (
            "<h1>Saved</h1>"
            "<p>Marked as worthwhile. It will not be used as a reason to filter later searches.</p>"
            f"{card}"
            f'<p><a class="quiet" href="/feedback/{token}/nogo">Change this to no-go</a></p>'
        )
        return _page("Saved", body)
    if record.verdict == "go":
        body = (
            "<h1>Saved</h1>"
            f"<p>{_plain_followup(store, record, 'The pitch')}</p>"
            f"{card}"
        )
        return _page("Saved", body)
    body = (
        "<h1>What should we do with this?</h1>"
        f"{card}"
        f'<p><a class="btn go" href="/feedback/{token}/go">Go</a> '
        f'<a class="btn no" href="/feedback/{token}/nogo">No-Go</a> '
        f'<a class="btn linkedin" href="/feedback/{token}/linkedin">Make it LinkedIn</a></p>'
    )
    return _page("What should we do with this?", body)


def _job_note(store: LocalStore, record: FeedbackRecord, job: object, label: str) -> str:
    status = getattr(job, "status", "")
    if status == "error":
        return f"The {label.lower()} did not go out. {escape(getattr(job, 'error', '') or '')}"
    if status == "sent":
        return f"The {label.lower()} was sent. {_plain_followup(store, record, label)}"
    return _plain_followup(store, record, label)


def _plain_followup(store: LocalStore, record: FeedbackRecord, label: str) -> str:
    from pipeline.followup import delivery_blurb, recipient_for

    return f"{escape(label)} is on the way. {escape(delivery_blurb(recipient_for(store, record)))}"


def _batch_saved_page(records: list[FeedbackRecord]) -> str:
    items = "".join(
        f"<li>Item {record.item_number}. {escape(record.reason)}</li>"
        for record in records
    )
    body = (
        "<h1>Saved</h1>"
        "<p>Future roundups will avoid opportunities like these.</p>"
        f"<ul>{items}</ul>"
        f"{_console_button()}"
    )
    return _page("Saved", body)


def _console_button() -> str:
    return '<p><a class="btn back" href="/">Back to admin console</a></p>'


def _message_page(title: str, message: str) -> str:
    return _page(title, f"<h1>{escape(title)}</h1><p>{escape(message)}</p>")

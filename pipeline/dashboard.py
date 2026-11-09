"""Portfolio-company dashboard: sign in, edit speakers, run a roundup."""

from __future__ import annotations

import logging
import os
import re
import secrets
from datetime import datetime, timezone
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

from pipeline.jobs import RunInProgress, enqueue_run
from pipeline.models import Spokesperson
from pipeline.ms_login import begin_flow, finish_flow, microsoft_configured, redirect_uri
from pipeline.provision import open_tenant_store, provision_tenant
from pipeline.registry import (
    AccessDenied,
    Tenant,
    clean_note_history,
    company_by_slug,
    company_for_email,
    default_registry,
    is_platform_admin,
)
from pipeline.schedule import (
    TIMEZONES,
    WEEKDAYS,
    describe_cadence,
    format_local,
    next_run_at,
    parse_stamp,
    stamp,
)
from pipeline.session import (
    clear_cookie,
    decode_session,
    encode_session,
    read_cookie,
    set_cookie,
)
from pipeline.wordmark import lockup_html, lockup_styles

logger = logging.getLogger("earned-media.dashboard")

# Portfolio companies with a theme in tlb-design-system. The Launch Box shell
# stays on TLB tokens. See tlb-design-system/docs/12-portco-themes.md.
_PORTCO_THEMES = frozenset({"echelon", "hyperscayle", "improv"})
_STYLES = Path(__file__).with_name("dashboard_theme.css").read_text(encoding="utf-8")

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PERSON = re.compile(r"^[0-9a-f]{16}$")
_NOTICES = {
    "saved": "Speaker saved.",
    "deleted": "Speaker removed. Opportunities already sent to them were cleared.",
    "queued": "Run queued. It starts within a minute. This page refreshes until it finishes.",
    "running": "Run started. This page refreshes until it finishes.",
    "schedule": "Schedule saved. Nothing was sent.",
    "notes": "Search notes saved. The next run will follow them.",
    "busy": "A run is already in progress for your company.",
    "need-recipient": "Add the email address that should receive the roundup.",
    "signin": "Sign-in expired. Start again.",
    "denied": "That Microsoft account can't use this workspace.",
}


class HttpResult:
    def __init__(
        self,
        status: int,
        body: str = "",
        *,
        content_type: str = "text/html; charset=utf-8",
        headers: list[tuple[str, str]] | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.content_type = content_type
        self.headers = headers or []


def handle_request(
    method: str,
    target: str,
    headers: dict,
    body: bytes,
) -> HttpResult:
    """One entry point for the local server and the Azure Function."""
    try:
        return _dispatch(method.upper(), target, headers, body)
    except Exception:
        logger.exception("Dashboard request failed")
        return HttpResult(
            500,
            _document(
                "Something went wrong",
                "<h1>Something went wrong</h1>"
                "<p>Try again. If it keeps happening, contact The Launch Box.</p>",
            ),
        )


def serve_site(host: str, port: int) -> None:
    """Dashboard, speaker editor, and the Yes/No feedback pages."""
    import threading

    from pipeline.jobs import dispatch_due

    stop = threading.Event()

    def _loop() -> None:
        while not stop.wait(30):
            try:
                dispatch_due()
            except Exception:
                logger.exception("Schedule check failed")
            try:
                from pipeline.followup import process_pending_followups

                process_pending_followups()
            except Exception:
                logger.exception("Follow-up check failed")

    threading.Thread(target=_loop, name="emo-schedule", daemon=True).start()
    httpd = ThreadingHTTPServer((host, port), _Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        httpd.server_close()


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        self._respond()

    def do_POST(self) -> None:  # noqa: N802
        self._respond()

    def _respond(self) -> None:
        length = int(self.headers.get("Content-Length") or "0")
        if length < 0 or length > 100_000:
            body = b""
        else:
            body = self.rfile.read(length) if length else b""
        header_map = {key: value for key, value in self.headers.items()}
        result = handle_request(self.command, self.path, header_map, body)
        payload = result.body.encode("utf-8")
        self.send_response(result.status)
        for key, value in result.headers:
            self.send_header(key, value)
        self.send_header("Content-Type", result.content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: object) -> None:
        logger.info("%s - " + fmt, self.address_string(), *args)


def _dispatch(method: str, target: str, headers: dict, body: bytes) -> HttpResult:
    parsed = urlparse(target)
    path = parsed.path or "/"
    if path.startswith("/feedback"):
        return _guarded_feedback(method, target, headers, body)
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    form = _form(body) if method == "POST" else {}
    query = {key: values[0] for key, values in parse_qs(parsed.query).items() if values}

    if path == "/login":
        return _login(method, form, query.get("next", ""))
    if path == "/auth/callback":
        return _callback(query)
    if path == "/logout" and method == "POST":
        return _logout(headers, form)

    session = _session(headers)
    if session is None:
        if path == "/" and method == "GET":
            return _login_page()
        return _redirect("/login")
    email = str(session.get("email") or "")
    slug = str(session.get("slug") or "")
    if not is_platform_admin(email):
        home = company_for_email(email)
        if home is None or home.slug != slug:
            return _redirect("/login", clearing=True)
    registry = default_registry()
    tenant = registry.get_tenant(slug)
    if tenant is None or company_by_slug(tenant.slug) is None:
        return _redirect("/login", clearing=True)
    if _needs_database(tenant):
        try:
            tenant = provision_tenant(registry, tenant)
        except Exception:
            logger.exception("Could not create the database for %s", tenant.slug)
            tenant = registry.get_tenant(slug) or tenant

    if method == "POST" and not _csrf_ok(session, form):
        return HttpResult(403, _shell(tenant, session, "<h1>Page expired</h1><p>Go back and try again.</p>"))

    parts = [part for part in path.split("/") if part]
    if not parts and method == "GET":
        return _home(tenant, session, query.get("notice", ""))
    if parts == ["runs"] and method == "POST":
        return _runs(registry, tenant, session, form)
    if parts == ["notes"] and method == "POST":
        return _save_notes(registry, tenant, session, form)
    if parts == ["speakers"] and method == "GET":
        return _speakers(tenant, session, query.get("notice", ""))
    if parts == ["speakers", "new"] and method == "GET":
        return _speaker_form(tenant, session, None, [])
    if parts == ["speakers"] and method == "POST":
        return _save_speaker(tenant, session, form, None)
    if len(parts) == 3 and parts[0] == "speakers" and parts[2] == "edit" and method == "GET":
        return _edit(tenant, session, parts[1])
    if len(parts) == 2 and parts[0] == "speakers" and method == "POST":
        return _save_speaker(tenant, session, form, parts[1])
    if len(parts) == 3 and parts[0] == "speakers" and parts[2] == "delete" and method == "GET":
        return _delete_prompt(tenant, session, parts[1])
    if len(parts) == 3 and parts[0] == "speakers" and parts[2] == "delete" and method == "POST":
        return _delete_speaker(tenant, session, parts[1])
    if parts == ["clients"] and method == "GET":
        return _clients(tenant, session)
    if parts == ["clients", "open"] and method == "POST":
        return _open_client(registry, tenant, session, form)
    return HttpResult(404, _shell(tenant, session, "<h1>Page not found</h1>"))


def _login(method: str, form: dict[str, str], next_path: str = "") -> HttpResult:
    if method == "GET":
        if microsoft_configured():
            return _microsoft_redirect(next_path)
        return _login_page()
    if method == "POST" and _dev_login() and not microsoft_configured():
        email = form.get("email", "").strip()
        registry = default_registry()
        try:
            tenant = registry.authorize(email, "", bind=False)
        except AccessDenied:
            return _login_page(error=_NOTICES["denied"])
        return _start_session(registry, tenant, email, email)
    return HttpResult(403, _document("Sign-in", "<h1>Sign-in isn't available</h1>"))


def _microsoft_redirect(next_path: str = "") -> HttpResult:
    registry = default_registry()
    flow = begin_flow()
    auth_uri = str(flow.get("auth_uri") or "")
    if not auth_uri.startswith("https://login.microsoftonline.com/"):
        raise RuntimeError("Unexpected Microsoft sign-in address.")
    state = str(flow.get("state") or "")
    if not state:
        raise RuntimeError("Microsoft didn't return a sign-in state.")
    registry.save_oauth(state, flow, _safe_next(next_path))
    return _redirect(auth_uri)


def _callback(query: dict[str, str]) -> HttpResult:
    if query.get("error"):
        return _login_page(error="Microsoft didn't complete sign-in. Try again.")
    state = query.get("state", "")
    flow = default_registry().pop_oauth(state)
    if flow is None:
        return _login_page(error=_NOTICES["signin"])
    next_path = str(flow.pop("_emo_next", "") or "")
    identity = finish_flow(flow, query)
    registry = default_registry()
    try:
        tenant = registry.authorize(identity["email"], identity["tid"], bind=True)
    except AccessDenied:
        return _login_page(error=_NOTICES["denied"])
    return _start_session(
        registry, tenant, identity["email"], identity["name"], next_path=next_path
    )


def _start_session(
    registry, tenant: Tenant, email: str, name: str, next_path: str = ""
) -> HttpResult:
    try:
        tenant = provision_tenant(registry, tenant)
    except Exception as exc:
        logger.exception("Could not create the database for %s", tenant.slug)
        tenant.provision_error = str(exc)
    payload = {
        "slug": tenant.slug,
        "email": email,
        "name": name or email,
        "csrf": secrets.token_urlsafe(24),
    }
    return _redirect(_safe_next(next_path) or "/", cookie=encode_session(payload))


def _logout(headers: dict, form: dict[str, str]) -> HttpResult:
    session = _session(headers)
    if session is not None and not _csrf_ok(session, form):
        return HttpResult(403, _document("Page expired", "<h1>Page expired</h1>"))
    return _redirect("/login", clearing=True)


def _clients(tenant: Tenant, session: dict) -> HttpResult:
    if not is_platform_admin(str(session.get("email") or "")):
        return HttpResult(404, _shell(tenant, session, "<h1>Page not found</h1>"))
    csrf = escape(str(session.get("csrf") or ""))
    cards = []
    companies = [
        company
        for company in default_registry().list_tenants()
        if company_by_slug(company.slug) is not None
    ]
    for company in companies:
        current = " current" if company.slug == tenant.slug else ""
        status = '<p class="muted">Open now</p>' if company.slug == tenant.slug else ""
        domains = escape(", ".join(company.domains))
        cards.append(
            f'<li class="client-card{current}">'
            f"<h2>{escape(company.name)}</h2>"
            f'<p class="muted">{domains}</p>'
            f"{status}"
            '<form method="post" action="/clients/open">'
            f'<input type="hidden" name="csrf" value="{csrf}">'
            f'<input type="hidden" name="slug" value="{escape(company.slug)}">'
            '<p class="actions">'
            '<button class="button" name="next" value="/" type="submit">Open console</button>'
            '<button class="quiet" name="next" value="/speakers" type="submit">Speaker list</button>'
            "</p></form></li>"
        )
    body = (
        '<p class="eyebrow">Platform</p><h1>Clients</h1>'
        '<p class="muted">Open a portfolio company\'s console or speaker list. '
        "Each company keeps its own speakers, schedule, and sent links.</p>"
        '<ul class="client-menu">' + "".join(cards) + "</ul>"
    )
    return HttpResult(200, _shell(tenant, session, body, section="clients"))


def _open_client(registry, tenant: Tenant, session: dict, form: dict[str, str]) -> HttpResult:
    if not is_platform_admin(str(session.get("email") or "")):
        return HttpResult(404, _shell(tenant, session, "<h1>Page not found</h1>"))
    slug = form.get("slug", "").strip()
    if company_by_slug(slug) is None:
        return HttpResult(404, _shell(tenant, session, "<h1>Company not found</h1>"))
    chosen = registry.get_tenant(slug)
    if chosen is None:
        return HttpResult(404, _shell(tenant, session, "<h1>Company not found</h1>"))
    try:
        provision_tenant(registry, chosen)
    except Exception as exc:
        logger.exception("Could not create the database for %s", chosen.slug)
        chosen.provision_error = str(exc)
    destination = form.get("next", "/")
    if destination not in {"/", "/speakers"}:
        destination = "/"
    payload = {
        "slug": chosen.slug,
        "email": str(session.get("email") or ""),
        "name": str(session.get("name") or ""),
        "csrf": str(session.get("csrf") or "") or secrets.token_urlsafe(24),
    }
    return _redirect(destination, cookie=encode_session(payload))


def _home(tenant: Tenant, session: dict, notice: str) -> HttpResult:
    registry = default_registry()
    active = registry.active_job(tenant.slug)
    jobs = registry.jobs_for(tenant.slug)
    refresh = "<meta http-equiv=\"refresh\" content=\"10\">" if active else ""
    banner = _notice(notice)
    if tenant.provision_error and not tenant.provisioned_at:
        banner += (
            '<p class="warn">Speakers are unavailable until the company database '
            "is ready. Refresh this page. If it stays this way, contact The Launch Box.</p>"
        )
    if active:
        banner += (
            f'<p class="warn">A {escape(active.cadence or "manual")} run is '
            f"{escape(active.status)}. This page refreshes until it finishes.</p>"
        )
    body = (
        banner
        + _schedule_form(tenant, session)
        + _job_list(tenant, jobs)
        + _notes_form(tenant, session)
        + _saved_notes(tenant)
    )
    return HttpResult(200, _shell(tenant, session, body, refresh=refresh, section="schedule"))


def _runs(registry, tenant: Tenant, session: dict, form: dict[str, str]) -> HttpResult:
    action = form.get("action", "")
    if action not in {"save", "daily", "weekly", "custom", "now"}:
        return HttpResult(
            400,
            _shell(tenant, session, "<h1>Choose a run button</h1>", section="schedule"),
        )
    recipients, recipient_error = _recipients(form.get("blast_to", ""))
    hour, hour_ok = _bounded(form.get("hour", ""), 0, 23)
    minute, minute_ok = _bounded(form.get("minute", ""), 0, 59)
    weekday, weekday_ok = _bounded(form.get("weekday", ""), 0, 6)
    interval, interval_ok = _bounded(form.get("interval_days", ""), 1, 90)
    tz_name = form.get("timezone", "")
    errors: list[str] = []
    if recipient_error:
        errors.append(recipient_error)
    if not hour_ok or not minute_ok:
        errors.append("Choose an hour from 0 to 23 and a minute from 0 to 59.")
    if not weekday_ok:
        errors.append("Choose a day of the week.")
    if tz_name not in {key for key, _label in TIMEZONES}:
        errors.append("Choose a time zone.")
    cadence = action if action in {"daily", "weekly", "custom"} else form.get("cadence", "")
    if action == "save" and cadence not in {"", "daily", "weekly", "custom"}:
        errors.append("Choose a schedule.")
    if cadence == "custom" and not interval_ok:
        errors.append("Custom cadence needs a number of days from 1 to 90.")
    if action in {"daily", "weekly", "custom", "now"} and registry.active_job(tenant.slug):
        return _redirect("/", notice="busy")
    if errors or hour is None or minute is None or weekday is None:
        values = {
            "blast_to": form.get("blast_to", ""),
            "cadence": cadence,
            "hour": form.get("hour", ""),
            "minute": form.get("minute", ""),
            "weekday": form.get("weekday", ""),
            "interval_days": form.get("interval_days", ""),
            "timezone": tz_name,
        }
        body = "".join(f'<p class="warn">{escape(item)}</p>' for item in errors)
        body += _schedule_form(tenant, session, values)
        return HttpResult(400, _shell(tenant, session, body, section="schedule"))

    assert hour is not None and minute is not None and weekday is not None
    tenant.blast_to = ", ".join(recipients)
    if action != "now":
        tenant.hour = hour
        tenant.minute = minute
        tenant.weekday = weekday
        tenant.interval_days = interval or tenant.interval_days
        tenant.timezone = tz_name
        tenant.cadence = cadence
        if cadence in {"daily", "weekly", "custom"}:
            upcoming = next_run_at(
                cadence=cadence,
                hour=hour,
                minute=minute,
                weekday=weekday,
                interval_days=interval or 1,
                tz_name=tz_name,
                after=datetime.now(timezone.utc),
            )
            tenant.next_run_at = stamp(upcoming)
        else:
            tenant.next_run_at = ""
    registry.save_tenant(tenant)
    if action == "save":
        return _redirect("/", notice="schedule")
    try:
        enqueue_run(
            registry,
            tenant,
            cadence=action if action != "now" else (tenant.cadence or "now"),
            trigger="manual",
            triggered_by=str(session.get("email") or ""),
        )
    except RunInProgress:
        return _redirect("/", notice="busy")
    notice = "queued" if _execution() == "defer" else "running"
    return _redirect("/", notice=notice)


def _save_notes(registry, tenant: Tenant, session: dict, form: dict[str, str]) -> HttpResult:
    text = _clip(form.get("research_notes", ""), 2000)
    history = clean_note_history(tenant.note_history)
    if not history and tenant.research_notes and tenant.research_notes != text:
        history.append({"text": tenant.research_notes, "saved_at": "", "saved_by": ""})
    if not history or history[0]["text"] != text:
        history.insert(
            0,
            {
                "text": text,
                "saved_at": stamp(datetime.now(timezone.utc)),
                "saved_by": _clip(str(session.get("email") or ""), 200),
            },
        )
    tenant.research_notes = text
    tenant.note_history = history[:30]
    registry.save_tenant(tenant)
    return _redirect("/", notice="notes")


def _notes_form(tenant: Tenant, session: dict) -> str:
    csrf = escape(str(session.get("csrf") or ""))
    return f"""
<section class="panel">
  <h2>Search notes</h2>
  <p class="muted">These instructions go to the research model on every run. For example: only target US audiences, or only look for speaking opportunities.</p>
  <form method="post" action="/notes">
    <input type="hidden" name="csrf" value="{csrf}">
    <label for="research_notes">Extra context</label>
    <textarea id="research_notes" class="short" name="research_notes" maxlength="2000">{escape(tenant.research_notes)}</textarea>
    <p class="actions"><button class="button" type="submit">Save notes</button></p>
  </form>
</section>
"""


def _saved_notes(tenant: Tenant) -> str:
    entries = clean_note_history(tenant.note_history)
    if not entries and (tenant.research_notes or "").strip():
        entries = [{"text": tenant.research_notes, "saved_at": "", "saved_by": ""}]
    if not entries:
        listing = '<p class="muted">No search notes saved yet.</p>'
    else:
        items = []
        for entry in entries:
            when = parse_stamp(str(entry.get("saved_at") or ""))
            clock = format_local(when, tenant.timezone) if when else "Saved earlier"
            who = str(entry.get("saved_by") or "").strip()
            byline = f" · {escape(who)}" if who else ""
            text = str(entry.get("text") or "").strip()
            if text:
                shown = f'<p class="saved-note">{escape(text)}</p>'
            else:
                shown = '<p class="saved-note muted">Notes cleared.</p>'
            items.append(f"<li><strong>{escape(clock)}</strong>{byline}{shown}</li>")
        listing = '<ul class="saved-notes">' + "".join(items) + "</ul>"
    return f"""
<section class="panel" id="saved-search-notes">
  <h2>Previously saved notes</h2>
  {listing}
</section>
"""


def _speakers(tenant: Tenant, session: dict, notice: str) -> HttpResult:
    try:
        people = _roster(tenant)
    except Exception:
        logger.exception("Could not load speakers for %s", tenant.slug)
        body = (
            f'<p class="eyebrow">{escape(tenant.name)}</p>'
            "<h1>Speakers</h1>"
            '<p class="warn">The speaker list could not be opened. Refresh and try again.</p>'
        )
        return HttpResult(200, _shell(tenant, session, body, section="speakers"))
    rows = []
    for person in people:
        topics = person.topics if len(person.topics) <= 80 else person.topics[:77] + "..."
        rows.append(
            "<tr>"
            f"<td>{escape(person.name)}</td>"
            f"<td>{escape(person.firm)}</td>"
            f"<td>{escape(person.title)}</td>"
            f"<td>{escape(topics)}</td>"
            f"<td>{escape(person.email or '')}</td>"
            f'<td><a href="/speakers/{escape(person.person_id)}/edit">Edit</a></td>'
            "</tr>"
        )
    table = (
        '<p class="muted">No speakers yet. Add the first person your company wants this tool to watch.</p>'
        if not rows
        else (
            '<div class="table-wrap"><table><thead><tr><th>Name</th><th>Firm</th><th>Title</th>'
            "<th>Topics</th><th>Email</th><th></th></tr></thead><tbody>"
            + "".join(rows)
            + "</tbody></table></div>"
        )
    )
    body = (
        f'<p class="eyebrow">{escape(tenant.name)}</p>'
        "<h1>Speakers</h1>"
        + _notice(notice)
        + '<p class="actions"><a class="button" href="/speakers/new">Add a speaker</a></p>'
        + table
    )
    return HttpResult(200, _shell(tenant, session, body, section="speakers"))


def _edit(tenant: Tenant, session: dict, person_id: str) -> HttpResult:
    if not _PERSON.match(person_id):
        return HttpResult(404, _shell(tenant, session, "<h1>Speaker not found</h1>"))
    person = _roster_store(tenant).get_person(person_id)
    if person is None:
        return HttpResult(404, _shell(tenant, session, "<h1>Speaker not found</h1>"))
    return _speaker_form(tenant, session, person, [])


def _speaker_form(
    tenant: Tenant,
    session: dict,
    person: Spokesperson | None,
    errors: list[str],
    values: dict[str, str] | None = None,
) -> HttpResult:
    data = values or {}
    if person is not None and not values:
        data = {
            "firm": person.firm,
            "name": person.name,
            "title": person.title,
            "linkedin": person.linkedin,
            "topics": person.topics,
            "prior_work": person.prior_work,
            "bio": person.bio,
            "email": person.email or "",
        }
    action = "/speakers" if person is None else f"/speakers/{person.person_id}"
    title = "Add a speaker" if person is None else f"Edit {person.name}"
    error_html = "".join(f'<p class="warn">{escape(item)}</p>' for item in errors)
    fields = [
        ("firm", "Firm", "text", True),
        ("name", "Name", "text", True),
        ("title", "Title", "text", False),
        ("email", "Email", "email", False),
        ("linkedin", "LinkedIn", "text", False),
    ]
    inputs = []
    for key, label, kind, required in fields:
        req = " required" if required else ""
        inputs.append(
            f'<label for="{key}">{label}</label>'
            f'<input id="{key}" name="{key}" type="{kind}"{req} '
            f'value="{escape(data.get(key, ""))}">'
        )
    for key, label in (
        ("topics", "Topics"),
        ("prior_work", "Prior work"),
        ("bio", "Bio"),
    ):
        inputs.append(
            f'<label for="{key}">{label}</label>'
            f'<textarea id="{key}" name="{key}">{escape(data.get(key, ""))}</textarea>'
        )
    delete = ""
    if person is not None:
        delete = (
            f'<p><a class="danger-link" href="/speakers/{escape(person.person_id)}/delete">'
            "Delete this speaker</a></p>"
        )
    body = (
        f'<p class="eyebrow">{escape(tenant.name)}</p>'
        f"<h1>{escape(title)}</h1>{error_html}"
        f'<form class="panel" method="post" action="{escape(action)}">'
        f'<input type="hidden" name="csrf" value="{escape(str(session.get("csrf") or ""))}">'
        + "".join(inputs)
        + '<p class="actions"><button class="button" type="submit">Save speaker</button></p></form>'
        + delete
    )
    return HttpResult(
        200 if not errors else 400,
        _shell(tenant, session, body, section="speakers"),
    )


def _save_speaker(
    tenant: Tenant,
    session: dict,
    form: dict[str, str],
    person_id: str | None,
) -> HttpResult:
    if person_id is not None and not _PERSON.match(person_id):
        return HttpResult(404, _shell(tenant, session, "<h1>Speaker not found</h1>"))
    store = _roster_store(tenant)
    if person_id is not None and store.get_person(person_id) is None:
        return HttpResult(404, _shell(tenant, session, "<h1>Speaker not found</h1>"))
    person, errors = _person_from_form(form, person_id)
    if person is None:
        current = store.get_person(person_id) if person_id else None
        return _speaker_form(tenant, session, current, errors, form)
    if person_id is None and store.get_person(person.person_id) is not None:
        return _speaker_form(
            tenant,
            session,
            None,
            ["A speaker with this firm and name is already on the list."],
            form,
        )
    store.save_person(person)
    return _redirect("/speakers", notice="saved")


def _delete_prompt(tenant: Tenant, session: dict, person_id: str) -> HttpResult:
    if not _PERSON.match(person_id):
        return HttpResult(404, _shell(tenant, session, "<h1>Speaker not found</h1>"))
    person = _roster_store(tenant).get_person(person_id)
    if person is None:
        return HttpResult(404, _shell(tenant, session, "<h1>Speaker not found</h1>"))
    body = (
        f"<h1>Delete {escape(person.name)}?</h1>"
        "<p>They will be removed from future roundups. Links already sent for them "
        "are cleared, so adding them again can bring those opportunities back.</p>"
        f'<form method="post" action="/speakers/{escape(person_id)}/delete">'
        f'<input type="hidden" name="csrf" value="{escape(str(session.get("csrf") or ""))}">'
        '<button class="danger" type="submit">Delete speaker</button></form>'
        f'<p><a href="/speakers/{escape(person_id)}/edit">Keep this speaker</a></p>'
    )
    return HttpResult(200, _shell(tenant, session, body, section="speakers"))


def _delete_speaker(tenant: Tenant, session: dict, person_id: str) -> HttpResult:
    if not _PERSON.match(person_id):
        return HttpResult(404, _shell(tenant, session, "<h1>Speaker not found</h1>"))
    if not _roster_store(tenant).delete_person(person_id):
        return HttpResult(404, _shell(tenant, session, "<h1>Speaker not found</h1>"))
    return _redirect("/speakers", notice="deleted")


def _schedule_form(
    tenant: Tenant,
    session: dict,
    values: dict[str, str] | None = None,
) -> str:
    data = values or {
        "blast_to": tenant.blast_to,
        "cadence": tenant.cadence,
        "hour": str(tenant.hour),
        "minute": str(tenant.minute),
        "weekday": str(tenant.weekday),
        "interval_days": str(tenant.interval_days),
        "timezone": tenant.timezone,
    }
    cadence_options = [
        ("", "Only when I press Run now"),
        ("daily", "Daily"),
        ("weekly", "Weekly"),
        ("custom", "Every N days"),
    ]
    options = "".join(
        f'<option value="{escape(value)}"{" selected" if data.get("cadence") == value else ""}>'
        f"{escape(label)}</option>"
        for value, label in cadence_options
    )
    days = "".join(
        f'<option value="{index}"{" selected" if data.get("weekday") == str(index) else ""}>'
        f"{day}</option>"
        for index, day in enumerate(WEEKDAYS)
    )
    zones = "".join(
        f'<option value="{escape(key)}"{" selected" if data.get("timezone") == key else ""}>'
        f"{escape(label)}</option>"
        for key, label in TIMEZONES
    )
    description = describe_cadence(
        cadence=tenant.cadence,
        hour=tenant.hour,
        minute=tenant.minute,
        weekday=tenant.weekday,
        interval_days=tenant.interval_days,
        tz_name=tenant.timezone,
    )
    upcoming = parse_stamp(tenant.next_run_at)
    next_line = (
        format_local(upcoming, tenant.timezone)
        if upcoming is not None
        else "not scheduled"
    )
    csrf = escape(str(session.get("csrf") or ""))
    return f"""
<section>
  <p class="eyebrow">Schedule</p>
  <h1>{escape(tenant.name)}</h1>
  <p class="muted">Speakers, sent links, and feedback for {escape(tenant.name)} stay in your company's own database.</p>
  <p class="summary"><strong>Schedule:</strong> {escape(description)}<br>
  <strong>Next automatic run:</strong> <span class="mono">{escape(next_line)}</span></p>
  <form class="panel" method="post" action="/runs">
    <input type="hidden" name="csrf" value="{csrf}">
    <label for="blast_to">Send the roundup to</label>
    <input id="blast_to" name="blast_to" type="text" required value="{escape(data.get("blast_to", ""))}" placeholder="name@company.com, teammate@company.com">
    <label for="cadence">Automatic schedule</label>
    <select id="cadence" name="cadence">{options}</select>
    <div class="grid">
      <div>
        <label for="weekday">Weekly day</label>
        <select id="weekday" name="weekday">{days}</select>
      </div>
      <div>
        <label for="interval_days">Every N days</label>
        <input id="interval_days" name="interval_days" type="number" min="1" max="90" value="{escape(data.get("interval_days", "14"))}">
      </div>
      <div>
        <label for="hour">Hour (0–23)</label>
        <input id="hour" name="hour" type="number" min="0" max="23" value="{escape(data.get("hour", "7"))}">
      </div>
      <div>
        <label for="minute">Minute</label>
        <input id="minute" name="minute" type="number" min="0" max="59" value="{escape(data.get("minute", "0"))}">
      </div>
      <div>
        <label for="timezone">Time zone</label>
        <select id="timezone" name="timezone">{zones}</select>
      </div>
    </div>
    <p class="actions">
      <button class="button" name="action" value="daily" type="submit">Run daily</button>
      <button class="button" name="action" value="weekly" type="submit">Run weekly</button>
      <button class="button" name="action" value="custom" type="submit">Run every N days</button>
      <button class="button secondary" name="action" value="now" type="submit">Run now</button>
      <button class="quiet" name="action" value="save" type="submit">Save schedule</button>
    </p>
    <p class="muted">Run daily, Run weekly, and Run every N days research every speaker now, email the addresses above, and keep that schedule. Run now sends once and leaves the schedule alone. Save schedule does not send anything.</p>
  </form>
</section>
"""


def _job_list(tenant: Tenant, jobs: list) -> str:
    if not jobs:
        return (
            '<section class="panel"><h2>Recent runs</h2>'
            '<p class="muted">No runs yet.</p></section>'
        )
    items = []
    for job in jobs:
        when = parse_stamp(job.created_at)
        clock = format_local(when, tenant.timezone) if when else job.created_at
        detail = job.summary or job.error or job.status
        extra = f" {escape(job.error)}" if job.error and job.summary else ""
        kind = job.status if job.status in {"queued", "running", "done", "error"} else "other"
        items.append(
            f'<li><strong class="status status-{kind}">{escape(job.status)}</strong> '
            f'<span class="mono">{escape(clock)}</span> · {escape(job.cadence or "now")}<br>'
            f"{escape(detail)}{extra}</li>"
        )
    return (
        '<section class="panel"><h2>Recent runs</h2><ul class="runs">'
        + "".join(items)
        + "</ul></section>"
    )


def _login_page(error: str = "") -> HttpResult:
    message = f'<p class="warn">{escape(error)}</p>' if error else ""
    if microsoft_configured():
        action = (
            f"{message}<p>Use your work Microsoft account.</p>"
            '<p><a class="button" href="/login">Sign in with Microsoft</a></p>'
        )
    elif _dev_login():
        action = (
            f"{message}"
            '<p class="warn">Developer sign-in is on. Turn off DASHBOARD_DEV_LOGIN before anyone outside your team can open this site.</p>'
            '<form method="post" action="/login">'
            '<label for="email">Work email</label>'
            '<input id="email" name="email" type="email" required placeholder="you@echelon.com">'
            '<button class="button" type="submit">Continue</button></form>'
        )
    else:
        action = (
            f"{message}<p>Sign-in isn't available yet. Contact Kaylee Scanlin at The Launch Box.</p>"
            "<p class=\"muted\">Operators: set DASHBOARD_CLIENT_ID and DASHBOARD_CLIENT_SECRET, "
            "or DASHBOARD_DEV_LOGIN=true on a private machine.</p>"
        )
    companies = "The Launch Box, Improv, Hyperscayle, and Echelon"
    body = (
        '<section class="panel sign-in">'
        "<h1>Earned media</h1>"
        f"<p>This workspace is for {companies} work accounts. "
        "Each company sees only its own speakers.</p>"
        + action
        + "</section>"
    )
    return HttpResult(200 if not error else 403, _document("Sign in", body))


def _roster(tenant: Tenant) -> list[Spokesperson]:
    return _roster_store(tenant).list_roster()


def _roster_store(tenant: Tenant):
    registry = default_registry()
    current = registry.get_tenant(tenant.slug) or tenant
    if _needs_database(current):
        current = provision_tenant(registry, current)
    return open_tenant_store(current)


def _needs_database(tenant: Tenant) -> bool:
    if not tenant.provisioned_at:
        return True
    if tenant.storage_backend in {"tables", "table", "azure"}:
        return not tenant.storage_account
    return not tenant.db_path


def _person_from_form(
    form: dict[str, str], person_id: str | None
) -> tuple[Spokesperson | None, list[str]]:
    errors: list[str] = []
    firm = _clip(form.get("firm", ""), 120)
    name = _clip(form.get("name", ""), 120)
    if not firm:
        errors.append("Firm is required.")
    if not name:
        errors.append("Name is required.")
    email = _clip(form.get("email", ""), 200)
    if email and not _EMAIL.match(email):
        errors.append("Email doesn't look right.")
    if errors:
        return None, errors
    person = Spokesperson.from_row(
        firm=firm,
        name=name,
        title=_clip(form.get("title", ""), 160),
        linkedin=_clip(form.get("linkedin", ""), 300),
        topics=_clip(form.get("topics", ""), 2000),
        prior_work=_clip(form.get("prior_work", ""), 4000),
        bio=_clip(form.get("bio", ""), 8000),
        email=email or None,
    )
    if person_id:
        person = person.model_copy(update={"person_id": person_id})
    return person, []


def _recipients(raw: str) -> tuple[list[str], str]:
    parts = [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]
    if not parts:
        return [], "Add at least one email address for the roundup."
    bad = [part for part in parts if not _EMAIL.match(part)]
    if bad:
        return [], "Check the recipient addresses."
    return parts, ""


def _bounded(raw: str, low: int, high: int) -> tuple[int | None, bool]:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None, False
    if value < low or value > high:
        return None, False
    return value, True


def _clip(value: str, limit: int) -> str:
    return (value or "").strip()[:limit]


def _csrf_ok(session: dict, form: dict[str, str]) -> bool:
    import hmac

    expected = str(session.get("csrf") or "")
    given = form.get("csrf") or ""
    if not expected or not given:
        return False
    return hmac.compare_digest(expected, given)


def _session(headers: dict) -> dict | None:
    return decode_session(read_cookie(_header(headers, "cookie")))


def _header(headers: dict, name: str) -> str:
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value)
    return ""


def _form(body: bytes) -> dict[str, str]:
    if len(body) > 100_000:
        return {}
    parsed = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[0] if values else "" for key, values in parsed.items()}


def _dev_login() -> bool:
    return os.getenv("DASHBOARD_DEV_LOGIN", "").strip().lower() in {"1", "true", "yes"}


def _execution() -> str:
    from pipeline.jobs import execution_mode

    return execution_mode()


def _notice(code: str) -> str:
    text = _NOTICES.get(code, "")
    if not text:
        return ""
    return f'<p class="note">{escape(text)}</p>'


def _redirect(
    location: str,
    *,
    notice: str = "",
    cookie: str = "",
    clearing: bool = False,
) -> HttpResult:
    if notice:
        location = f"{location}?notice={notice}"
    headers = [("Location", location), ("Cache-Control", "no-store")]
    if cookie:
        secure = redirect_uri().startswith("https://")
        headers.append(("Set-Cookie", set_cookie(cookie, secure=secure)))
    elif clearing:
        headers.append(("Set-Cookie", clear_cookie()))
    return HttpResult(303, "", headers=headers)


def _shell(
    tenant: Tenant,
    session: dict,
    body: str,
    *,
    refresh: str = "",
    section: str = "",
) -> str:
    who = escape(str(session.get("name") or session.get("email") or ""))
    csrf = escape(str(session.get("csrf") or ""))
    email = str(session.get("email") or "")
    clients = ""
    viewing = ""
    if is_platform_admin(email):
        mark = ' class="current"' if section == "clients" else ""
        clients = f'<a{mark} href="/clients">Clients</a>'
        home = company_for_email(email)
        if home is None or home.slug != tenant.slug:
            viewing = f'<p class="note">Viewing the {escape(tenant.name)} console.</p>'

    def _link(name: str, href: str, label: str) -> str:
        mark = ' class="current"' if section == name else ""
        return f'<a{mark} href="{href}">{label}</a>'

    nav = (
        _link("schedule", "/", "Schedule")
        + _link("speakers", "/speakers", "Speakers")
        + clients
        + f'<form method="post" action="/logout"><input type="hidden" name="csrf" value="{csrf}">'
        f'<button class="link" type="submit">Sign out</button></form>'
    )
    portco = tenant.slug if section != "clients" and tenant.slug in _PORTCO_THEMES else ""
    return _document(
        tenant.name,
        body,
        refresh=refresh,
        nav=nav,
        who=who,
        portco=portco,
        banner=viewing,
    )


def _document(
    title: str,
    body: str,
    *,
    refresh: str = "",
    nav: str = "",
    who: str = "",
    portco: str = "",
    banner: str = "",
) -> str:
    scope = f' data-portco="{escape(portco)}"' if portco else ""
    nav_html = f"<nav>{nav}</nav>" if nav else ""
    who_html = f'<span class="who">{who}</span>' if who else ""
    banner_html = f'<div class="banner">{banner}</div>' if banner else ""
    brand = lockup_html() + '<span class="product">Earned media</span>'
    return f"""<!DOCTYPE html>
<html lang="en" class="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{refresh}
<title>{escape(title)} · Earned media</title>
<style>
{_STYLES}
{lockup_styles()}
</style>
</head>
<body>
<header class="topbar">{brand}{nav_html}{who_html}</header>
{banner_html}
<div class="stage"{scope}>
<main>
{body}
</main>
</div>
</body>
</html>
"""


_FEEDBACK_ACTIONS = frozenset({"yes", "no", "nogo", "go", "linkedin", "saved"})


def _safe_next(value: str) -> str:
    """A feedback path on this site. Anything else is dropped."""
    path = (value or "").split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/feedback/") or "\\" in path or "//" in path:
        return ""
    parts = [part for part in path.split("/") if part]
    if len(parts) != 3 or parts[0] != "feedback":
        return ""
    token, action = parts[1], parts[2]
    if action not in _FEEDBACK_ACTIONS:
        return ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,128}", token):
        return ""
    return f"/feedback/{token}/{action}"


def _guarded_feedback(method: str, target: str, headers: dict, body: bytes) -> HttpResult:
    """Go, No-Go, and LinkedIn pages require a signed-in portfolio account."""
    parsed = urlparse(target)
    path = parsed.path or "/"
    nxt = _safe_next(path)
    session = _session(headers)
    if session is None:
        if not nxt:
            return _redirect("/login")
        return _redirect("/login?next=" + quote(nxt, safe=""))
    email = str(session.get("email") or "")
    slug = str(session.get("slug") or "")
    admin = is_platform_admin(email)
    if not admin:
        home = company_for_email(email)
        if home is None or home.slug != slug:
            return _redirect("/login", clearing=True)
    registry = default_registry()
    tenant = registry.get_tenant(slug)
    if tenant is None or company_by_slug(tenant.slug) is None:
        return _redirect("/login", clearing=True)
    return _feedback(method, target, body, actor_slug=slug, admin=admin)


def _feedback(
    method: str,
    target: str,
    body: bytes,
    *,
    actor_slug: str = "",
    admin: bool = False,
) -> HttpResult:
    from pipeline.feedback import _message_page, _route, handle_feedback_request, parse_form_body

    route = _route(target)
    if route is None:
        return HttpResult(
            404,
            _message_page("Link not found", "That feedback link is not valid."),
        )
    token, _action = route
    found = _feedback_owner(token)
    if found is None:
        return HttpResult(
            404,
            _message_page("Link not found", "That opportunity is no longer on file."),
        )
    store, owner = found
    if not admin and owner != actor_slug:
        return HttpResult(
            403,
            _message_page(
                "Not available",
                "Sign in with the company this roundup belongs to.",
            ),
        )
    form = parse_form_body(body) if method == "POST" else None
    result = handle_feedback_request(store, method, target, form)
    headers = [("Location", result.location)] if result.location else []
    return HttpResult(result.status, result.body, headers=headers)


def _feedback_owner(token: str):
    from pipeline.config import settings
    from pipeline.storage import LocalStore

    if settings.db_path.exists():
        legacy = LocalStore(settings.db_path)
        if legacy.get_feedback(token):
            return legacy, "thelaunchbox"
    registry = default_registry()
    for tenant in registry.list_tenants():
        if not tenant.provisioned_at:
            continue
        try:
            store = open_tenant_store(tenant)
        except Exception:
            logger.exception("Could not open the database for %s", tenant.slug)
            continue
        if store.get_feedback(token):
            return store, tenant.slug
    return None


def _feedback_store(token: str):
    found = _feedback_owner(token)
    return found[0] if found else None

"""Pitch packages and LinkedIn drafts emailed after Go or Make it LinkedIn.

Opening the link in the roundup does not call the model. The page button
queues the draft. Locally a background thread writes it. On Azure the minute
timer does, because the HTTP request would be closed before Claude finished.
Nothing is sent to an outlet, and nothing is published to LinkedIn.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from pipeline.config import settings
from pipeline.models import Spokesperson
from pipeline.research import _extract_text, _parse_json_payload, _web_search_tool

logger = logging.getLogger("earned-media.followup")

_EMAIL = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_PLACEHOLDER_HOSTS = {
    "example.com",
    "example.org",
    "example.net",
    "email.com",
    "domain.com",
}
_inbox_note: str | None = None


@dataclass
class Contact:
    name: str = ""
    role: str = ""
    email: str = ""
    outlet: str = ""
    source_url: str = ""


@dataclass
class PitchPackage:
    why_spend_time: str
    angle_guidance: str
    contacts: list[Contact] = field(default_factory=list)
    contact_note: str = ""
    pitch_subject: str = ""
    pitch_body: str = ""


@dataclass
class LinkedInDraft:
    post: str
    note: str = ""


def enqueue_followup(store: object, token: str, kind: str) -> str:
    """Queue a draft. Starts it unless this process only queues for the timer."""
    job_id = store.enqueue_action(token, kind)  # type: ignore[attr-defined]
    from pipeline.jobs import execution_mode

    mode = execution_mode()
    if mode == "defer":
        return job_id
    if mode == "inline":
        process_action(store, job_id)
        return job_id

    def _run() -> None:
        try:
            process_action(store, job_id)
        except Exception:
            logger.exception("Follow-up %s failed", job_id)

    threading.Thread(target=_run, name=f"emo-followup-{job_id}", daemon=True).start()
    return job_id


def process_action(
    store: object,
    job_id: str,
    *,
    dry_run: bool | None = None,
    outbox_dir: Path | None = None,
    deliver=None,
) -> None:
    """Write one queued draft and mark the job sent or error."""
    job = store.claim_action(job_id)  # type: ignore[attr-defined]
    if job is None:
        return
    try:
        record = store.get_feedback(job.token)  # type: ignore[attr-defined]
        if record is None:
            raise RuntimeError("That opportunity is no longer on file.")
        person = _person_for(store, record)
        use_dry = settings.dry_run_research if dry_run is None else dry_run
        if job.kind == "linkedin":
            draft = draft_linkedin(person, record, dry_run=use_dry)
            subject, text, html = render_linkedin(person, record, draft)
            slug = "LinkedIn"
        else:
            draft = draft_pitch(person, record, dry_run=use_dry)
            subject, text, html = render_pitch(person, record, draft)
            slug = "Pitch"
        to_line = _recipient(store, record)
        if deliver is not None:
            deliver(subject=subject, text=text, html=html, to=to_line)
        else:
            _deliver_followup(
                subject=subject,
                text=text,
                html=html,
                to_line=to_line,
                outbox_dir=outbox_dir,
                slug=slug,
            )
    except Exception as exc:  # noqa: BLE001 — the job row has to show the failure
        logger.exception("Follow-up %s failed", job_id)
        store.finish_action(job_id, status="error", error=str(exc))  # type: ignore[attr-defined]
        return
    store.finish_action(job_id, status="sent")  # type: ignore[attr-defined]


def process_pending_followups() -> None:
    """Send queued drafts, then read decline replies from the sending mailbox."""
    stores = list(_open_stores())
    for store in stores:
        try:
            for job in store.queued_actions():  # type: ignore[attr-defined]
                process_action(store, job.job_id)
        except Exception:
            logger.exception("Follow-up queue failed")
    ingest_inbox(stores)


def draft_pitch(person: Spokesperson, record: object, *, dry_run: bool = False) -> PitchPackage:
    if dry_run:
        return _stub_pitch(person, record)
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is missing, so the pitch could not be written."
        )
    raw = _complete(_pitch_system(), _pitch_user(person, record), search=True)
    return _pitch_from_model(raw, person, record)


def draft_linkedin(
    person: Spokesperson, record: object, *, dry_run: bool = False
) -> LinkedInDraft:
    if dry_run:
        return _stub_linkedin(person, record)
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is missing, so the LinkedIn draft could not be written."
        )
    raw = _complete(_linkedin_system(), _linkedin_user(person, record), search=False)
    data = _parse_json_payload(raw)
    post = str(data.get("post") or "").strip()
    if not post:
        post = raw.strip() or "A draft did not come back. Try Make it LinkedIn again."
    note = str(data.get("note") or "").strip()
    return LinkedInDraft(post=post, note=note)


def render_pitch(person: Spokesperson, record: object, draft: PitchPackage) -> tuple[str, str, str]:
    title = getattr(record, "title", "")
    url = getattr(record, "url", "")
    subject = _mail_subject("Pitch draft", person.name, title)
    where = _contacts_text(draft)
    item = _item_label(record)
    text = (
        f"For {person.name} — {item}\n"
        f"{title}\n"
        f"{url}\n\n"
        "Why this is worth the time\n"
        f"{draft.why_spend_time}\n\n"
        "Angle\n"
        f"{draft.angle_guidance}\n\n"
        "Where to pitch\n"
        f"{where}\n\n"
        "Draft pitch\n"
        f"Subject: {draft.pitch_subject}\n\n"
        f"{draft.pitch_body}\n\n"
        "Edit this before it goes to the outlet. Nothing has been sent to them.\n"
    )
    html = (
        "<!DOCTYPE html><html><body "
        'style="font-family: Georgia, serif; line-height: 1.5; color: #1a1a1a;">'
        f"<p>For {escape(person.name)} — {escape(item)}<br>"
        f"<strong>{escape(title)}</strong><br>"
        f'<a href="{escape(url)}">{escape(url)}</a></p>'
        f"<p><strong>Why this is worth the time</strong><br>{escape(draft.why_spend_time)}</p>"
        f"<p><strong>Angle</strong><br>{escape(draft.angle_guidance)}</p>"
        f"<p><strong>Where to pitch</strong><br>{_contacts_html(draft)}</p>"
        "<p><strong>Draft pitch</strong><br>"
        f"Subject: {escape(draft.pitch_subject)}</p>"
        f"<p>{escape(draft.pitch_body).replace(chr(10), '<br>')}</p>"
        "<p>Edit this before it goes to the outlet. Nothing has been sent to them.</p>"
        "</body></html>"
    )
    return subject, text, html


def render_linkedin(
    person: Spokesperson, record: object, draft: LinkedInDraft
) -> tuple[str, str, str]:
    subject = _mail_subject("LinkedIn draft", person.name, getattr(record, "title", ""))
    note = draft.note or "Edit the opening so it sounds like a moment from your own week."
    item = _item_label(record)
    title = getattr(record, "title", "")
    text = (
        f"Draft LinkedIn post for {person.name} — {item}\n"
        f"{title}\n\n"
        f"{note}\n\n"
        f"{draft.post}\n\n"
        "Edit this before posting. This was not published.\n"
    )
    html = (
        "<!DOCTYPE html><html><body "
        'style="font-family: Georgia, serif; line-height: 1.5; color: #1a1a1a;">'
        f"<p>Draft LinkedIn post for {escape(person.name)} — {escape(item)}<br>"
        f"<strong>{escape(title)}</strong></p>"
        f"<p>{escape(note)}</p>"
        f"<p>{escape(draft.post).replace(chr(10), '<br>')}</p>"
        "<p>Edit this before posting. This was not published.</p>"
        "</body></html>"
    )
    return subject, text, html


def delivery_blurb(to_line: str) -> str:
    mode = settings.delivery_mode.strip().lower()
    if mode == "graph" and to_line.strip():
        return f"It will be emailed to {to_line.strip()}."
    if mode == "stdout":
        return "It will be printed by the pipeline process."
    if mode == "none":
        return "Delivery is turned off, so it will not be sent."
    return "It will be written to the outbox."


def recipient_for(store: object, record: object) -> str:
    return _recipient(store, record)


def ingest_inbox(stores: list) -> int:
    """Read recent inbox replies and save numbered declines.

    Needs Mail.Read on the Graph app. Until that permission is granted, the
    No-Go page still saves reasons. A message is remembered after it matches
    a batch, or when it is not a decline reply, so the same mail is not
    applied twice.
    """
    global _inbox_note
    if not stores:
        return 0
    try:
        from pipeline.graph_mail import InboxUnavailable, list_recent_inbox

        messages = list_recent_inbox()
    except InboxUnavailable as exc:
        _note_once(str(exc))
        return 0
    except Exception as exc:  # noqa: BLE001 — the roundup timer still has to finish
        _note_once(f"Could not read decline replies: {exc}")
        return 0
    _inbox_note = None

    from pipeline.decisions import apply_decline_reply, parse_decline_reply

    applied = 0
    for message in messages:
        message_id = str(message.get("id") or "")
        if not message_id or any(store.message_seen(message_id) for store in stores):
            continue
        subject, body = _message_text(message)
        key, lines = parse_decline_reply(subject, body, require_reply_subject=True)
        matched = False
        if key and lines:
            for store in stores:
                if not any(store.get_batch_item(key, number) for number, _ in lines):
                    continue
                saved = apply_decline_reply(
                    store, subject, body, require_reply_subject=True
                )
                applied += len(saved)
                matched = True
                break
        if matched or not (key and lines):
            for store in stores:
                store.remember_message(message_id)
    return applied


def _note_once(message: str) -> None:
    global _inbox_note
    if _inbox_note == message:
        return
    logger.info("%s", message)
    _inbox_note = message


def _message_text(message: dict) -> tuple[str, str]:
    from pipeline.decisions import html_to_text

    subject = str(message.get("subject") or "")
    body = message.get("body") or {}
    if not isinstance(body, dict):
        return subject, str(body)
    content = str(body.get("content") or "")
    if str(body.get("contentType") or "").lower() == "html":
        content = html_to_text(content)
    return subject, content


def _open_stores() -> list:
    found = []
    if settings.db_path.exists():
        from pipeline.storage import LocalStore

        found.append(LocalStore(settings.db_path))
    try:
        from pipeline.provision import open_tenant_store
        from pipeline.registry import default_registry
    except Exception:
        return found
    for tenant in default_registry().list_tenants():
        if not tenant.provisioned_at:
            continue
        try:
            found.append(open_tenant_store(tenant))
        except Exception:
            logger.exception("Could not open the database for %s", tenant.slug)
    return found


def _person_for(store: object, record: object) -> Spokesperson:
    person = store.get_person(record.person_id)  # type: ignore[attr-defined]
    if person is not None:
        return person
    return Spokesperson(
        person_id=record.person_id,
        firm="",
        name=getattr(record, "person_name", "") or "the spokesperson",
    )


def _recipient(store: object, record: object) -> str:
    batch_key = getattr(record, "batch_key", "") or ""
    if batch_key:
        stored = store.batch_reply_to(batch_key)  # type: ignore[attr-defined]
        if stored.strip():
            return stored.strip()
    return settings.blast_to.strip()


def _deliver_followup(
    *,
    subject: str,
    text: str,
    html: str,
    to_line: str,
    outbox_dir: Path | None,
    slug: str,
) -> None:
    mode = settings.delivery_mode.strip().lower()
    if mode == "stdout":
        print(f"\n=== {subject} ===\n")
        print(text)
        return
    if mode == "none":
        raise RuntimeError("Delivery is disabled (DELIVERY_MODE=none).")
    directory = outbox_dir or settings.outbox_dir
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = directory / f"{stamp}_EMO-{slug}"
    header = f"To: {to_line or '(no recipient)'}\nSubject: {subject}\n\n"
    Path(str(base) + ".txt").write_text(header + text, encoding="utf-8")
    Path(str(base) + ".html").write_text(html, encoding="utf-8")
    if mode != "graph":
        return
    recipients = [
        part.strip() for part in to_line.replace(";", ",").split(",") if part.strip()
    ]
    if not recipients:
        raise RuntimeError("No recipient is saved for this roundup.")
    from pipeline.graph_mail import send_roundup_email

    send_roundup_email(subject=subject, html_body=html, to_addrs=recipients)


def _stub_pitch(person: Spokesperson, record: object) -> PitchPackage:
    angle = getattr(record, "suggested_angle", "") or ""
    why = getattr(record, "why_relevant", "") or f"This lines up with {person.name}'s work."
    outlet = getattr(record, "outlet", "") or "the outlet"
    who = person.name
    role = f", {person.title}" if person.title else ""
    firm = f" at {person.firm}" if person.firm else ""
    return PitchPackage(
        why_spend_time=why,
        angle_guidance=angle or "Use a specific result from their own work.",
        contacts=[],
        contact_note=(
            "Dry run: no editor inboxes were searched. Use the outlet's staff page, "
            "and do not guess an address."
        ),
        pitch_subject=f"Story idea for {outlet}: {getattr(record, 'title', '')}",
        pitch_body=(
            f"Hello,\n\n"
            f"I'm writing on behalf of {who}{role}{firm}.\n\n"
            f"{angle or why}\n\n"
            f"{who} can speak to this from direct experience.\n"
        ),
    )


def _stub_linkedin(person: Spokesperson, record: object) -> LinkedInDraft:
    angle = getattr(record, "suggested_angle", "") or getattr(record, "why_relevant", "")
    return LinkedInDraft(
        post=(
            f"A question I keep hearing: {getattr(record, 'title', '')}.\n\n"
            f"{angle or 'Here is the practitioner view.'}\n\n"
            "If this is on your desk too, I am glad to compare notes."
        ),
        note=f"Edit the opening so it sounds like a week in {person.name}'s work.",
    )


def _pitch_system() -> str:
    return (
        "You prepare a pitch package for one spokesperson. "
        "Use web search to find public editor, reporter, producer, or organizer "
        "emails at the outlet. Copy an email only from a page you opened, and "
        "include that page as source_url. Never invent an address, a name, or a URL. "
        "If you cannot find a public email, leave contacts empty and say where a "
        "person should look. Respond with JSON only — no markdown fences."
    )


def _pitch_user(person: Spokesperson, record: object) -> str:
    kind = (getattr(record, "opportunity_type", "") or "other").strip().lower()
    ask = {
        "podcast": "the host or the person who books guests",
        "speaking": "the organizer or the CFP contact",
        "journalist_query": "the reporter who posted the query",
        "press": "the editor or reporter who covers this beat",
        "backlink": "the editor who accepts contributed pieces",
    }.get(kind, "the person who would receive the pitch")
    return f"""Build a pitch package for this opportunity.

Spokesperson: {person.name}
Title: {person.title or "n/a"}
Firm: {person.firm or "n/a"}
Topics: {person.topics or "n/a"}
Bio: {(person.bio or "n/a")[:800]}

Opportunity: {getattr(record, "title", "")}
Outlet: {getattr(record, "outlet", "") or "n/a"}
Type: {kind}
URL: {getattr(record, "url", "")}
Why it was suggested: {getattr(record, "why_relevant", "") or "n/a"}
Suggested angle: {getattr(record, "suggested_angle", "") or "n/a"}

Find {ask}. Return JSON:
{{
  "why_spend_time": "1-2 sentences on why this is worth their time",
  "angle_guidance": "how to aim the story, in 2-4 sentences",
  "contacts": [
    {{
      "name": "",
      "role": "",
      "email": "",
      "outlet": "",
      "source_url": "https://page-where-the-email-was-listed"
    }}
  ],
  "contact_note": "where to look if no public email turned up",
  "pitch_subject": "subject line the spokesperson can edit",
  "pitch_body": "the pitch itself, ready to edit, signed as them"
}}
"""


def _linkedin_system() -> str:
    return (
        "You draft a LinkedIn post the spokesperson will edit and publish "
        "themselves. Write in first person, as them, about the idea — not a "
        "post announcing that they were featured. 80 to 160 words. No hashtags "
        "and no emoji. Respond with JSON only — no markdown fences."
    )


def _linkedin_user(person: Spokesperson, record: object) -> str:
    return f"""Draft a LinkedIn post for {person.name}.

Title: {person.title or "n/a"}
Firm: {person.firm or "n/a"}
Topics: {person.topics or "n/a"}
Bio: {(person.bio or "n/a")[:800]}

Idea: {getattr(record, "title", "")}
Outlet context: {getattr(record, "outlet", "") or "n/a"}
Why it fits: {getattr(record, "why_relevant", "") or "n/a"}
Angle: {getattr(record, "suggested_angle", "") or "n/a"}
Link they can mention if they want: {getattr(record, "url", "")}

Return JSON:
{{
  "post": "the post",
  "note": "one sentence on what they may want to personalize"
}}
"""


def _complete(system: str, user: str, *, search: bool) -> str:
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError("anthropic package required; pip install anthropic") from exc

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    messages: list[dict] = [{"role": "user", "content": user}]
    kwargs: dict = {
        "model": settings.claude_model,
        "max_tokens": min(settings.max_tokens, 2500),
        "system": system,
    }
    if search:
        kwargs["tools"] = [_web_search_tool()]
    last = None
    for _ in range(4):
        last = client.messages.create(**kwargs, messages=messages)
        if not search or getattr(last, "stop_reason", None) != "pause_turn":
            break
        dumped = last.model_dump(mode="json")
        messages.append({"role": "assistant", "content": dumped.get("content", [])})
    if last is None:
        raise RuntimeError("Claude returned no draft")
    text = _extract_text(last)
    if not text:
        raise RuntimeError("Claude returned an empty draft")
    return text


def _pitch_from_model(raw: str, person: Spokesperson, record: object) -> PitchPackage:
    data = _parse_json_payload(raw)
    if "pitch_body" not in data and "why_spend_time" not in data:
        note = "The draft did not come back in the expected shape. Check it before sending."
        return PitchPackage(
            why_spend_time=getattr(record, "why_relevant", "") or "",
            angle_guidance=getattr(record, "suggested_angle", "") or "",
            contact_note=note,
            pitch_subject=_mail_subject("Story idea", person.name, getattr(record, "title", "")),
            pitch_body=raw.strip()[:4000],
        )
    return PitchPackage(
        why_spend_time=str(data.get("why_spend_time") or "").strip()
        or (getattr(record, "why_relevant", "") or ""),
        angle_guidance=str(data.get("angle_guidance") or "").strip()
        or (getattr(record, "suggested_angle", "") or ""),
        contacts=_clean_contacts(data.get("contacts")),
        contact_note=str(data.get("contact_note") or "").strip(),
        pitch_subject=str(data.get("pitch_subject") or "").strip()
        or f"Story idea: {getattr(record, 'title', '')}",
        pitch_body=str(data.get("pitch_body") or "").strip() or raw.strip()[:4000],
    )


def _clean_contacts(raw: object) -> list[Contact]:
    if not isinstance(raw, list):
        return []
    contacts: list[Contact] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source_url") or "").strip()
        if not source.startswith(("http://", "https://")):
            source = ""
        email = _usable_email(str(item.get("email") or "")) if source else ""
        name = str(item.get("name") or "").strip()
        role = str(item.get("role") or "").strip()
        outlet = str(item.get("outlet") or "").strip()
        if not any((name, role, email, source)):
            continue
        contacts.append(
            Contact(name=name, role=role, email=email, outlet=outlet, source_url=source)
        )
    return contacts


def _usable_email(value: str) -> str:
    email = value.strip().strip("<>").lower()
    if not _EMAIL.match(email):
        return ""
    host = email.split("@", 1)[1]
    if host in _PLACEHOLDER_HOSTS or host.endswith(".example"):
        return ""
    return email


def _contacts_text(draft: PitchPackage) -> str:
    if not draft.contacts:
        note = draft.contact_note or (
            "No public email turned up. Start from the opportunity link and the "
            "outlet's staff page. Do not guess an address."
        )
        return note
    lines = []
    for contact in draft.contacts:
        who = ", ".join(part for part in (contact.name, contact.role, contact.outlet) if part)
        if contact.email:
            who = f"{who} — {contact.email}" if who else contact.email
        if contact.source_url:
            who = f"{who}\n  Source: {contact.source_url}"
        if who:
            lines.append(f"- {who}")
    if draft.contact_note:
        lines.append(draft.contact_note)
    return "\n".join(lines) if lines else draft.contact_note


def _contacts_html(draft: PitchPackage) -> str:
    if not draft.contacts:
        note = draft.contact_note or (
            "No public email turned up. Start from the opportunity link and the "
            "outlet's staff page. Do not guess an address."
        )
        return escape(note)
    items = []
    for contact in draft.contacts:
        who = ", ".join(part for part in (contact.name, contact.role, contact.outlet) if part)
        bits = escape(who)
        if contact.email:
            bits = f"{bits} — {escape(contact.email)}" if bits else escape(contact.email)
        if contact.source_url:
            bits += f'<br>Source: <a href="{escape(contact.source_url)}">{escape(contact.source_url)}</a>'
        if bits:
            items.append(f"<li>{bits}</li>")
    note = f"<br>{escape(draft.contact_note)}" if draft.contact_note else ""
    return f"<ul>{''.join(items)}</ul>{note}" if items else escape(draft.contact_note)


def _item_label(record: object) -> str:
    number = int(getattr(record, "item_number", 0) or 0)
    if number:
        return f"item {number} in the roundup"
    return "from the roundup"


def _mail_subject(prefix: str, person_name: str, title: str) -> str:
    raw = f"{prefix} for {person_name}: {title}".strip()
    if len(raw) <= 140:
        return raw
    return raw[:137].rstrip() + "..."

"""LinkedIn drafts emailed after Make it LinkedIn.

Opening the link in the roundup does not call the model. The page button
queues the draft. Locally a background thread writes it. On Azure the minute
timer does, because the HTTP request would be closed before Claude finished.
Nothing is published to LinkedIn.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from pipeline.config import settings
from pipeline.models import Spokesperson
from pipeline.research import _extract_text, _parse_json_payload

logger = logging.getLogger("earned-media.followup")


@dataclass
class LinkedInDraft:
    post: str
    note: str = ""


def execution_mode() -> str:
    """thread (local), inline (tests), or defer (Azure HTTP queues for the timer)."""
    forced = os.getenv("DASHBOARD_EXECUTION", "").strip().lower()
    if forced in {"inline", "thread", "defer"}:
        return forced
    if os.getenv("FUNCTIONS_WORKER_RUNTIME"):
        return "defer"
    return "thread"


def enqueue_followup(store: object, token: str, kind: str) -> str:
    """Queue a draft. Starts it unless this process only queues for the timer."""
    job_id = store.enqueue_action(token, kind)  # type: ignore[attr-defined]
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
) -> None:
    """Write one queued LinkedIn draft and mark the job sent or error."""
    job = store.claim_action(job_id)  # type: ignore[attr-defined]
    if job is None:
        return
    try:
        record = store.get_feedback(job.token)  # type: ignore[attr-defined]
        if record is None:
            raise RuntimeError("That opportunity is no longer on file.")
        person = _person_for(store, record)
        use_dry = settings.dry_run_research if dry_run is None else dry_run
        draft = draft_linkedin(person, record, dry_run=use_dry)
        subject, text, html = render_linkedin(person, record, draft)
        _deliver_followup(
            subject=subject,
            text=text,
            html=html,
            outbox_dir=outbox_dir,
        )
    except Exception as exc:  # noqa: BLE001 — the job row has to show the failure
        logger.exception("Follow-up %s failed", job_id)
        store.finish_action(job_id, status="error", error=str(exc))  # type: ignore[attr-defined]
        return
    store.finish_action(job_id, status="sent")  # type: ignore[attr-defined]


def process_pending_followups() -> None:
    """Send LinkedIn drafts that are still queued."""
    from pipeline.storage import open_store

    store = open_store()
    try:
        pending = store.queued_actions()
    except Exception:
        logger.exception("Follow-up queue failed")
        return
    for job in pending:
        process_action(store, job.job_id)


def draft_linkedin(
    person: Spokesperson, record: object, *, dry_run: bool = False
) -> LinkedInDraft:
    if dry_run:
        return _stub_linkedin(person, record)
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is missing, so the LinkedIn draft could not be written."
        )
    raw = _complete(_linkedin_system(), _linkedin_user(person, record))
    data = _parse_json_payload(raw)
    post = str(data.get("post") or "").strip()
    if not post:
        post = raw.strip() or "A draft did not come back. Try Make it LinkedIn again."
    note = str(data.get("note") or "").strip()
    return LinkedInDraft(post=post, note=note)


def render_linkedin(
    person: Spokesperson, record: object, draft: LinkedInDraft
) -> tuple[str, str, str]:
    subject = _mail_subject("LinkedIn draft", person.name, getattr(record, "title", ""))
    note = draft.note or "Edit the opening so it sounds like a moment from your own week."
    title = getattr(record, "title", "")
    text = (
        f"Draft LinkedIn post for {person.name}\n"
        f"{title}\n\n"
        f"{note}\n\n"
        f"{draft.post}\n\n"
        "Edit this before posting. This was not published.\n"
    )
    html = (
        "<!DOCTYPE html><html><body "
        'style="font-family: Georgia, serif; line-height: 1.5; color: #1a1a1a;">'
        f"<p>Draft LinkedIn post for {escape(person.name)}<br>"
        f"<strong>{escape(title)}</strong></p>"
        f"<p>{escape(note)}</p>"
        f"<p>{escape(draft.post).replace(chr(10), '<br>')}</p>"
        "<p>Edit this before posting. This was not published.</p>"
        "</body></html>"
    )
    return subject, text, html


def delivery_blurb() -> str:
    mode = settings.delivery_mode.strip().lower()
    to_line = settings.blast_to.strip()
    if mode == "graph" and to_line:
        return f"It will be emailed to {to_line}."
    if mode == "stdout":
        return "It will be printed by the pipeline process."
    if mode == "none":
        return "Delivery is turned off, so it will not be sent."
    return "It will be written to the outbox."


def _person_for(store: object, record: object) -> Spokesperson:
    person = store.get_person(record.person_id)  # type: ignore[attr-defined]
    if person is not None:
        return person
    return Spokesperson(
        person_id=record.person_id,
        firm="",
        name=getattr(record, "person_name", "") or "the spokesperson",
    )


def _deliver_followup(
    *,
    subject: str,
    text: str,
    html: str,
    outbox_dir: Path | None,
) -> None:
    mode = settings.delivery_mode.strip().lower()
    to_line = settings.blast_to.strip()
    if mode == "stdout":
        print(f"\n=== {subject} ===\n")
        print(text)
        return
    if mode == "none":
        raise RuntimeError("Delivery is disabled (DELIVERY_MODE=none).")
    directory = outbox_dir or settings.outbox_dir
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = directory / f"{stamp}_EMO-LinkedIn"
    header = f"To: {to_line or '(no recipient)'}\nSubject: {subject}\n\n"
    Path(str(base) + ".txt").write_text(header + text, encoding="utf-8")
    Path(str(base) + ".html").write_text(html, encoding="utf-8")
    if mode != "graph":
        return
    if not to_line:
        raise RuntimeError("No recipient is saved for this roundup.")
    from pipeline.graph_mail import send_roundup_email

    send_roundup_email(subject=subject, html_body=html)


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


def _complete(system: str, user: str) -> str:
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError("anthropic package required; pip install anthropic") from exc

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = client.messages.create(
        model=settings.claude_model,
        max_tokens=min(settings.max_tokens, 2500),
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    text = _extract_text(response)
    if not text:
        raise RuntimeError("Claude returned an empty draft")
    return text


def _mail_subject(prefix: str, person_name: str, title: str) -> str:
    raw = f"{prefix} for {person_name}: {title}".strip()
    if len(raw) <= 140:
        return raw
    return raw[:137].rstrip() + "..."

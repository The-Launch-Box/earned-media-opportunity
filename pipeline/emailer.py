"""Delivery: outbox files, plus optional Microsoft Graph send."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console

from pipeline.config import settings
from pipeline.models import Roundup

console = Console()


@dataclass
class DeliveryResult:
    mode: str
    path: Path | None
    skipped: bool = False
    mailed: bool = False
    reason: str = ""


def deliver(
    roundup: Roundup,
    *,
    blast_to: str | None = None,
    outbox_dir: Path | None = None,
    delivery_mode: str | None = None,
    file_slug: str = "",
) -> DeliveryResult:
    """Send one roundup.

    `blast_to=None` uses the global BLAST_TO. A company run passes its own
    recipient, including an empty string, so it never falls back to another
    company's address.
    """
    mode = (delivery_mode if delivery_mode is not None else settings.delivery_mode).strip().lower()
    to_line = settings.blast_to if blast_to is None else blast_to.strip()

    if mode == "none":
        return DeliveryResult(
            mode=mode, path=None, skipped=True, reason="delivery disabled"
        )

    if mode == "stdout":
        print(f"\n=== {roundup.subject} ===\n")
        print(roundup.body_text)
        return DeliveryResult(mode=mode, path=None)

    text_path = _write_outbox(
        roundup,
        to_line=to_line,
        outbox_dir=outbox_dir,
        file_slug=file_slug,
    )

    if mode != "graph":
        return DeliveryResult(mode="outbox", path=text_path)

    recipients = _split_recipients(to_line)
    if not recipients:
        return DeliveryResult(
            mode="graph",
            path=text_path,
            mailed=False,
            reason="No recipient",
        )

    try:
        from pipeline.graph_mail import send_roundup_email

        send_roundup_email(
            subject=roundup.subject,
            html_body=roundup.body_html,
            to_addrs=recipients,
        )
    except Exception as exc:  # noqa: BLE001 — surface Graph errors without crashing research
        console.print(f"  [red]Graph send failed:[/] {exc}")
        return DeliveryResult(
            mode="graph",
            path=text_path,
            mailed=False,
            reason=str(exc),
        )

    console.print(
        f"  [green]sent[/] {settings.mail_from} → {', '.join(recipients)}"
    )
    return DeliveryResult(mode="graph", path=text_path, mailed=True)


def _split_recipients(raw: str) -> list[str]:
    return [part.strip() for part in raw.replace(";", ",").split(",") if part.strip()]


def _write_outbox(
    roundup: Roundup,
    *,
    to_line: str,
    outbox_dir: Path | None = None,
    file_slug: str = "",
) -> Path:
    directory = outbox_dir or settings.outbox_dir
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    slug = f"_{file_slug}" if file_slug else ""
    base = directory / f"{stamp}{slug}_EMO-Roundup"

    text_path = Path(str(base) + ".txt")
    html_path = Path(str(base) + ".html")
    meta_path = Path(str(base) + ".json")

    to_line = to_line or "(no blast recipient — local outbox only)"
    from_line = settings.mail_from or settings.from_name
    header = (
        f"To: {to_line}\n"
        f"From: {from_line}\n"
        f"Subject: {roundup.subject}\n"
        f"People: {len(roundup.people)}\n"
        f"Opportunities: {len(roundup.opportunities)}\n"
        f"Urgent: {roundup.urgent_count}\n"
        f"{'=' * 60}\n\n"
    )
    text_path.write_text(header + roundup.body_text, encoding="utf-8")
    html_path.write_text(roundup.body_html, encoding="utf-8")
    meta_path.write_text(
        json.dumps(
            {
                "to": None if to_line.startswith("(no blast recipient") else to_line,
                "from": settings.mail_from or None,
                "subject": roundup.subject,
                "people": [
                    {"person_id": p.person_id, "firm": p.firm, "name": p.name}
                    for p in roundup.people
                ],
                "new_people": roundup.new_people,
                "opportunity_count": len(roundup.opportunities),
                "urgent_count": roundup.urgent_count,
                "fingerprints": [o.fingerprint for o in roundup.opportunities],
                "urls": [o.url for o in roundup.opportunities],
                "sites": [
                    {
                        "url": o.url,
                        "domain": o.site.domain,
                        "monthly_visits": o.site.monthly_visits,
                        "visits_kind": o.site.visits_kind,
                        "authority_score": o.site.authority_score,
                        "legitimacy": o.site.legitimacy,
                    }
                    for o in roundup.opportunities
                    if o.site
                ],
                "generated_on": roundup.generated_on,
                "batch_key": roundup.batch_key,
                "items": [
                    {
                        "number": opp.item_number,
                        "title": opp.title,
                        "fingerprint": opp.fingerprint,
                    }
                    for opp in roundup.opportunities
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return text_path

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


def deliver(roundup: Roundup) -> DeliveryResult:
    mode = settings.delivery_mode.strip().lower()

    if mode == "none":
        return DeliveryResult(
            mode=mode, path=None, skipped=True, reason="delivery disabled"
        )

    if mode == "stdout":
        print(f"\n=== {roundup.subject} ===\n")
        print(roundup.body_text)
        return DeliveryResult(mode=mode, path=None)

    text_path = _write_outbox(roundup)

    if mode != "graph":
        return DeliveryResult(mode="outbox", path=text_path)

    try:
        from pipeline.graph_mail import send_roundup_email

        send_roundup_email(subject=roundup.subject, html_body=roundup.body_html)
    except Exception as exc:  # noqa: BLE001 — surface Graph errors without crashing research
        console.print(f"  [red]Graph send failed:[/] {exc}")
        return DeliveryResult(
            mode="graph",
            path=text_path,
            mailed=False,
            reason=str(exc),
        )

    console.print(
        f"  [green]sent[/] {settings.mail_from} → {settings.blast_to}"
    )
    return DeliveryResult(mode="graph", path=text_path, mailed=True)


def _write_outbox(roundup: Roundup) -> Path:
    settings.outbox_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = settings.outbox_dir / f"{stamp}_EMO-Roundup"

    text_path = Path(str(base) + ".txt")
    html_path = Path(str(base) + ".html")
    meta_path = Path(str(base) + ".json")

    to_line = settings.blast_to or "(no blast recipient — local outbox only)"
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
                "to": settings.blast_to or None,
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
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return text_path

"""Team earned-media blast in the Example_e-mail_blast.md format."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from html import escape

from pipeline.config import settings
from pipeline.feedback import feedback_href
from pipeline.models import Opportunity, Roundup, Spokesperson

_LEGITIMACY_COLOR = {
    "high": "#0d6b3d",
    "medium": "#8a5a00",
    "low": "#9b1c1c",
}


@dataclass
class PersonSection:
    person: Spokesperson
    opportunities: list[Opportunity] = field(default_factory=list)
    is_new: bool = False


def build_roundup(
    sections: list[PersonSection],
    *,
    generated_on: date | None = None,
    feedback_base_url: str | None = None,
) -> Roundup:
    """
    One blast for the whole roster:

        Subject: Earned Media Opportunities Roundup [DATE]

        Time-Sensitive
        **Name**
        …

        Other
        **Name**
        …
    """
    today = generated_on or date.today()
    date_label = today.strftime("%B %-d, %Y")
    subject = f"Earned Media Opportunities Roundup [{date_label}]"

    ordered = _with_newcomers_last(sections)
    people = [s.person for s in ordered]
    new_names = [s.person.name for s in ordered if s.is_new]
    all_opps = [o for s in ordered for o in s.opportunities]
    urgent_count = sum(
        1 for s in ordered for o in s.opportunities if o.is_urgent(today=today)
    )

    feedback_base = (
        settings.feedback_public_url if feedback_base_url is None else feedback_base_url
    ).rstrip("/")
    body_text = _text_body(ordered, date_label, new_names, today, feedback_base)
    body_html = _html_body(ordered, date_label, new_names, today, feedback_base)

    return Roundup(
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        opportunities=all_opps,
        people=people,
        new_people=new_names,
        urgent_count=urgent_count,
        generated_on=today.isoformat(),
    )


def _with_newcomers_last(sections: list[PersonSection]) -> list[PersonSection]:
    existing = [s for s in sections if not s.is_new]
    newcomers = [s for s in sections if s.is_new]
    return existing + newcomers


def _split(
    section: PersonSection, today: date
) -> tuple[list[Opportunity], list[Opportunity]]:
    urgent: list[Opportunity] = []
    other: list[Opportunity] = []
    for opp in section.opportunities:
        if opp.is_urgent(today=today):
            urgent.append(opp)
        else:
            other.append(opp)
    return urgent, other


def _text_body(
    sections: list[PersonSection],
    date_label: str,
    new_names: list[str],
    today: date,
    feedback_base: str,
) -> str:
    lines = [
        f"Subject: Earned Media Opportunities Roundup [{date_label}]",
        "",
    ]
    if new_names:
        lines.append(
            "New on the roster: "
            + ", ".join(new_names)
            + ". Their opportunities are included below."
        )
        lines.append("")

    time_sensitive = [
        (s.person, opps)
        for s in sections
        if (opps := _split(s, today)[0])
    ]
    other = [
        (s.person, opps)
        for s in sections
        if (opps := _split(s, today)[1])
    ]

    if time_sensitive:
        lines.append("Time-Sensitive")
        for person, opps in time_sensitive:
            lines.append(f"**{person.name}**")
            for opp in opps:
                lines.extend(_item_lines(opp, feedback_base))
            lines.append("")

    if other:
        lines.append("Other")
        for person, opps in other:
            lines.append(f"**{person.name}**")
            for opp in opps:
                lines.extend(_item_lines(opp, feedback_base))
            lines.append("")

    if not time_sensitive and not other:
        lines.append("No new earned-media opportunities today.")
        lines.append("")

    if _has_site_metrics(sections):
        lines.append(
            "Site metrics: Semrush monthly visits and Authority Score. "
            "High legitimacy = AS 50+, Medium = 30–49, Low = below 30."
        )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _item_lines(opp: Opportunity, feedback_base: str) -> list[str]:
    due = f" ({opp.deadline})" if opp.deadline else ""
    lines = [f"• {opp.title}{due}"]
    if opp.outlet:
        lines.append(f"  Outlet: {opp.outlet} ({opp.opportunity_type})")
    else:
        lines.append(f"  Type: {opp.opportunity_type}")
    lines.append(f"  Link: {opp.url}")
    if opp.site:
        lines.append(f"  {opp.site.summary_line()}")
    if opp.why_relevant:
        lines.append(f"  Why: {opp.why_relevant}")
    if opp.suggested_angle:
        lines.append(f"  Angle: {opp.suggested_angle}")
    lines.extend(_feedback_text(opp, feedback_base))
    return lines


def _feedback_text(opp: Opportunity, feedback_base: str) -> list[str]:
    if not feedback_base or not opp.feedback_token:
        return []
    yes = feedback_href(feedback_base, opp.feedback_token, "yes")
    no = feedback_href(feedback_base, opp.feedback_token, "no")
    return [
        "  Is this a worthwhile opportunity?",
        f"  Yes: {yes}",
        f"  No: {no}",
    ]


def _html_body(
    sections: list[PersonSection],
    date_label: str,
    new_names: list[str],
    today: date,
    feedback_base: str,
) -> str:
    chunks: list[str] = [
        "<!DOCTYPE html>",
        '<html><body style="font-family: Georgia, serif; line-height: 1.5; color: #1a1a1a;">',
        f"<p><strong>Subject: Earned Media Opportunities Roundup [{escape(date_label)}]</strong></p>",
    ]
    if new_names:
        names = ", ".join(escape(n) for n in new_names)
        chunks.append(
            f"<p>New on the roster: {names}. Their opportunities are included below.</p>"
        )

    time_sensitive = [
        (s.person, opps)
        for s in sections
        if (opps := _split(s, today)[0])
    ]
    other = [
        (s.person, opps)
        for s in sections
        if (opps := _split(s, today)[1])
    ]

    if time_sensitive:
        chunks.append("<p>Time-Sensitive</p>")
        for person, opps in time_sensitive:
            chunks.append(f"<p><strong>{escape(person.name)}</strong></p>")
            chunks.append(_items_html(opps, feedback_base))

    if other:
        chunks.append("<p>Other</p>")
        for person, opps in other:
            chunks.append(f"<p><strong>{escape(person.name)}</strong></p>")
            chunks.append(_items_html(opps, feedback_base))

    if not time_sensitive and not other:
        chunks.append("<p>No new earned-media opportunities today.</p>")

    if _has_site_metrics(sections):
        chunks.append(
            "<p style=\"font-size: 0.9em; color: #555;\">"
            "Site metrics: Semrush monthly visits and Authority Score. "
            "High legitimacy = AS 50+, Medium = 30–49, Low = below 30."
            "</p>"
        )

    chunks.append("</body></html>")
    return "\n".join(chunks)


def _items_html(opportunities: list[Opportunity], feedback_base: str) -> str:
    items: list[str] = []
    for opp in opportunities:
        due = f" ({escape(opp.deadline)})" if opp.deadline else ""
        meta = (
            f"Outlet: {escape(opp.outlet)} ({escape(opp.opportunity_type)})"
            if opp.outlet
            else f"Type: {escape(opp.opportunity_type)}"
        )
        extra = ""
        if opp.site:
            extra += f"<br>{_site_html(opp)}"
        if opp.why_relevant:
            extra += f"<br>Why: {escape(opp.why_relevant)}"
        if opp.suggested_angle:
            extra += f"<br>Angle: {escape(opp.suggested_angle)}"
        extra += _feedback_html(opp, feedback_base)
        items.append(
            "<li>"
            f"{escape(opp.title)}{due}<br>"
            f"{meta}<br>"
            f'<a href="{escape(opp.url)}">{escape(opp.url)}</a>'
            f"{extra}"
            "</li>"
        )
    return f"<ul>{''.join(items)}</ul>"


def _feedback_html(opp: Opportunity, feedback_base: str) -> str:
    if not feedback_base or not opp.feedback_token:
        return ""
    yes = escape(feedback_href(feedback_base, opp.feedback_token, "yes"))
    no = escape(feedback_href(feedback_base, opp.feedback_token, "no"))
    button = (
        "display:inline-block;padding:6px 14px;color:#ffffff;"
        "text-decoration:none;border-radius:4px;font-family:Georgia,serif;"
    )
    return (
        '<br><span style="display:inline-block;margin-top:8px;">'
        "Is this a worthwhile opportunity?</span><br>"
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:6px;">'
        "<tr>"
        '<td style="padding-right:8px;">'
        f'<a href="{yes}" style="{button}background:#0d6b3d;">Yes</a>'
        "</td><td>"
        f'<a href="{no}" style="{button}background:#9b1c1c;">No</a>'
        "</td></tr></table>"
    )


def _has_site_metrics(sections: list[PersonSection]) -> bool:
    return any(opp.site for s in sections for opp in s.opportunities)


def _site_html(opp: Opportunity) -> str:
    site = opp.site
    if site is None:
        return ""
    if site.error and site.legitimacy == "unknown" and site.monthly_visits is None:
        domain = escape(site.domain) if site.domain else "this outlet"
        return f"Site: no Semrush data for {domain}"
    color = _LEGITIMACY_COLOR.get(site.legitimacy, "#555")
    return (
        f"Site: {escape(site.visits_display())} · "
        f'<span style="color:{color}">{escape(site.legitimacy_display())}</span>'
    )

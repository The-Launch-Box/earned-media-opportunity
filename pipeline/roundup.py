"""Team earned-media blast in the Example_e-mail_blast.md format."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from html import escape

from pipeline.config import settings
from pipeline.decisions import batch_key_for
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


@dataclass
class ResearchNotice:
    """Model line, and the Claude credit notice when that balance is exhausted."""

    model: str = ""
    credit_code: str = ""
    research_incomplete: bool = False


def build_roundup(
    sections: list[PersonSection],
    *,
    generated_on: date | None = None,
    feedback_base_url: str | None = None,
    notice: ResearchNotice | None = None,
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

    ordered = _with_newcomers_last(sections)
    numbered = _number_in_email_order(ordered, today)
    batch_key = batch_key_for(
        today.isoformat(),
        [opp.fingerprint for opp in numbered],
    )
    for opp in numbered:
        opp.batch_key = batch_key
    subject = f"Earned Media Opportunities Roundup [{date_label}]"
    if batch_key:
        subject = f"{subject} [{batch_key}]"

    people = [s.person for s in ordered]
    new_names = [s.person.name for s in ordered if s.is_new]
    urgent_count = sum(1 for opp in numbered if opp.is_urgent(today=today))

    feedback_base = (
        settings.feedback_public_url if feedback_base_url is None else feedback_base_url
    ).rstrip("/")
    body_text = _text_body(
        ordered, subject, new_names, today, feedback_base, batch_key, notice
    )
    body_html = _html_body(
        ordered, subject, new_names, today, feedback_base, batch_key, notice
    )

    return Roundup(
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        opportunities=numbered,
        people=people,
        new_people=new_names,
        urgent_count=urgent_count,
        generated_on=today.isoformat(),
        batch_key=batch_key,
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


def _number_in_email_order(
    sections: list[PersonSection], today: date
) -> list[Opportunity]:
    """Time-sensitive items first, then the rest, in the order the email shows them."""
    flat: list[Opportunity] = []
    for bucket in (0, 1):
        for section in sections:
            flat.extend(_split(section, today)[bucket])
    for number, opp in enumerate(flat, start=1):
        opp.item_number = number
    return flat


def _notice_lines(notice: ResearchNotice | None) -> list[str]:
    if notice is None or not (notice.model or notice.credit_code):
        return []
    lines: list[str] = []
    if notice.model:
        lines.append(f"Research model: {notice.model}")
    if notice.credit_code:
        lines.append(_credit_line(notice))
    return lines


def _credit_line(notice: ResearchNotice) -> str:
    line = (
        f"Claude credits are exhausted (error {notice.credit_code}). "
        "Contact your Claude administrator to restore access. "
        "Functionality will resume when the Claude balance is restored. "
    )
    if notice.research_incomplete:
        return (
            line
            + "After the balance is restored in the Anthropic admin console, "
            "run the pipeline again to redo this day's run."
        )
    return (
        line
        + "This run continued on the model named above. "
        "After the balance is restored in the Anthropic admin console, "
        "run the pipeline again to redo this day's run on Claude. "
        "Opportunities already listed below stay on the sent list."
    )


def _empty_day_line(notice: ResearchNotice | None) -> str:
    if notice is not None and notice.credit_code and notice.research_incomplete:
        return "This day's research did not run."
    return "No new earned-media opportunities today."


def _text_body(
    sections: list[PersonSection],
    subject: str,
    new_names: list[str],
    today: date,
    feedback_base: str,
    batch_key: str,
    notice: ResearchNotice | None = None,
) -> str:
    lines = [
        f"Subject: {subject}",
        "",
    ]
    notice_lines = _notice_lines(notice)
    if notice_lines:
        lines.extend(notice_lines)
        lines.append("")
    if batch_key:
        lines.append(f"Batch {batch_key}")
        lines.append("")
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
        lines.append(_empty_day_line(notice))
        lines.append("")

    if _has_radar(sections):
        lines.append(_RADAR_NOTE)
        lines.append("")
    if _has_site_metrics(sections):
        lines.append(
            "Site metrics: Semrush monthly visits and Authority Score. "
            "High legitimacy = AS 50+, Medium = 30–49, Low = below 30."
        )
        lines.append("")
    if _has_podcast_reach(sections):
        lines.append(
            "Podcast reach: estimated weekly downloads from Rephonic "
            "for shows found on Spotify. Spotify's own API does not publish listener counts."
        )
        lines.append("")
    if batch_key:
        lines.append(
            "To pass on an item, reply to this email. Each line starts with that "
            "item's number, then a period, then the reason you are passing. "
            "The reason is what the next search uses, so name the outlet, the format, or the fit."
        )
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def _item_lines(opp: Opportunity, feedback_base: str) -> list[str]:
    due = f" ({opp.deadline})" if opp.deadline else ""
    number = f"{opp.item_number}. " if opp.item_number else ""
    lines = [f"{number}{opp.title}{due}"]
    if opp.outlet:
        lines.append(f"  Outlet: {opp.outlet} ({opp.opportunity_type})")
    else:
        lines.append(f"  Type: {opp.opportunity_type}")
    lines.append(f"  Link: {opp.url}")
    if opp.radar and (rank := opp.radar.summary_line()):
        lines.append(f"  {rank}")
    if opp.site:
        lines.append(f"  {opp.site.summary_line()}")
    if opp.podcast and (reach := opp.podcast.summary_line()):
        lines.append(f"  {reach}")
    if opp.why_relevant:
        lines.append(f"  Why: {opp.why_relevant}")
    if opp.suggested_angle:
        lines.append(f"  Angle: {opp.suggested_angle}")
    lines.extend(_feedback_text(opp, feedback_base))
    return lines


def _feedback_text(opp: Opportunity, feedback_base: str) -> list[str]:
    if not feedback_base or not opp.feedback_token:
        return []
    go = feedback_href(feedback_base, opp.feedback_token, "go")
    nogo = feedback_href(feedback_base, opp.feedback_token, "nogo")
    linkedin = feedback_href(feedback_base, opp.feedback_token, "linkedin")
    return [
        "  What should we do with this?",
        f"  Go: {go}",
        f"  No-Go: {nogo}",
        f"  Make it LinkedIn: {linkedin}",
    ]


def _html_body(
    sections: list[PersonSection],
    subject: str,
    new_names: list[str],
    today: date,
    feedback_base: str,
    batch_key: str,
    notice: ResearchNotice | None = None,
) -> str:
    chunks: list[str] = [
        "<!DOCTYPE html>",
        '<html><body style="font-family: Georgia, serif; line-height: 1.5; color: #1a1a1a;">',
        f"<p><strong>Subject: {escape(subject)}</strong></p>",
    ]
    for line in _notice_lines(notice):
        chunks.append(f"<p>{escape(line)}</p>")
    if batch_key:
        chunks.append(f"<p>Batch {escape(batch_key)}</p>")
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
        chunks.append(f"<p>{escape(_empty_day_line(notice))}</p>")

    if _has_radar(sections):
        chunks.append(
            f'<p style="font-size: 0.9em; color: #555;">{escape(_RADAR_NOTE)}</p>'
        )
    if _has_site_metrics(sections):
        chunks.append(
            "<p style=\"font-size: 0.9em; color: #555;\">"
            "Site metrics: Semrush monthly visits and Authority Score. "
            "High legitimacy = AS 50+, Medium = 30–49, Low = below 30."
            "</p>"
        )
    if _has_podcast_reach(sections):
        chunks.append(
            "<p style=\"font-size: 0.9em; color: #555;\">"
            "Podcast reach: estimated weekly downloads from Rephonic "
            "for shows found on Spotify. Spotify's own API does not publish listener counts."
            "</p>"
        )
    if batch_key:
        chunks.append(
            "<p style=\"font-size: 0.9em; color: #555;\">"
            "To pass on an item, reply to this email. Each line starts with that "
            "item's number, then a period, then the reason you are passing. "
            "The reason is what the next search uses, so name the outlet, the format, or the fit."
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
        if opp.radar and (rank := opp.radar.summary_line()):
            extra += f"<br>{escape(rank)}"
        if opp.site:
            extra += f"<br>{_site_html(opp)}"
        if opp.podcast and (reach := opp.podcast.summary_line()):
            extra += f"<br>{escape(reach)}"
        if opp.why_relevant:
            extra += f"<br>Why: {escape(opp.why_relevant)}"
        if opp.suggested_angle:
            extra += f"<br>Angle: {escape(opp.suggested_angle)}"
        extra += _feedback_html(opp, feedback_base)
        number = f"{opp.item_number}. " if opp.item_number else ""
        items.append(
            '<div style="margin:0 0 1.15rem 0;">'
            f"<strong>{escape(number)}</strong>{escape(opp.title)}{due}<br>"
            f"{meta}<br>"
            f'<a href="{escape(opp.url)}">{escape(opp.url)}</a>'
            f"{extra}"
            "</div>"
        )
    return "".join(items)


def _feedback_html(opp: Opportunity, feedback_base: str) -> str:
    if not feedback_base or not opp.feedback_token:
        return ""
    go = escape(feedback_href(feedback_base, opp.feedback_token, "go"))
    nogo = escape(feedback_href(feedback_base, opp.feedback_token, "nogo"))
    linkedin = escape(feedback_href(feedback_base, opp.feedback_token, "linkedin"))
    button = (
        "display:inline-block;padding:6px 12px;color:#ffffff;"
        "text-decoration:none;border-radius:4px;font-family:Georgia,serif;"
    )
    return (
        '<br><span style="display:inline-block;margin-top:8px;">'
        "What should we do with this?</span><br>"
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin-top:6px;">'
        "<tr>"
        '<td style="padding-right:8px;">'
        f'<a href="{go}" style="{button}background:#0d6b3d;">Go</a>'
        "</td>"
        '<td style="padding-right:8px;">'
        f'<a href="{nogo}" style="{button}background:#9b1c1c;">No-Go</a>'
        "</td><td>"
        f'<a href="{linkedin}" style="{button}background:#0a66c2;">Make it LinkedIn</a>'
        "</td></tr></table>"
    )


_RADAR_NOTE = (
    "Site: Cloudflare Radar rank from DNS queries to 1.1.1.1. "
    "The global top 100 have an exact rank. Other ranked domains are a "
    "bucket such as top 10K, and the rest are outside the top 1M. "
    "This is a traffic-popularity rank, not an estimated visit count. "
    "Links on Spotify, YouTube, Apple, and similar hosts are skipped."
)


def _has_radar(sections: list[PersonSection]) -> bool:
    return any(
        opp.radar and opp.radar.summary_line()
        for s in sections
        for opp in s.opportunities
    )


def _has_site_metrics(sections: list[PersonSection]) -> bool:
    return any(opp.site for s in sections for opp in s.opportunities)


def _has_podcast_reach(sections: list[PersonSection]) -> bool:
    return any(
        opp.podcast and opp.podcast.summary_line()
        for s in sections
        for opp in s.opportunities
    )


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

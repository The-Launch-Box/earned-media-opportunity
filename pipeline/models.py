from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta, timezone

from pydantic import BaseModel, Field, model_validator


def person_id(firm: str, name: str) -> str:
    """Stable id used as the local equivalent of a Table Storage row key."""
    raw = f"{firm.strip().lower()}|{name.strip().lower()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def url_fingerprint(url: str) -> str:
    """Short hash of a normalized URL — the sent-item fingerprint."""
    normalized = url.strip().lower().rstrip("/")
    # drop common tracking junk
    normalized = re.sub(r"[?#].*$", "", normalized)
    return hashlib.sha256(normalized.encode()).hexdigest()[:24]


class Spokesperson(BaseModel):
    person_id: str
    firm: str
    name: str
    title: str = ""
    linkedin: str = ""
    topics: str = ""
    prior_work: str = ""
    bio: str = ""
    email: str | None = None

    @classmethod
    def from_row(
        cls,
        *,
        firm: str,
        name: str,
        title: str = "",
        linkedin: str = "",
        topics: str = "",
        prior_work: str = "",
        bio: str = "",
        email: str | None = None,
    ) -> Spokesperson:
        return cls(
            person_id=person_id(firm, name),
            firm=firm.strip(),
            name=name.strip(),
            title=(title or "").strip(),
            linkedin=_normalize_linkedin(linkedin or ""),
            topics=(topics or "").strip(),
            prior_work=(prior_work or "").strip(),
            bio=(bio or "").strip(),
            email=(email or None),
        )


def _normalize_linkedin(url: str) -> str:
    url = url.strip()
    if not url:
        return ""
    if url.startswith("http"):
        return url
    return f"https://www.{url.lstrip('/')}" if not url.startswith("www.") else f"https://{url}"


_URGENT_WINDOW = re.compile(
    r"\b("
    r"24\s*hours?|48\s*hours?|12\s*hours?|"
    r"today|tonight|asap|same[\s-]?day|"
    r"eod|end of (the )?day|this afternoon|"
    r"by tomorrow|due (today|tomorrow)"
    r")\b",
    re.IGNORECASE,
)

_ISO_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


class SiteMetrics(BaseModel):
    """Semrush snapshot for the opportunity's root domain."""

    domain: str = ""
    monthly_visits: int | None = None
    visits_kind: str = ""  # visits | organic | ""
    organic_traffic: int | None = None
    organic_keywords: int | None = None
    rank: int | None = None
    authority_score: int | None = None
    referring_domains: int | None = None
    legitimacy: str = "unknown"  # high | medium | low | unknown
    legitimacy_reason: str = ""
    fetched_at: str = ""
    error: str = ""
    source: str = ""  # live | cache | stub

    def visits_display(self) -> str:
        if self.monthly_visits is None:
            return "visits n/a"
        count = format_count(self.monthly_visits)
        if self.visits_kind == "organic":
            return f"~{count} monthly organic visits"
        return f"~{count} monthly visits"

    def legitimacy_display(self) -> str:
        label = {
            "high": "High legitimacy",
            "medium": "Medium legitimacy",
            "low": "Low legitimacy",
        }.get(self.legitimacy, "Legitimacy unknown")
        if self.authority_score is not None:
            return f"{label} (AS {self.authority_score})"
        return label

    def summary_line(self) -> str:
        if self.error and self.legitimacy == "unknown" and self.monthly_visits is None:
            return f"Site: no Semrush data for {self.domain}" if self.domain else "Site: no Semrush data"
        return f"Site: {self.visits_display()} · {self.legitimacy_display()}"


def format_count(n: int) -> str:
    """Compact human count: 1490 → 1.5K, 1_200_000 → 1.2M."""
    abs_n = abs(n)
    if abs_n >= 1_000_000_000:
        value = n / 1_000_000_000
        return f"{value:.1f}B".replace(".0B", "B")
    if abs_n >= 1_000_000:
        value = n / 1_000_000
        return f"{value:.1f}M".replace(".0M", "M")
    if abs_n >= 1_000:
        value = n / 1_000
        return f"{value:.1f}K".replace(".0K", "K")
    return str(n)


class Opportunity(BaseModel):
    title: str
    url: str
    outlet: str = ""
    opportunity_type: str = Field(
        description="press | podcast | speaking | backlink | journalist_query | other"
    )
    why_relevant: str = ""
    suggested_angle: str = ""
    deadline: str | None = None
    urgent: bool = False
    fingerprint: str = ""
    site: SiteMetrics | None = None
    # Local-only link token for the Yes/No line in the roundup. Not from Claude.
    feedback_token: str = ""

    @model_validator(mode="after")
    def ensure_fingerprint(self) -> Opportunity:
        if not self.fingerprint and self.url:
            self.fingerprint = url_fingerprint(self.url)
        return self

    def is_urgent(self, *, today: date | None = None) -> bool:
        """True when the model flagged it or the deadline is effectively now."""
        if self.urgent:
            return True
        raw = (self.deadline or "").strip()
        if not raw:
            return False
        if _URGENT_WINDOW.search(raw):
            return True
        parsed = _deadline_date(raw)
        if parsed is None:
            return False
        today = today or date.today()
        return parsed <= today + timedelta(days=2)


def _deadline_date(raw: str) -> date | None:
    match = _ISO_DATE.search(raw)
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


class ResearchResult(BaseModel):
    person_id: str
    opportunities: list[Opportunity]
    raw_summary: str = ""
    search_count: int = 0
    searched_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class Roundup(BaseModel):
    """One team blast covering every spokesperson with new opportunities."""

    subject: str
    body_text: str
    body_html: str
    opportunities: list[Opportunity] = Field(default_factory=list)
    people: list[Spokesperson] = Field(default_factory=list)
    new_people: list[str] = Field(default_factory=list)
    urgent_count: int = 0
    generated_on: str = Field(default_factory=lambda: date.today().isoformat())

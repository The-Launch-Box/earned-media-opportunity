"""Unit tests for Semrush scoring helpers (no live API)."""

from __future__ import annotations

from datetime import date

from pipeline.models import Opportunity, SiteMetrics, Spokesperson, format_count
from pipeline.roundup import PersonSection, build_roundup
from pipeline.semrush import parse_semrush_csv, root_domain, score_legitimacy, SemrushError


def test_root_domain() -> None:
    assert root_domain("https://www.cio.com/article/4179715/foo.html") == "cio.com"
    assert root_domain("https://finance.yahoo.com/news/x") == "yahoo.com"
    assert root_domain("bbc.co.uk") == "bbc.co.uk"
    assert root_domain("https://orgdev.td.org/wp-content/file.pdf") == "td.org"


def test_format_count() -> None:
    assert format_count(940) == "940"
    assert format_count(1_500) == "1.5K"
    assert format_count(12_000) == "12K"
    assert format_count(1_200_000) == "1.2M"


def test_legitimacy_thresholds() -> None:
    assert score_legitimacy(
        authority_score=74, referring_domains=1000, organic_traffic=10
    ) == ("high", "Authority Score 74")
    assert score_legitimacy(
        authority_score=41, referring_domains=10, organic_traffic=10
    )[0] == "medium"
    assert score_legitimacy(
        authority_score=12, referring_domains=1, organic_traffic=1
    )[0] == "low"
    assert score_legitimacy(
        authority_score=None, referring_domains=None, organic_traffic=None, not_found=True
    )[0] == "low"


def test_parse_csv_and_error() -> None:
    rows = parse_semrush_csv(
        "Domain;Rank;Organic Traffic\nseobook.com;24041;37332\n"
    )
    assert rows[0]["Organic Traffic"] == "37332"
    try:
        parse_semrush_csv("ERROR 50 :: NOTHING FOUND")
    except SemrushError as exc:
        assert "NOTHING FOUND" in str(exc)
    else:
        raise AssertionError("expected SemrushError")


def test_roundup_includes_site_line() -> None:
    person = Spokesperson.from_row(firm="TLB", name="Amy Kenly", title="CMO")
    opp = Opportunity(
        title="Guest post",
        url="https://www.cio.com/contribute",
        outlet="CIO.com",
        opportunity_type="backlink",
        why_relevant="Fits the beat.",
        suggested_angle="Pitch a practitioner take.",
        site=SiteMetrics(
            domain="cio.com",
            monthly_visits=1_200_000,
            visits_kind="visits",
            authority_score=74,
            legitimacy="high",
            legitimacy_reason="Authority Score 74",
        ),
    )
    roundup = build_roundup(
        [PersonSection(person=person, opportunities=[opp])],
        generated_on=date(2026, 9, 21),
    )
    assert "Site: ~1.2M monthly visits · High legitimacy (AS 74)" in roundup.body_text
    assert "High legitimacy (AS 74)" in roundup.body_html
    assert "Site metrics: Semrush monthly visits" in roundup.body_text


if __name__ == "__main__":
    test_root_domain()
    test_format_count()
    test_legitimacy_thresholds()
    test_parse_csv_and_error()
    test_roundup_includes_site_line()
    print("ok")

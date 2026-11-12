"""Cloudflare Radar site traffic. No live API calls."""

from __future__ import annotations

from datetime import date, datetime, timezone

import httpx

from pipeline.models import Opportunity, RadarMetrics, Spokesperson
from pipeline.radar import (
    PLATFORM_DOMAINS,
    RadarClient,
    RadarStats,
    enrich_opportunities as enrich_radar,
    outlet_domain,
    parse_ranking_payload,
)
from pipeline.roundup import PersonSection, build_roundup
from pipeline.storage import LocalStore

_SHOW = "b" * 22


def test_outlet_domain_skips_platforms() -> None:
    assert outlet_domain("https://www.cio.com/article/1") == "cio.com"
    assert outlet_domain("https://open.spotify.com/show/" + _SHOW) == ""
    assert "spotify.com" in PLATFORM_DOMAINS


def test_parse_ranking_bucket_and_rank() -> None:
    metrics = parse_ranking_payload(
        {
            "success": True,
            "result": {
                "main": {
                    "bucket": "5000",
                    "categories": [],
                    "top_locations": [
                        {"locationCode": "GB", "locationName": "United Kingdom", "rank": 40},
                        {"locationCode": "US", "locationName": "United States", "rank": 8},
                    ],
                }
            },
        },
        domain="cio.com",
    )
    assert metrics.bucket == 5000
    assert metrics.listed
    assert metrics.top_location == "US #8"
    assert metrics.summary_line() == "Site: Cloudflare Radar top 5K domains"

    ranked = parse_ranking_payload(
        {"success": True, "result": {"details_0": {"rank": 4, "bucket": "100"}}},
        domain="google.com",
    )
    assert ranked.summary_line() == "Site: Cloudflare Radar global rank #4"

    missing = parse_ranking_payload(
        {"success": True, "result": {"details_0": {"categories": []}}},
        domain="tiny.example",
    )
    assert missing.listed is False
    assert missing.summary_line() == "Site: outside the Cloudflare Radar top 1M"


def test_radar_auth_failure_is_not_cached(tmp_path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            401,
            json={"success": False, "errors": [{"message": "Invalid API Token"}]},
        )

    store = LocalStore(tmp_path / "radar.db")
    client = RadarClient(
        store=store,
        api_token="token",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    stats = RadarStats()
    try:
        found = client.lookup_many(["cio.com", "forbes.com"], stats)
    finally:
        client.close()
    assert found == {}
    assert "Invalid API Token" in stats.skipped
    assert stats.failed == 2
    assert store.get_radar_cache("cio.com", 7) is None


def test_radar_caches_unlisted_domain(tmp_path) -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(
            200,
            json={"success": True, "result": {"details_0": {"categories": []}}},
        )

    store = LocalStore(tmp_path / "radar.db")
    client = RadarClient(
        store=store,
        api_token="token",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    try:
        first = client.lookup("https://tiny.example/guest")
        second = client.lookup("tiny.example")
    finally:
        client.close()
    assert calls["n"] == 1
    assert first.summary_line().startswith("Site: outside")
    assert second.source == "cache"


def test_radar_cache_roundtrip(tmp_path) -> None:
    store = LocalStore(tmp_path / "cache.db")
    now = datetime.now(timezone.utc).isoformat()
    store.put_radar_cache(
        RadarMetrics(domain="cio.com", bucket=10000, listed=True, fetched_at=now, source="live")
    )
    cached = store.get_radar_cache("cio.com", 7)
    assert cached is not None and cached.bucket == 10000


def test_dry_run_skips_platform_hosts(tmp_path) -> None:
    store = LocalStore(tmp_path / "dry.db")
    podcast = Opportunity(
        title="On Spotify",
        url="https://open.spotify.com/show/" + _SHOW,
        outlet="Acquired",
        opportunity_type="podcast",
    )
    article = Opportunity(
        title="Contribute",
        url="https://www.cio.com/contribute",
        outlet="CIO",
        opportunity_type="press",
    )
    enriched, stats = enrich_radar([podcast, article], dry_run=True, store=store)
    assert stats.domains == 1
    assert enriched[0].radar is None
    assert enriched[1].radar is not None
    assert enriched[1].radar.summary_line().startswith("Site:")


def test_roundup_site_line_comes_from_radar() -> None:
    person = Spokesperson.from_row(firm="TLB", name="Amy Kenly", title="CMO")
    opp = Opportunity(
        title="Guest post",
        url="https://www.cio.com/contribute",
        outlet="CIO.com",
        opportunity_type="backlink",
        radar=RadarMetrics(domain="cio.com", bucket=10_000, listed=True),
    )
    roundup = build_roundup(
        [PersonSection(person=person, opportunities=[opp])],
        generated_on=date(2026, 9, 21),
    )
    assert "Site: Cloudflare Radar top 10K domains" in roundup.body_text
    assert "Site: Cloudflare Radar top 10K domains" in roundup.body_html
    assert "Cloudflare Radar rank from DNS" in roundup.body_text
    assert "Semrush" not in roundup.body_text
    assert "Semrush" not in roundup.body_html


def test_daily_run_uses_radar_not_semrush() -> None:
    import pipeline.runner as runner

    assert runner.enrich_radar is enrich_radar
    assert not hasattr(runner, "_attach_semrush")


def test_foundry_fallback_still_ranks_with_radar(monkeypatch, tmp_path) -> None:
    """Foundry replaces Claude. Site rank still comes from Cloudflare Radar."""
    import pipeline.runner as runner
    from pipeline.models import ResearchResult
    from pipeline.radar import RadarStats
    from pipeline.storage import LocalStore

    store = LocalStore(tmp_path / "pipeline.db")
    person = Spokesperson.from_row(firm="TLB", name="Amy Kenly", title="CMO", topics="B2B")
    opp = Opportunity(
        title="Guest post",
        url="https://www.cio.com/contribute",
        outlet="CIO.com",
        opportunity_type="backlink",
    )

    def research_person(who, **kwargs):
        del kwargs
        return ResearchResult(
            person_id=who.person_id,
            opportunities=[opp.model_copy()],
            provider="foundry",
            model_name="gpt-4o (Azure AI Foundry)",
        )

    ranked: list[Opportunity] = []

    def enrich(opportunities, **kwargs):
        del kwargs
        ranked.extend(opportunities)
        stamped = [
            item.model_copy(
                update={"radar": RadarMetrics(domain="cio.com", bucket=10_000, listed=True)}
            )
            for item in opportunities
        ]
        return stamped, RadarStats(domains=len(stamped), fetched=len(stamped))

    class _Quiet:
        skipped = ""
        fetched = 0
        cached = 0
        checked = 0
        matched = 0
        failed = 0

    class _Delivery:
        mode = "none"
        mailed = False
        skipped = True
        path = None

    monkeypatch.setattr(runner, "research_person", research_person)
    monkeypatch.setattr(runner, "enrich_radar", enrich)
    monkeypatch.setattr(runner, "enrich_podcasts", lambda opportunities, **kwargs: (opportunities, _Quiet()))
    monkeypatch.setattr(runner, "deliver", lambda *args, **kwargs: _Delivery())
    monkeypatch.setattr(runner, "_publish_links", lambda *args, **kwargs: None)

    result = runner._execute_run(
        store,
        [person],
        newcomers=[],
        synced=1,
        dry_run=False,
        skip_empty=False,
    )
    assert result.used_foundry
    assert ranked and ranked[0].url == opp.url

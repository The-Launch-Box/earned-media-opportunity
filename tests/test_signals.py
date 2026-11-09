"""Radar popularity and Spotify podcast stats. No live API calls.

The host on Azure scores outlets with Semrush and pipeline.podcasts.
These clients stay in the repo and are not on that path.
"""

from __future__ import annotations

import pytest

pytest.skip(
    "Azure host uses Semrush and pipeline.podcasts, not these clients.",
    allow_module_level=True,
)

from datetime import date, datetime, timezone

import httpx

from pipeline.models import Opportunity, PodcastMetrics, RadarMetrics, Spokesperson
from pipeline.radar import (
    PLATFORM_DOMAINS,
    RadarClient,
    RadarStats,
    enrich_opportunities as enrich_radar,
    outlet_domain,
    parse_ranking_payload,
)
from pipeline.roundup import PersonSection, build_roundup
from pipeline.spotify import (
    PodcastStats,
    SpotifyClient,
    metrics_from_show,
    names_match,
    normalize_show_name,
)
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


def test_show_name_match_is_exact() -> None:
    assert names_match("Acquired", "Acquired Podcast")
    assert names_match("Andreessen & Horowitz", "Andreessen and Horowitz")
    assert names_match("  SMARTLESS  ", "SmartLess")
    assert not names_match("The Daily", "The Daily Stoic")
    assert not names_match("AI", "The AI Daily")
    assert normalize_show_name("The Journal Podcast") == "the journal"


def test_spotify_reports_matched_show_only(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if request.url.path == "/v1/search":
            query = request.url.params.get("q")
            if query == "Acquired":
                items = [
                    _show("a" * 22, "The Daily Stoic", 10),
                    _show(_SHOW, "Acquired", 214, followers=12500),
                ]
            else:
                items = [_show("c" * 22, "The Daily Stoic", 10)]
            return httpx.Response(200, json={"shows": {"items": items}})
        if request.url.path.endswith("/episodes"):
            return httpx.Response(200, json={"items": [{"release_date": "2026-09-14"}]})
        if request.url.path.startswith("/v1/shows/"):
            show_id = request.url.path.split("/")[3]
            name = "Acquired" if show_id == _SHOW else "The Daily Stoic"
            followers = 12500 if show_id == _SHOW else None
            return httpx.Response(200, json=_show(show_id, name, 214, followers=followers))
        return httpx.Response(404, json={"error": {"message": "not found"}})

    store = LocalStore(tmp_path / "pod.db")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    acquired = Opportunity(
        title="Guest opening",
        url="https://www.acquired.fm/guest",
        outlet="Acquired",
        opportunity_type="podcast",
    )
    daily = Opportunity(
        title="Guest opening",
        url="https://www.example.com/daily",
        outlet="The Daily",
        opportunity_type="podcast",
    )
    client = SpotifyClient(
        store=store,
        client_id="id",
        client_secret="secret",
        market="US",
        http=http,
    )
    stats = PodcastStats()
    try:
        acquired_metrics = client.lookup_opportunity(acquired, stats)
        daily_metrics = client.lookup_opportunity(daily, stats)
    finally:
        client.close()
        http.close()

    assert acquired_metrics is not None
    assert acquired_metrics.followers == 12500
    assert acquired_metrics.total_episodes == 214
    assert acquired_metrics.latest_episode == "2026-09-14"
    assert acquired_metrics.summary_line() == (
        "Podcast: Acquired · on Spotify · 12.5K subscribers · 214 episodes · latest 2026-09-14"
    )
    assert daily_metrics is not None
    assert daily_metrics.on_spotify is False
    assert daily_metrics.summary_line() == ""


def test_direct_spotify_url_skips_name_match(tmp_path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "accounts.spotify.com":
            return httpx.Response(200, json={"access_token": "t", "expires_in": 3600})
        if request.url.path.endswith("/episodes"):
            return httpx.Response(200, json={"items": [{"release_date": "2026-01-02"}]})
        if request.url.path.startswith("/v1/shows/"):
            return httpx.Response(
                200,
                json=_show(_SHOW, "Odd Name The Search Would Miss", 3, publisher="Wondery"),
            )
        return httpx.Response(404, json={"error": {"message": "not found"}})

    store = LocalStore(tmp_path / "pod.db")
    client = SpotifyClient(
        store=store,
        client_id="id",
        client_secret="secret",
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    opp = Opportunity(
        title="Be a guest",
        url=f"https://open.spotify.com/show/{_SHOW}?si=tracking",
        outlet="Something Else",
        opportunity_type="podcast",
    )
    stats = PodcastStats()
    try:
        metrics = client.lookup_opportunity(opp, stats)
    finally:
        client.close()
    assert metrics is not None
    assert metrics.show_name == "Odd Name The Search Would Miss"
    assert "Wondery" in metrics.summary_line()
    assert "3 episodes" in metrics.summary_line()


def test_roundup_includes_popularity_and_podcast() -> None:
    person = Spokesperson.from_row(firm="TLB", name="Amy Kenly", title="CMO")
    opp = Opportunity(
        title="Guest on Acquired",
        url="https://www.acquired.fm/guest",
        outlet="Acquired",
        opportunity_type="podcast",
        radar=RadarMetrics(
            domain="acquired.fm",
            bucket=100_000,
            listed=True,
            top_location="US #20",
        ),
        podcast=PodcastMetrics(
            on_spotify=True,
            show_name="Acquired",
            total_episodes=214,
            latest_episode="2026-09-14",
        ),
    )
    roundup = build_roundup(
        [PersonSection(person=person, opportunities=[opp])],
        generated_on=date(2026, 9, 21),
    )
    assert "Site: Cloudflare Radar top 100K domains" in roundup.body_text
    assert "Podcast: Acquired · on Spotify · 214 episodes · latest 2026-09-14" in roundup.body_text
    assert "Cloudflare Radar rank from DNS" in roundup.body_html
    assert "usually omits both" in roundup.body_text


def test_cache_roundtrip(tmp_path) -> None:
    store = LocalStore(tmp_path / "cache.db")
    now = datetime.now(timezone.utc).isoformat()
    store.put_radar_cache(
        RadarMetrics(domain="cio.com", bucket=10000, listed=True, fetched_at=now, source="live")
    )
    cached = store.get_radar_cache("cio.com", 7)
    assert cached is not None and cached.bucket == 10000

    store.put_podcast_cache(
        "q-us-acquired",
        PodcastMetrics(on_spotify=False, query="Acquired", fetched_at=now, source="live"),
    )
    miss = store.get_podcast_cache("q-us-acquired", 7)
    assert miss is not None and miss.on_spotify is False


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


def test_show_object_reads_subscribers_and_listeners() -> None:
    metrics = metrics_from_show(
        {
            "id": _SHOW,
            "name": "Acquired",
            "publisher": "Acquired",
            "total_episodes": 10,
            "followers": {"total": 12_000},
            "stats": {"monthly_listeners": 340_000},
        }
    )
    assert metrics.followers == 12_000
    assert metrics.listeners == 340_000
    line = metrics.summary_line()
    assert "12K subscribers" in line
    assert "340K monthly listeners" in line


def _show(
    show_id: str,
    name: str,
    episodes: int,
    *,
    followers: int | None = None,
    publisher: str = "",
) -> dict:
    payload: dict = {
        "id": show_id,
        "name": name,
        "publisher": publisher or name,
        "total_episodes": episodes,
        "external_urls": {"spotify": f"https://open.spotify.com/show/{show_id}"},
    }
    if followers is not None:
        payload["followers"] = {"total": followers}
    return payload


if __name__ == "__main__":
    test_outlet_domain_skips_platforms()
    test_parse_ranking_bucket_and_rank()
    print("ok")

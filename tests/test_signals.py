"""Spotify catalog stats from pipeline.spotify. No live API calls.

The daily run gets podcast reach from pipeline.podcasts. pipeline.spotify
stays in the repo and is not on that path. Radar tests are in test_radar.py.
"""

from __future__ import annotations

import pytest

pytest.skip(
    "The daily run uses pipeline.podcasts, not pipeline.spotify.",
    allow_module_level=True,
)

from datetime import date, datetime, timezone

import httpx

from pipeline.models import Opportunity, PodcastMetrics, RadarMetrics, Spokesperson
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
    store.put_podcast_cache(
        "q-us-acquired",
        PodcastMetrics(on_spotify=False, query="Acquired", fetched_at=now, source="live"),
    )
    miss = store.get_podcast_cache("q-us-acquired", 7)
    assert miss is not None and miss.on_spotify is False


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
    test_show_name_match_is_exact()
    print("ok")

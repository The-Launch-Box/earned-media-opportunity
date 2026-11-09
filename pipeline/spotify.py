"""Spotify audience for podcast opportunities that are actually on Spotify.

Only items typed as podcast are looked up. The Web API show object always has
the show name, publisher, and episode count. Subscriber count is the show's
follower total, and monthly listeners are read from the same object. The
public schema omits both, so a number is kept only when the response actually
carries it. A show is reported when the opportunity URL is that Spotify show
or episode, or when a search hit's name matches the outlet (or title) after
light normalization. Near misses are left off.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from pipeline.config import settings
from pipeline.models import Opportunity, PodcastMetrics
from pipeline.storage import LocalStore, open_store

TOKEN_URL = "https://accounts.spotify.com/api/token"
API_BASE = "https://api.spotify.com/v1"

_SHOW_ID = re.compile(
    r"(?:open\.spotify\.com/(?:embed/)?show/|spotify:show:)([A-Za-z0-9]{22})"
)
_EPISODE_ID = re.compile(
    r"(?:open\.spotify\.com/(?:embed/)?episode/|spotify:episode:)([A-Za-z0-9]{22})"
)


class SpotifyError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        missing: bool = False,
        fatal: bool = False,
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.missing = missing
        self.fatal = fatal
        self.transient = transient


@dataclass
class PodcastStats:
    considered: int = 0
    matched: int = 0
    unmatched: int = 0
    cached: int = 0
    fetched: int = 0
    failed: int = 0
    skipped: str = ""


def normalize_show_name(name: str) -> str:
    """Lowercase, strip punctuation, and drop one trailing 'podcast'."""
    text = (name or "").lower().replace("&", " and ").replace("+", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return re.sub(r"\s+podcast$", "", text).strip()


def names_match(query: str, show_name: str) -> bool:
    left = normalize_show_name(query)
    right = normalize_show_name(show_name)
    return bool(left) and left == right


def spotify_show_id(url: str) -> str:
    match = _SHOW_ID.search(url or "")
    return match.group(1) if match else ""


def spotify_episode_id(url: str) -> str:
    match = _EPISODE_ID.search(url or "")
    return match.group(1) if match else ""


def search_cache_key(query: str, market: str) -> str:
    norm = normalize_show_name(query).replace(" ", "-")
    raw = f"q-{market.lower()}-{norm}"
    raw = re.sub(r"[^a-z0-9._-]", "", raw)
    if len(raw) <= 200:
        return raw
    digest = hashlib.sha256(raw.encode()).hexdigest()[:32]
    return f"q-{digest}"


def show_cache_key(show_id: str) -> str:
    return f"show-{show_id}"


def metrics_from_show(
    show: dict[str, Any],
    *,
    market: str = "",
    latest_episode: str = "",
    query: str = "",
) -> PodcastMetrics:
    show_id = str(show.get("id") or "").strip()
    urls = show.get("external_urls") if isinstance(show.get("external_urls"), dict) else {}
    spotify_url = str((urls or {}).get("spotify") or "").strip()
    if not spotify_url and show_id:
        spotify_url = f"https://open.spotify.com/show/{show_id}"
    return PodcastMetrics(
        query=query,
        on_spotify=bool(show_id or show.get("name")),
        market=market,
        show_name=str(show.get("name") or "").strip(),
        show_id=show_id,
        spotify_url=spotify_url,
        publisher=str(show.get("publisher") or "").strip(),
        total_episodes=_to_int(show.get("total_episodes")),
        followers=_audience(show, _SUBSCRIBER_KEYS),
        listeners=_audience(show, _LISTENER_KEYS),
        latest_episode=latest_episode,
        source="live",
    )


def enrich_opportunities(
    opportunities: list[Opportunity],
    *,
    dry_run: bool = False,
    store: LocalStore | None = None,
) -> tuple[list[Opportunity], PodcastStats]:
    """Attach Spotify subscribers and monthly listeners to podcast items only."""
    if not opportunities:
        return opportunities, PodcastStats()

    client = SpotifyClient(store=store or open_store(), dry_run=dry_run)
    stats = PodcastStats()
    enriched: list[Opportunity] = []
    try:
        for opp in opportunities:
            if (opp.opportunity_type or "").strip().lower() != "podcast":
                enriched.append(opp)
                continue
            stats.considered += 1
            metrics = client.lookup_opportunity(opp, stats)
            if metrics is not None and metrics.summary_line():
                if metrics.on_spotify and not metrics.unavailable_in_market:
                    stats.matched += 1
                else:
                    stats.unmatched += 1
                opp = opp.model_copy(update={"podcast": metrics})
            elif metrics is not None:
                stats.unmatched += 1
            enriched.append(opp)
    finally:
        client.close()
    return enriched, stats


class SpotifyClient:
    def __init__(
        self,
        *,
        store: LocalStore,
        dry_run: bool = False,
        client_id: str | None = None,
        client_secret: str | None = None,
        market: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.store = store
        self.dry_run = dry_run
        self.client_id = (
            client_id if client_id is not None else settings.spotify_client_id
        ).strip()
        self.client_secret = (
            client_secret
            if client_secret is not None
            else settings.spotify_client_secret
        ).strip()
        self.market = (market if market is not None else settings.spotify_market).strip() or "US"
        self.cache_days = settings.podcast_cache_days
        self._token = ""
        self._token_until = 0.0
        self._fatal: str | None = None
        self._http = http or httpx.Client(timeout=20.0)
        self._owns_http = http is None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def lookup_opportunity(
        self, opp: Opportunity, stats: PodcastStats
    ) -> PodcastMetrics | None:
        if self.dry_run:
            stats.fetched += 1
            return _stub_metrics(opp, market=self.market)

        if self._fatal:
            stats.failed += 1
            return None
        if not self.client_id or not self.client_secret:
            stats.skipped = "SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET is missing"
            return None

        show_id = spotify_show_id(opp.url)
        episode_id = "" if show_id else spotify_episode_id(opp.url)
        if show_id or episode_id:
            key = show_cache_key(show_id or episode_id)
            cached = self._cached(key, stats)
            if cached is not None:
                return cached
            try:
                if episode_id and not show_id:
                    show_id = self._show_id_from_episode(episode_id)
                metrics = self._load_show(show_id, query=opp.outlet or opp.title)
            except SpotifyError as exc:
                return self._failure(exc, stats, cache_key=key, direct=True)
            self._remember(key, metrics)
            if metrics.show_id:
                self._remember(show_cache_key(metrics.show_id), metrics)
            stats.fetched += 1
            return metrics

        query = _search_query(opp)
        if not query:
            stats.unmatched += 1
            return None
        key = search_cache_key(query, self.market)
        cached = self._cached(key, stats)
        if cached is not None:
            return cached
        try:
            metrics = self._search(query)
        except SpotifyError as exc:
            return self._failure(exc, stats, cache_key=key, direct=False)
        self._remember(key, metrics)
        if metrics.show_id:
            self._remember(show_cache_key(metrics.show_id), metrics)
        stats.fetched += 1
        return metrics

    def lookup_text(self, text: str) -> PodcastMetrics:
        """CLI helper: a Spotify URL or a show name."""
        stats = PodcastStats()
        show_id = spotify_show_id(text)
        episode_id = "" if show_id else spotify_episode_id(text)
        opp = Opportunity(
            title=text,
            url=text if (show_id or episode_id or "://" in text) else "https://example.com/podcast",
            outlet="" if (show_id or episode_id) else text,
            opportunity_type="podcast",
        )
        if show_id or episode_id or opp.outlet:
            found = self.lookup_opportunity(opp, stats)
            if found is not None:
                return found
        return PodcastMetrics(
            query=text,
            market=self.market,
            error=stats.skipped or "No matching Spotify show",
            source="live",
        )

    def _cached(self, key: str, stats: PodcastStats) -> PodcastMetrics | None:
        cached = self.store.get_podcast_cache(key, self.cache_days)
        if cached is None:
            return None
        stats.cached += 1
        return cached.model_copy(update={"source": "cache"})

    def _remember(self, key: str, metrics: PodcastMetrics) -> None:
        self.store.put_podcast_cache(key, metrics)

    def _failure(
        self,
        exc: SpotifyError,
        stats: PodcastStats,
        *,
        cache_key: str,
        direct: bool,
    ) -> PodcastMetrics | None:
        if exc.fatal:
            self._fatal = str(exc)
            stats.skipped = str(exc)
            stats.failed += 1
            return None
        if exc.transient:
            stats.failed += 1
            return None
        if exc.missing and direct:
            metrics = PodcastMetrics(
                on_spotify=False,
                unavailable_in_market=True,
                market=self.market,
                error=str(exc),
                fetched_at=datetime.now(timezone.utc).isoformat(),
                source="live",
            )
            self._remember(cache_key, metrics)
            stats.fetched += 1
            return metrics
        if exc.missing:
            metrics = PodcastMetrics(
                on_spotify=False,
                market=self.market,
                fetched_at=datetime.now(timezone.utc).isoformat(),
                source="live",
            )
            self._remember(cache_key, metrics)
            stats.fetched += 1
            return metrics
        stats.failed += 1
        return None

    def _search(self, query: str) -> PodcastMetrics:
        payload = self._get_json(
            f"{API_BASE}/search",
            {"q": query, "type": "show", "market": self.market, "limit": 5},
        )
        shows = payload.get("shows") if isinstance(payload.get("shows"), dict) else {}
        items = shows.get("items") if isinstance(shows, dict) else None
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if not names_match(query, str(item.get("name") or "")):
                continue
            show_id = str(item.get("id") or "").strip()
            if not show_id:
                continue
            return self._load_show(show_id, query=query)
        return PodcastMetrics(
            query=query,
            on_spotify=False,
            market=self.market,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            source="live",
        )

    def _load_show(self, show_id: str, *, query: str) -> PodcastMetrics:
        show = self._get_json(f"{API_BASE}/shows/{show_id}", {"market": self.market})
        latest = ""
        try:
            latest = self._latest_episode_date(show_id)
        except SpotifyError as exc:
            if exc.fatal:
                raise
        metrics = metrics_from_show(
            show, market=self.market, latest_episode=latest, query=query
        )
        metrics.fetched_at = datetime.now(timezone.utc).isoformat()
        return metrics

    def _show_id_from_episode(self, episode_id: str) -> str:
        episode = self._get_json(
            f"{API_BASE}/episodes/{episode_id}",
            {"market": self.market},
        )
        show = episode.get("show") if isinstance(episode.get("show"), dict) else {}
        show_id = str(show.get("id") or "").strip()
        if not show_id:
            raise SpotifyError("episode has no show", missing=True)
        return show_id

    def _latest_episode_date(self, show_id: str) -> str:
        payload = self._get_json(
            f"{API_BASE}/shows/{show_id}/episodes",
            {"market": self.market, "limit": 1},
        )
        items = payload.get("items")
        if not isinstance(items, list) or not items or not isinstance(items[0], dict):
            return ""
        return str(items[0].get("release_date") or "").strip()

    def _get_json(self, url: str, params: dict[str, Any]) -> dict[str, Any]:
        response = self._request(url, params)
        if response.status_code == 401:
            self._token = ""
            self._token_until = 0.0
            response = self._request(url, params)
        return self._decode(response)

    def _request(self, url: str, params: dict[str, Any]) -> httpx.Response:
        return self._http.get(url, params=params, headers=self._auth_header())

    def _decode(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code == 404:
            raise SpotifyError("not found", missing=True)
        if response.status_code == 429:
            raise SpotifyError("Spotify rate limit", transient=True)
        if response.status_code in {401, 403}:
            raise SpotifyError(_spotify_message(response), fatal=True)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SpotifyError(
                f"HTTP {response.status_code}: Spotify did not return JSON",
                transient=response.status_code >= 500,
            ) from exc
        if response.status_code >= 500:
            raise SpotifyError(_spotify_message(response, payload), transient=True)
        if response.status_code >= 400:
            raise SpotifyError(_spotify_message(response, payload))
        if not isinstance(payload, dict):
            raise SpotifyError("Spotify response was not a JSON object")
        return payload

    def _auth_header(self) -> dict[str, str]:
        import time

        now = time.time()
        if self._token and now < self._token_until:
            return {"Authorization": f"Bearer {self._token}"}
        response = self._http.post(
            TOKEN_URL,
            data={"grant_type": "client_credentials"},
            auth=(self.client_id, self.client_secret),
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise SpotifyError(
                f"HTTP {response.status_code}: Spotify token response was not JSON",
                fatal=response.status_code in {401, 403},
                transient=response.status_code >= 500,
            ) from exc
        if response.status_code >= 400 or not isinstance(payload, dict):
            raise SpotifyError(
                _spotify_message(response, payload if isinstance(payload, dict) else None),
                fatal=True,
            )
        token = str(payload.get("access_token") or "").strip()
        if not token:
            raise SpotifyError("Spotify token response had no access_token", fatal=True)
        expires = _to_int(payload.get("expires_in")) or 3600
        self._token = token
        self._token_until = now + max(expires - 60, 30)
        return {"Authorization": f"Bearer {token}"}


def _search_query(opp: Opportunity) -> str:
    outlet = (opp.outlet or "").strip()
    title = (opp.title or "").strip()
    for candidate in (outlet, title):
        if candidate and "://" not in candidate and normalize_show_name(candidate):
            return candidate
    return ""


_SUBSCRIBER_KEYS = (
    "followers",
    "follower_count",
    "total_followers",
    "subscribers",
    "subscriber_count",
    "total_subscribers",
)
_LISTENER_KEYS = (
    "monthly_listeners",
    "monthlyListeners",
    "listeners",
    "listener_count",
    "monthly_listener_count",
)


def _audience(show: dict[str, Any], keys: tuple[str, ...]) -> int | None:
    """Read a count from the show object or a nested stats object on it.

    Earlier keys win, including a nested monthly_listeners over a generic
    listeners field on the show itself.
    """
    containers = _audience_containers(show)
    for key in keys:
        for container in containers:
            if key not in container:
                continue
            count = _count_value(container.get(key))
            if count is not None:
                return count
    return None


def _audience_containers(show: dict[str, Any]) -> list[dict[str, Any]]:
    containers = [show]
    for key in ("stats", "statistics", "audience", "metrics"):
        nested = show.get(key)
        if isinstance(nested, dict):
            containers.append(nested)
    return containers


def _count_value(raw: Any) -> int | None:
    if isinstance(raw, dict):
        for key in ("total", "count", "value"):
            if key in raw:
                return _to_int(raw.get(key))
        return None
    return _to_int(raw)


def _to_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    raw = str(value).strip().replace(",", "")
    if not raw:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _spotify_message(response: httpx.Response, payload: dict[str, Any] | None = None) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error:
            description = payload.get("error_description")
            if description:
                return f"{error}: {description}"
            return error
    text = (response.text or "").strip().splitlines()
    snippet = text[0][:200] if text else response.reason_phrase
    return f"HTTP {response.status_code}: {snippet}"


def _stub_metrics(opp: Opportunity, *, market: str) -> PodcastMetrics:
    name = (opp.outlet or opp.title or "Podcast").strip()
    seed = int(hashlib.sha1(name.encode()).hexdigest()[:6], 16)
    return PodcastMetrics(
        query=name,
        on_spotify=True,
        market=market,
        show_name=name,
        publisher="Example Network",
        total_episodes=40 + (seed % 400),
        followers=2_400 + (seed % 80_000),
        listeners=15_000 + (seed % 500_000),
        latest_episode="2026-10-01",
        fetched_at=datetime.now(timezone.utc).isoformat(),
        source="stub",
    )

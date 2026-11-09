"""Confirm podcast opportunities on Spotify and attach a listener estimate.

Spotify's public show API returns catalog metadata only. Estimated weekly
downloads come from Rephonic after a show id is known.
"""

from __future__ import annotations

import base64
import hashlib
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from pipeline.config import settings
from pipeline.models import Opportunity, PodcastAudience
from pipeline.storage import LocalStore, open_store

SPOTIFY_TOKEN_URL = "https://accounts.spotify.com/api/token"
SPOTIFY_API = "https://api.spotify.com/v1"
REPHONIC_API = "https://api.rephonic.com"

_SHOW_URL = re.compile(
    r"(?:open\.spotify\.com/show/|spotify:show:)([A-Za-z0-9]+)",
    re.IGNORECASE,
)
_PODCAST_URL = re.compile(
    r"open\.spotify\.com/show/|spotify:show:|podcasts\.apple\.com|"
    r"podcasts\.google\.com|podbean\.com|libsyn\.com|megaphone\.fm|"
    r"simplecast\.com|anchor\.fm|/podcasts?/",
    re.IGNORECASE,
)
_NAME_STOP = {"the", "a", "an", "podcast", "podcasts", "show", "with", "and"}


class PodcastLookupError(RuntimeError):
    """Spotify or Rephonic rejected a request."""


@dataclass
class PodcastStats:
    checked: int = 0
    cached: int = 0
    fetched: int = 0
    matched: int = 0
    failed: int = 0
    skipped: str = ""


def looks_like_podcast(opp: Opportunity) -> bool:
    """True when research tagged a podcast, or the URL is a podcast host."""
    if (opp.opportunity_type or "").strip().lower() == "podcast":
        return True
    return bool(_PODCAST_URL.search(opp.url or ""))


def spotify_show_id(url: str) -> str:
    match = _SHOW_URL.search(url or "")
    return match.group(1) if match else ""


def lookup_key(opp: Opportunity) -> str:
    show_id = spotify_show_id(opp.url)
    if show_id:
        return f"spotify:{show_id}"
    name = re.sub(r"\s+", " ", (opp.outlet or opp.title or "").strip().lower())
    return f"name:{name}" if name else ""


def _norm_name(value: str) -> str:
    words = re.findall(r"[a-z0-9]+", (value or "").lower())
    return " ".join(word for word in words if word not in _NAME_STOP)


def names_match(query: str, show_name: str) -> bool:
    """True when a Spotify result is the same show, not a loose keyword hit."""
    left, right = _norm_name(query), _norm_name(show_name)
    if not left or not right:
        return False
    if left == right:
        return True
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    if shorter in longer and len(shorter) >= 8:
        return True
    query_tokens = set(left.split())
    if len(query_tokens) < 2:
        return False
    overlap = query_tokens & set(right.split())
    return len(overlap) / len(query_tokens) >= 0.75 and len(overlap) >= 2


def pick_show(query: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    shows = payload.get("shows") if isinstance(payload, dict) else None
    items = shows.get("items") if isinstance(shows, dict) else None
    if not isinstance(items, list):
        return None
    for item in items:
        if isinstance(item, dict) and names_match(query, str(item.get("name") or "")):
            return item
    return None


def enrich_podcasts(
    opportunities: list[Opportunity],
    *,
    dry_run: bool = False,
    store: LocalStore | None = None,
    client: PodcastClient | None = None,
) -> tuple[list[Opportunity], PodcastStats]:
    """Attach a Spotify match to each podcast opportunity. Others are unchanged."""
    stats = PodcastStats()
    if not opportunities:
        return opportunities, stats

    owns_client = client is None
    client = client or PodcastClient(store=store or open_store(), dry_run=dry_run)
    try:
        return client.attach(opportunities, stats)
    finally:
        if owns_client:
            client.close()


class PodcastClient:
    def __init__(
        self,
        *,
        store: LocalStore,
        dry_run: bool = False,
        http: httpx.Client | None = None,
        spotify_client_id: str | None = None,
        spotify_client_secret: str | None = None,
        rephonic_api_key: str | None = None,
    ) -> None:
        self.store = store
        self.dry_run = dry_run
        self.client_id = (
            settings.spotify_client_id if spotify_client_id is None else spotify_client_id
        ).strip()
        self.client_secret = (
            settings.spotify_client_secret
            if spotify_client_secret is None
            else spotify_client_secret
        ).strip()
        self.rephonic_key = (
            settings.rephonic_api_key if rephonic_api_key is None else rephonic_api_key
        ).strip()
        self.market = settings.spotify_market
        self.cache_days = settings.podcast_cache_days
        self._http = http or httpx.Client(timeout=20.0, follow_redirects=True)
        self._owns_http = http is None
        self._token = ""
        self._token_deadline = 0.0
        self._fatal: str | None = None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    @property
    def spotify_ready(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def attach(
        self, opportunities: list[Opportunity], stats: PodcastStats
    ) -> tuple[list[Opportunity], PodcastStats]:
        groups: dict[str, list[int]] = {}
        for index, opp in enumerate(opportunities):
            if not looks_like_podcast(opp):
                continue
            key = lookup_key(opp)
            if not key or key == "name:":
                continue
            groups.setdefault(key, []).append(index)
        stats.checked = len(groups)
        if not groups:
            return opportunities, stats

        if not self.dry_run and not self.spotify_ready:
            stats.skipped = "SPOTIFY_CLIENT_ID or SPOTIFY_CLIENT_SECRET is missing"
            return opportunities, stats

        enriched = list(opportunities)
        for key, indexes in groups.items():
            sample = opportunities[indexes[0]]
            audience = self.lookup(key, sample, stats)
            if audience is None:
                continue
            if audience.on_spotify:
                stats.matched += 1
            for index in indexes:
                enriched[index] = enriched[index].model_copy(update={"podcast": audience})
        if self._fatal and not stats.skipped:
            stats.skipped = self._fatal
        return enriched, stats

    def lookup(self, key: str, sample: Opportunity, stats: PodcastStats) -> PodcastAudience | None:
        if self.dry_run:
            stats.fetched += 1
            return _stub_audience(key, sample)

        now = datetime.now(timezone.utc)
        cached = self.store.get_podcast_cache(key, self.cache_days, now=now)
        if cached is not None:
            stats.cached += 1
            return cached.model_copy(update={"source": "cache"})
        if self._fatal:
            return None

        try:
            audience = self._fetch(key, sample)
            stats.fetched += 1
        except PodcastLookupError as exc:
            message = str(exc)
            if _is_fatal(message):
                self._fatal = message
                stats.skipped = message
            stats.failed += 1
            return None
        except httpx.HTTPError:
            stats.failed += 1
            return None

        self.store.put_podcast_cache(audience)
        return audience

    def lookup_query(self, query: str) -> PodcastAudience:
        """One-off lookup for the CLI. `query` is a show name or Spotify URL."""
        show_id = spotify_show_id(query)
        if show_id:
            key = f"spotify:{show_id}"
            sample = Opportunity(
                title=query,
                url=f"https://open.spotify.com/show/{show_id}",
                opportunity_type="podcast",
            )
        else:
            key = lookup_key(
                Opportunity(title=query, url="https://example.com", outlet=query, opportunity_type="podcast")
            )
            sample = Opportunity(
                title=query,
                url="https://example.com",
                outlet=query,
                opportunity_type="podcast",
            )
        stats = PodcastStats()
        found = self.lookup(key, sample, stats)
        if found is not None:
            return found
        return PodcastAudience(
            query=key,
            error=stats.skipped or "No Spotify match",
            source="live",
            fetched_at=datetime.now(timezone.utc).isoformat(),
        )

    def _fetch(self, key: str, sample: Opportunity) -> PodcastAudience:
        show_id = spotify_show_id(sample.url)
        if show_id:
            show = self._get_show(show_id)
        else:
            query = (sample.outlet or sample.title).strip()
            show = pick_show(query, self._search_shows(query))
        now = datetime.now(timezone.utc).isoformat()
        if not show:
            return PodcastAudience(query=key, on_spotify=False, fetched_at=now, source="live")

        spotify_id = str(show.get("id") or show_id)
        urls = show.get("external_urls") if isinstance(show.get("external_urls"), dict) else {}
        audience = PodcastAudience(
            query=key,
            show_name=str(show.get("name") or ""),
            spotify_id=spotify_id,
            spotify_url=str(urls.get("spotify") or f"https://open.spotify.com/show/{spotify_id}"),
            publisher=str(show.get("publisher") or ""),
            on_spotify=True,
            fetched_at=now,
            source="live",
        )
        if self.rephonic_key and spotify_id:
            self._fill_rephonic(audience)
        return audience

    def _fill_rephonic(self, audience: PodcastAudience) -> None:
        try:
            self._fill_rephonic_payload(audience)
        except (PodcastLookupError, httpx.HTTPError):
            return

    def _fill_rephonic_payload(self, audience: PodcastAudience) -> None:
        payload = self._rephonic_get(
            "/api/podcasts/lookup/",
            {"spotify_id": audience.spotify_id},
        )
        podcasts = payload.get("podcasts") if isinstance(payload, dict) else None
        if not isinstance(podcasts, list) or not podcasts:
            return
        match = podcasts[0] if isinstance(podcasts[0], dict) else None
        if match is None:
            return
        _copy_reach(audience, match)
        if audience.est_weekly_downloads is not None or audience.spotify_followers is not None:
            return
        slug = str(match.get("id") or "").strip()
        if not slug:
            return
        detail = self._rephonic_get(f"/api/podcasts/{slug}/", {})
        podcast = detail.get("podcast") if isinstance(detail, dict) else None
        if isinstance(podcast, dict):
            _copy_reach(audience, podcast)

    def _search_shows(self, query: str) -> dict[str, Any]:
        return self._spotify_get(
            "/search",
            {"q": query, "type": "show", "market": self.market, "limit": "5"},
        )

    def _get_show(self, show_id: str) -> dict[str, Any]:
        response = self._http.get(
            f"{SPOTIFY_API}/shows/{show_id}",
            params={"market": self.market},
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )
        if response.status_code == 404:
            return {}
        payload = _json_or_error(response, "Spotify")
        return payload if payload.get("id") else {}

    def _spotify_get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        response = self._http.get(
            f"{SPOTIFY_API}{path}",
            params=params,
            headers={"Authorization": f"Bearer {self._access_token()}"},
        )
        return _json_or_error(response, "Spotify")

    def _rephonic_get(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        response = self._http.get(
            f"{REPHONIC_API}{path}",
            params=params,
            headers={"X-Rephonic-Api-Key": self.rephonic_key},
        )
        return _json_or_error(response, "Rephonic")

    def _access_token(self) -> str:
        if self._token and time.time() < self._token_deadline:
            return self._token
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
        response = self._http.post(
            SPOTIFY_TOKEN_URL,
            headers={"Authorization": f"Basic {basic}"},
            data={"grant_type": "client_credentials"},
        )
        payload = _json_or_error(response, "Spotify")
        token = str(payload.get("access_token") or "")
        if not token:
            raise PodcastLookupError("Spotify token response had no access_token")
        expires_in = int(payload.get("expires_in") or 3600)
        self._token = token
        self._token_deadline = time.time() + max(expires_in - 30, 30)
        return token


def _copy_reach(audience: PodcastAudience, payload: dict[str, Any]) -> None:
    weekly = _to_int(payload.get("est_weekly_downloads"))
    per_episode = _to_int(payload.get("downloads_per_episode"))
    followers = _to_int(payload.get("spotify_followers"))
    if weekly is not None:
        audience.est_weekly_downloads = weekly
    if per_episode is not None:
        audience.downloads_per_episode = per_episode
    if followers is not None:
        audience.spotify_followers = followers


def _to_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    raw = str(value).strip().replace(",", "")
    if not raw:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def _json_or_error(response: httpx.Response, source: str) -> dict[str, Any]:
    if response.status_code in {401, 403}:
        raise PodcastLookupError(f"{source} rejected the API key (HTTP {response.status_code})")
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if response.status_code >= 400:
        message = ""
        if isinstance(payload, dict):
            message = str(payload.get("error") or payload.get("message") or "")
        raise PodcastLookupError(message or f"{source} HTTP {response.status_code}")
    if not isinstance(payload, dict):
        raise PodcastLookupError(f"{source} returned an unexpected payload")
    return payload


def _is_fatal(message: str) -> bool:
    lowered = message.lower()
    return "rejected the api key" in lowered or "access_token" in lowered


def _stub_audience(key: str, sample: Opportunity) -> PodcastAudience:
    seed = int(hashlib.sha1(key.encode()).hexdigest()[:6], 16)
    name = (sample.outlet or sample.title or "Example Podcast").strip()
    show_id = spotify_show_id(sample.url) or hashlib.sha1(key.encode()).hexdigest()[:22]
    return PodcastAudience(
        query=key,
        show_name=name,
        spotify_id=show_id,
        spotify_url=f"https://open.spotify.com/show/{show_id}",
        publisher="Stub publisher",
        on_spotify=True,
        est_weekly_downloads=5_000 + (seed % 400_000),
        fetched_at=datetime.now(timezone.utc).isoformat(),
        source="stub",
    )

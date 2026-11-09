"""Cloudflare Radar site traffic for an outlet's own domain.

This replaces Semrush. Radar ranks domains from DNS queries to 1.1.1.1. It
is a traffic-popularity rank, not an estimated visit count: the global top
100 get an exact rank, and other ranked domains come back as a bucket (top
200 through top 1M).

The API needs a token with Account → Radar → Read
(https://dash.cloudflare.com/profile/api-tokens). A missing token skips the
Site line and does not block the roundup.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx

from pipeline.config import settings
from pipeline.models import Opportunity, RadarMetrics
from pipeline.semrush import root_domain
from pipeline.storage import LocalStore, open_store

RANKING_URL = "https://api.cloudflare.com/client/v4/radar/ranking/domain/{domain}"

# Hosts whose rank would describe the platform, not the outlet.
PLATFORM_DOMAINS = {
    "amazon.com",
    "anchor.fm",
    "apple.com",
    "buzzsprout.com",
    "castbox.fm",
    "facebook.com",
    "google.com",
    "iheart.com",
    "instagram.com",
    "libsyn.com",
    "linkedin.com",
    "megaphone.fm",
    "overcast.fm",
    "pandora.com",
    "podbean.com",
    "pocketcasts.com",
    "simplecast.com",
    "spotify.com",
    "spreaker.com",
    "stitcher.com",
    "tiktok.com",
    "transistor.fm",
    "tunein.com",
    "twitter.com",
    "x.com",
    "youtube.com",
    "youtu.be",
}


class RadarError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        missing: bool = False,
        fatal: bool = False,
    ) -> None:
        super().__init__(message)
        self.missing = missing
        self.fatal = fatal


@dataclass
class RadarStats:
    domains: int = 0
    cached: int = 0
    fetched: int = 0
    failed: int = 0
    unlisted: int = 0
    skipped: str = ""


def outlet_domain(url: str) -> str:
    """Root domain worth ranking, or "" for blank, local, and platform hosts."""
    domain = root_domain(url)
    if not domain or domain in PLATFORM_DOMAINS:
        return ""
    return domain


def parse_ranking_payload(payload: dict[str, Any], *, domain: str) -> RadarMetrics:
    """Turn a Radar domain-ranking JSON body into metrics.

    Raises RadarError when the body is an error. A successful body with no
    rank and no bucket means the domain is outside the top 1M.
    """
    if not isinstance(payload, dict):
        raise RadarError("Radar response was not a JSON object")
    if payload.get("success") is False:
        message = _error_message(payload) or "Radar request failed"
        missing = "not found" in message.lower() or "no ranking" in message.lower()
        raise RadarError(message, missing=missing)

    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    details = _details(result if isinstance(result, dict) else {})
    rank = _positive_int(details.get("rank"))
    bucket = _positive_int(details.get("bucket"))
    listed = rank is not None or bucket is not None
    return RadarMetrics(
        domain=domain,
        rank=rank,
        bucket=bucket,
        listed=listed,
        top_location=_best_location(details.get("top_locations")),
        source="live",
    )


def enrich_opportunities(
    opportunities: list[Opportunity],
    *,
    dry_run: bool = False,
    store: LocalStore | None = None,
) -> tuple[list[Opportunity], RadarStats]:
    """Attach a Radar site-traffic line to each opportunity on its own domain."""
    if not opportunities:
        return opportunities, RadarStats()

    client = RadarClient(store=store or open_store(), dry_run=dry_run)
    domains: list[str] = []
    seen: set[str] = set()
    for opp in opportunities:
        domain = outlet_domain(opp.url)
        if domain and domain not in seen:
            seen.add(domain)
            domains.append(domain)

    stats = RadarStats(domains=len(domains))
    try:
        by_domain = client.lookup_many(domains, stats)
    finally:
        client.close()

    enriched: list[Opportunity] = []
    for opp in opportunities:
        data = by_domain.get(outlet_domain(opp.url))
        if data is not None and data.summary_line():
            opp = opp.model_copy(update={"radar": data})
        enriched.append(opp)
    return enriched, stats


class RadarClient:
    def __init__(
        self,
        *,
        store: LocalStore,
        dry_run: bool = False,
        api_token: str | None = None,
        http: httpx.Client | None = None,
    ) -> None:
        self.store = store
        self.dry_run = dry_run
        self.api_token = (
            api_token if api_token is not None else settings.cloudflare_api_token
        ).strip()
        self.cache_days = settings.radar_cache_days
        self._fatal: str | None = None
        self._http = http or httpx.Client(timeout=20.0)
        self._owns_http = http is None

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def lookup_many(self, domains: list[str], stats: RadarStats) -> dict[str, RadarMetrics]:
        out: dict[str, RadarMetrics] = {}
        if self.dry_run:
            for domain in domains:
                out[domain] = _stub_metrics(domain)
                stats.fetched += 1
            return out

        if not self.api_token:
            stats.skipped = "CLOUDFLARE_API_TOKEN is missing"
            return out

        now = datetime.now(timezone.utc)
        pending: list[str] = []
        for domain in domains:
            cached = self.store.get_radar_cache(domain, self.cache_days, now=now)
            if cached is not None:
                out[domain] = cached.model_copy(update={"source": "cache"})
                stats.cached += 1
                if not cached.listed and not cached.error:
                    stats.unlisted += 1
            else:
                pending.append(domain)

        for domain in pending:
            if self._fatal:
                stats.failed += 1
                continue
            try:
                metrics = self._fetch(domain)
            except RadarError as exc:
                if exc.fatal:
                    self._fatal = str(exc)
                    stats.skipped = str(exc)
                if exc.missing:
                    metrics = _unlisted(domain)
                    self.store.put_radar_cache(metrics)
                    out[domain] = metrics
                    stats.unlisted += 1
                    stats.fetched += 1
                    continue
                stats.failed += 1
                continue
            except httpx.HTTPError:
                stats.failed += 1
                continue

            if not metrics.listed and not metrics.error:
                stats.unlisted += 1
            stats.fetched += 1
            self.store.put_radar_cache(metrics)
            out[domain] = metrics

        if self._fatal and not stats.skipped:
            stats.skipped = self._fatal
        return out

    def lookup(self, domain_or_url: str) -> RadarMetrics:
        domain = outlet_domain(domain_or_url) or root_domain(domain_or_url)
        if not domain:
            return RadarMetrics(error="could not parse domain")
        if domain in PLATFORM_DOMAINS:
            return RadarMetrics(
                domain=domain,
                error="platform host; rank would describe the platform, not the outlet",
            )
        stats = RadarStats()
        found = self.lookup_many([domain], stats)
        if domain in found:
            return found[domain]
        return RadarMetrics(
            domain=domain,
            error=stats.skipped or "No Cloudflare Radar data",
            source="live",
        )

    def _fetch(self, domain: str) -> RadarMetrics:
        response = self._http.get(
            RANKING_URL.format(domain=domain),
            headers={"Authorization": f"Bearer {self.api_token}"},
            params={
                "rankingType": "POPULAR",
                "format": "JSON",
                "includeTopLocations": "true",
            },
        )
        if response.status_code in {401, 403}:
            raise RadarError(_http_message(response), fatal=True)
        if response.status_code == 404:
            raise RadarError("not found", missing=True)
        try:
            payload = response.json()
        except ValueError as exc:
            raise RadarError(f"HTTP {response.status_code}: Radar did not return JSON") from exc
        if response.status_code >= 400:
            message = _error_message(payload) if isinstance(payload, dict) else ""
            raise RadarError(message or _http_message(response))
        try:
            metrics = parse_ranking_payload(payload, domain=domain)
        except RadarError:
            raise
        metrics.fetched_at = datetime.now(timezone.utc).isoformat()
        return metrics


def _unlisted(domain: str) -> RadarMetrics:
    return RadarMetrics(
        domain=domain,
        listed=False,
        fetched_at=datetime.now(timezone.utc).isoformat(),
        source="live",
    )


def _stub_metrics(domain: str) -> RadarMetrics:
    seed = int(hashlib.sha1(domain.encode()).hexdigest()[:6], 16)
    buckets = [200, 1_000, 10_000, 100_000, 1_000_000]
    bucket = buckets[seed % len(buckets)]
    rank = (seed % 80) + 1 if bucket == 200 else None
    return RadarMetrics(
        domain=domain,
        rank=rank,
        bucket=None if rank is not None else bucket,
        listed=True,
        top_location="US #12",
        fetched_at=datetime.now(timezone.utc).isoformat(),
        source="stub",
    )


def _details(result: dict[str, Any]) -> dict[str, Any]:
    preferred = result.get("details_0")
    if isinstance(preferred, dict):
        return preferred
    for key, value in result.items():
        if key == "meta" or not isinstance(value, dict):
            continue
        if any(field in value for field in ("bucket", "rank", "categories", "top_locations")):
            return value
    return {}


def _best_location(locations: Any) -> str:
    if not isinstance(locations, list):
        return ""
    best: tuple[int, str] | None = None
    for loc in locations:
        if not isinstance(loc, dict):
            continue
        rank = _positive_int(loc.get("rank"))
        code = str(loc.get("locationCode") or loc.get("locationName") or "").strip()
        if rank is None or not code:
            continue
        if best is None or rank < best[0]:
            best = (rank, code)
    if best is None:
        return ""
    return f"{best[1]} #{best[0]}"


def _positive_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    raw = str(value).strip().replace(",", "")
    if not raw:
        return None
    try:
        number = int(float(raw))
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _error_message(payload: dict[str, Any]) -> str:
    errors = payload.get("errors")
    if isinstance(errors, list):
        parts = [
            str(item.get("message")).strip()
            for item in errors
            if isinstance(item, dict) and item.get("message")
        ]
        if parts:
            return "; ".join(parts)
    if isinstance(errors, dict) and errors.get("message"):
        return str(errors["message"])
    return ""


def _http_message(response: httpx.Response) -> str:
    text = (response.text or "").strip().splitlines()
    snippet = text[0][:200] if text else response.reason_phrase
    return f"HTTP {response.status_code}: {snippet}"

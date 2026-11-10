"""Score opportunity outlets with Semrush traffic and Authority Score.

The daily run does not call this module; its Site line comes from
pipeline.radar. `python -m pipeline semrush <domain>` still uses it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from pipeline.config import settings
from pipeline.models import Opportunity, SiteMetrics
from pipeline.storage import LocalStore, open_store

SEO_BASE = "https://api.semrush.com/"
BACKLINKS_BASE = "https://api.semrush.com/analytics/v1/"
TRAFFIC_SUMMARY = "https://api.semrush.com/analytics/ta/api/v3/summary"

# Compound public suffixes we should keep as three labels.
_MULTI_SUFFIXES = {
    "ac.uk",
    "co.in",
    "co.jp",
    "co.nz",
    "co.uk",
    "co.za",
    "com.au",
    "com.br",
    "com.cn",
    "com.mx",
    "gov.uk",
    "org.uk",
}

_FATAL_UNITS = re.compile(
    r"ERROR\s+13[0-3]\b|UNITS EXPIRED|KEY INVALID|KEY DISABLED|LIMIT EXCEEDED",
    re.IGNORECASE,
)


class SemrushError(RuntimeError):
    """Parsed ERROR line from the Semrush API."""


@dataclass
class EnrichStats:
    domains: int = 0
    cached: int = 0
    fetched: int = 0
    failed: int = 0
    skipped: str = ""


def root_domain(url: str) -> str:
    """Registrable domain for a URL or hostname (cio.com, yahoo.com)."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    host = (urlparse(raw).hostname or "").lower().strip(".")
    if host.startswith("www."):
        host = host[4:]
    if not host or host in {"localhost"} or re.fullmatch(r"\d+(?:\.\d+){3}", host):
        return ""
    labels = [p for p in host.split(".") if p]
    if len(labels) < 2:
        return host
    suffix = ".".join(labels[-2:])
    if suffix in _MULTI_SUFFIXES and len(labels) >= 3:
        return ".".join(labels[-3:])
    return suffix


def score_legitimacy(
    *,
    authority_score: int | None,
    referring_domains: int | None,
    organic_traffic: int | None,
    not_found: bool = False,
) -> tuple[str, str]:
    """
    Map Semrush signals to high / medium / low / unknown.

    Authority Score is the primary quality metric (0–100). Traffic and
    referring domains backfill when AS is missing.
    """
    if not_found:
        return "low", "Not in Semrush index"

    if authority_score is not None:
        if authority_score >= 50:
            return "high", f"Authority Score {authority_score}"
        if authority_score >= 30:
            return "medium", f"Authority Score {authority_score}"
        return "low", f"Authority Score {authority_score}"

    traffic = organic_traffic or 0
    refs = referring_domains or 0
    if traffic >= 100_000 or refs >= 5_000:
        return "high", "Strong organic traffic / referring domains"
    if traffic >= 5_000 or refs >= 200:
        return "medium", "Moderate organic traffic / referring domains"
    if traffic > 0 or refs > 0:
        return "low", "Thin traffic and backlink profile"
    return "unknown", "No Semrush data"


def parse_semrush_csv(text: str) -> list[dict[str, str]]:
    """Parse a semicolon CSV body, raising on Semrush ERROR lines."""
    body = (text or "").strip()
    if not body:
        return []
    if body.upper().startswith("ERROR"):
        first = body.splitlines()[0].strip()
        raise SemrushError(first)
    lines = [ln for ln in body.splitlines() if ln.strip()]
    if len(lines) < 2:
        return []
    headers = [h.strip() for h in lines[0].split(";")]
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        parts = line.split(";")
        rows.append(
            {
                headers[i]: (parts[i].strip() if i < len(parts) else "")
                for i in range(len(headers))
            }
        )
    return rows


def _to_int(value: str | None) -> int | None:
    if value is None:
        return None
    raw = str(value).strip().replace(",", "")
    if not raw or raw.upper() in {"N/A", "NA", "NONE", "-"}:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def enrich_opportunities(
    opportunities: list[Opportunity],
    *,
    dry_run: bool = False,
    store: LocalStore | None = None,
) -> tuple[list[Opportunity], EnrichStats]:
    """Attach Semrush site metrics to each opportunity (mutating copies)."""
    if not opportunities:
        return opportunities, EnrichStats()

    client = SemrushClient(store=store or open_store(), dry_run=dry_run)
    domains = []
    seen: set[str] = set()
    for opp in opportunities:
        domain = root_domain(opp.url)
        if domain and domain not in seen:
            seen.add(domain)
            domains.append(domain)

    stats = EnrichStats(domains=len(domains))
    try:
        metrics_by_domain = client.lookup_many(domains, stats)
    finally:
        client.close()
    enriched: list[Opportunity] = []
    for opp in opportunities:
        domain = root_domain(opp.url)
        data = metrics_by_domain.get(domain)
        if data is not None:
            opp = opp.model_copy(update={"site": data})
        enriched.append(opp)
    return enriched, stats


class SemrushClient:
    def __init__(
        self,
        *,
        store: LocalStore,
        dry_run: bool = False,
        api_key: str | None = None,
    ) -> None:
        self.store = store
        self.dry_run = dry_run
        self.api_key = (api_key if api_key is not None else settings.semrush_api_key).strip()
        self.database = settings.semrush_database
        self.cache_days = settings.semrush_cache_days
        self.use_traffic = settings.semrush_use_traffic_analytics
        self._ta_available = self.use_traffic
        self._fatal: str | None = None
        self._http = httpx.Client(timeout=20.0, follow_redirects=True)

    def close(self) -> None:
        self._http.close()

    def lookup_many(self, domains: list[str], stats: EnrichStats) -> dict[str, SiteMetrics]:
        out: dict[str, SiteMetrics] = {}
        pending: list[str] = []

        if self.dry_run:
            for domain in domains:
                out[domain] = _stub_metrics(domain)
                stats.fetched += 1
            return out

        if not self.api_key:
            stats.skipped = "SEMRUSH_API_KEY is missing"
            return out

        now = datetime.now(timezone.utc)
        for domain in domains:
            cached = self.store.get_semrush_cache(domain, self.cache_days, now=now)
            if cached is not None:
                out[domain] = cached.model_copy(update={"source": "cache"})
                stats.cached += 1
            else:
                pending.append(domain)

        visits_by_domain: dict[str, int] = {}
        if pending and self._ta_available and self._fatal is None:
            try:
                visits_by_domain = self._traffic_summary(pending)
            except SemrushError as exc:
                if _is_fatal(str(exc)):
                    self._fatal = str(exc)
                else:
                    self._ta_available = False

        for domain in pending:
            if self._fatal:
                metrics = SiteMetrics(
                    domain=domain,
                    legitimacy="unknown",
                    legitimacy_reason=self._fatal,
                    error=self._fatal,
                    source="live",
                    fetched_at=now.isoformat(),
                )
                stats.failed += 1
                out[domain] = metrics
                continue
            try:
                metrics = self._fetch_domain(domain, visits=visits_by_domain.get(domain))
                stats.fetched += 1
            except SemrushError as exc:
                msg = str(exc)
                if _is_fatal(msg):
                    self._fatal = msg
                    stats.skipped = msg
                not_found = "NOTHING FOUND" in msg.upper()
                legitimacy, reason = score_legitimacy(
                    authority_score=None,
                    referring_domains=None,
                    organic_traffic=None,
                    not_found=not_found,
                )
                metrics = SiteMetrics(
                    domain=domain,
                    monthly_visits=0 if not_found else None,
                    legitimacy=legitimacy,
                    legitimacy_reason=reason,
                    error=msg,
                    source="live",
                    fetched_at=now.isoformat(),
                )
                stats.failed += 1
            except httpx.HTTPError as exc:
                metrics = SiteMetrics(
                    domain=domain,
                    legitimacy="unknown",
                    legitimacy_reason="Semrush request failed",
                    error=str(exc),
                    source="live",
                    fetched_at=now.isoformat(),
                )
                stats.failed += 1

            self.store.put_semrush_cache(metrics)
            out[domain] = metrics

        if self._fatal and not stats.skipped:
            stats.skipped = self._fatal
        return out

    def lookup(self, domain_or_url: str) -> SiteMetrics:
        domain = root_domain(domain_or_url)
        if not domain:
            return SiteMetrics(error="could not parse domain", legitimacy="unknown")
        stats = EnrichStats()
        found = self.lookup_many([domain], stats)
        return found.get(
            domain,
            SiteMetrics(
                domain=domain,
                legitimacy="unknown",
                legitimacy_reason=stats.skipped or "No Semrush data",
                error=stats.skipped,
                source="live",
            ),
        )

    def _fetch_domain(self, domain: str, *, visits: int | None) -> SiteMetrics:
        overview = self._domain_rank(domain)
        backlinks = self._backlinks_overview(domain)
        organic = _to_int(overview.get("Organic Traffic"))
        keywords = _to_int(overview.get("Organic Keywords"))
        rank = _to_int(overview.get("Rank"))
        ascore = _to_int(backlinks.get("ascore") or backlinks.get("score"))
        refs = _to_int(backlinks.get("domains_num"))
        not_found = not overview and not backlinks and visits is None
        legitimacy, reason = score_legitimacy(
            authority_score=ascore,
            referring_domains=refs,
            organic_traffic=organic,
            not_found=not_found,
        )
        monthly = visits if visits is not None else organic
        visits_kind = "visits" if visits is not None else ("organic" if organic is not None else "")
        return SiteMetrics(
            domain=domain,
            monthly_visits=monthly,
            visits_kind=visits_kind,
            organic_traffic=organic,
            organic_keywords=keywords,
            rank=rank,
            authority_score=ascore,
            referring_domains=refs,
            legitimacy=legitimacy,
            legitimacy_reason=reason,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            source="live",
        )

    def _domain_rank(self, domain: str) -> dict[str, str]:
        rows = self._get(
            SEO_BASE,
            {
                "type": "domain_rank",
                "key": self.api_key,
                "domain": domain,
                "database": self.database,
                "export_columns": "Dn,Rk,Or,Ot,Oc",
            },
        )
        return rows[0] if rows else {}

    def _backlinks_overview(self, domain: str) -> dict[str, str]:
        rows = self._get(
            BACKLINKS_BASE,
            {
                "type": "backlinks_overview",
                "key": self.api_key,
                "target": domain,
                "target_type": "root_domain",
                "export_columns": "ascore,total,domains_num,urls_num,ips_num",
            },
        )
        return rows[0] if rows else {}

    def _traffic_summary(self, domains: list[str]) -> dict[str, int]:
        if not domains:
            return {}
        rows = self._get(
            TRAFFIC_SUMMARY,
            {
                "key": self.api_key,
                "targets": ",".join(domains),
                "export_columns": "target,visits,users",
            },
        )
        out: dict[str, int] = {}
        for row in rows:
            target = (row.get("target") or "").strip().lower()
            visits = _to_int(row.get("visits") or row.get("users"))
            if target and visits is not None:
                out[target] = visits
        return out

    def _get(self, url: str, params: dict[str, Any]) -> list[dict[str, str]]:
        response = self._http.get(url, params=params)
        text = response.text
        try:
            rows = parse_semrush_csv(text)
        except SemrushError as exc:
            if "NOTHING FOUND" in str(exc).upper():
                return []
            raise
        if response.status_code >= 400:
            snippet = text.strip().splitlines()[0][:200] if text.strip() else response.reason_phrase
            raise SemrushError(f"HTTP {response.status_code}: {snippet}")
        return rows


def _is_fatal(message: str) -> bool:
    return bool(_FATAL_UNITS.search(message))


def _stub_metrics(domain: str) -> SiteMetrics:
    seed = int(hashlib.sha1(domain.encode()).hexdigest()[:6], 16)
    visits = 8_000 + (seed % 900_000)
    ascore = 22 + (seed % 68)
    refs = 80 + (seed % 8_000)
    legitimacy, reason = score_legitimacy(
        authority_score=ascore,
        referring_domains=refs,
        organic_traffic=visits,
    )
    return SiteMetrics(
        domain=domain,
        monthly_visits=visits,
        visits_kind="visits",
        organic_traffic=visits,
        organic_keywords=200 + (seed % 20_000),
        rank=1_000 + (seed % 80_000),
        authority_score=ascore,
        referring_domains=refs,
        legitimacy=legitimacy,
        legitimacy_reason=reason + " (stub)",
        fetched_at=datetime.now(timezone.utc).isoformat(),
        source="stub",
    )

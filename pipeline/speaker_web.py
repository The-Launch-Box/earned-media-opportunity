"""Search roundup: published coverage of each speaker, found by web search.

Before each person's opportunity research, Claude searches for articles, news,
interviews, podcasts, and talks by or about that speaker at that firm. New finds
are appended to the speaker's web_results. Nothing already saved is changed or
removed; the dashboard's Remove button is the only delete.
"""

from __future__ import annotations

from typing import Any

from pipeline.config import settings
from pipeline.models import Spokesperson, WebResult, url_fingerprint
from pipeline.research import (
    _call_with_search,
    _extract_text,
    _parse_json_payload,
)

KINDS = {"article", "interview", "podcast", "talk", "award", "other"}
MAX_NEW = 8
MAX_KNOWN_IN_PROMPT = 40

SYSTEM = """You research public coverage of one professional for their marketing team.

Find published pieces by or about this specific person at this specific firm: \
news articles, bylined articles, interviews, podcast appearances, conference talks, \
webinars, awards, and press releases that name them.

You MUST use the web_search tool. Do not rely on training data.
Use the firm, title, and LinkedIn profile to make sure each result is about this \
person and not someone else with the same name. Leave out anything you are not sure of.
Leave out their own LinkedIn profile, the firm's staff bio page, and directory listings.

Always copy real URLs from your web search results. Do not invent links.
After searching, respond with JSON only — no markdown fences, no extra prose.
"""


def coverage_prompt(person: Spokesperson, known: list[WebResult]) -> str:
    window = (
        "Look back about 12 months."
        if not known
        else "Focus on anything published in roughly the last 30 days."
    )
    skip = ""
    if known:
        urls = "\n".join(f"- {item.url}" for item in known[:MAX_KNOWN_IN_PROMPT])
        skip = f"\nAlready on file. Do not return these URLs again:\n{urls}\n"
    return f"""Find public coverage of this person.

Firm: {person.firm}
Name: {person.name}
Title: {person.title or "n/a"}
LinkedIn: {person.linkedin or "n/a"}
Expertise topics: {person.topics or "n/a"}
Prior articles / references: {person.prior_work or "(none listed)"}

{window}
{skip}
Return ONLY a JSON object (no markdown fences) with this shape:
{{
  "results": [
    {{
      "title": "headline or episode title",
      "url": "https://...",
      "outlet": "publication, show, or event",
      "kind": "article|interview|podcast|talk|award|other",
      "summary": "1-2 sentences on what the piece covers and what {person.name} said or did in it",
      "published_on": "YYYY-MM-DD or null"
    }}
  ]
}}

If you find nothing new, return {{"results": []}}. Cap at {MAX_NEW} results, most recent first.
"""


def search_speaker_coverage(
    person: Spokesperson,
    known: list[WebResult],
    *,
    dry_run: bool = False,
    use_fallback: bool = False,
) -> tuple[list[WebResult], str]:
    """Return (new results not already in known, model label)."""
    if dry_run or settings.dry_run_research:
        return _drop_known(_mock_results(person), known), "dry-run"

    if use_fallback or not settings.anthropic_api_key:
        text, model = _fallback_text(person, known)
        return _drop_known(parse_results(text), known), model

    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError("anthropic package required; pip install anthropic") from exc

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    try:
        response = _call_with_search(
            client,
            SYSTEM,
            coverage_prompt(person, known),
            max_uses=settings.speaker_web_max_search_uses,
        )
    except Exception:
        if not settings.research_fallback:
            raise
        text, model = _fallback_text(person, known)
        return _drop_known(parse_results(text), known), model
    return (
        _drop_known(parse_results(_extract_text(response)), known),
        settings.claude_model,
    )


def _fallback_text(person: Spokesperson, known: list[WebResult]) -> tuple[str, str]:
    if not settings.research_fallback:
        raise RuntimeError("Claude is unavailable and RESEARCH_FALLBACK is not set.")
    from pipeline.fallback import foundry_search_text

    text, _searches, model = foundry_search_text(
        SYSTEM,
        coverage_prompt(person, known),
        max_uses=settings.speaker_web_max_search_uses,
    )
    return text, model


def parse_results(text: str) -> list[WebResult]:
    payload = _parse_json_payload(text)
    raw = payload.get("results")
    if not isinstance(raw, list):
        return []
    results: list[WebResult] = []
    seen: set[str] = set()
    for item in raw:
        result = _to_result(item)
        if result is None or result.fingerprint in seen:
            continue
        seen.add(result.fingerprint)
        results.append(result)
    return results[:MAX_NEW]


def _to_result(item: Any) -> WebResult | None:
    if not isinstance(item, dict):
        return None
    url = str(item.get("url") or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return None
    kind = str(item.get("kind") or "other").strip().lower()
    published = item.get("published_on")
    if published in {"", "null", "None", None}:
        published = None
    return WebResult(
        url=url,
        fingerprint=url_fingerprint(url),
        title=_clip(item.get("title"), 300) or "Untitled",
        outlet=_clip(item.get("outlet"), 200),
        kind=kind if kind in KINDS else "other",
        summary=_clip(item.get("summary"), 600),
        published_on=_clip(published, 40) or None,
    )


def _clip(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip()


def _drop_known(items: list[WebResult], known: list[WebResult]) -> list[WebResult]:
    have = {item.fingerprint for item in known}
    return [item for item in items if item.fingerprint not in have]


def _mock_results(person: Spokesperson) -> list[WebResult]:
    slug = person.name.lower().replace(" ", "-")
    topic = (person.topics.split(",")[0] if person.topics else "their field").strip()
    return [
        WebResult(
            url=f"https://example.com/coverage/{slug}-interview",
            title=f"{person.name} on {topic} (stub)",
            outlet="Industry Voices (stub)",
            kind="interview",
            summary=(
                f"[dry-run] {person.name} of {person.firm} discusses {topic} "
                "and what clients are asking for this year."
            ),
        )
    ]

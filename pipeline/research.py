"""Claude web-search research for one spokesperson."""

from __future__ import annotations

import json
import re
from typing import Any

from pipeline.config import settings
from pipeline.models import Opportunity, ResearchResult, Spokesperson, url_fingerprint


SYSTEM = """You are an earned-media research assistant for a professional-services \
portfolio company network (The Launch Box / PortCos).

Find timely, pitchable opportunities: press coverage angles, podcast guest calls, \
speaking CFPs, journalist queries (HARO / Qwoted / similar), newsletter features, \
and high-quality backlink/guest-post openings.

You MUST use the web_search tool. Do not rely on training data for opportunities, \
dates, or URLs. Run several distinct searches covering journalist queries, podcast \
guest calls, speaking CFPs, and trade-press story hooks.

Be selective. Only return opportunities that a busy executive would actually want \
to pursue this week. Prefer concrete calls-to-action with URLs over vague trends.

Always copy real URLs from your web search results. Do not invent links.
After searching, respond with JSON only — no markdown fences, no extra prose.
"""


def _user_prompt(person: Spokesperson, avoidance: str = "") -> str:
    prior = person.prior_work or "(none listed)"
    rules = f"\n{avoidance.strip()}\n" if avoidance.strip() else ""
    return f"""Research earned-media opportunities for this spokesperson.

Firm: {person.firm}
Name: {person.name}
Title: {person.title}
Expertise topics: {person.topics}
LinkedIn: {person.linkedin or "n/a"}
Prior articles / references: {prior}
Bio (for angle context): {person.bio[:800] or "n/a"}

Search for recent (roughly last 14–30 days) opportunities matching their expertise:
- journalist / expert-source queries
- podcasts seeking guests on their topics
- speaking / panel / webinar CFPs
- trade press story hooks where they could comment
- guest post / backlink opportunities on reputable industry sites

Return ONLY a JSON object (no markdown fences) with this shape:
{{
  "summary": "1-3 sentence overview of what you found",
  "opportunities": [
    {{
      "title": "short title",
      "url": "https://...",
      "outlet": "publication or show",
      "opportunity_type": "press|podcast|speaking|backlink|journalist_query|other",
      "why_relevant": "1-2 sentences tying this to their expertise",
      "suggested_angle": "concrete pitch angle in 1-2 sentences",
      "deadline": "YYYY-MM-DD, '24 hours', or null",
      "urgent": false
    }}
  ]
}}

Set urgent to true when the window to respond is short (about 48 hours or less),
including journalist queries that expire in 24 hours. Put the remaining time
in deadline (for example "24 hours" or "2026-09-02").

If nothing worthwhile is available, return {{"summary": "...", "opportunities": []}}.
Cap at 5 opportunities, best first.
{rules}"""


def research_person(
    person: Spokesperson,
    *,
    dry_run: bool | None = None,
    avoidance: str = "",
) -> ResearchResult:
    use_dry = settings.dry_run_research or bool(dry_run)
    if use_dry:
        return _mock_research(person)
    if not settings.anthropic_api_key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY is missing. Add it to .env, or pass --dry-run."
        )

    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError("anthropic package required; pip install anthropic") from exc

    client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
    response = _run_live_research(client, person, avoidance=avoidance)
    parsed = _parse_json_payload(_extract_text(response))
    opportunities = [_to_opportunity(item) for item in parsed.get("opportunities", [])]
    opportunities = [o for o in opportunities if o.url]
    return ResearchResult(
        person_id=person.person_id,
        opportunities=opportunities,
        raw_summary=str(parsed.get("summary", "")).strip(),
        search_count=_search_count(response),
    )


def _web_search_tool() -> dict[str, Any]:
    tool: dict[str, Any] = {
        "type": settings.web_search_tool_type,
        "name": "web_search",
        "max_uses": settings.max_search_uses,
        "user_location": {
            "type": "approximate",
            "country": "US",
        },
    }
    # Keep basic search on the direct path so we do not need code-execution.
    if settings.web_search_tool_type != "web_search_20250305":
        tool["allowed_callers"] = ["direct"]
    return tool


def _run_live_research(client: Any, person: Spokesperson, avoidance: str = "") -> Any:
    """Call Claude and continue if a long search returns pause_turn."""
    messages: list[dict[str, Any]] = [
        {"role": "user", "content": _user_prompt(person, avoidance)}
    ]
    last = None
    for _ in range(4):
        last = client.messages.create(
            model=settings.claude_model,
            max_tokens=settings.max_tokens,
            system=SYSTEM,
            tools=[_web_search_tool()],
            messages=messages,
        )
        if getattr(last, "stop_reason", None) != "pause_turn":
            return last
        dumped = last.model_dump(mode="json")
        messages.append({"role": "assistant", "content": dumped.get("content", [])})

    if last is None:
        raise RuntimeError("Claude returned no research response")
    return last


def _extract_text(response: Any) -> str:
    chunks: list[str] = []
    for block in getattr(response, "content", []) or []:
        btype = getattr(block, "type", None)
        if btype == "text":
            chunks.append(getattr(block, "text", ""))
    return "\n".join(chunks).strip()


def _search_count(response: Any) -> int:
    usage = getattr(response, "usage", None)
    server = getattr(usage, "server_tool_use", None)
    count = getattr(server, "web_search_requests", None)
    try:
        return int(count or 0)
    except (TypeError, ValueError):
        return 0


def _parse_json_payload(text: str) -> dict[str, Any]:
    if not text:
        return {"summary": "", "opportunities": []}

    # strip fences if the model ignored instructions
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text

    # find outermost JSON object
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1:
        return {"summary": text[:500], "opportunities": []}

    try:
        data = json.loads(candidate[start : end + 1])
    except json.JSONDecodeError:
        return {"summary": text[:500], "opportunities": []}

    if not isinstance(data, dict):
        return {"summary": "", "opportunities": []}
    return data


def _to_opportunity(item: Any) -> Opportunity:
    if not isinstance(item, dict):
        item = {}
    url = str(item.get("url") or "").strip()
    deadline = item.get("deadline")
    if deadline in {"", "null", "None"}:
        deadline = None
    urgent_raw = item.get("urgent", False)
    if isinstance(urgent_raw, str):
        urgent = urgent_raw.strip().lower() in {"1", "true", "yes"}
    else:
        urgent = bool(urgent_raw)
    return Opportunity(
        title=str(item.get("title") or "Untitled opportunity").strip(),
        url=url,
        outlet=str(item.get("outlet") or "").strip(),
        opportunity_type=str(item.get("opportunity_type") or "other").strip(),
        why_relevant=str(item.get("why_relevant") or "").strip(),
        suggested_angle=str(item.get("suggested_angle") or "").strip(),
        deadline=str(deadline).strip() if deadline else None,
        urgent=urgent,
        fingerprint=url_fingerprint(url) if url else "",
    )


def _mock_research(person: Spokesperson) -> ResearchResult:
    """Deterministic fake hits so the local pipeline can be exercised without API spend."""
    topic = (person.topics.split(",")[0] if person.topics else "industry trends").strip()
    slug = person.name.lower().replace(" ", "-")
    opportunities = [
        Opportunity(
            title=f"Expert sources wanted: {topic}",
            url=f"https://example.com/queries/{slug}-sources",
            outlet="Help a Reporter Out (stub)",
            opportunity_type="journalist_query",
            why_relevant=f"Matches {person.name}'s focus on {topic}.",
            suggested_angle=f"Offer a practitioner take from {person.firm} on {topic}.",
            deadline="24 hours",
            urgent=True,
        ),
        Opportunity(
            title=f"Podcast guest call — {topic}",
            url=f"https://example.com/podcasts/{slug}-episode",
            outlet="Industry Voices (stub)",
            opportunity_type="podcast",
            why_relevant=f"{person.title} perspective from {person.firm} fits the show brief.",
            suggested_angle=f"Pitch a 20-min segment on lessons learned scaling {topic}.",
            deadline=None,
        ),
    ]
    return ResearchResult(
        person_id=person.person_id,
        opportunities=opportunities,
        raw_summary=f"[dry-run] Stubbed two opportunities for {person.name} around '{topic}'.",
    )

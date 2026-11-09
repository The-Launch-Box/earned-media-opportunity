"""Azure AI Foundry research when Claude credits are exhausted.

The Responses API web_search tool retrieves live pages before the model answers.
"""

from __future__ import annotations

from typing import Any

import httpx

from pipeline.config import settings
from pipeline.models import ResearchResult, Spokesperson
from pipeline.research import (
    SYSTEM,
    _parse_json_payload,
    _to_opportunity,
    _user_prompt,
)


class FallbackError(RuntimeError):
    """The configured fallback could not finish research."""


def fallback_research(
    person: Spokesperson,
    *,
    avoidance: str = "",
    brief: str = "",
    http: httpx.Client | None = None,
) -> ResearchResult:
    provider = settings.research_fallback
    client = http or httpx.Client(timeout=300.0)
    owns = http is None
    try:
        if provider == "foundry":
            return _foundry(client, person, avoidance, brief)
        raise FallbackError(
            "RESEARCH_FALLBACK must be 'foundry' "
            f"(got {provider or 'empty'})."
        )
    finally:
        if owns:
            client.close()


def foundry_responses_url(endpoint: str) -> str:
    base = (endpoint or "").strip().rstrip("/")
    if not base:
        return ""
    if base.endswith("/responses"):
        return base
    if base.endswith("/openai/v1"):
        return base + "/responses"
    return base + "/openai/v1/responses"


def parse_foundry_output(payload: dict[str, Any]) -> tuple[str, int]:
    """Return assistant text and the number of web searches in a Responses body."""
    output = payload.get("output")
    texts: list[str] = []
    searches = 0
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "web_search_call":
                searches += 1
            if item.get("type") != "message":
                continue
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"output_text", "text"} and part.get("text"):
                    texts.append(str(part["text"]))
    if not texts and isinstance(payload.get("output_text"), str):
        texts.append(payload["output_text"])
    return "\n".join(texts).strip(), searches


def _foundry(
    client: httpx.Client, person: Spokesperson, avoidance: str, brief: str = ""
) -> ResearchResult:
    if not settings.foundry_endpoint or not settings.foundry_api_key or not settings.foundry_model:
        raise FallbackError(
            "Foundry fallback needs FOUNDRY_ENDPOINT, FOUNDRY_API_KEY, and FOUNDRY_MODEL."
        )
    url = foundry_responses_url(settings.foundry_endpoint)
    response = client.post(
        url,
        headers={
            "api-key": settings.foundry_api_key,
            "Content-Type": "application/json",
        },
        json={
            "model": settings.foundry_model,
            "instructions": SYSTEM,
            "input": _user_prompt(person, avoidance, brief),
            "tools": [{"type": "web_search"}],
            "max_output_tokens": settings.max_tokens,
        },
    )
    payload = _json_or_error(response, "Foundry")
    text, searches = parse_foundry_output(payload)
    return _result_from_text(
        person,
        text,
        search_count=searches,
        model_name=f"{settings.foundry_model} (Azure AI Foundry)",
        provider="foundry",
    )


def _result_from_text(
    person: Spokesperson,
    text: str,
    *,
    search_count: int,
    model_name: str,
    provider: str,
) -> ResearchResult:
    parsed = _parse_json_payload(text)
    opportunities = [_to_opportunity(item) for item in parsed.get("opportunities", [])]
    opportunities = [item for item in opportunities if item.url]
    return ResearchResult(
        person_id=person.person_id,
        opportunities=opportunities,
        raw_summary=str(parsed.get("summary", "")).strip(),
        search_count=search_count,
        model_name=model_name,
        provider=provider,
    )


def _json_or_error(response: httpx.Response, source: str) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise FallbackError(
            f"{source} HTTP {response.status_code}: response was not JSON"
        ) from exc
    if response.status_code >= 400:
        message = _error_text(payload) or response.reason_phrase
        raise FallbackError(f"{source} HTTP {response.status_code}: {message}")
    if not isinstance(payload, dict):
        raise FallbackError(f"{source} response was not a JSON object")
    return payload


def _error_text(payload: Any) -> str:
    if not isinstance(payload, dict):
        return ""
    error = payload.get("error")
    if isinstance(error, dict) and error.get("message"):
        return str(error["message"])
    if isinstance(error, str):
        return error
    if payload.get("message"):
        return str(payload["message"])
    return ""

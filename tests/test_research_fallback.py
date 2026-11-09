"""Claude credit detection, Foundry parsing, and the roundup credit line."""

from __future__ import annotations

from datetime import date

import httpx

from pipeline.config import settings
from pipeline.fallback import fallback_research, foundry_responses_url, parse_foundry_output
from pipeline.models import Spokesperson
from pipeline.research import format_credit_code, is_claude_credit_error
from pipeline.roundup import PersonSection, ResearchNotice, build_roundup


def test_credit_error_matches_anthropic_billing_message() -> None:
    message = (
        "Error code: 400 - {'type': 'error', 'error': {'type': 'invalid_request_error', "
        "'message': 'Your credit balance is too low to access the Anthropic API. "
        "Please go to Plans & Billing to upgrade or purchase credits.'}}"
    )
    assert is_claude_credit_error(RuntimeError(message))
    assert not is_claude_credit_error(RuntimeError("Error code: 500 - overloaded"))
    assert format_credit_code("anthropic_credit_balance", 400) == (
        "anthropic_credit_balance (HTTP 400)"
    )


def test_foundry_url_and_output() -> None:
    assert foundry_responses_url("https://example.openai.azure.com") == (
        "https://example.openai.azure.com/openai/v1/responses"
    )
    assert foundry_responses_url("https://example.openai.azure.com/openai/v1") == (
        "https://example.openai.azure.com/openai/v1/responses"
    )
    text, searches = parse_foundry_output(
        {
            "output": [
                {"type": "web_search_call", "status": "completed"},
                {
                    "type": "message",
                    "content": [
                        {
                            "type": "output_text",
                            "text": '{"summary": "One hit.", "opportunities": []}',
                        }
                    ],
                },
            ]
        }
    )
    assert searches == 1
    assert "One hit." in text


def test_foundry_fallback_reads_show_json() -> None:
    previous = _assign_settings(
        research_fallback="foundry",
        foundry_endpoint="https://example.openai.azure.com",
        foundry_api_key="key",
        foundry_model="gpt-5.5",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/openai/v1/responses")
        assert request.headers["api-key"] == "key"
        body = request.read()
        assert b"web_search" in body
        return httpx.Response(
            200,
            json={
                "output": [
                    {"type": "web_search_call"},
                    {
                        "type": "message",
                        "content": [
                            {
                                "type": "output_text",
                                "text": (
                                    '{"summary": "Found one.", "opportunities": ['
                                    '{"title": "Guest call", "url": "https://example.com/call",'
                                    '"outlet": "Trade Weekly", "opportunity_type": "press",'
                                    '"why_relevant": "Fits.", "suggested_angle": "Pitch it.",'
                                    '"deadline": null, "urgent": false}]}'
                                ),
                            }
                        ],
                    },
                ]
            },
        )

    person = Spokesperson.from_row(firm="TLB", name="Amy Kenly", topics="pricing")
    http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        result = fallback_research(person, http=http)
    finally:
        http.close()
        _restore_settings(previous)
    assert result.provider == "foundry"
    assert result.model_name == "gpt-5.5 (Azure AI Foundry)"
    assert result.search_count == 1
    assert result.opportunities[0].url == "https://example.com/call"


def _assign_settings(**values: str) -> dict[str, str]:
    previous = {name: getattr(settings, name) for name in values}
    for name, value in values.items():
        object.__setattr__(settings, name, value)
    return previous


def _restore_settings(previous: dict[str, str]) -> None:
    for name, value in previous.items():
        object.__setattr__(settings, name, value)


def test_roundup_names_model_and_credit_error() -> None:
    person = Spokesperson.from_row(firm="TLB", name="Amy Kenly")
    roundup = build_roundup(
        [PersonSection(person=person, opportunities=[])],
        generated_on=date(2026, 11, 6),
        notice=ResearchNotice(
            model="claude-sonnet-4-6",
            credit_code="anthropic_credit_balance (HTTP 400)",
            research_incomplete=True,
        ),
    )
    assert roundup.body_text.startswith(
        "Subject: Earned Media Opportunities Roundup [November 6, 2026]\n\n"
        "Research model: claude-sonnet-4-6\n"
    )
    assert "error anthropic_credit_balance (HTTP 400)" in roundup.body_text
    assert "Contact your Claude administrator to restore access." in roundup.body_text
    assert "Functionality will resume when the Claude balance is restored." in roundup.body_text
    assert "run the pipeline again to redo this day's run." in roundup.body_text
    assert "This day's research did not run." in roundup.body_text
    assert "No new earned-media opportunities today." not in roundup.body_text
    assert "Research model: claude-sonnet-4-6" in roundup.body_html

"""Action links stay findable when the email opens the public site."""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

from pipeline.feedback_publish import publish_public_feedback
from pipeline.models import Opportunity, Spokesperson
from pipeline.roundup import PersonSection, build_roundup
from pipeline.storage import LocalStore


def test_publish_keeps_the_email_token_and_a_saved_decision() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _test_publish_keeps_the_email_token_and_a_saved_decision(Path(tmp))


def _test_publish_keeps_the_email_token_and_a_saved_decision(tmp: Path) -> None:
    source = LocalStore(tmp / "local.db")
    public = LocalStore(tmp / "public.db")
    person = Spokesperson.from_row(firm="TLB", name="Amy Kenly", title="CMO", topics="B2B")
    opp = Opportunity(
        title="Podcast guest call",
        url="https://example.com/podcasts/amy",
        outlet="Industry Voices",
        opportunity_type="podcast",
        why_relevant="Fits the beat.",
        suggested_angle="Pitch a practitioner take.",
    )
    source.issue_feedback(person, [opp])
    section = PersonSection(person=person, opportunities=[opp])
    roundup = build_roundup([section], generated_on=date(2026, 11, 10), feedback_base_url="https://func.example")
    source.save_batch(roundup.batch_key, roundup.subject, "ada@example.com", roundup.opportunities)

    added = publish_public_feedback(
        source, [section], roundup, reply_to="ada@example.com", dest=public
    )
    assert added == 1
    found = public.get_feedback(opp.feedback_token)
    assert found is not None
    assert found.title == opp.title
    assert found.item_number == opp.item_number
    assert found.batch_key == roundup.batch_key
    assert public.batch_reply_to(roundup.batch_key) == "ada@example.com"

    public.record_feedback(opp.feedback_token, "no", "Not our beat.")
    again = publish_public_feedback(
        source, [section], roundup, reply_to="ada@example.com", dest=public
    )
    assert again == 0
    kept = public.get_feedback(opp.feedback_token)
    assert kept is not None
    assert kept.verdict == "no"
    assert kept.reason == "Not our beat."

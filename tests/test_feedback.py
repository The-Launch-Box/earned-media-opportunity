"""Yes/No feedback: storage, roundup links, prompt rules, and the feedback page."""

from __future__ import annotations

import tempfile
import threading
from datetime import date
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from pipeline.feedback import _handler, avoidance_guidance, feedback_href
from pipeline.models import Opportunity, Spokesperson
from pipeline.research import _user_prompt
from pipeline.roundup import PersonSection, build_roundup
from pipeline.storage import LocalStore


def _person() -> Spokesperson:
    return Spokesperson.from_row(firm="TLB", name="Amy Kenly", title="CMO", topics="B2B")


def _opp(url: str = "https://example.com/podcasts/amy") -> Opportunity:
    return Opportunity(
        title="Podcast guest call",
        url=url,
        outlet="Industry Voices",
        opportunity_type="podcast",
        why_relevant="Fits the beat.",
        suggested_angle="Pitch a practitioner take.",
    )


def test_rejection_reaches_the_prompt() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _test_rejection_reaches_the_prompt(Path(tmp))


def _test_rejection_reaches_the_prompt(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "pipeline.db")
    person = _person()
    opp = _opp()
    store.issue_feedback(person, [opp])
    token = opp.feedback_token
    assert token
    store.issue_feedback(person, [opp])
    assert opp.feedback_token == token

    saved = store.record_feedback(token, "no", "Pay-to-play directory, not a real show.")
    assert saved is not None
    assert opp.fingerprint in store.rejected_fingerprints()

    guidance = avoidance_guidance(store.rejections())
    assert "Pay-to-play directory" in guidance
    assert "Amy Kenly" in guidance
    prompt = _user_prompt(person, guidance)
    assert "Pay-to-play directory" in prompt
    assert "Standing rules from the team" in prompt

    store.record_feedback(token, "yes")
    assert opp.fingerprint not in store.rejected_fingerprints()
    assert store.rejections() == []


def test_roundup_links_only_when_configured() -> None:
    person = _person()
    opp = _opp()
    opp.feedback_token = "abcDEF_123-token"
    base = "http://127.0.0.1:8787"
    roundup = build_roundup(
        [PersonSection(person=person, opportunities=[opp])],
        generated_on=date(2026, 9, 25),
        feedback_base_url=base,
    )
    yes = feedback_href(base, opp.feedback_token, "yes")
    no = feedback_href(base, opp.feedback_token, "no")
    assert "Is this a worthwhile opportunity?" in roundup.body_text
    assert yes in roundup.body_text
    assert no in roundup.body_html
    assert 'background:#0d6b3d;">Yes</a>' in roundup.body_html
    assert 'background:#9b1c1c;">No</a>' in roundup.body_html

    quiet = build_roundup(
        [PersonSection(person=person, opportunities=[opp])],
        generated_on=date(2026, 9, 25),
        feedback_base_url="",
    )
    assert "worthwhile" not in quiet.body_html


def test_get_does_not_save_and_no_requires_a_reason() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        _test_get_does_not_save_and_no_requires_a_reason(Path(tmp))


def _test_get_does_not_save_and_no_requires_a_reason(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "pipeline.db")
    person = _person()
    opp = _opp()
    store.issue_feedback(person, [opp])
    token = opp.feedback_token

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _handler(store))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    port = httpd.server_address[1]
    try:
        with urlopen(f"http://127.0.0.1:{port}/feedback/{token}/yes") as response:
            page = response.read().decode()
            assert response.status == 200
        assert "Yes" in page
        assert store.get_feedback(token).verdict == ""

        body = urlencode({"reason": "no"}).encode()
        request = Request(
            f"http://127.0.0.1:{port}/feedback/{token}/no",
            data=body,
            method="POST",
        )
        try:
            urlopen(request)
            raise AssertionError("expected a short reason to be rejected")
        except HTTPError as exc:
            assert exc.code == 400
        assert store.get_feedback(token).verdict == ""

        reason = "Wrong audience — this show covers consumer apps."
        body = urlencode({"reason": reason}).encode()
        request = Request(
            f"http://127.0.0.1:{port}/feedback/{token}/no",
            data=body,
            method="POST",
        )
        with urlopen(request) as response:
            saved = response.read().decode()
            assert response.status == 200
        assert "Future roundups will avoid opportunities like this." in saved
        assert reason in saved
        record = store.get_feedback(token)
        assert record is not None
        assert record.verdict == "no"
        assert record.reason == reason
    finally:
        httpd.shutdown()
        thread.join(timeout=5)


if __name__ == "__main__":
    test_rejection_reaches_the_prompt()
    test_roundup_links_only_when_configured()
    test_get_does_not_save_and_no_requires_a_reason()
    print("ok")

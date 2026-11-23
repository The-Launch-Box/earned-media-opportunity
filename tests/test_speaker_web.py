"""Search roundup: per-speaker web coverage that is only ever appended to."""

from __future__ import annotations

from pathlib import Path

from pipeline.dashboard import handle_request
from pipeline.models import Opportunity, ResearchResult, Spokesperson, WebResult
from pipeline.registry import Tenant
from pipeline.research import _user_prompt
from pipeline.runner import PersonRunResult, _refresh_web_results
from pipeline.session import COOKIE, encode_session
from pipeline.speaker_web import coverage_prompt, parse_results, search_speaker_coverage
from pipeline.storage import LocalStore


def _person(bio: str = "Pricing strategist.") -> Spokesperson:
    return Spokesperson.from_row(
        firm="Echelon",
        name="Amy Kenly",
        title="Partner",
        topics="pricing, M&A",
        bio=bio,
    )


def _item(n: int, summary: str = "") -> WebResult:
    return WebResult(
        url=f"https://news.example.com/story-{n}",
        title=f"Story {n}",
        outlet="Trade Weekly",
        kind="article",
        summary=summary or f"Amy Kenly on pricing, part {n}.",
    )


def test_add_web_results_appends_and_never_overwrites(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "pipeline.db")
    person = _person()
    store.save_person(person)

    first = store.add_web_results(person.person_id, [_item(1), _item(2)])
    assert [i.title for i in first] == ["Story 1", "Story 2"]

    # Same URL with a different summary is ignored, not rewritten.
    again = store.add_web_results(
        person.person_id, [_item(1, summary="REWRITTEN"), _item(3)]
    )
    assert [i.title for i in again] == ["Story 3"]

    saved = store.web_results(person.person_id)
    assert len(saved) == 3
    assert {i.summary for i in saved if i.title == "Story 1"} == {
        "Amy Kenly on pricing, part 1."
    }
    assert store.web_result_counts() == {person.person_id: 3}


def test_roster_sync_keeps_web_results(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "pipeline.db")
    person = _person()
    store.sync_roster([person])
    store.add_web_results(person.person_id, [_item(1)])

    store.sync_roster([_person(bio="A brand new bio from the workbook.")])
    store.save_person(_person(bio="Edited on the dashboard."))

    assert [i.title for i in store.web_results(person.person_id)] == ["Story 1"]


def test_delete_web_result_only_removes_one(tmp_path: Path) -> None:
    store = LocalStore(tmp_path / "pipeline.db")
    person = _person()
    store.add_web_results(person.person_id, [_item(1), _item(2)])
    target = _item(1).fingerprint
    assert store.delete_web_result(person.person_id, target)
    assert not store.delete_web_result(person.person_id, target)
    assert [i.title for i in store.web_results(person.person_id)] == ["Story 2"]


def test_parse_results_drops_bad_urls_and_duplicates() -> None:
    text = """```json
    {"results": [
      {"title": "Good", "url": "https://a.example.com/x", "kind": "interview",
       "summary": "She explains pricing.", "published_on": "2026-11-01"},
      {"title": "Same", "url": "https://a.example.com/x/"},
      {"title": "No link", "url": ""},
      {"title": "Script", "url": "javascript:alert(1)"},
      {"title": "Odd kind", "url": "https://b.example.com/y", "kind": "tweet",
       "published_on": "null"}
    ]}
    ```"""
    results = parse_results(text)
    assert [r.title for r in results] == ["Good", "Odd kind"]
    assert results[0].kind == "interview"
    assert results[0].published_on == "2026-11-01"
    assert results[1].kind == "other"
    assert results[1].published_on is None


def test_known_urls_are_listed_and_filtered() -> None:
    person = _person()
    known = [_item(1)]
    prompt = coverage_prompt(person, known)
    assert "https://news.example.com/story-1" in prompt
    assert "Echelon" in prompt and "Amy Kenly" in prompt

    found, model = search_speaker_coverage(person, [], dry_run=True)
    assert model == "dry-run" and len(found) == 1
    again, _ = search_speaker_coverage(person, found, dry_run=True)
    assert again == []


def test_research_prompt_includes_recent_coverage() -> None:
    person = _person()
    assert "Recent coverage of this speaker" not in _user_prompt(person)
    person.web_results = [_item(1, summary="Quoted on deal pricing.")]
    prompt = _user_prompt(person)
    assert "Recent coverage of this speaker" in prompt
    assert "Quoted on deal pricing." in prompt
    assert "https://news.example.com/story-1" in prompt


def test_refresh_appends_and_failures_only_warn(tmp_path: Path, monkeypatch) -> None:
    store = LocalStore(tmp_path / "pipeline.db")
    person = _person()
    store.add_web_results(person.person_id, [_item(1)])

    result = PersonRunResult(person=person)
    covered = _refresh_web_results(store, person, result, dry_run=True, use_fallback=False)
    assert result.web_new == 1
    assert len(person.web_results) == 2
    assert _item(1).fingerprint in covered

    def boom(*_args, **_kwargs):
        raise RuntimeError("search exploded")

    monkeypatch.setattr("pipeline.runner.search_speaker_coverage", boom)
    result = PersonRunResult(person=person)
    covered = _refresh_web_results(store, person, result, dry_run=False, use_fallback=False)
    assert result.web_new == 0
    assert len(covered) == 2
    assert len(store.web_results(person.person_id)) == 2


def test_run_never_pitches_coverage_back(tmp_path: Path, monkeypatch) -> None:
    from pipeline import runner

    store = LocalStore(tmp_path / "pipeline.db")
    person = _person()
    store.save_person(person)
    store.add_web_results(person.person_id, [_item(1)])

    def research(p, **_kwargs):
        assert p.web_results, "coverage should reach the research prompt"
        return ResearchResult(
            person_id=p.person_id,
            opportunities=[
                Opportunity(
                    title="Already coverage",
                    url="https://news.example.com/story-1",
                    opportunity_type="press",
                ),
                Opportunity(
                    title="Real opening",
                    url="https://podcast.example.com/guests",
                    opportunity_type="podcast",
                ),
            ],
            model_name="test",
            provider="claude",
        )

    monkeypatch.setattr(runner, "research_person", research)
    monkeypatch.setattr(runner, "_attach_radar", lambda *a, **k: None)
    monkeypatch.setattr(runner, "_attach_podcasts", lambda *a, **k: None)
    outcome = runner._execute_run(
        store,
        [person],
        newcomers=[],
        synced=1,
        dry_run=True,
        skip_empty=False,
        outbox_dir=tmp_path / "outbox",
        delivery_mode="none",
    )
    assert outcome.results[0].found == 2
    assert outcome.results[0].new == 1


class _Registry:
    def __init__(self, tenant: Tenant) -> None:
        self.tenant = tenant

    def get_tenant(self, slug: str):
        return self.tenant if slug == self.tenant.slug else None


def _dashboard(monkeypatch, tmp_path: Path) -> tuple[LocalStore, Spokesperson, dict]:
    monkeypatch.setenv("SESSION_SECRET", "test-secret-for-speaker-web")
    tenant = Tenant(
        slug="echelon",
        name="Echelon",
        domains=("echelon.com",),
        provisioned_at="2026-01-01T00:00:00+00:00",
    )
    store = LocalStore(tmp_path / "pipeline.db")
    person = _person()
    store.save_person(person)
    monkeypatch.setattr("pipeline.dashboard.default_registry", lambda: _Registry(tenant))
    monkeypatch.setattr("pipeline.dashboard._needs_database", lambda _t: False)
    monkeypatch.setattr("pipeline.dashboard._roster_store", lambda _t: store)
    token = encode_session(
        {"email": "pat@echelon.com", "name": "Pat", "slug": "echelon", "csrf": "csrf-ok"}
    )
    return store, person, {"Cookie": f"{COOKIE}={token}"}


def test_roundup_tab_shows_summary_above_link(monkeypatch, tmp_path: Path) -> None:
    store, person, headers = _dashboard(monkeypatch, tmp_path)
    empty = handle_request("GET", f"/speakers/{person.person_id}/roundup", headers, b"")
    assert empty.status == 200
    assert "Nothing found yet" in empty.body

    store.add_web_results(
        person.person_id,
        [_item(1, summary="Said <script>alert(1)</script> on pricing.")],
    )
    page = handle_request("GET", f"/speakers/{person.person_id}/roundup", headers, b"")
    assert page.status == 200
    assert "Search roundup" in page.body
    assert "<script>alert(1)</script>" not in page.body
    summary_at = page.body.index("&lt;script&gt;alert(1)&lt;/script&gt;")
    link_at = page.body.index('href="https://news.example.com/story-1"')
    assert summary_at < link_at

    bio = handle_request("GET", f"/speakers/{person.person_id}/bio", headers, b"")
    assert bio.status == 200
    assert 'name="bio"' in bio.body
    assert f'href="/speakers/{person.person_id}/roundup"' in bio.body

    listing = handle_request("GET", "/speakers", headers, b"")
    assert "1 found" in listing.body


def test_remove_requires_csrf(monkeypatch, tmp_path: Path) -> None:
    store, person, headers = _dashboard(monkeypatch, tmp_path)
    store.add_web_results(person.person_id, [_item(1)])
    path = f"/speakers/{person.person_id}/roundup/{_item(1).fingerprint}/delete"

    denied = handle_request("POST", path, headers, b"csrf=wrong")
    assert denied.status == 403
    assert len(store.web_results(person.person_id)) == 1

    ok = handle_request("POST", path, headers, b"csrf=csrf-ok")
    assert ok.status in {302, 303}
    assert store.web_results(person.person_id) == []

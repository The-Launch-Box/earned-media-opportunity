"""Daily pipeline orchestration — the local stand-in for the Azure Function body."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from rich.console import Console

from pipeline.config import settings
from pipeline.emailer import deliver
from pipeline.feedback import avoidance_guidance
from pipeline.models import Spokesperson
from pipeline.research import ClaudeCreditError, format_credit_code, research_person
from pipeline.roundup import PersonSection, ResearchNotice, build_roundup
from pipeline.roster import load_roster_from_xlsx
from pipeline.podcasts import enrich_podcasts
from pipeline.radar import enrich_opportunities as enrich_radar
from pipeline.storage import LocalStore, open_store

console = Console()


@dataclass
class PersonRunResult:
    person: Spokesperson
    found: int = 0
    new: int = 0
    is_new_to_roster: bool = False
    error: str | None = None


@dataclass
class PipelineResult:
    synced: int = 0
    new_roster_names: list[str] = field(default_factory=list)
    results: list[PersonRunResult] = field(default_factory=list)
    delivered: bool = False
    outbox_path: str | None = None
    urgent: int = 0
    skipped_empty: bool = False
    used_foundry: bool = False

    @property
    def errors(self) -> list[PersonRunResult]:
        return [r for r in self.results if r.error]


def sync_roster(store: LocalStore | None = None) -> tuple[LocalStore, int, list[Spokesperson]]:
    store = store or open_store()
    people = load_roster_from_xlsx()
    n, newcomers = store.sync_roster(people)
    return store, n, newcomers


def run_daily(
    *,
    dry_run: bool = False,
    person_filter: str | None = None,
    firm_filter: str | None = None,
    skip_empty: bool = False,
    limit: int | None = None,
) -> PipelineResult:
    """
    One morning's work:
      1. Sync roster from Excel (new names are flagged, then researched)
      2. For each person: Claude research → dedupe against sent_opportunities
      3. Rank remaining outlets with Cloudflare Radar (site traffic rank)
      4. Build one Time-Sensitive / Other blast
      5. Deliver once, then record newly sent fingerprints
    """
    store, synced, newcomers = sync_roster()
    people = store.list_roster()

    if firm_filter:
        people = [p for p in people if p.firm.lower() == firm_filter.lower()]
    if person_filter:
        needle = person_filter.lower()
        people = [p for p in people if needle in p.name.lower()]
    if limit is not None:
        people = people[:limit]

    return _execute_run(
        store,
        people,
        newcomers=newcomers,
        synced=synced,
        dry_run=dry_run,
        skip_empty=skip_empty,
        filtered=bool(firm_filter or person_filter or limit is not None),
    )


def run_for_tenant(
    tenant,
    *,
    dry_run: bool = False,
    delivery_mode: str | None = None,
    outbox_dir: Path | None = None,
) -> PipelineResult:
    """Research one company's roster and send only to that company's recipients.

    The Launch Box list is the shared workbook already in blob storage.
    Other companies use the speakers saved on their own page.
    """
    from pipeline.provision import open_tenant_store

    store = open_tenant_store(tenant)
    newcomers: list = []
    if getattr(tenant, "slug", "") == "thelaunchbox":
        from pipeline.roster import load_roster_from_xlsx

        _, newcomers = store.sync_roster(load_roster_from_xlsx())
    people = store.list_roster()
    directory = outbox_dir or (settings.outbox_dir / tenant.slug)
    return _execute_run(
        store,
        people,
        newcomers=newcomers,
        synced=len(people),
        dry_run=dry_run,
        skip_empty=False,
        blast_to=tenant.blast_to,
        outbox_dir=directory,
        delivery_mode=delivery_mode,
        file_slug=tenant.slug,
        empty_message="No speakers yet. Add speakers in the dashboard, then run again.",
        brief=getattr(tenant, "research_notes", "") or "",
    )


def _execute_run(
    store: LocalStore,
    people: list[Spokesperson],
    *,
    newcomers: list[Spokesperson],
    synced: int,
    dry_run: bool,
    skip_empty: bool,
    filtered: bool = False,
    blast_to: str | None = None,
    outbox_dir: Path | None = None,
    delivery_mode: str | None = None,
    file_slug: str = "",
    empty_message: str = "No spokespeople matched. Check the SME workbook or filters.",
    brief: str = "",
) -> PipelineResult:
    new_ids = {p.person_id for p in newcomers}
    outcome = PipelineResult(
        synced=synced,
        new_roster_names=[p.name for p in newcomers],
    )
    if newcomers:
        console.print(
            "[green]New on the SME list:[/] "
            + ", ".join(f"{p.firm} · {p.name}" for p in newcomers)
        )

    if dry_run:
        console.print(
            f"[dim]Dry-run stubs for {len(people)} SME"
            f"{'s' if len(people) != 1 else ''}"
            + (" (filtered)" if filtered else " (full roster)")
            + ".[/]"
        )
    else:
        console.print(
            f"[yellow]Live Claude + web search for {len(people)} SME"
            f"{'s' if len(people) != 1 else ''}"
            + (" (filtered)" if filtered else " on the full portfolio roster")
            + "[/]"
        )
    if not people:
        console.print(f"[red]{empty_message}[/]")
        return outcome

    rejections = store.rejections()
    avoidance = avoidance_guidance(rejections)
    if rejections:
        console.print(
            f"Research will avoid {len(rejections)} rejected opportunity pattern"
            f"{'s' if len(rejections) != 1 else ''}."
        )

    sections: list[PersonSection] = []
    models: list[str] = []
    credit_code = ""
    credit_status: int | None = None
    completed = 0
    claude_down = False
    use_fallback = bool(settings.research_fallback) and not dry_run

    for person in people:
        is_new = person.person_id in new_ids
        label = " (new)" if is_new else ""
        console.print(
            f"[bold]{person.firm}[/] · {person.name}{label} — researching…",
            highlight=False,
        )
        result = PersonRunResult(person=person, is_new_to_roster=is_new)
        if credit_code and not use_fallback:
            result.error = (
                f"Claude credits are exhausted (error {credit_code}). "
                "Remaining people were not sent to Claude."
            )
            console.print(f"  [yellow]{result.error}[/]")
            outcome.results.append(result)
            continue
        try:
            research = research_person(
                person,
                dry_run=dry_run,
                avoidance=avoidance,
                brief=brief,
                force_fallback=claude_down and use_fallback,
                mark_credit=bool(credit_code),
                credit_http_status=credit_status,
            )
            if research.provider == "foundry":
                outcome.used_foundry = True
            if research.model_name and research.model_name not in models:
                models.append(research.model_name)
                if research.provider not in {"", "claude", "dry-run"}:
                    console.print(f"  research model: {research.model_name}")
            if research.provider == "foundry" and not claude_down:
                claude_down = True
                if research.claude_credit_code:
                    credit_code = research.claude_credit_code
                    credit_status = research.claude_credit_http_status
                    console.print(
                        f"  [yellow]Claude credits are exhausted (error {credit_code}). "
                        f"Continuing on {research.model_name}.[/]"
                    )
                else:
                    console.print(
                        "  [yellow]Claude returned an error. "
                        f"Continuing on {research.model_name}.[/]"
                    )
            completed += 1
            result.found = len(research.opportunities)
            fresh = store.filter_new(person.person_id, research.opportunities)
            blocked = store.rejected_fingerprints()
            kept = [opp for opp in fresh if opp.fingerprint not in blocked]
            skipped = len(fresh) - len(kept)
            if skipped:
                console.print(
                    f"  skipped {skipped} previously rejected"
                )
            fresh = kept
            result.new = len(fresh)
            if research.search_count:
                console.print(
                    f"  {research.search_count} web search"
                    f"{'es' if research.search_count != 1 else ''}"
                )
            console.print(f"  found {result.found}, new {result.new}")

            if fresh:
                sections.append(
                    PersonSection(
                        person=person,
                        opportunities=fresh,
                        is_new=is_new,
                    )
                )
        except ClaudeCreditError as exc:
            credit_code = format_credit_code(exc.code, exc.http_status)
            credit_status = exc.http_status
            claude_down = True
            result.error = str(exc)
            console.print(f"  [red]error:[/] {exc}")
        except Exception as exc:  # noqa: BLE001 — keep the run going for others
            if use_fallback and _claude_call_failed(exc):
                claude_down = True
            result.error = str(exc)
            console.print(f"  [red]error:[/] {exc}")

        outcome.results.append(result)

    if sections:
        _attach_radar(sections, store=store, dry_run=dry_run)
        _attach_podcasts(sections, store=store, dry_run=dry_run)
        for section in sections:
            store.issue_feedback(section.person, section.opportunities)
        if not settings.feedback_public_url:
            console.print(
                "[yellow]FEEDBACK_PUBLIC_URL is unset — "
                "the roundup will not include Go / No-Go / LinkedIn buttons.[/]"
            )

    if skip_empty and not sections:
        outcome.skipped_empty = True
        console.print("  no new items — skipping email")
        return outcome

    model_label = ", ".join(models)
    if not model_label:
        model_label = "dry-run stub" if dry_run else settings.claude_model
    roundup = build_roundup(
        sections,
        notice=ResearchNotice(
            model=model_label,
            credit_code=credit_code,
            research_incomplete=bool(credit_code) and completed == 0,
        ),
    )
    reply_to = settings.blast_to if blast_to is None else (blast_to or "")
    if roundup.batch_key:
        store.save_batch(
            roundup.batch_key,
            roundup.subject,
            reply_to,
            roundup.opportunities,
        )
    _publish_links(store, sections, roundup, reply_to)
    delivery = deliver(
        roundup,
        blast_to=blast_to,
        outbox_dir=outbox_dir,
        delivery_mode=delivery_mode,
        file_slug=file_slug,
    )
    outcome.outbox_path = str(delivery.path) if delivery.path else None
    outcome.urgent = roundup.urgent_count

    graph_mode = delivery.mode == "graph"
    sent_ok = delivery.mailed if graph_mode else not delivery.skipped
    outcome.delivered = sent_ok

    if sent_ok:
        for section in sections:
            store.record_sent(section.person.person_id, section.opportunities)
    elif graph_mode:
        console.print(
            "  [yellow]fingerprints not recorded — fix Graph send and re-run "
            "to retry the same opportunities[/]"
        )

    if outcome.outbox_path:
        console.print(f"  blast → {outcome.outbox_path}")
    console.print(
        f"  {len(roundup.opportunities)} new opportunities "
        f"({roundup.urgent_count} time-sensitive)"
    )
    return outcome


def _publish_links(store: LocalStore, sections: list[PersonSection], roundup, reply_to: str) -> None:
    """Put the email's tokens where the public site looks them up."""
    if not sections:
        return
    from pipeline.feedback_publish import publish_public_feedback

    try:
        added = publish_public_feedback(store, sections, roundup, reply_to=reply_to)
    except Exception as exc:  # noqa: BLE001 — the roundup still sends
        console.print(f"[yellow]Action links were not published to the public site:[/] {exc}")
        return
    if added:
        console.print(
            f"  published {added} action link"
            f"{'s' if added != 1 else ''} to the public site"
        )


def _claude_call_failed(exc: BaseException) -> bool:
    """True when Claude's API call failed and Foundry could not finish it."""
    text = str(exc)
    return text.startswith("Claude failed (") or text.startswith("Claude is unavailable")


def _attach_radar(
    sections: list[PersonSection],
    *,
    store: LocalStore,
    dry_run: bool,
) -> None:
    """Rank outlet domains once per run, whether Claude or Foundry found them.

    Platform hosts are skipped. A missing Cloudflare token omits the Site line
    and does not stop the email.
    """
    flat = [opp for section in sections for opp in section.opportunities]
    if not flat:
        return
    try:
        enriched, stats = enrich_radar(flat, dry_run=dry_run, store=store)
    except Exception as exc:  # noqa: BLE001 — roundup still goes out
        console.print(f"[yellow]Site traffic skipped:[/] {exc}")
        return

    by_fp = {opp.fingerprint: opp for opp in enriched if opp.fingerprint}
    for section in sections:
        section.opportunities = [
            by_fp.get(opp.fingerprint, opp) for opp in section.opportunities
        ]

    if stats.skipped and stats.fetched == 0 and stats.cached == 0:
        console.print(f"[yellow]Site traffic skipped:[/] {stats.skipped}")
        return
    if not stats.domains:
        return
    detail = f"{stats.cached} cache, {stats.fetched} live"
    if stats.unlisted:
        detail += f", {stats.unlisted} outside top 1M"
    if stats.failed:
        detail += f", {stats.failed} failed"
    console.print(
        f"Site traffic (Cloudflare Radar): {stats.domains} unique domain"
        f"{'s' if stats.domains != 1 else ''} ({detail})"
    )
    if stats.skipped:
        console.print(f"  [yellow]stopped early:[/] {stats.skipped}")


def _attach_podcasts(
    sections: list[PersonSection],
    *,
    store: LocalStore,
    dry_run: bool,
) -> None:
    """Look up Spotify for podcast items and stamp a listener estimate."""
    flat = [opp for section in sections for opp in section.opportunities]
    if not flat:
        return
    try:
        enriched, stats = enrich_podcasts(flat, dry_run=dry_run, store=store)
    except Exception as exc:  # noqa: BLE001 — roundup still goes out
        console.print(f"[yellow]Podcast lookup skipped:[/] {exc}")
        return

    by_fp = {opp.fingerprint: opp for opp in enriched if opp.fingerprint}
    for section in sections:
        section.opportunities = [
            by_fp.get(opp.fingerprint, opp) for opp in section.opportunities
        ]

    if stats.skipped and stats.fetched == 0 and stats.cached == 0:
        console.print(f"[yellow]Podcast lookup skipped:[/] {stats.skipped}")
        return
    if not stats.checked:
        return
    detail = f"{stats.cached} cache, {stats.fetched} live, {stats.matched} on Spotify"
    if stats.failed:
        detail += f", {stats.failed} failed"
    console.print(
        f"Podcasts: {stats.checked} show"
        f"{'s' if stats.checked != 1 else ''} ({detail})"
    )
    if stats.skipped:
        console.print(f"  [yellow]stopped early:[/] {stats.skipped}")

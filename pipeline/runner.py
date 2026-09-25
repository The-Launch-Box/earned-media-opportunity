"""Daily pipeline orchestration — the local stand-in for the Azure Function body."""

from __future__ import annotations

from dataclasses import dataclass, field

from rich.console import Console

from pipeline.config import settings
from pipeline.emailer import deliver
from pipeline.feedback import avoidance_guidance
from pipeline.models import Spokesperson
from pipeline.research import research_person
from pipeline.roundup import PersonSection, build_roundup
from pipeline.roster import load_roster_from_xlsx
from pipeline.semrush import enrich_opportunities
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
      3. Score remaining outlets with Semrush (visits + legitimacy)
      4. Build one Time-Sensitive / Other blast
      5. Deliver once, then record newly sent fingerprints
    """
    store, synced, newcomers = sync_roster()
    new_ids = {p.person_id for p in newcomers}
    people = store.list_roster()

    if firm_filter:
        people = [p for p in people if p.firm.lower() == firm_filter.lower()]
    if person_filter:
        needle = person_filter.lower()
        people = [p for p in people if needle in p.name.lower()]
    if limit is not None:
        people = people[:limit]

    outcome = PipelineResult(
        synced=synced,
        new_roster_names=[p.name for p in newcomers],
    )
    if newcomers:
        console.print(
            "[green]New on the SME list:[/] "
            + ", ".join(f"{p.firm} · {p.name}" for p in newcomers)
        )

    filtered = bool(firm_filter or person_filter or limit is not None)
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
        console.print("[red]No spokespeople matched. Check the SME workbook or filters.[/]")
        return outcome

    rejections = store.rejections()
    avoidance = avoidance_guidance(rejections)
    if rejections:
        console.print(
            f"Research will avoid {len(rejections)} rejected opportunity pattern"
            f"{'s' if len(rejections) != 1 else ''}."
        )

    sections: list[PersonSection] = []

    for person in people:
        is_new = person.person_id in new_ids
        label = " (new)" if is_new else ""
        console.print(
            f"[bold]{person.firm}[/] · {person.name}{label} — researching…",
            highlight=False,
        )
        result = PersonRunResult(person=person, is_new_to_roster=is_new)
        try:
            research = research_person(person, dry_run=dry_run, avoidance=avoidance)
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
        except Exception as exc:  # noqa: BLE001 — keep the run going for others
            result.error = str(exc)
            console.print(f"  [red]error:[/] {exc}")

        outcome.results.append(result)

    if sections:
        _attach_semrush(sections, store=store, dry_run=dry_run)
        if settings.feedback_public_url:
            for section in sections:
                store.issue_feedback(section.person, section.opportunities)
        else:
            console.print(
                "[yellow]FEEDBACK_PUBLIC_URL is unset — "
                "the roundup will not include Yes/No links.[/]"
            )

    if skip_empty and not sections:
        outcome.skipped_empty = True
        console.print("  no new items — skipping email")
        return outcome

    roundup = build_roundup(sections)
    delivery = deliver(roundup)
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


def _attach_semrush(
    sections: list[PersonSection],
    *,
    store: LocalStore,
    dry_run: bool,
) -> None:
    """Score unique outlet domains once per run and stamp each opportunity."""
    flat = [opp for section in sections for opp in section.opportunities]
    if not flat:
        return
    try:
        enriched, stats = enrich_opportunities(flat, dry_run=dry_run, store=store)
    except Exception as exc:  # noqa: BLE001 — roundup still goes out
        console.print(f"[yellow]Semrush skipped:[/] {exc}")
        return

    by_fp = {opp.fingerprint: opp for opp in enriched if opp.fingerprint}
    for section in sections:
        section.opportunities = [
            by_fp.get(opp.fingerprint, opp) for opp in section.opportunities
        ]

    if stats.skipped and stats.fetched == 0 and stats.cached == 0:
        console.print(f"[yellow]Semrush skipped:[/] {stats.skipped}")
        return
    if not stats.domains:
        return
    detail = f"{stats.cached} cache, {stats.fetched} live"
    if stats.failed:
        detail += f", {stats.failed} failed"
    console.print(
        f"Semrush: {stats.domains} unique domain"
        f"{'s' if stats.domains != 1 else ''} ({detail})"
    )
    if stats.skipped:
        console.print(f"  [yellow]stopped early:[/] {stats.skipped}")

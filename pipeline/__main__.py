"""CLI: python -m pipeline <command>"""

from __future__ import annotations

import argparse
import os
import sys

from rich.console import Console
from rich.table import Table

from pipeline.runner import run_daily, sync_roster
from pipeline.storage import open_store

console = Console()


def cmd_sync(_args: argparse.Namespace) -> int:
    store, n, newcomers = sync_roster()
    console.print(f"Synced [bold]{n}[/] spokespeople into {store.db_path}")
    if newcomers:
        console.print(
            "New: " + ", ".join(f"{p.firm} · {p.name}" for p in newcomers)
        )
    return 0


def cmd_roster(_args: argparse.Namespace) -> int:
    store = open_store()
    people = store.list_roster()
    if not people:
        store, _, _ = sync_roster(store)
        people = store.list_roster()

    table = Table(title="Earned media roster")
    table.add_column("Firm")
    table.add_column("Name")
    table.add_column("Title")
    table.add_column("Topics", max_width=40)
    table.add_column("Email")

    for p in people:
        table.add_row(
            p.firm,
            p.name,
            p.title,
            (p.topics[:37] + "…") if len(p.topics) > 40 else p.topics,
            p.email or "—",
        )
    console.print(table)
    console.print(f"{len(people)} people · sent fingerprints: {store.sent_count()}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    # Live Claude + web search for the full SME roster. --dry-run opts into stubs.
    dry_run = args.dry_run
    if not dry_run:
        from pipeline.config import settings

        if not settings.anthropic_api_key:
            console.print(
                "[red]Live run requires ANTHROPIC_API_KEY in .env "
                "(copy from .env.example). Pass --dry-run to skip the API.[/]"
            )
            return 1

    result = run_daily(
        dry_run=dry_run,
        person_filter=args.person,
        firm_filter=args.firm,
        skip_empty=args.skip_empty,
        limit=args.limit,
    )

    errors = result.errors
    extra = ""
    if result.new_roster_names:
        extra += f", new_roster={len(result.new_roster_names)}"
    if result.outbox_path:
        extra += f"\nBlast: {result.outbox_path}"
    console.print(
        f"\nDone. Roster synced={result.synced}, "
        f"processed={len(result.results)}, "
        f"urgent={result.urgent}, errors={len(errors)}"
        f"{extra}"
    )
    return 1 if errors else 0


def cmd_auth(_args: argparse.Namespace) -> int:
    from pipeline.config import settings
    from pipeline.graph_mail import acquire_graph_token

    if not settings.azure_tenant_id or not settings.azure_client_id:
        console.print(
            "[red]Set AZURE_TENANT_ID and AZURE_CLIENT_ID in .env first.[/]"
        )
        return 1
    acquire_graph_token(interactive=True)
    console.print(
        "[green]Microsoft login cached.[/] "
        "python -m pipeline run can now send to "
        f"{settings.blast_to or 'BLAST_TO'}."
    )
    return 0


def cmd_sent(args: argparse.Namespace) -> int:
    store = open_store()
    console.print(f"Total sent fingerprints: {store.sent_count()}")
    if args.dump:
        console.print(store.export_debug())
    return 0


def cmd_serve(_args: argparse.Namespace) -> int:
    from pipeline.config import settings
    from pipeline.dashboard import serve_site
    from pipeline.ms_login import microsoft_configured

    host = settings.feedback_bind
    port = settings.feedback_port
    local_url = f"http://{host}:{port}"
    public = settings.feedback_public_url or local_url
    console.print(f"Dashboard: {local_url}")
    console.print(f"Go / No-Go / LinkedIn links use: {public}")
    if microsoft_configured():
        console.print("[green]Microsoft sign-in is configured.[/]")
    else:
        console.print(
            "[yellow]Microsoft sign-in is not configured.[/] "
            "Set DASHBOARD_CLIENT_ID and DASHBOARD_CLIENT_SECRET."
        )
    if os.getenv("DASHBOARD_DEV_LOGIN", "").strip().lower() in {"1", "true", "yes"}:
        console.print(
            "[yellow]Developer sign-in is on.[/] "
            "Turn off DASHBOARD_DEV_LOGIN before sharing this site."
        )
    if not settings.feedback_public_url:
        console.print(
            "[yellow]Set FEEDBACK_PUBLIC_URL="
            f"{local_url} in .env so the roundup email includes the action buttons.[/]"
        )
    serve_site(host, port)
    return 0


def cmd_tenants(_args: argparse.Namespace) -> int:
    from pipeline.registry import default_registry

    registry = default_registry()
    for tenant in registry.list_tenants():
        console.print(f"[bold]{tenant.name}[/] ({', '.join(tenant.domains)})")
        where = tenant.storage_account or tenant.db_path or "not created yet"
        console.print(f"  database: {tenant.storage_backend or '—'} {where}")
        console.print(
            f"  schedule: {tenant.cadence or 'manual'}  next: {tenant.next_run_at or '—'}"
        )
        console.print(f"  recipients: {tenant.blast_to or '—'}")
        console.print(f"  Microsoft tenant: {tenant.entra_tenant_id or 'not bound yet'}")
        if tenant.provision_error:
            console.print(f"  [red]{tenant.provision_error}[/]")
    return 0


def cmd_feedback(_args: argparse.Namespace) -> int:
    store = open_store()
    decisions = list(store.latest_decisions().values())
    goes = [row for row in decisions if row.verdict == "go"]
    nos = [row for row in decisions if row.verdict == "no"]
    console.print(f"{len(goes)} go · {len(nos)} no-go")
    for record in store.rejections(limit=50):
        number = f"{record.item_number}. " if record.item_number else ""
        console.print(f"[red]no-go[/] {number}{record.person_name}: {record.title}")
        console.print(f"  {record.reason}")
    return 0


def cmd_replies(args: argparse.Namespace) -> int:
    """Save numbered decline lines from a file, or from the sending mailbox."""
    from pipeline.decisions import apply_decline_reply
    from pipeline.followup import _open_stores, ingest_inbox

    stores = list(_open_stores())
    if not stores:
        stores = [open_store()]
    if args.file:
        text = open(args.file, encoding="utf-8").read()
        subject, _, body = text.partition("\n")
        if subject.lower().startswith("subject:"):
            subject = subject.split(":", 1)[1].strip()
            body = body.lstrip("\n")
        else:
            body = text
            subject = ""
        saved = []
        for store in stores:
            saved.extend(apply_decline_reply(store, subject, body))
        console.print(f"Saved {len(saved)} no-go reason(s) from {args.file}.")
        for record in saved:
            console.print(f"  {record.item_number}. {record.title}: {record.reason}")
        return 0 if saved else 1
    count = ingest_inbox(stores)
    console.print(f"Saved {count} no-go reason(s) from the inbox.")
    return 0


def cmd_podcast(args: argparse.Namespace) -> int:
    from pipeline.config import settings
    from pipeline.podcasts import PodcastClient

    if not args.dry_run and not (
        settings.spotify_client_id and settings.spotify_client_secret
    ):
        console.print(
            "[red]SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are missing from .env. "
            "Pass --dry-run to print a stub listener estimate.[/]"
        )
        return 1

    client = PodcastClient(store=open_store(), dry_run=args.dry_run)
    try:
        audience = client.lookup_query(args.query)
    finally:
        client.close()

    table = Table(title=f"Podcast · {args.query}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("On Spotify", "yes" if audience.on_spotify else "no")
    table.add_row("Show", audience.show_name or "—")
    table.add_row("Spotify URL", audience.spotify_url or "—")
    weekly = (
        f"~{audience.est_weekly_downloads:,}"
        if audience.est_weekly_downloads is not None
        else "—"
    )
    followers = (
        f"~{audience.spotify_followers:,}"
        if audience.spotify_followers is not None
        else "—"
    )
    table.add_row("Estimated weekly downloads", weekly)
    table.add_row("Spotify followers", followers)
    table.add_row("Roundup line", audience.summary_line() or "—")
    table.add_row("Source", audience.source or "—")
    if audience.error:
        table.add_row("Error", audience.error)
    if not args.dry_run and not settings.rephonic_api_key:
        table.add_row(
            "Note",
            "REPHONIC_API_KEY is unset, so listener estimates are omitted",
        )
    console.print(table)
    return 0 if audience.on_spotify or args.dry_run else 1


def cmd_site(args: argparse.Namespace) -> int:
    from pipeline.config import settings
    from pipeline.semrush import SemrushClient, root_domain

    if not args.dry_run and not settings.semrush_api_key:
        console.print(
            "[red]SEMRUSH_API_KEY is missing from .env. "
            "Pass --dry-run to print stub metrics.[/]"
        )
        return 1

    domain = root_domain(args.target)
    if not domain:
        console.print(f"[red]Could not parse a domain from[/] {args.target}")
        return 1

    client = SemrushClient(store=open_store(), dry_run=args.dry_run)
    try:
        metrics = client.lookup(domain)
    finally:
        client.close()

    table = Table(title=f"Semrush · {metrics.domain or domain}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Monthly visits", metrics.visits_display())
    table.add_row("Visit source", metrics.visits_kind or "—")
    table.add_row(
        "Authority Score",
        str(metrics.authority_score) if metrics.authority_score is not None else "—",
    )
    table.add_row("Legitimacy", metrics.legitimacy_display())
    table.add_row("Reason", metrics.legitimacy_reason or "—")
    table.add_row(
        "Referring domains",
        str(metrics.referring_domains) if metrics.referring_domains is not None else "—",
    )
    table.add_row(
        "Organic keywords",
        str(metrics.organic_keywords) if metrics.organic_keywords is not None else "—",
    )
    table.add_row("Source", metrics.source or "—")
    if metrics.error:
        table.add_row("Error", metrics.error)
    console.print(table)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pipeline",
        description="Local daily earned-media roundup pipeline",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_sync = sub.add_parser("sync", help="Load SME Excel into local SQLite roster")
    p_sync.set_defaults(func=cmd_sync)

    p_roster = sub.add_parser("roster", help="Show the current roster")
    p_roster.set_defaults(func=cmd_roster)

    p_run = sub.add_parser(
        "run",
        help="Claude + web search for every SME on the portfolio roster",
    )
    mode = p_run.add_mutually_exclusive_group()
    mode.add_argument(
        "--live",
        action="store_true",
        help="Call Claude + web search (default; the full roster unless filtered)",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Stub opportunities locally, no API calls",
    )
    p_run.add_argument(
        "--person",
        help="Testing only: process names containing this string",
    )
    p_run.add_argument(
        "--firm",
        help="Testing only: process this firm (exact, case-insensitive)",
    )
    p_run.add_argument(
        "--limit",
        type=int,
        help="Testing only: process at most N people",
    )
    p_run.add_argument(
        "--skip-empty",
        action="store_true",
        help="Don't write an outbox email when there are no new opportunities",
    )
    p_run.set_defaults(func=cmd_run)

    p_auth = sub.add_parser(
        "auth",
        help="Sign in to Microsoft (device code / MFA) so Graph can send mail",
    )
    p_auth.set_defaults(func=cmd_auth)

    p_sent = sub.add_parser("sent", help="Show how many opportunities have been filed as sent")
    p_sent.add_argument("--dump", action="store_true", help="Dump full SQLite debug JSON")
    p_sent.set_defaults(func=cmd_sent)

    p_serve = sub.add_parser(
        "serve",
        help="Run the portco dashboard and the action pages",
    )
    p_serve.set_defaults(func=cmd_serve)

    p_tenants = sub.add_parser(
        "tenants",
        help="Show each portfolio company's database and schedule",
    )
    p_tenants.set_defaults(func=cmd_tenants)

    p_feedback = sub.add_parser(
        "feedback",
        help="Show saved go / no-go decisions",
    )
    p_feedback.set_defaults(func=cmd_feedback)

    p_replies = sub.add_parser(
        "replies",
        help="Save no-go lines from a reply file, or from the sending mailbox",
    )
    p_replies.add_argument(
        "--file",
        help="A saved reply. First line may be 'Subject: ...'. Body lines are '2. reason'.",
    )
    p_replies.set_defaults(func=cmd_replies)

    p_podcast = sub.add_parser(
        "podcast",
        help="See whether a show is on Spotify and what its listener estimate is",
    )
    p_podcast.add_argument("query", help="Show name or Spotify show URL")
    p_podcast.add_argument(
        "--dry-run",
        action="store_true",
        help="Return a stub estimate instead of calling Spotify and Rephonic",
    )
    p_podcast.set_defaults(func=cmd_podcast)

    p_site = sub.add_parser(
        "site",
        help="Look up Semrush visits and legitimacy for a domain or URL",
    )
    p_site.add_argument("target", help="Domain or URL, e.g. cio.com")
    p_site.add_argument(
        "--dry-run",
        action="store_true",
        help="Return stub metrics instead of calling Semrush",
    )
    p_site.set_defaults(func=cmd_site)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

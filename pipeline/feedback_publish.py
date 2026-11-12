"""Copy action-link rows to the store the public site reads.

A local run keeps its own SQLite file. The buttons in the email open
FEEDBACK_PUBLIC_URL, which on Azure only searches each company's tables.
Without this copy, those buttons report that the opportunity is not on file.
"""

from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from pipeline.config import settings
from pipeline.models import Opportunity, Roundup, Spokesperson
from pipeline.storage import FeedbackRecord, LocalStore

logger = logging.getLogger("earned-media.feedback")

_LOCAL_HOSTS = frozenset({"", "localhost", "127.0.0.1", "::1"})


def publish_public_feedback(
    store: LocalStore,
    sections: list,
    roundup: Roundup,
    *,
    reply_to: str = "",
    dest: LocalStore | None = None,
) -> int:
    """Write the tokens this email links to. Returns how many rows were added.

    `dest` is for tests. Production opens the company table behind the public URL.
    """
    target = dest if dest is not None else open_public_store(store)
    if target is None:
        return 0
    added = 0
    records: list[FeedbackRecord] = []
    for section in sections:
        person = section.person
        for opp in section.opportunities:
            if not opp.feedback_token:
                continue
            record = _record_from_opp(person, opp)
            records.append(record)
            if target.ensure_feedback_row(record):
                added += 1
    if roundup.batch_key and records:
        target.save_batch(
            roundup.batch_key,
            roundup.subject,
            reply_to,
            [_opp_from_record(record) for record in records if record.item_number],
        )
    return added


def copy_missing_feedback(
    source: LocalStore,
    dest: LocalStore,
    *,
    since: str = "",
) -> int:
    """Insert source tokens that dest does not have yet. Existing decisions stay."""
    added = 0
    chosen: list[FeedbackRecord] = []
    for record in source.list_feedback():
        if since and (record.created_at or "") < since:
            continue
        chosen.append(record)
        if dest.ensure_feedback_row(record):
            added += 1
    batches = {key: (subject, reply) for key, subject, reply in source.list_batches()}
    grouped: dict[str, list[FeedbackRecord]] = {}
    for record in chosen:
        if record.batch_key:
            grouped.setdefault(record.batch_key, []).append(record)
    for key, group in grouped.items():
        subject, reply = batches.get(key, ("", ""))
        opps = [_opp_from_record(record) for record in group if record.item_number]
        if opps:
            dest.save_batch(key, subject or key, reply, opps)
    return added


def open_public_store(store: LocalStore) -> LocalStore | None:
    """The company table the public site searches, or None when this store is it."""
    if not _public_links():
        return None
    account = _account_name()
    if not account:
        raise RuntimeError(
            "Set AZURE_STORAGE_ACCOUNT so the action links are stored "
            "where the public site can read them."
        )
    if _already_public(store, account):
        return None
    _ensure_connection(account)
    from pipeline.provision import tenant_table_prefix
    from pipeline.table_store import TableStore

    slug = _company_slug(store)
    return TableStore(account=account, prefix=tenant_table_prefix(slug))  # type: ignore[return-value]


def _public_links() -> bool:
    raw = os.getenv("FEEDBACK_PUBLIC_URL", settings.feedback_public_url).strip()
    host = (urlparse(raw).hostname or "").lower()
    return host not in _LOCAL_HOSTS


def _account_name() -> str:
    return (os.getenv("AZURE_STORAGE_ACCOUNT") or settings.azure_storage_account or "").strip()


def _already_public(store: object, account: str) -> bool:
    """Prefixed tables on the platform account are the ones the site searches."""
    store_account = getattr(store, "account", "")
    prefix = getattr(store, "prefix", "")
    return bool(store_account) and store_account == account and bool(prefix)


def _company_slug(store: object) -> str:
    path = getattr(store, "db_path", None)
    if isinstance(path, Path) and not str(path).startswith("https://"):
        from pipeline.provision import tenant_data_dir
        from pipeline.registry import company_by_slug

        try:
            rel = path.resolve().relative_to(tenant_data_dir().resolve())
        except ValueError:
            rel = None
        if rel and rel.parts and company_by_slug(rel.parts[0]):
            return rel.parts[0]
    return "thelaunchbox"


def _ensure_connection(account: str) -> None:
    """Use the Function App connection string, or the key from an az login."""
    if os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip():
        return
    if os.getenv("FUNCTIONS_WORKER_RUNTIME"):
        return
    key = _account_key(account)
    if not key:
        raise RuntimeError(
            f"Could not read the storage key for {account}. "
            "Set AZURE_STORAGE_CONNECTION_STRING, or sign in with az."
        )
    os.environ["AZURE_STORAGE_CONNECTION_STRING"] = (
        "DefaultEndpointsProtocol=https;"
        f"AccountName={account};AccountKey={key};EndpointSuffix=core.windows.net"
    )


def _account_key(account: str) -> str:
    try:
        groups = subprocess.check_output(
            [
                "az",
                "storage",
                "account",
                "list",
                "--query",
                f"[?name=='{account}'].resourceGroup",
                "-o",
                "tsv",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        ).split()
    except (OSError, subprocess.CalledProcessError):
        logger.exception("Could not list storage accounts")
        return ""
    if not groups:
        return ""
    try:
        return subprocess.check_output(
            [
                "az",
                "storage",
                "account",
                "keys",
                "list",
                "-g",
                groups[0],
                "-n",
                account,
                "--query",
                "[0].value",
                "-o",
                "tsv",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        logger.exception("Could not read the storage key")
        return ""


def _record_from_opp(person: Spokesperson, opp: Opportunity) -> FeedbackRecord:
    return FeedbackRecord(
        token=opp.feedback_token,
        person_id=person.person_id,
        person_name=person.name,
        fingerprint=opp.fingerprint,
        url=opp.url,
        title=opp.title,
        outlet=opp.outlet,
        opportunity_type=opp.opportunity_type,
        why_relevant=opp.why_relevant,
        suggested_angle=opp.suggested_angle,
        verdict="",
        reason="",
        created_at=datetime.now(timezone.utc).isoformat(),
        decided_at="",
        batch_key=opp.batch_key,
        item_number=opp.item_number,
    )


def _opp_from_record(record: FeedbackRecord) -> Opportunity:
    return Opportunity(
        title=record.title or "Opportunity",
        url=record.url or "https://example.invalid",
        outlet=record.outlet,
        opportunity_type=record.opportunity_type or "other",
        why_relevant=record.why_relevant,
        suggested_angle=record.suggested_angle,
        fingerprint=record.fingerprint,
        feedback_token=record.token,
        item_number=record.item_number,
        batch_key=record.batch_key,
    )

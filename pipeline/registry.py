"""Which portfolio companies may use the dashboard, and their schedules.

Speaker rows, sent links, and feedback live in each company's own database.
This registry only stores the allowlist, the schedule, and where that
database is. On a laptop it is a SQLite file. On Azure it is a table in the
platform storage account, separate from every company's database.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from pipeline.config import settings
from pipeline.schedule import parse_stamp, stamp

_CLAIM = threading.Lock()


class AccessDenied(Exception):
    """The Microsoft account is not one of the portfolio companies."""


@dataclass(frozen=True)
class Company:
    slug: str
    name: str
    domains: tuple[str, ...]


# Closed list. A new company is a code change plus its own database, not a
# domain someone can type into the sign-in form.
CATALOG = (
    Company("thelaunchbox", "The Launch Box", ("thelaunchbox.com",)),
    Company("improv", "Improv", ("improv.com",)),
    Company("hyperscayle", "Hyperscayle", ("hyperscayle.com",)),
    Company("echelon", "Echelon", ("echelon.com",)),
)

# These work accounts can open every company's console. A matching domain
# still signs them into their own company first.
PLATFORM_ADMINS = frozenset(
    {
        "kaylee.scanlin@thelaunchbox.com",
        "esteban.gutierrez@thelaunchbox.com",
        "isabel.espinal@thelaunchbox.com",
    }
)


def is_platform_admin(email: str) -> bool:
    """Exact work-account match. Lookalike addresses are not admins."""
    return (email or "").strip().lower() in PLATFORM_ADMINS


def company_for_email(email: str) -> Company | None:
    """Match the exact email domain. Subdomains and lookalikes do not count."""
    address = (email or "").strip().lower()
    if address.count("@") != 1:
        return None
    domain = address.rsplit("@", 1)[1]
    if not domain or "." not in domain:
        return None
    for company in CATALOG:
        if domain in company.domains:
            return company
    return None


def clean_note_history(raw: object) -> list[dict]:
    """Newest first. Each entry is the note text, when it was saved, and who saved it."""
    if not isinstance(raw, list):
        return []
    cleaned: list[dict] = []
    for item in raw[:30]:
        if not isinstance(item, dict):
            continue
        cleaned.append(
            {
                "text": str(item.get("text") or "")[:2000],
                "saved_at": str(item.get("saved_at") or "")[:40],
                "saved_by": str(item.get("saved_by") or "")[:200],
            }
        )
    return cleaned


def company_by_slug(slug: str) -> Company | None:
    for company in CATALOG:
        if company.slug == slug:
            return company
    return None


@dataclass
class Tenant:
    slug: str
    name: str
    domains: tuple[str, ...]
    entra_tenant_id: str = ""
    storage_backend: str = ""
    storage_account: str = ""
    db_path: str = ""
    blast_to: str = ""
    cadence: str = ""
    weekday: int = 0
    interval_days: int = 14
    hour: int = 7
    minute: int = 0
    timezone: str = "America/Chicago"
    next_run_at: str = ""
    last_run_at: str = ""
    research_notes: str = ""
    note_history: list[dict] = field(default_factory=list)
    provisioned_at: str = ""
    provision_error: str = ""

    def to_json(self) -> dict:
        payload = asdict(self)
        payload["domains"] = list(self.domains)
        payload["note_history"] = clean_note_history(self.note_history)
        return payload

    @classmethod
    def from_json(cls, payload: dict) -> Tenant:
        domains = payload.get("domains") or ()
        data = dict(payload)
        data["domains"] = tuple(str(item) for item in domains)
        data["note_history"] = clean_note_history(data.get("note_history"))
        known = {field for field in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in data.items() if key in known})


@dataclass
class Job:
    id: str
    tenant_slug: str
    cadence: str
    trigger: str
    status: str
    created_at: str
    started_at: str = ""
    finished_at: str = ""
    summary: str = ""
    error: str = ""
    triggered_by: str = ""

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, payload: dict) -> Job:
        known = {field for field in cls.__dataclass_fields__}
        return cls(**{key: value for key, value in payload.items() if key in known})


class DocumentStore:
    def get(self, kind: str, key: str) -> dict | None:
        raise NotImplementedError

    def put(self, kind: str, key: str, payload: dict) -> None:
        raise NotImplementedError

    def delete(self, kind: str, key: str) -> None:
        raise NotImplementedError

    def list(self, kind: str) -> list[dict]:
        raise NotImplementedError


class SqliteDocuments(DocumentStore):
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS documents (
                    kind TEXT NOT NULL,
                    key TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (kind, key)
                )
                """
            )

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def get(self, kind: str, key: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload FROM documents WHERE kind = ? AND key = ?",
                (kind, key),
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["payload"])

    def put(self, kind: str, key: str, payload: dict) -> None:
        blob = json.dumps(payload)
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO documents (kind, key, payload)
                VALUES (?, ?, ?)
                ON CONFLICT(kind, key) DO UPDATE SET payload = excluded.payload
                """,
                (kind, key, blob),
            )

    def delete(self, kind: str, key: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "DELETE FROM documents WHERE kind = ? AND key = ?",
                (kind, key),
            )

    def list(self, kind: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM documents WHERE kind = ?",
                (kind,),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]


class TableDocuments(DocumentStore):
    """Platform control table. Not a company's speaker database."""

    def __init__(self, account: str | None = None) -> None:
        account_name = (account or settings.azure_storage_account).strip()
        if not account_name:
            raise RuntimeError(
                "AZURE_STORAGE_ACCOUNT is required when CONTROL_BACKEND=tables"
            )
        from azure.data.tables import TableServiceClient

        connection = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()
        if connection:
            service = TableServiceClient.from_connection_string(connection)
        else:
            from azure.identity import DefaultAzureCredential

            service = TableServiceClient(
                endpoint=f"https://{account_name}.table.core.windows.net",
                credential=DefaultAzureCredential(),
            )
        self._table = service.create_table_if_not_exists("PortcoControl")

    def get(self, kind: str, key: str) -> dict | None:
        try:
            entity = self._table.get_entity(kind, _doc_key(key))
        except _not_found():
            return None
        return json.loads(str(entity["payload"]))

    def put(self, kind: str, key: str, payload: dict) -> None:
        from azure.data.tables import UpdateMode

        self._table.upsert_entity(
            {
                "PartitionKey": kind,
                "RowKey": _doc_key(key),
                "payload": json.dumps(payload),
            },
            mode=UpdateMode.REPLACE,
        )

    def delete(self, kind: str, key: str) -> None:
        try:
            self._table.delete_entity(kind, _doc_key(key))
        except _not_found():
            return

    def list(self, kind: str) -> list[dict]:
        rows = self._table.query_entities(query_filter=f"PartitionKey eq '{kind}'")
        return [json.loads(str(entity["payload"])) for entity in rows]


class Registry:
    def __init__(self, store: DocumentStore) -> None:
        self.store = store

    def ensure_seeded(self) -> None:
        for company in CATALOG:
            current = self.get_tenant(company.slug)
            if current is None:
                self.save_tenant(_blank_tenant(company))
                continue
            if current.name != company.name or current.domains != company.domains:
                current.name = company.name
                current.domains = company.domains
                self.save_tenant(current)

    def get_tenant(self, slug: str) -> Tenant | None:
        payload = self.store.get("tenant", slug)
        if payload is None:
            return None
        return Tenant.from_json(payload)

    def list_tenants(self) -> list[Tenant]:
        tenants = [Tenant.from_json(row) for row in self.store.list("tenant")]
        order = {company.slug: index for index, company in enumerate(CATALOG)}
        tenants.sort(key=lambda tenant: order.get(tenant.slug, 99))
        return tenants

    def save_tenant(self, tenant: Tenant) -> None:
        if company_by_slug(tenant.slug) is None:
            raise ValueError(f"Unknown company {tenant.slug}")
        self.store.put("tenant", tenant.slug, tenant.to_json())

    def authorize(self, email: str, entra_tenant_id: str, *, bind: bool) -> Tenant:
        """Return the company for this work email, or raise AccessDenied.

        The first real Microsoft sign-in binds the Entra tenant id. Later
        sign-ins from a different tenant are refused, even if the domain matches.
        Developer sign-in does not bind a tenant.
        """
        company = company_for_email(email)
        if company is None:
            raise AccessDenied(
                "That Microsoft account isn't on the list of companies for this workspace."
            )
        self.ensure_seeded()
        tenant = self.get_tenant(company.slug)
        if tenant is None:
            raise AccessDenied("That company is not set up.")
        tid = (entra_tenant_id or "").strip().lower()
        if not bind:
            return tenant
        if not tid:
            raise AccessDenied("Microsoft didn't include a tenant id for that account.")
        if tenant.entra_tenant_id and tenant.entra_tenant_id != tid:
            raise AccessDenied(
                f"{tenant.name} is already tied to a different Microsoft tenant."
            )
        if not tenant.entra_tenant_id:
            tenant.entra_tenant_id = tid
            self.save_tenant(tenant)
        return tenant

    def create_job(
        self,
        slug: str,
        *,
        cadence: str,
        trigger: str,
        triggered_by: str,
    ) -> Job:
        job = Job(
            id=secrets.token_hex(8),
            tenant_slug=slug,
            cadence=cadence,
            trigger=trigger,
            status="queued",
            created_at=stamp(datetime.now(timezone.utc)),
            triggered_by=triggered_by,
        )
        self.store.put("job", job.id, job.to_json())
        return job

    def get_job(self, job_id: str) -> Job | None:
        payload = self.store.get("job", job_id)
        if payload is None:
            return None
        return Job.from_json(payload)

    def claim_job(self, job_id: str) -> Job | None:
        """Move queued -> running. A second worker gets nothing."""
        with _CLAIM:
            job = self.get_job(job_id)
            if job is None or job.status != "queued":
                return None
            job.status = "running"
            job.started_at = stamp(datetime.now(timezone.utc))
            self.store.put("job", job.id, job.to_json())
            return job

    def finish_job(
        self,
        job_id: str,
        *,
        status: str,
        summary: str,
        error: str = "",
    ) -> None:
        job = self.get_job(job_id)
        if job is None:
            return
        job.status = status
        job.summary = summary
        job.error = error
        job.finished_at = stamp(datetime.now(timezone.utc))
        self.store.put("job", job.id, job.to_json())
        self._prune_jobs(job.tenant_slug)

    def jobs_for(self, slug: str, *, limit: int = 8) -> list[Job]:
        rows = [job for job in self._all_jobs() if job.tenant_slug == slug]
        rows.sort(key=lambda job: job.created_at, reverse=True)
        return rows[:limit]

    def active_job(self, slug: str) -> Job | None:
        active = [
            job
            for job in self._all_jobs()
            if job.tenant_slug == slug and job.status in {"queued", "running"}
        ]
        active.sort(key=lambda job: job.created_at, reverse=True)
        return active[0] if active else None

    def queued_jobs(self) -> list[Job]:
        rows = [job for job in self._all_jobs() if job.status == "queued"]
        rows.sort(key=lambda job: job.created_at)
        return rows

    def due_tenants(self, now: datetime) -> list[Tenant]:
        current = stamp(now.astimezone(timezone.utc))
        due: list[Tenant] = []
        for tenant in self.list_tenants():
            if tenant.cadence not in {"daily", "weekly", "custom"}:
                continue
            if not tenant.next_run_at or tenant.next_run_at > current:
                continue
            due.append(tenant)
        return due

    def save_oauth(self, state: str, flow: dict, next_path: str = "") -> None:
        self.store.put(
            "oauth",
            state,
            {
                "flow": flow,
                "saved_at": stamp(datetime.now(timezone.utc)),
                "next": next_path,
            },
        )

    def pop_oauth(self, state: str) -> dict | None:
        payload = self.store.get("oauth", state)
        if payload is None:
            return None
        self.store.delete("oauth", state)
        saved = parse_stamp(str(payload.get("saved_at") or ""))
        if saved is None:
            return None
        age = datetime.now(timezone.utc) - saved
        if age > timedelta(minutes=15):
            return None
        flow = payload.get("flow")
        if not isinstance(flow, dict):
            return None
        flow = dict(flow)
        nxt = str(payload.get("next") or "")
        if nxt.startswith("/feedback/"):
            flow["_emo_next"] = nxt
        return flow

    def _all_jobs(self) -> list[Job]:
        return [Job.from_json(row) for row in self.store.list("job")]

    def _prune_jobs(self, slug: str, *, keep: int = 30) -> None:
        rows = [job for job in self._all_jobs() if job.tenant_slug == slug]
        rows.sort(key=lambda job: job.created_at, reverse=True)
        for job in rows[keep:]:
            if job.status in {"queued", "running"}:
                continue
            self.store.delete("job", job.id)


def control_db_path() -> Path:
    raw = os.getenv("CONTROL_DB_PATH", "").strip()
    if raw:
        return Path(raw)
    return settings.root / "data" / "control.db"


def default_registry() -> Registry:
    backend = os.getenv("CONTROL_BACKEND", "sqlite").strip().lower()
    if backend in {"tables", "table", "azure"}:
        store: DocumentStore = TableDocuments()
    else:
        store = SqliteDocuments(control_db_path())
    registry = Registry(store)
    registry.ensure_seeded()
    return registry


def _blank_tenant(company: Company) -> Tenant:
    return Tenant(slug=company.slug, name=company.name, domains=company.domains)


def _doc_key(value: str) -> str:
    """Table row keys reject some characters. Hash so OAuth state is safe."""
    return hashlib.sha256(value.encode()).hexdigest()[:40]


def _not_found() -> type[Exception]:
    from azure.core.exceptions import ResourceNotFoundError

    return ResourceNotFoundError

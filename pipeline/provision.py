"""Create one database per portfolio company.

Local and single-server installs get a SQLite file each. When TENANT_STORAGE
is `tables`, each company gets its own tables in the platform storage account.
Table names are prefixed with the company slug, so one company cannot read
another company's speakers. The app identity cannot create storage accounts,
so sign-in does not try to.
"""

from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from pipeline.registry import Registry, Tenant
from pipeline.schedule import stamp
from pipeline.storage import LocalStore


def tenant_storage_mode() -> str:
    return os.getenv("TENANT_STORAGE", "sqlite").strip().lower() or "sqlite"


def tenant_data_dir() -> Path:
    raw = os.getenv("TENANT_DATA_DIR", "").strip()
    if raw:
        return Path(raw)
    from pipeline.config import settings

    return settings.root / "data" / "tenants"


def tenant_sqlite_path(slug: str) -> Path:
    return tenant_data_dir() / slug / "pipeline.db"


def tenant_table_prefix(slug: str) -> str:
    """Prefix that keeps one company's tables separate on the shared account."""
    cleaned = re.sub(r"[^A-Za-z0-9]", "", slug)
    if not cleaned or not cleaned[0].isalpha():
        cleaned = "t" + cleaned
    return cleaned[:40]


def storage_account_name(slug: str) -> str:
    """Globally unique storage account name, 3–24 lowercase letters and digits."""
    cleaned = re.sub(r"[^a-z0-9]", "", slug.lower())
    digest = hashlib.sha256(f"earned-media:{slug}".encode()).hexdigest()[:6]
    base = "emo" + cleaned
    return base[: 24 - len(digest)] + digest


def provision_tenant(registry: Registry, tenant: Tenant) -> Tenant:
    """Create the company database if it does not exist yet."""
    if tenant.provisioned_at and _ready(tenant):
        return tenant
    mode = tenant_storage_mode()
    try:
        if mode in {"tables", "table", "azure"}:
            from pipeline.config import settings
            from pipeline.table_store import TableStore

            account = (settings.azure_storage_account or "").strip()
            if not account:
                raise RuntimeError(
                    "AZURE_STORAGE_ACCOUNT is required when TENANT_STORAGE=tables."
                )
            TableStore(account=account, prefix=tenant_table_prefix(tenant.slug))
            tenant.storage_backend = "tables"
            tenant.storage_account = account
            tenant.db_path = ""
        else:
            path = tenant_sqlite_path(tenant.slug)
            LocalStore(path)
            tenant.storage_backend = "sqlite"
            tenant.storage_account = ""
            tenant.db_path = str(path)
        tenant.provisioned_at = stamp(datetime.now(timezone.utc))
        tenant.provision_error = ""
    except Exception as exc:
        tenant.provision_error = str(exc)
        registry.save_tenant(tenant)
        raise
    registry.save_tenant(tenant)
    return tenant


def open_tenant_store(tenant: Tenant) -> LocalStore:
    """Open this company's database and no other."""
    if tenant.storage_backend in {"tables", "table", "azure"}:
        if not tenant.storage_account:
            raise RuntimeError("This company's Azure database has not been created yet.")
        from pipeline.config import settings
        from pipeline.table_store import TableStore

        platform = (settings.azure_storage_account or "").strip()
        prefix = (
            tenant_table_prefix(tenant.slug) if tenant.storage_account == platform else ""
        )
        return TableStore(account=tenant.storage_account, prefix=prefix)  # type: ignore[return-value]
    if not tenant.db_path:
        raise RuntimeError("This company's database has not been created yet.")
    return LocalStore(Path(tenant.db_path))


def ensure_azure_storage_account(slug: str) -> str:
    """Older callers asked for a new account. Return the one this app already uses."""
    del slug
    from pipeline.config import settings

    account = (settings.azure_storage_account or "").strip()
    if not account:
        raise RuntimeError(
            "AZURE_STORAGE_ACCOUNT is required. This app does not create storage accounts."
        )
    return account


def _ready(tenant: Tenant) -> bool:
    if tenant.storage_backend in {"tables", "table", "azure"}:
        return bool(tenant.storage_account)
    return bool(tenant.db_path) and Path(tenant.db_path).exists()

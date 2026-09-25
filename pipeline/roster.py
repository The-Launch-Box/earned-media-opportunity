"""Load the SME roster from the PortCo Excel workbook."""

from __future__ import annotations

import json
from pathlib import Path

from openpyxl import load_workbook

from pipeline.config import settings
from pipeline.models import Spokesperson


HEADER_MARKERS = {"firm", "expert", "title"}


def load_email_overlay(path: Path | None = None) -> dict[str, str]:
    """Optional name/email map so we can deliver without editing the xlsx."""
    path = path or settings.emails_overlay
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    # keys may be "Name" or "Firm|Name"
    return {
        str(k).strip().lower(): str(v).strip()
        for k, v in data.items()
        if v and not str(k).startswith("_")
    }


def _resolve_email(
    overlay: dict[str, str], firm: str, name: str
) -> str | None:
    key_full = f"{firm}|{name}".lower()
    key_name = name.lower()
    return overlay.get(key_full) or overlay.get(key_name)


def resolve_sme_workbook(path: Path | None = None) -> Path:
    """Local Excel file, or a copy downloaded from Blob Storage."""
    if path is not None:
        return path
    if settings.roster_source != "blob":
        return settings.sme_xlsx
    if not settings.azure_storage_account:
        raise RuntimeError(
            "AZURE_STORAGE_ACCOUNT is required when ROSTER_SOURCE=blob"
        )
    import os

    from azure.storage.blob import BlobServiceClient

    dest = Path("/tmp") / "TLB-PortCo-SME-List.xlsx"
    connection = os.getenv("AZURE_STORAGE_CONNECTION_STRING", "").strip()
    if connection:
        service = BlobServiceClient.from_connection_string(connection)
    else:
        from azure.identity import ManagedIdentityCredential

        service = BlobServiceClient(
            account_url=f"https://{settings.azure_storage_account}.blob.core.windows.net",
            credential=ManagedIdentityCredential(),
        )
    blob = service.get_blob_client(settings.roster_container, settings.roster_blob_name)
    dest.write_bytes(blob.download_blob().readall())
    return dest


def load_roster_from_xlsx(
    path: Path | None = None,
    emails: dict[str, str] | None = None,
) -> list[Spokesperson]:
    path = resolve_sme_workbook(path)
    if not path.exists():
        raise FileNotFoundError(f"SME list not found: {path}")

    overlay = emails if emails is not None else load_email_overlay()
    wb = load_workbook(path, data_only=True)
    ws = wb.active

    people: list[Spokesperson] = []
    current_firm = ""
    header_seen = False

    for row in ws.iter_rows(values_only=True):
        cells = [(_cell_str(c) if c is not None else "") for c in row]
        # pad to 7 cols
        while len(cells) < 7:
            cells.append("")
        firm, expert, title, linkedin, topics, prior, bio = cells[:7]

        if not header_seen:
            lowered = {c.lower() for c in cells if c}
            if HEADER_MARKERS.issubset(lowered):
                header_seen = True
            continue

        if firm:
            current_firm = firm
        if not expert or not current_firm:
            continue

        people.append(
            Spokesperson.from_row(
                firm=current_firm,
                name=expert,
                title=title,
                linkedin=linkedin,
                topics=topics,
                prior_work=prior,
                bio=bio,
                email=_resolve_email(overlay, current_firm, expert),
            )
        )

    return people


def _cell_str(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()

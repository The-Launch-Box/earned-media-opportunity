from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


@dataclass(frozen=True)
class Settings:
    root: Path = ROOT
    sme_xlsx: Path = ROOT / "TLB PortCo SME List.xlsx"
    emails_overlay: Path = ROOT / "config" / "emails.json"
    db_path: Path = ROOT / "data" / "pipeline.db"
    outbox_dir: Path = Path(os.getenv("OUTBOX_DIR", str(ROOT / "outbox")))

    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    claude_model: str = os.getenv("CLAUDE_MODEL", "claude-sonnet-4-6")
    # foundry | empty. Used when Claude credits are exhausted.
    research_fallback: str = os.getenv("RESEARCH_FALLBACK", "").strip().lower()
    foundry_endpoint: str = os.getenv("FOUNDRY_ENDPOINT", "").strip().rstrip("/")
    foundry_api_key: str = os.getenv("FOUNDRY_API_KEY", "").strip()
    foundry_model: str = os.getenv("FOUNDRY_MODEL", "").strip()
    web_search_tool_type: str = os.getenv("WEB_SEARCH_TOOL_TYPE", "web_search_20250305")
    max_search_uses: int = int(os.getenv("MAX_SEARCH_USES", "5"))
    max_tokens: int = int(os.getenv("MAX_TOKENS", "4096"))

    # outbox | stdout | none | graph (outbox + Microsoft Graph send)
    delivery_mode: str = os.getenv("DELIVERY_MODE", "outbox")
    from_name: str = os.getenv("FROM_NAME", "Earned Media Roundup")
    blast_to: str = os.getenv("BLAST_TO", "")
    mail_from: str = os.getenv("MAIL_FROM", "")
    azure_tenant_id: str = os.getenv("AZURE_TENANT_ID", "")
    # GRAPH_* is the Function App name so AZURE_CLIENT_ID stays free for managed identity.
    azure_client_id: str = os.getenv("GRAPH_CLIENT_ID") or os.getenv("AZURE_CLIENT_ID", "")
    azure_client_secret: str = (
        os.getenv("GRAPH_CLIENT_SECRET") or os.getenv("AZURE_CLIENT_SECRET", "")
    )
    msal_cache_path: Path = ROOT / "data" / "msal_token_cache.bin"
    # sqlite (local) or tables (Azure Table Storage)
    storage_backend: str = os.getenv("STORAGE_BACKEND", "sqlite").strip().lower()
    azure_storage_account: str = os.getenv("AZURE_STORAGE_ACCOUNT", "").strip()
    # file (local workbook) or blob (Azure Blob Storage)
    roster_source: str = os.getenv("ROSTER_SOURCE", "file").strip().lower()
    roster_container: str = os.getenv("ROSTER_BLOB_CONTAINER", "roster").strip()
    roster_blob_name: str = os.getenv(
        "ROSTER_BLOB_NAME", "TLB PortCo SME List.xlsx"
    ).strip()
    dry_run_research: bool = os.getenv("DRY_RUN_RESEARCH", "").lower() in {"1", "true", "yes"}

    semrush_api_key: str = os.getenv("SEMRUSH_API_KEY", "")
    semrush_database: str = os.getenv("SEMRUSH_DATABASE", "us")
    semrush_cache_days: int = int(os.getenv("SEMRUSH_CACHE_DAYS", "7") or "7")
    semrush_use_traffic_analytics: bool = os.getenv(
        "SEMRUSH_USE_TRAFFIC_ANALYTICS", "true"
    ).lower() in {"1", "true", "yes"}

    # Podcast reach: Spotify confirms the show; Rephonic supplies the estimate.
    spotify_client_id: str = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    spotify_client_secret: str = os.getenv("SPOTIFY_CLIENT_SECRET", "").strip()
    spotify_market: str = os.getenv("SPOTIFY_MARKET", "US").strip() or "US"
    rephonic_api_key: str = os.getenv("REPHONIC_API_KEY", "").strip()
    podcast_cache_days: int = int(os.getenv("PODCAST_CACHE_DAYS", "7") or "7")

    # Public base URL readers can open from the roundup email (no trailing path).
    # Empty hides the Yes/No links. Example: http://127.0.0.1:8787
    feedback_public_url: str = os.getenv("FEEDBACK_PUBLIC_URL", "").strip().rstrip("/")
    feedback_bind: str = os.getenv("FEEDBACK_BIND", "127.0.0.1")
    feedback_port: int = int(os.getenv("FEEDBACK_PORT", "8787") or "8787")


settings = Settings()

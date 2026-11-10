"""Microsoft Graph mail send.

Delegated: a device-code sign-in and POST /me/sendMail. Locally the token
cache is a file. On the Function App, GRAPH_TOKEN_VAULT_URL points at Key Vault,
which holds the cache uploaded with `python -m pipeline auth --upload`. A timer
cannot show a device-code prompt, so that cache must already be signed in.

Application: when AZURE_CLIENT_SECRET is set, POST /users/{mailbox}/sendMail
with the application Mail.Send permission.
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import quote

import httpx
from rich.console import Console

from pipeline.config import settings

console = Console()
logger = logging.getLogger("earned-media.graph")

GRAPH_ME_SEND_URL = "https://graph.microsoft.com/v1.0/me/sendMail"
DELEGATED_SCOPES = [
    "https://graph.microsoft.com/Mail.Send",
    "https://graph.microsoft.com/User.Read",
]
APPLICATION_SCOPE = ["https://graph.microsoft.com/.default"]


def _application_auth() -> bool:
    return bool(settings.azure_client_secret.strip())


def acquire_graph_token(*, interactive: bool = True) -> str:
    """Return a Graph access token.

    Application auth (client secret) when AZURE_CLIENT_SECRET is set.
    Otherwise device-code MFA, prompting only when interactive=True.
    """
    try:
        import msal
    except ImportError as exc:
        raise RuntimeError("msal package required; pip install msal") from exc

    if not settings.azure_tenant_id or not settings.azure_client_id:
        raise RuntimeError(
            "AZURE_TENANT_ID and AZURE_CLIENT_ID must be set in .env for Graph send."
        )
    if _application_auth():
        return _acquire_application_token(msal)

    store = token_cache_store()
    if store.remote:
        interactive = False
    cache = msal.SerializableTokenCache()
    loaded = store.load()
    if loaded:
        cache.deserialize(loaded)

    app = msal.PublicClientApplication(
        settings.azure_client_id,
        authority=f"https://login.microsoftonline.com/{settings.azure_tenant_id}",
        token_cache=cache,
    )

    account = _matching_account(app)
    result = None
    if account:
        result = app.acquire_token_silent(DELEGATED_SCOPES, account=account)
        if result and "access_token" in result:
            console.print("[dim]Using cached Microsoft login (no browser prompt)[/]")

    if not result or "access_token" not in result:
        if not interactive:
            detail = _silent_failure(result)
            raise RuntimeError(
                f"No usable Microsoft login in {store.label}{detail}. "
                "Run: python -m pipeline auth"
                + (" --upload" if store.remote else "")
            )
        flow = app.initiate_device_flow(scopes=DELEGATED_SCOPES)
        if "user_code" not in flow:
            raise RuntimeError(
                "Could not start Microsoft device login. "
                "In Entra, enable 'Allow public client flows' on the app."
            )
        console.print(
            "\n[bold yellow]Microsoft sign-in required[/] "
            "(one time; later runs reuse this login)\n"
            f"{flow.get('message', '')}\n"
            "Sign in as "
            f"[bold]{settings.mail_from or 'kaylee.scanlin@thelaunchbox.com'}[/].\n"
            "You may see two browser pages: enter the code, then MFA / permissions.\n"
        )
        result = app.acquire_token_by_device_flow(flow)

    store.save(cache.serialize(), previous=loaded)

    if not result or "access_token" not in result:
        detail = (result or {}).get("error_description") or (
            result or {}
        ).get("error") or "unknown error"
        raise RuntimeError(f"Microsoft login failed: {detail}")

    return str(result["access_token"])


def _acquire_application_token(msal: object) -> str:
    mailbox = settings.mail_from.strip()
    if not mailbox:
        raise RuntimeError(
            "MAIL_FROM must be the mailbox Graph sends as when AZURE_CLIENT_SECRET is set."
        )
    app = msal.ConfidentialClientApplication(  # type: ignore[attr-defined]
        settings.azure_client_id,
        authority=f"https://login.microsoftonline.com/{settings.azure_tenant_id}",
        client_credential=settings.azure_client_secret,
    )
    result = app.acquire_token_for_client(scopes=APPLICATION_SCOPE)
    if not result or "access_token" not in result:
        detail = (result or {}).get("error_description") or (
            result or {}
        ).get("error") or "unknown error"
        raise RuntimeError(f"Microsoft application login failed: {detail}")
    return str(result["access_token"])


def _send_url() -> str:
    if not _application_auth():
        return GRAPH_ME_SEND_URL
    mailbox = quote(settings.mail_from.strip())
    return f"https://graph.microsoft.com/v1.0/users/{mailbox}/sendMail"


class InboxUnavailable(RuntimeError):
    """The sending mailbox cannot be read yet. Button declines still work."""


def list_recent_inbox(*, top: int = 25) -> list[dict]:
    """Recent inbox messages, newest first. Requires Mail.Read."""
    if not settings.azure_tenant_id or not settings.azure_client_id:
        raise InboxUnavailable("Graph is not configured, so reply declines are not read.")
    if _application_auth() and not settings.mail_from.strip():
        raise InboxUnavailable("MAIL_FROM is empty, so reply declines are not read.")
    token = acquire_graph_token(interactive=False)
    if _application_auth():
        base = (
            "https://graph.microsoft.com/v1.0/users/"
            f"{quote(settings.mail_from.strip())}"
        )
    else:
        base = "https://graph.microsoft.com/v1.0/me"
    url = (
        f"{base}/mailFolders/inbox/messages"
        f"?$top={int(top)}&$select=id,subject,body,receivedDateTime"
        "&$orderby=receivedDateTime desc"
    )
    response = httpx.get(
        url,
        headers={"Authorization": f"Bearer {token}"},
        timeout=60.0,
    )
    if response.status_code in {401, 403}:
        raise InboxUnavailable(
            "Inbox replies need Mail.Read on the Graph app. "
            "No-Go on the page still saves the reason."
        )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Graph inbox read failed ({response.status_code}): {response.text[:400]}"
        )
    payload = response.json()
    messages = payload.get("value")
    if not isinstance(messages, list):
        return []
    return [item for item in messages if isinstance(item, dict)]


def send_roundup_email(
    *,
    subject: str,
    html_body: str,
    to_addrs: list[str] | None = None,
) -> None:
    recipients = [item.strip() for item in (to_addrs or []) if item.strip()]
    if not recipients:
        fallback = settings.blast_to.strip()
        if fallback:
            recipients = [fallback]
    if not recipients:
        raise RuntimeError("BLAST_TO is empty. Set it in .env (Amy's address).")

    token = acquire_graph_token(interactive=not _application_auth())
    message: dict = {
        "subject": subject,
        "body": {"contentType": "HTML", "content": html_body},
        "toRecipients": [{"emailAddress": {"address": addr}} for addr in recipients],
    }
    payload = {"message": message, "saveToSentItems": True}

    response = httpx.post(
        _send_url(),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60.0,
    )
    if response.status_code >= 400:
        raise RuntimeError(
            f"Graph sendMail failed ({response.status_code}): {response.text[:800]}"
        )


def _matching_account(app: object) -> dict | None:
    accounts = app.get_accounts()  # type: ignore[attr-defined]
    if not accounts:
        return None
    wanted = (settings.mail_from or "").strip().lower()
    if wanted:
        for account in accounts:
            username = str(account.get("username") or "").lower()
            if username == wanted:
                return account
    return accounts[0]


def _silent_failure(result: dict | None) -> str:
    if not result:
        return ""
    detail = result.get("error_description") or result.get("error") or ""
    return f" ({str(detail).splitlines()[0]})" if detail else ""


class FileTokenCache:
    remote = False

    def __init__(self, path: Path) -> None:
        self.path = path
        self.label = str(path)

    def load(self) -> str:
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8")

    def save(self, serialized: str, *, previous: str = "") -> None:
        # Always write after a successful acquire. Relying on has_state_changed
        # can skip the save and force another device-code prompt on the next run.
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(serialized, encoding="utf-8")
        self.path.chmod(0o600)


class VaultTokenCache:
    """MSAL cache in one Key Vault secret, read and written with managed identity."""

    remote = True

    def __init__(self, vault_url: str, secret_name: str, client: object | None = None) -> None:
        self.vault_url = vault_url
        self.secret_name = secret_name
        self.label = f"Key Vault secret {secret_name}"
        self._client = client

    def client(self) -> object:
        if self._client is None:
            from azure.identity import DefaultAzureCredential
            from azure.keyvault.secrets import SecretClient

            self._client = SecretClient(
                vault_url=self.vault_url, credential=DefaultAzureCredential()
            )
        return self._client

    def load(self) -> str:
        from azure.core.exceptions import ResourceNotFoundError

        try:
            secret = self.client().get_secret(self.secret_name)  # type: ignore[attr-defined]
        except ResourceNotFoundError:
            return ""
        return secret.value or ""

    def save(self, serialized: str, *, previous: str = "") -> None:
        # Each write is a new secret version, so skip it when nothing rotated.
        if serialized == previous:
            return
        try:
            self.client().set_secret(  # type: ignore[attr-defined]
                self.secret_name, serialized, content_type="application/json"
            )
        except Exception:  # noqa: BLE001 — the token is valid; only the refresh was not saved
            logger.exception(
                "Could not save the refreshed Microsoft login to %s", self.label
            )


def token_cache_store() -> FileTokenCache | VaultTokenCache:
    if settings.graph_token_vault_url:
        return VaultTokenCache(settings.graph_token_vault_url, settings.graph_token_secret)
    return FileTokenCache(settings.msal_cache_path)


def upload_token_cache(vault_url: str, secret_name: str) -> None:
    """Copy the local signed-in cache to Key Vault for the Function App."""
    local = FileTokenCache(settings.msal_cache_path).load()
    if not local:
        raise RuntimeError(
            f"No local Microsoft login at {settings.msal_cache_path}. "
            "Run: python -m pipeline auth"
        )
    VaultTokenCache(vault_url, secret_name).save(local)

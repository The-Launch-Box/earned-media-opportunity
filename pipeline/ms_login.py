"""Microsoft work-account sign-in for the dashboard.

The app registration has to allow accounts in any organizational directory.
Personal Microsoft accounts are not accepted. After Microsoft returns the
account, the registry checks the email domain and the Entra tenant id.
"""

from __future__ import annotations

import os

AUTHORITY = "https://login.microsoftonline.com/organizations"
SCOPES = ["User.Read"]


def microsoft_configured() -> bool:
    return bool(_client_id() and _client_secret())


def redirect_uri() -> str:
    explicit = os.getenv("DASHBOARD_REDIRECT_URI", "").strip()
    if explicit:
        return explicit
    from pipeline.config import settings

    public = settings.feedback_public_url or (
        f"http://{settings.feedback_bind}:{settings.feedback_port}"
    )
    return public.rstrip("/") + "/auth/callback"


def begin_flow() -> dict:
    flow = _app().initiate_auth_code_flow(scopes=SCOPES, redirect_uri=redirect_uri())
    if "error" in flow:
        raise RuntimeError(flow.get("error_description") or flow["error"])
    return flow


def finish_flow(flow: dict, query: dict[str, str]) -> dict[str, str]:
    result = _app().acquire_token_by_auth_code_flow(flow, query)
    if "error" in result:
        raise RuntimeError(result.get("error_description") or result["error"])
    claims = result.get("id_token_claims") or {}
    email = str(claims.get("preferred_username") or claims.get("email") or "").strip()
    name = str(claims.get("name") or email).strip()
    tid = str(claims.get("tid") or "").strip()
    if not email:
        raise RuntimeError("Microsoft didn't return an email address for that account.")
    return {"email": email, "name": name, "tid": tid}


def _app():
    import msal

    return msal.ConfidentialClientApplication(
        _client_id(),
        authority=AUTHORITY,
        client_credential=_client_secret(),
    )


def _client_id() -> str:
    return os.getenv("DASHBOARD_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.getenv("DASHBOARD_CLIENT_SECRET", "").strip()

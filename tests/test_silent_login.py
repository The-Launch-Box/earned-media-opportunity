"""Opening the site reuses a Microsoft session before showing the sign-in button."""

from pipeline.dashboard import handle_request
from pipeline.ms_login import begin_flow, clean_login_hint


class _Registry:
    def __init__(self) -> None:
        self.saved = None
        self.flow = None

    def save_oauth(self, state, flow, next_path="") -> None:
        self.saved = (state, flow, next_path)

    def pop_oauth(self, state):
        return self.flow


def _install(monkeypatch, registry: _Registry, captured: dict) -> None:
    def fake_begin_flow(*, prompt=None, login_hint=None):
        captured["prompt"] = prompt
        captured["login_hint"] = login_hint
        return {
            "auth_uri": "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize",
            "state": "state-1",
        }

    monkeypatch.setattr("pipeline.dashboard.microsoft_configured", lambda: True)
    monkeypatch.setattr("pipeline.dashboard.begin_flow", fake_begin_flow)
    monkeypatch.setattr("pipeline.dashboard.default_registry", lambda: registry)


def test_home_tries_the_existing_microsoft_session(monkeypatch) -> None:
    registry = _Registry()
    captured: dict = {}
    _install(monkeypatch, registry, captured)

    result = handle_request("GET", "/", {}, b"")

    assert result.status == 303
    assert result.headers[0] == (
        "Location",
        "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize",
    )
    assert captured["prompt"] == "none"
    assert registry.saved is not None
    assert registry.saved[1]["_emo_silent"] is True


def test_sign_in_button_still_asks_microsoft_to_show_a_page(monkeypatch) -> None:
    registry = _Registry()
    captured: dict = {}
    _install(monkeypatch, registry, captured)

    result = handle_request("GET", "/login", {}, b"")

    assert result.status == 303
    assert captured["prompt"] is None
    assert registry.saved is not None
    assert "_emo_silent" not in registry.saved[1]


def test_missing_microsoft_session_shows_the_existing_button(monkeypatch) -> None:
    registry = _Registry()
    registry.flow = {"_emo_silent": True, "state": "state-1"}
    captured: dict = {}
    _install(monkeypatch, registry, captured)

    result = handle_request(
        "GET",
        "/auth/callback?error=login_required&state=state-1",
        {},
        b"",
    )

    assert result.status == 200
    assert "Sign in with Microsoft" in result.body
    assert "complete sign-in" not in result.body


def test_real_microsoft_error_still_explains_the_failure(monkeypatch) -> None:
    registry = _Registry()
    registry.flow = {"_emo_silent": True, "state": "state-1"}
    captured: dict = {}
    _install(monkeypatch, registry, captured)

    result = handle_request(
        "GET",
        "/auth/callback?error=access_denied&state=state-1",
        {},
        b"",
    )

    assert result.status == 403
    assert "complete sign-in" in result.body


def test_begin_flow_sends_prompt_none_for_a_work_account(monkeypatch) -> None:
    captured: dict = {}

    class _App:
        def initiate_auth_code_flow(self, **kwargs):
            captured.update(kwargs)
            return {
                "auth_uri": "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize",
                "state": "s",
            }

    monkeypatch.setattr("pipeline.ms_login._app", lambda: _App())
    monkeypatch.setattr(
        "pipeline.ms_login.redirect_uri",
        lambda: "https://func-tlb-earned-media.azurewebsites.net/auth/callback",
    )

    begin_flow(prompt="none")
    plain = {}

    class _App2:
        def initiate_auth_code_flow(self, **kwargs):
            plain.update(kwargs)
            return {
                "auth_uri": "https://login.microsoftonline.com/organizations/oauth2/v2.0/authorize",
                "state": "s",
            }

    monkeypatch.setattr("pipeline.ms_login._app", lambda: _App2())
    begin_flow()

    assert captured["prompt"] == "none"
    assert captured["domain_hint"] == "organizations"
    assert "prompt" not in plain
    assert clean_login_hint("  kaylee@thelaunchbox.com ") == "kaylee@thelaunchbox.com"
    assert clean_login_hint("bad\nhint") is None

"""Delegated Graph sign-in cache: local file, or Key Vault on the Function App."""

from __future__ import annotations

from types import SimpleNamespace

from azure.core.exceptions import ResourceNotFoundError

from pipeline.config import settings
from pipeline.graph_mail import (
    FileTokenCache,
    VaultTokenCache,
    _send_url,
    token_cache_store,
)


class FakeSecrets:
    def __init__(self, value: str | None = None, fail_set: bool = False) -> None:
        self.value = value
        self.fail_set = fail_set
        self.writes: list[str] = []

    def get_secret(self, name: str):
        if self.value is None:
            raise ResourceNotFoundError("missing")
        return SimpleNamespace(value=self.value)

    def set_secret(self, name: str, value: str, **_kwargs) -> None:
        if self.fail_set:
            raise RuntimeError("Forbidden")
        self.writes.append(value)
        self.value = value


def _assign(**values: str) -> dict[str, str]:
    previous = {name: getattr(settings, name) for name in values}
    for name, value in values.items():
        object.__setattr__(settings, name, value)
    return previous


def _restore(previous: dict[str, str]) -> None:
    for name, value in previous.items():
        object.__setattr__(settings, name, value)


def test_vault_url_selects_key_vault_cache() -> None:
    previous = _assign(
        graph_token_vault_url="https://kv-tlb-emo.vault.azure.net/",
        graph_token_secret="graph-token-cache",
        azure_client_secret="",
    )
    try:
        store = token_cache_store()
        assert isinstance(store, VaultTokenCache)
        assert store.remote
        assert _send_url() == "https://graph.microsoft.com/v1.0/me/sendMail"
    finally:
        _restore(previous)


def test_no_vault_url_keeps_local_file(tmp_path) -> None:
    previous = _assign(graph_token_vault_url="")
    try:
        store = token_cache_store()
        assert isinstance(store, FileTokenCache)
        assert not store.remote
    finally:
        _restore(previous)
    local = FileTokenCache(tmp_path / "cache.bin")
    assert local.load() == ""
    local.save('{"a": 1}')
    assert local.load() == '{"a": 1}'


def test_vault_cache_writes_only_when_tokens_change() -> None:
    secrets = FakeSecrets('{"old": 1}')
    store = VaultTokenCache("https://vault", "graph-token-cache", client=secrets)
    loaded = store.load()
    store.save(loaded, previous=loaded)
    assert secrets.writes == []
    store.save('{"new": 2}', previous=loaded)
    assert secrets.writes == ['{"new": 2}']


def test_vault_cache_missing_secret_and_failed_write() -> None:
    assert VaultTokenCache("https://vault", "x", client=FakeSecrets(None)).load() == ""
    blocked = VaultTokenCache("https://vault", "x", client=FakeSecrets("{}", fail_set=True))
    blocked.save('{"new": 2}', previous="{}")

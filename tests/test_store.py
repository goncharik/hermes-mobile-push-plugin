"""Tests for the hermes-push token store (Task B2).

All tests inject a tmp base dir so the real ``~/.hermes`` is never touched.
"""

from __future__ import annotations

import json

import pytest

from hermes_push.store import TokenStore


@pytest.fixture
def store(tmp_path):
    return TokenStore(base_dir=tmp_path / "hermes-push")


def _upsert(store, token="tok-1", env="sandbox", version="1.0.0"):
    return store.upsert(
        device_token=token,
        apns_env=env,
        app_version=version,
    )


def test_upsert_new_token_persists_full_record(store):
    rec = _upsert(store)

    assert rec["device_token"] == "tok-1"
    assert rec["apns_env"] == "sandbox"
    assert rec["app_version"] == "1.0.0"
    # No secret material is stored — the plugin signs with a shared env secret.
    assert "hmac_secret" not in rec
    assert rec["created_at"]
    assert rec["updated_at"]

    # Survives a fresh store instance reading the same file.
    reloaded = TokenStore(base_dir=store.path.parent).get("tok-1")
    assert reloaded == rec


def test_upsert_existing_token_updates_env_version_and_keeps_created(store):
    first = _upsert(store, env="sandbox", version="1.0.0")

    second = _upsert(store, token="tok-1", env="production", version="2.0.0")

    # Env + version change...
    assert second["apns_env"] == "production"
    assert second["app_version"] == "2.0.0"
    # created_at preserved; updated_at present.
    assert second["created_at"] == first["created_at"]
    assert second["updated_at"]


def test_get_missing_returns_none(store):
    assert store.get("nope") is None


def test_list_all_returns_every_record(store):
    _upsert(store, token="a")
    _upsert(store, token="b")

    tokens = {r["device_token"] for r in store.list_all()}
    assert tokens == {"a", "b"}


def test_remove_existing_returns_true_and_deletes(store):
    _upsert(store, token="a")
    assert store.remove("a") is True
    assert store.get("a") is None
    assert store.list_all() == []


def test_remove_missing_returns_false(store):
    assert store.remove("ghost") is False


def test_prune_invalid_removes_token(store):
    _upsert(store, token="bad")
    assert store.prune_invalid("bad") is True
    assert store.get("bad") is None
    # Pruning an unknown token is a no-op false.
    assert store.prune_invalid("bad") is False


def test_missing_file_tolerated_as_empty(tmp_path):
    store = TokenStore(base_dir=tmp_path / "does-not-exist-yet")
    assert store.list_all() == []
    assert store.get("x") is None
    # And it can write a fresh file from nothing.
    _upsert(store, token="x")
    assert store.get("x")["device_token"] == "x"


def test_corrupt_file_tolerated_as_empty(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ this is not valid json", encoding="utf-8")

    # Reads as empty rather than raising.
    assert store.list_all() == []
    # A write recovers the file to valid JSON.
    _upsert(store, token="recovered")
    on_disk = json.loads(store.path.read_text(encoding="utf-8"))
    assert "recovered" in on_disk


def test_non_dict_file_tolerated_as_empty(store):
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("[1, 2, 3]", encoding="utf-8")
    assert store.list_all() == []


def test_write_is_atomic_no_leftover_temp_files(store):
    _upsert(store, token="a")
    _upsert(store, token="b")
    siblings = list(store.path.parent.iterdir())
    # Only the tokens.json file should remain — no .tmp leftovers.
    assert [p.name for p in siblings] == ["tokens.json"]

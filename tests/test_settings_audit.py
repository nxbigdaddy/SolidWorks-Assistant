import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from sw_assistant.infrastructure import AppSettings, AuditLog, ModelSettings, SettingsStore


def test_settings_never_store_plaintext_key(tmp_path, monkeypatch):
    monkeypatch.setattr("sw_assistant.infrastructure.settings._protect", lambda value: "dpapi:cipher")
    monkeypatch.setattr("sw_assistant.infrastructure.settings._unprotect", lambda value: "secret")
    path = tmp_path / "settings.json"
    SettingsStore(path).save(
        AppSettings(model=ModelSettings("https://example.com/v1", "model", "secret"))
    )
    assert "secret" not in path.read_text("utf-8")
    assert SettingsStore(path).load().model.api_key == "secret"


def test_automatic_recovery_preferences_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr("sw_assistant.infrastructure.settings._protect", lambda value: "")
    path = tmp_path / "settings.json"
    SettingsStore(path).save(
        AppSettings(
            agent_enabled=False,
            agent_max_model_requests=6,
            agent_backend="pi",
            pi_max_repair_rounds=9,
            auto_repair_failures=False,
            auto_resume_after_repair=False,
        )
    )

    loaded = SettingsStore(path).load()

    assert loaded.agent_enabled is False
    assert loaded.agent_max_model_requests == 6
    assert loaded.agent_backend == "pi"
    assert loaded.pi_max_repair_rounds == 9
    assert loaded.auto_repair_failures is False
    assert loaded.auto_resume_after_repair is False


def test_legacy_settings_enable_full_agent_with_six_request_default(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("sw_assistant.infrastructure.settings._unprotect", lambda value: "")
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "model": {
                    "api_base": "https://example.com",
                    "model": "model",
                    "timeout_seconds": 90,
                    "api_key_protected": "",
                },
            }
        ),
        "utf-8",
    )

    loaded = SettingsStore(path).load()

    assert loaded.agent_enabled is True
    assert loaded.agent_max_model_requests == 6
    assert loaded.agent_backend == "auto"
    assert loaded.pi_max_repair_rounds == 6


def test_unreadable_key_is_explicit_and_cannot_be_saved_blank(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "model": {
                    "api_base": "https://example.com/v1",
                    "model": "m",
                    "timeout_seconds": 90,
                    "api_key_protected": "dpapi:bad",
                },
            }
        ),
        "utf-8",
    )
    monkeypatch.setattr(
        "sw_assistant.infrastructure.settings._unprotect",
        lambda value: (_ for _ in ()).throw(RuntimeError("bad")),
    )
    store = SettingsStore(path)
    settings = store.load()
    assert settings.model.credential_unreadable is True
    with pytest.raises(Exception, match="重新输入"):
        store.save(settings)


def test_audit_redacts_credentials(tmp_path):
    path = tmp_path / "audit.jsonl"
    AuditLog(path).write(
        "request", api_key="secret", headers={"Authorization": "Bearer token"}, model="safe"
    )
    text = path.read_text("utf-8")
    assert "secret" not in text
    assert "token" not in text
    assert '"model":"safe"' in text


def test_audit_prunes_only_expired_session_logs(tmp_path):
    old_session = tmp_path / "session-old.jsonl"
    recent_session = tmp_path / "session-recent.jsonl"
    unrelated = tmp_path / "events.jsonl"
    for path in (old_session, recent_session, unrelated):
        path.write_text("{}\n", "utf-8")
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    old_timestamp = (now - timedelta(days=31)).timestamp()
    os.utime(old_session, (old_timestamp, old_timestamp))

    result = AuditLog.prune(tmp_path, 30, now=now)

    assert result == {"deleted": ["session-old.jsonl"], "errors": []}
    assert not old_session.exists()
    assert recent_session.exists()
    assert unrelated.exists()


def test_document_manager_license_is_dpapi_protected(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "sw_assistant.infrastructure.settings._protect",
        lambda value: "dpapi:" + value[::-1] if value else "",
    )
    monkeypatch.setattr(
        "sw_assistant.infrastructure.settings._unprotect",
        lambda value: value[6:][::-1] if value else "",
    )
    path = tmp_path / "settings.json"
    settings = AppSettings(document_manager_key="dm-secret")
    SettingsStore(path).save(settings)
    assert "dm-secret" not in path.read_text("utf-8")
    assert SettingsStore(path).load().document_manager_key == "dm-secret"

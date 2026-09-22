"""Настройки и список аккаунтов"""
import json

from shalost import settings as config


def test_missing_file_gives_one_account(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SETTINGS_PATH", tmp_path / "settings.json")
    data = config.load_settings()
    assert len(data["accounts"]) == 1
    assert data["current"] == data["accounts"][0]["id"]
    assert data["cooldown_minutes"] == 10


def test_old_file_gets_missing_fields(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"cooldown_minutes": 5, "accounts": [{"id": "a1"}]}), encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_PATH", path)
    data = config.load_settings()
    account = data["accounts"][0]
    assert data["cooldown_minutes"] == 5
    assert account["enabled"] is True
    assert account["login"] == "" and account["name"] == ""
    assert data["current"] == "a1"


def test_broken_file_falls_back_to_defaults(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    path.write_text("{ это не json", encoding="utf-8")
    monkeypatch.setattr(config, "SETTINGS_PATH", path)
    assert config.load_settings()["accounts"][0]["id"] == "default"


def test_current_account_falls_back_to_first():
    data = {"current": "нет такого", "accounts": [{"id": "a1", "title": "первый"}, {"id": "a2"}]}
    assert config.find_account(data)["id"] == "a1"
    assert config.find_account(data, "a2")["id"] == "a2"


def test_enabled_accounts_never_empty():
    data = {"current": "a1", "accounts": [{"id": "a1", "enabled": False}, {"id": "a2", "enabled": False}]}
    assert [item["id"] for item in config.enabled_accounts(data)] == ["a1"]


def test_saved_settings_are_read_back(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_PATH", path)
    data = config.load_settings()
    data["accounts"].append(config.new_account("второй"))
    config.save_settings(data)
    assert len(config.load_settings()["accounts"]) == 2

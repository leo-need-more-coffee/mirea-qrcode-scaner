"""Настройки приложения и список аккаунтов Pulse"""
from __future__ import annotations

import json
from uuid import uuid4

from .paths import SETTINGS_PATH

DEFAULTS = {"cooldown_minutes": 10, "accounts": [], "current": "", "telegram_chat": 0,
            "telegram_enabled": False, "lecture_url": ""}


def new_account(title: str) -> dict:
    return {"id": uuid4().hex[:12], "title": title, "enabled": True, "name": "", "login": ""}


def load_settings() -> dict:
    """Настройки вместе со списком аккаунтов; старые файлы дополняются аккаунтом по умолчанию"""
    data = dict(DEFAULTS)
    try:
        data.update(json.loads(SETTINGS_PATH.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        pass
    accounts = [item for item in data.get("accounts", []) if isinstance(item, dict) and item.get("id")]
    if not accounts:
        accounts = [{"id": "default", "title": "Аккаунт 1", "enabled": True, "name": ""}]
    for account in accounts:
        account.setdefault("title", "Аккаунт")
        account.setdefault("enabled", True)
        account.setdefault("name", "")
        account.setdefault("login", "")
    data["accounts"] = accounts
    if data.get("current") not in [account["id"] for account in accounts]:
        data["current"] = accounts[0]["id"]
    return data


def save_settings(data: dict) -> None:
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def find_account(data: dict, account_id: str | None = None) -> dict:
    """Аккаунт по идентификатору, по умолчанию активный"""
    wanted = account_id or data["current"]
    for item in data["accounts"]:
        if item["id"] == wanted:
            return item
    return data["accounts"][0]


def enabled_accounts(data: dict) -> list[dict]:
    """Аккаунты, которые подтверждают найденный QR"""
    return [item for item in data["accounts"] if item.get("enabled")] or [find_account(data)]

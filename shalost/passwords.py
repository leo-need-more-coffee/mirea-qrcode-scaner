"""Пароли и токены в системном хранилище

В Windows это Credential Manager, в Linux — KWallet или GNOME Keyring.
Файлы приложения паролей не содержат.
"""
from __future__ import annotations

from .paths import LOGGER

KEYRING_SERVICE = "Shalost FOTUR"
TELEGRAM_KEY = "telegram-bot-token"


def keyring_module():
    """Системное хранилище паролей, если оно доступно в этой системе"""
    try:
        import keyring
        if keyring.get_keyring().priority <= 0:
            return None
        return keyring
    except Exception:
        LOGGER.exception("Keyring is unavailable")
        return None


def save_password(account_id: str, password: str) -> bool:
    store = keyring_module()
    if not store:
        return False
    try:
        store.set_password(KEYRING_SERVICE, account_id, password)
        return True
    except Exception:
        LOGGER.exception("Password cannot be saved")
        return False


def load_password(account_id: str) -> str:
    store = keyring_module()
    if not store:
        return ""
    try:
        return store.get_password(KEYRING_SERVICE, account_id) or ""
    except Exception:
        LOGGER.exception("Password cannot be read")
        return ""


def forget_password(account_id: str) -> None:
    store = keyring_module()
    if not store:
        return
    try:
        store.delete_password(KEYRING_SERVICE, account_id)
    except Exception:
        LOGGER.info("Stored password not found for %s", account_id)

"""Пути приложения и журнал

Портативная версия хранит журнал рядом с исполняемым файлом, установленная
и AppImage — в папке данных пользователя, потому что рядом с ними писать нельзя.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from pathlib import Path


def data_dir() -> Path:
    """Папка приложения в стандартном месте текущей системы"""
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Shalost"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Shalost"
    return Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share") / "Shalost"


DATA_DIR = data_dir()
LEGACY_PROFILE = DATA_DIR / "pulse-chrome-profile"
ACCOUNTS_DIR = DATA_DIR / "accounts"
SETTINGS_PATH = DATA_DIR / "settings.json"
RUN_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent.parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", RUN_DIR))
APP_ICON = BUNDLE_DIR / "assets" / "shalost-fotur.ico"
APP_ICON_PNG = BUNDLE_DIR / "assets" / "shalost-fotur.png"
OPEN_FOLDER_TEXT = "Открыть в Проводнике" if sys.platform == "win32" else "Открыть папку"


def log_path() -> Path:
    candidate = RUN_DIR / "pulseqr.log"
    try:
        candidate.parent.mkdir(parents=True, exist_ok=True)
        with candidate.open("a", encoding="utf-8"):
            pass
        return candidate
    except OSError:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        return DATA_DIR / "pulseqr.log"


LOG_PATH = log_path()


def build_logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    result = logging.getLogger("shalost")
    result.handlers.clear()
    result.setLevel(logging.INFO)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    result.addHandler(handler)
    return result


LOGGER = build_logger()


def profile_path(account_id: str) -> Path:
    """Папка Chrome для отдельного аккаунта Pulse"""
    return ACCOUNTS_DIR / account_id / "pulse-chrome-profile"


def migrate_profile() -> None:
    """Переносит единственный профиль прежних версий в папку первого аккаунта"""
    target = profile_path("default")
    if LEGACY_PROFILE.exists() and not target.exists():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(LEGACY_PROFILE), str(target))
            LOGGER.info("Profile moved to %s", target)
        except OSError:
            LOGGER.exception("Profile migration failed")


def open_folder(folder: Path) -> None:
    """Открывает папку системным файловым менеджером"""
    import subprocess

    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        os.startfile(folder)  # noqa: S606 — штатный способ открыть папку в Windows
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(folder)])
    else:
        subprocess.Popen(["xdg-open", str(folder)])

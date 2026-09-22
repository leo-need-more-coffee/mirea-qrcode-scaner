"""Нативное приложение Shalost FOTUR для Windows и Linux"""
from __future__ import annotations

import io
import json
import logging
import os
import queue
import re
import secrets
import shutil
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import tkinter as tk
import webbrowser
from pathlib import Path
from tkinter import font as tkfont, messagebox, simpledialog, ttk
from urllib.parse import parse_qs, unquote, unquote_plus, urlparse
from uuid import UUID, uuid4

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

APP_NAME = "Shalost FOTUR"
PULSE_HOME = "https://pulse.mirea.ru/"
EDU_HOME = "https://online-edu.mirea.ru/"
PRESENCE_TEXT = re.compile(r"подтверждаю", re.IGNORECASE)
PULSE_RPC = "https://pulse.mirea.ru/rtu_tc.attendance.api.AttendanceService/SelfApproveAttendanceThroughQRCode"
CLOUDTIPS_URL = "https://pay.cloudtips.ru/p/b58c4bc1"


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
RUN_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", RUN_DIR))
QR_IMAGE = BUNDLE_DIR / "assets" / "cloudtips-donation-qr.png"
APP_ICON = BUNDLE_DIR / "assets" / "shalost-fotur.ico"
APP_ICON_PNG = BUNDLE_DIR / "assets" / "shalost-fotur.png"


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

STATE_JS = """() => {
  const settingsLinks = [...document.querySelectorAll('a[href*="/settings"]')];
  const values = settingsLinks.map(x => (x.innerText || '').trim())
    .filter(x => x && !/настройки|settings/i.test(x));
  return {onPulse: location.hostname === 'pulse.mirea.ru', name: values.at(-1) || ''};
}"""


OPEN_FOLDER_TEXT = "Открыть в Проводнике" if sys.platform == "win32" else "Открыть папку"

UI_FONTS = ("Segoe UI", "Inter", "Noto Sans", "Cantarell", "DejaVu Sans", "Liberation Sans")
MONO_FONTS = ("Cascadia Mono", "JetBrains Mono", "Fira Mono", "Noto Sans Mono", "DejaVu Sans Mono", "Liberation Mono")


def pick_font(root: tk.Misc, wanted: tuple[str, ...], fallback: str) -> str:
    """Первый доступный шрифт: Windows берёт первый из списка, Linux — свой системный"""
    try:
        families = set(tkfont.families(root))
    except tk.TclError:
        return fallback
    for family in wanted:
        if family in families:
            return family
    return fallback


def logger() -> logging.Logger:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    result = logging.getLogger("shalost")
    result.handlers.clear()
    result.setLevel(logging.INFO)
    handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    result.addHandler(handler)
    return result


LOGGER = logger()


KEYRING_SERVICE = "Shalost FOTUR"
TELEGRAM_KEY = "telegram-bot-token"
TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
NOTIFY_TAGS = ("PULSE", "PRESENCE", "AUTH", "LECTURE")


def telegram_call(token: str, method: str, payload: dict | None = None, timeout: int = 40) -> dict:
    """Вызов Bot API без сторонних библиотек"""
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(TELEGRAM_API.format(token=token, method=method), data=data,
                                     headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


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


def profile_path(account_id: str) -> Path:
    """Папка Chrome для отдельного аккаунта Pulse"""
    return ACCOUNTS_DIR / account_id / "pulse-chrome-profile"


def new_account(title: str) -> dict:
    return {"id": uuid4().hex[:12], "title": title, "enabled": True, "name": "", "login": ""}


def load_settings() -> dict:
    """Настройки вместе со списком аккаунтов; старые файлы дополняются аккаунтом по умолчанию"""
    data = {"cooldown_minutes": 10, "accounts": [], "current": "", "telegram_chat": 0,
            "telegram_enabled": False, "lecture_url": ""}
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


def qr_token(value: str) -> str | None:
    value = unquote(value.strip())
    if "token=" in value:
        value = parse_qs(urlparse(value).query).get("token", [""])[0]
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        return None


def grpc_body(token: str) -> bytes:
    raw = token.encode("utf-8")
    proto = b"\x0a" + bytes([len(raw)]) + raw
    return b"\x00" + struct.pack(">I", len(proto)) + proto


def grpc_web_trailer(data: bytes) -> tuple[str | None, str]:
    """Извлекает grpc-status и grpc-message из кадров gRPC-Web в теле ответа"""
    offset = 0
    status: str | None = None
    message = ""
    while offset + 5 <= len(data):
        flags = data[offset]
        size = struct.unpack(">I", data[offset + 1:offset + 5])[0]
        offset += 5
        if offset + size > len(data):
            break
        payload = data[offset:offset + size]
        offset += size
        if flags & 0x80:
            for line in payload.decode("utf-8", errors="replace").splitlines():
                key, separator, value = line.partition(":")
                if not separator:
                    continue
                if key.lower() == "grpc-status":
                    status = value.strip()
                elif key.lower() == "grpc-message":
                    message = unquote_plus(value.strip())
    return status, message


SCREEN_CAPTURE = None


def wayland_session() -> bool:
    """Сеанс Wayland: снимок всех экранов и автоклик через X11 там недоступны"""
    return sys.platform == "linux" and (os.environ.get("XDG_SESSION_TYPE") == "wayland" or bool(os.environ.get("WAYLAND_DISPLAY")))


def grab_screens():
    """Скриншот всех экранов и начало координат виртуального рабочего стола

    Windows отдаёт объединённый снимок через ImageGrab, Linux — через mss,
    потому что all_screens в Pillow поддерживается только на Windows.
    """
    global SCREEN_CAPTURE
    if sys.platform == "win32":
        from PIL import ImageGrab
        import ctypes
        user32 = ctypes.windll.user32
        # Начало виртуального стола может быть отрицательным при мониторе слева
        return ImageGrab.grab(all_screens=True), user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
    if sys.platform == "darwin":
        from PIL import ImageGrab
        return ImageGrab.grab(), 0, 0
    import mss
    from PIL import Image
    if SCREEN_CAPTURE is None:
        SCREEN_CAPTURE = mss.mss()
    area = SCREEN_CAPTURE.monitors[0]
    shot = SCREEN_CAPTURE.grab(area)
    image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    return image, area["left"], area["top"]


def click_x11(x: int, y: int) -> bool:
    """Клик левой кнопкой через XTEST: работает в сеансе X11 и в XWayland"""
    import ctypes
    import ctypes.util
    try:
        x11 = ctypes.CDLL(ctypes.util.find_library("X11") or "libX11.so.6")
        xtst = ctypes.CDLL(ctypes.util.find_library("Xtst") or "libXtst.so.6")
    except OSError:
        return False
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = ctypes.c_void_p
    x11.XFlush.argtypes = [ctypes.c_void_p]
    x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
    xtst.XTestFakeMotionEvent.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_ulong]
    xtst.XTestFakeButtonEvent.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_int, ctypes.c_ulong]
    display = x11.XOpenDisplay(None)
    if not display:
        return False
    try:
        xtst.XTestFakeMotionEvent(display, -1, x, y, 0)
        xtst.XTestFakeButtonEvent(display, 1, 1, 0)
        xtst.XTestFakeButtonEvent(display, 1, 0, 10)
        x11.XFlush(display)
    finally:
        x11.XCloseDisplay(display)
    return True


def click_xdotool(x: int, y: int) -> bool:
    """Запасной вариант клика, если библиотеки XTEST нет в системе"""
    try:
        subprocess.run(["xdotool", "mousemove", str(x), str(y), "click", "1"], check=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def system_browser() -> str | None:
    """Путь к установленному в системе Chrome или Chromium"""
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def launch_context(playwright, headless: bool, profile: Path):
    """Открывает профиль Pulse: сначала официальный Chrome, затем браузер системы

    На Windows подходит канал chrome, в Linux Chrome часто установлен как chromium,
    поэтому запасным вариантом идёт найденный в PATH браузер.
    """
    profile.mkdir(parents=True, exist_ok=True)
    # Без этих флагов Chrome замедляет таймеры в фоновых вкладках,
    # и окно «Контроль присутствия» появляется с большой задержкой
    args = ["--disable-background-timer-throttling", "--disable-backgrounding-occluded-windows",
            "--disable-renderer-backgrounding"]
    attempts: list[dict] = [{"channel": "chrome"}]
    executable = system_browser()
    if executable:
        attempts.append({"executable_path": executable})
    attempts.append({"channel": "chromium"})
    failure = None
    for options in attempts:
        try:
            context = playwright.chromium.launch_persistent_context(str(profile), headless=headless, args=args, **options)
            LOGGER.info("Browser started with %s", options)
            return context
        except PlaywrightError as exc:
            failure = exc
    raise failure


class App:
    BG, CARD, FIELD = "#0f1929", "#192841", "#0a1424"
    TEXT, MUTED, BLUE, SECONDARY, RED = "#f4f7ff", "#9fc5ff", "#397cff", "#29466f", "#ca435b"

    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        migrate_profile()
        self.settings = load_settings()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop = threading.Event()
        self.scanning = False
        self.lecture = False
        self.bot_running = False
        self.bot_thread: threading.Thread | None = None
        self.pair_code = ""
        self.browser_busy = False
        self.account = ""
        self.last_success = 0.0
        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.geometry("940x900")
        self.root.minsize(860, 800)
        self.root.configure(bg=self.BG)
        self.font = pick_font(self.root, UI_FONTS, "TkDefaultFont")
        self.mono = pick_font(self.root, MONO_FONTS, "TkFixedFont")
        self.set_icon()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.styles()
        self.build()
        self.root.update_idletasks()
        self.enable_rounded_window()
        self.log("UI", "Интерфейс готов.")
        store = keyring_module()
        LOGGER.info("Keyring backend: %s", store.get_keyring() if store else "недоступно")
        self.root.after(150, self.pump)
        self.root.after(1000, self.check_session)
        if self.settings.get("telegram_enabled"):
            self.start_bot()

    def set_icon(self) -> None:
        """Windows берёт значок из ICO, остальные системы — из PNG"""
        try:
            if sys.platform == "win32" and APP_ICON.exists():
                self.root.iconbitmap(default=str(APP_ICON))
            elif APP_ICON_PNG.exists():
                self.window_icon = tk.PhotoImage(file=str(APP_ICON_PNG))
                self.root.iconphoto(True, self.window_icon)
        except tk.TclError:
            LOGGER.exception("Application icon cannot be loaded")

    def styles(self) -> None:
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("TButton", font=(self.font, 10, "bold"), padding=(12, 8), background=self.BLUE, foreground="white", borderwidth=0)
        style.map("TButton", background=[("active", "#5b92ff")])
        style.configure("Secondary.TButton", background=self.SECONDARY)
        style.configure("Danger.TButton", background=self.RED)
        style.configure("TEntry", fieldbackground=self.FIELD, foreground=self.TEXT)

    def enable_rounded_window(self) -> None:
        if sys.platform != "win32":
            return
        try:
            import ctypes
            preference = ctypes.c_int(2)  # Скруглённый режим DWM
            hwnd = ctypes.windll.user32.GetAncestor(self.root.winfo_id(), 2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(preference), ctypes.sizeof(preference))
        except Exception:
            pass

    def label(self, parent, text, size=10, bold=False, color=None, **kwargs):
        return tk.Label(parent, text=text, bg=self.CARD, fg=color or self.TEXT, font=(self.font, size, "bold" if bold else "normal"), **kwargs)

    def card(self, parent):
        return self.round_widget(tk.Frame(parent, bg=self.CARD, highlightbackground="#2d4971", highlightthickness=1, padx=14, pady=12), 12)

    def round_widget(self, widget, radius=9):
        """Обрезает дочернее окно Tk по скруглённому прямоугольнику Windows"""
        if sys.platform != "win32":
            return widget
        def apply(_event=None):
            try:
                import ctypes
                width, height = widget.winfo_width(), widget.winfo_height()
                if width > 2 and height > 2:
                    region = ctypes.windll.gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2)
                    ctypes.windll.user32.SetWindowRgn(widget.winfo_id(), region, True)
            except Exception:
                pass
        widget.bind("<Configure>", apply, add="+")
        widget.after_idle(apply)
        return widget

    def button(self, parent, **kwargs):
        return self.round_widget(ttk.Button(parent, **kwargs), 8)

    def entry(self, parent, **kwargs):
        return self.round_widget(ttk.Entry(parent, **kwargs), 7)

    def build(self) -> None:
        outer = tk.Frame(self.root, bg=self.BG, padx=20, pady=18)
        outer.pack(fill="both", expand=True)
        tk.Label(outer, text=APP_NAME, bg=self.BG, fg=self.TEXT, font=(self.font, 22, "bold")).pack(anchor="w")
        nav = tk.Frame(outer, bg=self.BG, pady=12)
        nav.pack(fill="x")
        self.button(nav, text="Главная", command=self.show_main).pack(side="left")
        self.button(nav, text="Настройки", style="Secondary.TButton", command=self.show_settings).pack(side="left", padx=8)
        self.content = tk.Frame(outer, bg=self.BG)
        self.content.pack(fill="both", expand=True)
        self.main_page, self.settings_page = tk.Frame(self.content, bg=self.BG), tk.Frame(self.content, bg=self.BG)
        self.settings_body = self.scrollable(self.settings_page)
        self.build_main()
        self.build_settings()
        footer = tk.Frame(outer, bg=self.BG)
        footer.pack(fill="x", pady=(8, 0))
        tk.Label(footer, text="Сделано ", bg=self.BG, fg="#9db1ce", font=(self.font, 9)).pack(side="left")
        self.link(footer, "FOTUR", "https://fotur.tech").pack(side="left")
        tk.Label(footer, text=" для студентов by ", bg=self.BG, fg="#9db1ce", font=(self.font, 9)).pack(side="left")
        self.link(footer, "@Woonze", "https://github.com/Woonze").pack(side="left")
        self.show_main()

    def link(self, parent, text, url):
        item = tk.Label(parent, text=text, bg=parent.cget("bg"), fg="#77adff", cursor="hand2", font=(self.font, 9, "underline"))
        item.bind("<Button-1>", lambda _event: webbrowser.open(url))
        return item

    def build_main(self) -> None:
        state = self.card(self.main_page)
        state.pack(fill="x", pady=(0, 10))
        self.account_label = self.label(state, "Pulse: проверяю сессию…", bold=True, color="#cfe1ff")
        self.account_label.pack(anchor="w")
        self.accounts_label = self.label(state, f"Подтверждаем за аккаунтов: {len(self.enabled_accounts())} из {len(self.settings['accounts'])}", 9, color=self.MUTED)
        self.accounts_label.pack(anchor="w", pady=(2, 0))
        self.status_label = self.label(state, "Запуск интерфейса…", color=self.MUTED)
        self.status_label.pack(anchor="w", pady=(6, 10))
        controls = tk.Frame(state, bg=self.CARD)
        controls.pack(anchor="w")
        self.login_button = self.button(controls, text="Войти в Pulse", command=self.open_login)
        self.login_button.pack(side="left")
        self.button(controls, text="Проверить сессию", style="Secondary.TButton", command=self.check_session).pack(side="left", padx=8)
        self.scan_button = self.button(controls, text="Начать сканирование", command=self.toggle_scan)
        self.scan_button.pack(side="left")
        lecture_row = tk.Frame(state, bg=self.CARD)
        lecture_row.pack(anchor="w", pady=(8, 0))
        self.lecture_button = self.button(lecture_row, text="Открыть лекцию", style="Secondary.TButton", command=self.toggle_lecture)
        self.lecture_button.pack(side="left")
        self.label(lecture_row, "СДО и Pulse в одном окне", 9, color=self.MUTED).pack(side="left", padx=10)
        logs = self.card(self.main_page)
        logs.pack(fill="both", expand=True)
        self.label(logs, "Журнал работы", 11, True).pack(anchor="w")
        self.label(logs, f"Файл: {LOG_PATH}", 9, color=self.MUTED).pack(anchor="w", pady=(4, 8))
        self.console = tk.Text(logs, height=12, bg=self.FIELD, fg="#dcebff", insertbackground="white", relief="flat", wrap="word", font=(self.mono, 9), padx=10, pady=8)
        self.console.pack(fill="both", expand=True)
        self.console.configure(state="disabled")

    def scrollable(self, parent):
        """Возвращает прокручиваемую область: карточек настроек больше, чем высота окна"""
        canvas = tk.Canvas(parent, bg=self.BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=self.BG)
        window = canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        inner.bind("<Configure>", lambda _event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda event: canvas.itemconfigure(window, width=event.width))
        for sequence, step in (("<Button-4>", -1), ("<Button-5>", 1)):
            canvas.bind_all(sequence, lambda _event, value=step: self.scroll_settings(canvas, value))
        canvas.bind_all("<MouseWheel>", lambda event: self.scroll_settings(canvas, -1 if event.delta > 0 else 1))
        return inner

    def scroll_settings(self, canvas, step: int) -> None:
        if self.settings_page.winfo_ismapped():
            canvas.yview_scroll(step, "units")

    def telegram_status(self) -> str:
        if not self.settings.get("telegram_enabled"):
            return "Бот не подключён"
        if not self.settings.get("telegram_chat"):
            return "Ожидаю команду /start " + (self.pair_code or "в приложении")
        return f"Чат подключён: {self.settings['telegram_chat']}"

    def build_settings(self) -> None:
        scan = self.card(self.settings_body)
        scan.pack(fill="x", pady=(0, 10))
        self.label(scan, "Сканирование", 11, True).pack(anchor="w")
        self.label(scan, "Пауза после успешного подтверждения, минут.", 9, color=self.MUTED).pack(anchor="w", pady=(6, 5))
        row = tk.Frame(scan, bg=self.CARD)
        row.pack(anchor="w")
        self.cooldown = tk.StringVar(value=str(self.settings["cooldown_minutes"]))
        self.entry(row, width=8, textvariable=self.cooldown).pack(side="left")
        self.label(row, "минут").pack(side="left", padx=8)
        self.button(row, text="Сохранить", command=self.save_cooldown).pack(side="left")
        lecture = self.card(self.settings_body)
        lecture.pack(fill="x", pady=(0, 10))
        self.label(lecture, "Ссылка на занятие", 11, True).pack(anchor="w")
        self.label(lecture, "Откроется вкладкой рядом с Pulse: QR и кнопка присутствия ищутся прямо в ней,\nпоэтому снимки экрана не нужны.", 9, color=self.MUTED, justify="left").pack(anchor="w", pady=(6, 5))
        lecture_row = tk.Frame(lecture, bg=self.CARD)
        lecture_row.pack(anchor="w", fill="x")
        self.lecture_link = tk.StringVar(value=self.settings.get("lecture_url", ""))
        self.entry(lecture_row, textvariable=self.lecture_link).pack(side="left", fill="x", expand=True)
        self.button(lecture_row, text="Сохранить", command=self.save_lecture_link).pack(side="left", padx=8)
        support = self.card(self.settings_body)
        support.pack(fill="x", pady=(0, 10))
        self.label(support, "Поддержка", 11, True).pack(anchor="w")
        support_row = tk.Frame(support, bg=self.CARD)
        support_row.pack(fill="x", pady=(6, 0))
        left = tk.Frame(support_row, bg=self.CARD)
        left.pack(side="left", fill="x", expand=True)
        self.label(left, "Поддержите автора: подключите FOTUR VPN (Работаем через Hiddify)\nили поддержите используя QR.", 9, color=self.MUTED, justify="left").pack(anchor="w")
        buttons = tk.Frame(left, bg=self.CARD)
        buttons.pack(anchor="w", pady=(8, 0))
        self.button(buttons, text="Открыть @foturvpnbot", command=lambda: webbrowser.open("https://t.me/foturvpnbot")).pack(side="left")
        self.button(buttons, text="Поддержать донатом", style="Secondary.TButton", command=lambda: webbrowser.open(CLOUDTIPS_URL)).pack(side="left", padx=8)
        if QR_IMAGE.exists():
            try:
                from PIL import Image, ImageTk
                with Image.open(QR_IMAGE) as image:
                    image.thumbnail((110, 110))
                    self.donation_qr = ImageTk.PhotoImage(image.copy())
                qr = tk.Label(support_row, image=self.donation_qr, bg=self.CARD, cursor="hand2")
                qr.pack(side="right", padx=(10, 0))
                qr.bind("<Button-1>", lambda _event: webbrowser.open(CLOUDTIPS_URL))
            except (tk.TclError, ImportError):
                LOGGER.exception("Donation QR cannot be loaded")
        profile = self.card(self.settings_body)
        profile.pack(fill="x")
        self.label(profile, "Аккаунты Pulse", 11, True).pack(anchor="w")
        self.label(profile, "Каждый аккаунт входит сам и хранит свою сессию.\nОтметка QR — аккаунт подтверждает найденный код.", 9, color=self.MUTED, justify="left").pack(anchor="w", pady=(6, 8))
        self.accounts_box = tk.Frame(profile, bg=self.CARD)
        self.accounts_box.pack(fill="x")
        buttons = tk.Frame(profile, bg=self.CARD)
        buttons.pack(anchor="w", pady=(10, 0))
        self.button(buttons, text="Добавить аккаунт", command=self.add_account).pack(side="left")
        self.button(buttons, text="Копировать путь", style="Secondary.TButton", command=self.copy_profile).pack(side="left", padx=8)
        self.button(buttons, text=OPEN_FOLDER_TEXT, style="Secondary.TButton", command=self.open_profile).pack(side="left")
        self.draw_accounts()
        telegram = self.card(self.settings_body)
        telegram.pack(fill="x", pady=(10, 0))
        self.label(telegram, "Управление из Telegram", 11, True).pack(anchor="w")
        self.label(telegram, "Бот присылает события и понимает команды /status, /scan, /stop, /lecture, /close.", 9, color=self.MUTED).pack(anchor="w", pady=(6, 6))
        self.telegram_label = self.label(telegram, self.telegram_status(), 9, True, "#cfe1ff")
        self.telegram_label.pack(anchor="w")
        telegram_row = tk.Frame(telegram, bg=self.CARD)
        telegram_row.pack(anchor="w", pady=(8, 0))
        self.button(telegram_row, text="Подключить бота", command=self.connect_bot).pack(side="left")
        self.button(telegram_row, text="Отключить", style="Danger.TButton", command=self.disconnect_bot).pack(side="left", padx=8)

    def open_profile(self) -> None:
        """Открывает папку профиля активного аккаунта системным файловым менеджером"""
        folder = profile_path(self.settings["current"])
        try:
            folder.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(folder)
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.exception("Profile folder cannot be opened")
            self.log("UI", "Не удалось открыть папку профиля: " + self.short(exc))

    def show_main(self) -> None:
        self.settings_page.pack_forget()
        self.main_page.pack(fill="both", expand=True)

    def show_settings(self) -> None:
        self.main_page.pack_forget()
        self.settings_page.pack(fill="both", expand=True)

    def log(self, tag: str, message: str) -> None:
        LOGGER.info("[%s] %s", tag, message)
        self.events.put(("log", (tag, message)))
        if tag in NOTIFY_TAGS:
            self.notify(f"{tag}: {message}")

    def pump(self) -> None:
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind == "log":
                    tag, message = value
                    self.console.configure(state="normal")
                    self.console.insert("end", f"{time.strftime('%H:%M:%S')} | {tag:<7} {message}\n")
                    self.console.see("end")
                    self.console.configure(state="disabled")
                elif kind == "status":
                    self.status_label.configure(text=value)
                elif kind == "account":
                    self.account = value
                    if value:
                        self.account_label.configure(text=f"Pulse: {value} · {self.current_account()['title']}")
                        self.login_button.pack_forget()
                    else:
                        self.account_label.configure(text="Pulse: вход не выполнен")
                        if not self.login_button.winfo_ismapped():
                            self.login_button.pack(side="left")
                elif kind == "scan":
                    self.scan_button.configure(text="Остановить сканирование" if value else "Начать сканирование")
                elif kind == "telegram":
                    self.telegram_label.configure(text=self.telegram_status())
                elif kind == "accounts":
                    self.draw_accounts()
                    enabled = len(self.enabled_accounts())
                    self.accounts_label.configure(text=f"Подтверждаем за аккаунтов: {enabled} из {len(self.settings['accounts'])}")
                elif kind == "lecture":
                    self.lecture_button.configure(text="Закрыть лекцию" if value else "Открыть лекцию")
        except queue.Empty:
            pass
        if not self.stop.is_set():
            self.root.after(150, self.pump)

    def post(self, kind: str, value: object) -> None:
        self.events.put((kind, value))

    def current_account(self, account_id: str | None = None) -> dict:
        """Аккаунт по идентификатору, по умолчанию активный"""
        wanted = account_id or self.settings["current"]
        for item in self.settings["accounts"]:
            if item["id"] == wanted:
                return item
        return self.settings["accounts"][0]

    def enabled_accounts(self) -> list[dict]:
        """Аккаунты, которые подтверждают найденный QR"""
        return [item for item in self.settings["accounts"] if item.get("enabled")] or [self.current_account()]

    def save_settings(self) -> None:
        SETTINGS_PATH.write_text(json.dumps(self.settings, ensure_ascii=False, indent=2), encoding="utf-8")

    def draw_accounts(self) -> None:
        """Перерисовывает список аккаунтов на странице настроек"""
        for child in self.accounts_box.winfo_children():
            child.destroy()
        self.current_choice = tk.StringVar(value=self.settings["current"])
        for account in self.settings["accounts"]:
            row = tk.Frame(self.accounts_box, bg=self.CARD)
            row.pack(fill="x", pady=3)
            # Кнопки занимают место первыми, иначе длинное имя вытесняет их за край строки
            self.button(row, text="Удалить", style="Danger.TButton",
                        command=lambda item=account: self.remove_account(item)).pack(side="right")
            self.button(row, text="Выйти", style="Secondary.TButton",
                        command=lambda item=account: self.logout(item)).pack(side="right", padx=8)
            self.button(row, text="Пароль", style="Secondary.TButton",
                        command=lambda item=account: self.edit_credentials(item)).pack(side="right")
            enabled = tk.BooleanVar(value=bool(account["enabled"]))
            tk.Checkbutton(row, text="QR", variable=enabled, bg=self.CARD, fg=self.MUTED,
                           selectcolor=self.FIELD, activebackground=self.CARD, activeforeground=self.MUTED,
                           highlightthickness=0, font=(self.font, 9),
                           command=lambda item=account, flag=enabled: self.toggle_account(item, flag)).pack(side="right", padx=8)
            tk.Radiobutton(row, text=account["title"], variable=self.current_choice, value=account["id"],
                           command=self.select_account, bg=self.CARD, fg=self.TEXT, selectcolor=self.FIELD,
                           activebackground=self.CARD, activeforeground=self.TEXT, highlightthickness=0,
                           anchor="w", font=(self.font, 10, "bold")).pack(side="left", fill="x", expand=True)
            if account["name"]:
                status = "вход выполнен: " + account["name"]
            elif account.get("login"):
                status = "автовход настроен, войдите для проверки"
            else:
                status = "вход не выполнен"
            self.label(self.accounts_box, status, 9, color=self.MUTED).pack(anchor="w", padx=(26, 0), pady=(0, 6))

    def select_account(self) -> None:
        self.settings["current"] = self.current_choice.get()
        self.save_settings()
        account = self.current_account()
        self.post("account", account["name"])
        self.log("ACCOUNT", f"Активный аккаунт: {account['title']}")

    def toggle_account(self, account: dict, flag) -> None:
        account["enabled"] = bool(flag.get())
        self.save_settings()
        self.post("accounts", None)
        self.log("ACCOUNT", f"{account['title']}: {'участвует' if account['enabled'] else 'не участвует'} в подтверждении")

    def notify(self, text: str) -> None:
        """Отправляет сообщение в привязанный чат, не задерживая работу приложения"""
        chat = self.settings.get("telegram_chat")
        if not chat or not self.settings.get("telegram_enabled"):
            return
        token = load_password(TELEGRAM_KEY)
        if not token:
            return

        def send() -> None:
            try:
                telegram_call(token, "sendMessage", {"chat_id": chat, "text": text}, timeout=15)
            except (urllib.error.URLError, OSError, ValueError):
                LOGGER.exception("Telegram message failed")

        threading.Thread(target=send, daemon=True).start()

    def start_bot(self) -> None:
        """Запускает бота в единственном экземпляре: два опроса одним токеном дают ошибку 409"""
        token = load_password(TELEGRAM_KEY)
        if not token:
            self.log("BOT", "Токен бота не сохранён")
            return
        threading.Thread(target=self.restart_bot, args=(token,), daemon=True).start()

    def restart_bot(self, token: str) -> None:
        previous = self.bot_thread
        self.bot_running = False
        if previous and previous.is_alive():
            previous.join(timeout=60)
        self.bot_thread = threading.current_thread()
        self.bot_running = True
        self.bot_worker(token)

    def bot_alive(self) -> bool:
        return self.bot_running and self.bot_thread is threading.current_thread() and not self.stop.is_set()

    def bot_worker(self, token: str) -> None:
        """Слушает команды Telegram длинным опросом"""
        offset = 0
        conflict_at = 0.0
        try:
            # Вебхук и опрос вместе не работают, поэтому вебхук снимается
            telegram_call(token, "deleteWebhook", {"drop_pending_updates": False}, timeout=15)
        except (urllib.error.URLError, OSError, ValueError):
            LOGGER.info("deleteWebhook failed")
        self.log("BOT", "Бот слушает команды")
        while self.bot_alive():
            try:
                answer = telegram_call(token, "getUpdates", {"timeout": 25, "offset": offset})
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    if time.time() - conflict_at > 60:
                        conflict_at = time.time()
                        self.log("BOT", "Этот бот уже опрашивается другой программой, жду освобождения")
                    self.stop.wait(15)
                    continue
                if exc.code == 401:
                    self.log("BOT", "Telegram не принял токен, подключите бота заново")
                    break
                self.log("BOT", "Telegram ответил ошибкой: " + self.short(exc))
                self.stop.wait(10)
                continue
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self.log("BOT", "Связь с Telegram потеряна: " + self.short(exc))
                self.stop.wait(10)
                continue
            for update in answer.get("result", []):
                offset = update.get("update_id", 0) + 1
                try:
                    self.handle_update(token, update)
                except Exception:
                    LOGGER.exception("Telegram update failed")
        if self.bot_thread is threading.current_thread():
            self.bot_running = False
            self.bot_thread = None
            self.log("BOT", "Бот остановлен")

    def handle_update(self, token: str, update: dict) -> None:
        """Привязывает чат по коду и отвечает на команды только привязанному чату"""
        message = update.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if not chat or not text:
            return
        known = self.settings.get("telegram_chat")
        if not known:
            if self.pair_code and text.startswith("/start") and self.pair_code in text:
                self.settings["telegram_chat"] = chat
                self.settings["telegram_enabled"] = True
                self.pair_code = ""
                self.save_settings()
                self.post("telegram", None)
                self.log("BOT", "Чат привязан, команды доступны")
                telegram_call(token, "sendMessage", {"chat_id": chat, "text": self.bot_command("/help")})
            return
        if chat != known:
            return
        telegram_call(token, "sendMessage", {"chat_id": chat, "text": self.bot_command(text)})

    def bot_command(self, text: str) -> str:
        """Выполняет команду чата и возвращает ответ"""
        command = text.split()[0].lower().split("@")[0]
        if command == "/status":
            logged = [item["title"] for item in self.settings["accounts"] if item["name"]]
            lines = [f"Аккаунтов: {len(self.settings['accounts'])}, вошли: {len(logged) or 0}",
                     "Подтверждают QR: " + ", ".join(item["title"] for item in self.enabled_accounts()),
                     "Поиск QR на экранах: " + ("идёт" if self.scanning else "остановлен"),
                     "Режим лекции: " + ("открыт" if self.lecture else "закрыт"),
                     "Ссылка занятия: " + (self.settings.get("lecture_url") or "не задана")]
            return "\n".join(lines)
        if command == "/scan":
            if self.scanning:
                return "Поиск QR уже идёт"
            self.root.after(0, self.toggle_scan)
            return "Запускаю поиск QR на экранах"
        if command == "/stop":
            if not self.scanning:
                return "Поиск QR не запущен"
            self.root.after(0, self.toggle_scan)
            return "Останавливаю поиск QR"
        if command == "/lecture":
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                link = parts[1].strip()
                if not link.startswith(("http://", "https://")):
                    return "Ссылка должна начинаться с http:// или https://"
                self.settings["lecture_url"] = link
                self.save_settings()
                self.root.after(0, lambda: self.lecture_link.set(link))
                if self.lecture:
                    return "Ссылка сохранена. Закройте окно командой /close и откройте заново"
            if self.lecture:
                return "Окно лекции уже открыто"
            self.root.after(0, self.toggle_lecture)
            link = self.settings.get("lecture_url", "")
            return "Открываю занятие: " + link if link else "Открываю СДО и Pulse"
        if command == "/close":
            if not self.lecture:
                return "Окно лекции не открыто"
            self.root.after(0, self.toggle_lecture)
            return "Закрываю окно лекции"
        return ("Команды:\n/status — что сейчас происходит\n/scan — начать поиск QR\n"
                "/stop — остановить поиск\n/lecture — открыть занятие\n"
                "/lecture <ссылка> — запомнить ссылку и открыть её\n/close — закрыть окно лекции")

    def connect_bot(self) -> None:
        """Сохраняет токен бота и ждёт команду привязки чата"""
        if not keyring_module():
            messagebox.showinfo(APP_NAME, "Системное хранилище паролей недоступно, токен бота сохранить негде")
            return
        token = simpledialog.askstring(APP_NAME, "Токен бота от @BotFather", parent=self.root)
        if token is None:
            return
        token = token.strip()
        if not token:
            return
        if not save_password(TELEGRAM_KEY, token):
            messagebox.showerror(APP_NAME, "Не удалось сохранить токен в системном хранилище")
            return
        self.settings["telegram_chat"] = 0
        self.settings["telegram_enabled"] = True
        self.save_settings()
        self.pair_code = f"{secrets.randbelow(1000000):06d}"
        self.start_bot()
        self.post("telegram", None)
        self.log("BOT", "Отправьте боту команду /start " + self.pair_code)
        threading.Thread(target=self.check_bot_token, args=(token,), daemon=True).start()
        messagebox.showinfo(APP_NAME, "Откройте своего бота в Telegram и отправьте ему:\n\n/start " + self.pair_code)

    def check_bot_token(self, token: str) -> None:
        """Показывает имя бота, чтобы сразу было видно, принят ли токен"""
        try:
            answer = telegram_call(token, "getMe", timeout=15)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.log("BOT", "Проверить токен не удалось: " + self.short(exc))
            return
        username = (answer.get("result") or {}).get("username", "")
        if username:
            self.log("BOT", f"Бот @{username} на связи, отправьте ему /start {self.pair_code}")
        else:
            self.log("BOT", "Telegram не принял токен")

    def disconnect_bot(self) -> None:
        self.bot_running = False
        self.pair_code = ""
        self.settings["telegram_chat"] = 0
        self.settings["telegram_enabled"] = False
        self.save_settings()
        forget_password(TELEGRAM_KEY)
        self.post("telegram", None)
        self.log("BOT", "Телеграм отключён")

    def edit_credentials(self, account: dict) -> None:
        """Сохраняет логин и пароль аккаунта в системном хранилище"""
        if not keyring_module():
            messagebox.showinfo(APP_NAME, "Системное хранилище паролей недоступно.\n"
                                          "В Linux нужен KWallet или GNOME Keyring, иначе входите вручную.")
            return
        login = simpledialog.askstring(APP_NAME, f"Логин для «{account['title']}»",
                                       initialvalue=account.get("login", ""), parent=self.root)
        if login is None:
            return
        login = login.strip()
        if not login:
            account["login"] = ""
            forget_password(account["id"])
            self.save_settings()
            self.post("accounts", None)
            self.log("AUTH", f"{account['title']}: сохранённые данные входа удалены")
            return
        password = simpledialog.askstring(APP_NAME, f"Пароль для «{account['title']}»", show="*", parent=self.root)
        if password is None:
            return
        if not save_password(account["id"], password):
            messagebox.showerror(APP_NAME, "Не удалось сохранить пароль в системном хранилище")
            return
        account["login"] = login
        self.save_settings()
        self.post("accounts", None)
        self.log("AUTH", f"{account['title']}: данные входа сохранены, пароль лежит в системном хранилище")

    def add_account(self) -> None:
        title = simpledialog.askstring(APP_NAME, "Название аккаунта", parent=self.root)
        if not title or not title.strip():
            return
        account = new_account(title.strip()[:40])
        self.settings["accounts"].append(account)
        self.settings["current"] = account["id"]
        self.save_settings()
        self.post("accounts", None)
        self.log("ACCOUNT", f"Добавлен аккаунт {account['title']}. Нажмите «Войти в Pulse» для входа")
        self.post("status", "Аккаунт добавлен — войдите в Pulse")

    def remove_account(self, account: dict) -> None:
        if len(self.settings["accounts"]) == 1:
            messagebox.showinfo(APP_NAME, "Нужен хотя бы один аккаунт")
            return
        if self.browser_busy:
            messagebox.showinfo(APP_NAME, "Сначала завершите проверку или вход в Pulse")
            return
        if not messagebox.askyesno(APP_NAME, f"Удалить аккаунт «{account['title']}» вместе с его сессией?"):
            return
        shutil.rmtree(profile_path(account["id"]).parent, ignore_errors=True)
        forget_password(account["id"])
        self.settings["accounts"].remove(account)
        if self.settings["current"] == account["id"]:
            self.settings["current"] = self.settings["accounts"][0]["id"]
        self.save_settings()
        self.post("accounts", None)
        self.post("account", self.current_account()["name"])
        self.log("ACCOUNT", f"Аккаунт {account['title']} удалён")

    def save_cooldown(self) -> None:
        try:
            minutes = int(self.cooldown.get())
            if not 1 <= minutes <= 180:
                raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "Введите целое число от 1 до 180")
            return
        self.settings["cooldown_minutes"] = minutes
        self.save_settings()
        self.log("SETTINGS", f"Пауза после успеха: {minutes} мин.")
        self.post("status", "Настройки сохранены")

    def save_lecture_link(self) -> None:
        link = self.lecture_link.get().strip()
        if link and not link.startswith(("http://", "https://")):
            messagebox.showerror(APP_NAME, "Ссылка должна начинаться с http:// или https://")
            return
        self.settings["lecture_url"] = link
        self.save_settings()
        self.log("LECTURE", "Ссылка занятия сохранена" if link else "Ссылка занятия очищена")
        self.post("status", "Настройки сохранены")

    def copy_profile(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(str(profile_path(self.settings["current"])))
        self.post("status", "Путь профиля скопирован")

    def state(self, page) -> tuple[str, bool]:
        page.goto(PULSE_HOME, wait_until="domcontentloaded", timeout=60000)
        time.sleep(2)
        state = page.evaluate(STATE_JS)
        name = str(state.get("name", "")).strip()
        return name, bool(name) and bool(state.get("onPulse"))

    def check_session(self) -> None:
        if self.browser_busy:
            return
        self.browser_busy = True
        self.post("status", "Проверяю сохранённую сессию…")
        threading.Thread(target=self.check_worker, daemon=True).start()

    def check_worker(self) -> None:
        """Проверяет сохранённую сессию каждого аккаунта по очереди"""
        try:
            with sync_playwright() as p:
                for account in self.settings["accounts"]:
                    try:
                        ctx = launch_context(p, headless=True, profile=profile_path(account["id"]))
                        name, logged = self.state(ctx.pages[0] if ctx.pages else ctx.new_page())
                        ctx.close()
                    except Exception as exc:
                        LOGGER.exception("Session check failure")
                        account["name"] = ""
                        self.log("AUTH", f"{account['title']}: проверка недоступна — " + self.short(exc))
                        continue
                    account["name"] = name if logged else ""
                    self.log("AUTH", f"{account['title']}: " + (f"сессия активна, {name}" if logged else "вход не выполнен"))
            self.save_settings()
            self.post("accounts", None)
            self.post("account", self.current_account()["name"])
            logged_in = [item for item in self.settings["accounts"] if item["name"]]
            self.post("status", f"Сессий активно: {len(logged_in)} из {len(self.settings['accounts'])}" if logged_in else "Войдите в Pulse")
        finally:
            self.browser_busy = False

    def open_login(self) -> None:
        if self.browser_busy:
            self.post("status", "Уже выполняется проверка или вход…")
            return
        self.browser_busy = True
        self.post("status", f"Завершите вход и 2FA в окне Chrome: {self.current_account()['title']}")
        self.log("AUTH", f"{self.current_account()['title']}: открыто официальное окно Pulse для входа")
        threading.Thread(target=self.login_worker, daemon=True).start()

    @staticmethod
    def login_fields(page):
        """Поля логина и пароля в любом кадре страницы входа"""
        for frame in page.frames:
            try:
                password = frame.locator("input[type=password]:visible")
                if not password.count():
                    continue
                login = frame.locator("input[type=text]:visible, input[type=email]:visible, input[type=tel]:visible")
                return login.first if login.count() else None, password.first
            except PlaywrightError:
                continue
        return None, None

    @staticmethod
    def code_field(page):
        """Поле одноразового кода двухфакторной проверки"""
        selector = ("input[autocomplete='one-time-code']:visible, input[name*='otp' i]:visible, "
                    "input[name*='code' i]:visible, input[id*='otp' i]:visible, input[id*='code' i]:visible")
        for frame in page.frames:
            try:
                field = frame.locator(selector)
                if field.count():
                    return field.first
            except PlaywrightError:
                continue
        return None

    def ask_code(self, title: str) -> str:
        """Спрашивает код 2FA в главном потоке и ждёт ответа"""
        answer: queue.Queue[str | None] = queue.Queue(maxsize=1)
        self.root.after(0, lambda: answer.put(simpledialog.askstring(APP_NAME, f"Код двухфакторной проверки для «{title}»", parent=self.root)))
        try:
            return (answer.get(timeout=300) or "").strip()
        except queue.Empty:
            return ""

    def autologin(self, page, account: dict) -> None:
        """Заполняет форму входа сохранёнными данными, код 2FA спрашивает у пользователя"""
        login = account.get("login", "")
        password = load_password(account["id"]) if login else ""
        if not login or not password:
            return
        for _ in range(20):
            login_field, password_field = self.login_fields(page)
            if password_field:
                break
            if self.stop.wait(1):
                return
        else:
            self.log("AUTH", f"{account['title']}: форма входа не найдена, войдите вручную")
            return
        try:
            if login_field:
                login_field.fill(login)
            password_field.fill(password)
            password_field.press("Enter")
            self.log("AUTH", f"{account['title']}: данные входа отправлены")
        except PlaywrightError as exc:
            self.log("AUTH", f"{account['title']}: не удалось заполнить форму — " + self.short(exc))
            return
        for _ in range(20):
            if self.stop.wait(1):
                return
            field = self.code_field(page)
            if not field:
                continue
            self.post("status", "Введите код двухфакторной проверки")
            code = self.ask_code(account["title"])
            if not code:
                self.log("AUTH", f"{account['title']}: код не введён, завершите вход в окне Chrome")
                return
            try:
                field.fill(code)
                field.press("Enter")
                self.log("AUTH", f"{account['title']}: код двухфакторной проверки отправлен")
            except PlaywrightError as exc:
                self.log("AUTH", f"{account['title']}: не удалось отправить код — " + self.short(exc))
            return

    def login_worker(self) -> None:
        try:
            with sync_playwright() as p:
                account = self.current_account()
                ctx = launch_context(p, headless=False, profile=profile_path(account["id"]))
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(PULSE_HOME, wait_until="domcontentloaded", timeout=60000)
                self.autologin(page, account)
                for _ in range(600):
                    if self.stop.wait(1):
                        break
                    try:
                        state = page.evaluate(STATE_JS)
                        name = str(state.get("name", "")).strip()
                        if name and state.get("onPulse"):
                            ctx.close()
                            account["name"] = name
                            self.save_settings()
                            self.post("accounts", None)
                            self.post("account", name)
                            self.post("status", "Сессия активна")
                            self.log("AUTH", f"{account['title']}: вход завершён, {name}")
                            return
                    except PlaywrightError:
                        break
                ctx.close()
                self.post("status", "Вход не подтверждён — откройте Pulse ещё раз")
                self.log("AUTH", "Окно входа закрыто до подтверждения сессии")
        except Exception as exc:
            LOGGER.exception("Login failure")
            self.post("status", "Не удалось открыть Chrome: " + self.short(exc))
            self.log("AUTH", "Ошибка запуска официального окна: " + self.short(exc))
        finally:
            self.browser_busy = False

    def toggle_scan(self) -> None:
        if self.lecture and not self.scanning:
            self.post("status", "Работает режим лекции — сначала закройте его")
            return
        if self.scanning:
            self.scanning = False
            self.stop.set()
            self.stop = threading.Event()
            self.post("scan", False)
            self.post("status", "Сканирование остановлено")
            self.log("SCAN", "Поиск QR остановлен")
            return
        if not self.account:
            self.post("status", "Сначала войдите в Pulse")
            return
        self.scanning = True
        self.post("scan", True)
        self.post("status", "Ищу QR на экранах…")
        self.log("SCAN", "Начат поиск QR на всех экранах (каждые 2 секунды)")
        threading.Thread(target=self.scan_worker, daemon=True).start()

    def toggle_lecture(self) -> None:
        """Открывает СДО и Pulse в одном окне браузера и следит за ними"""
        if self.lecture:
            self.lecture = False
            self.post("lecture", False)
            self.post("status", "Закрываю окно лекции…")
            return
        if self.scanning:
            self.post("status", "Сначала остановите сканирование экрана")
            return
        if self.browser_busy:
            self.post("status", "Уже выполняется проверка или вход…")
            return
        self.lecture = True
        self.browser_busy = True
        self.post("lecture", True)
        self.post("status", "Открываю СДО и Pulse…")
        threading.Thread(target=self.lecture_worker, daemon=True).start()

    @staticmethod
    def presence_button(page):
        """Ищет кнопку подтверждения присутствия в любом кадре страницы"""
        for frame in page.frames:
            for locator in (frame.get_by_role("button", name=PRESENCE_TEXT), frame.get_by_text(PRESENCE_TEXT)):
                try:
                    if locator.count() and locator.first.is_visible():
                        return locator.first
                except PlaywrightError:
                    continue
        return None

    def press_presence(self, button) -> bool:
        """Нажимает кнопку присутствия

        В неактивной вкладке Chrome не доставляет настоящие события мыши,
        поэтому запасной вариант — событие click из самой страницы.
        """
        try:
            button.click(timeout=3000)
            return True
        except PlaywrightError:
            LOGGER.info("Presence button click timed out, falling back to DOM event")
        try:
            button.dispatch_event("click")
            return True
        except PlaywrightError as exc:
            self.log("PRESENCE", "Не удалось нажать кнопку подтверждения: " + self.short(exc))
            return False

    def page_token(self, page) -> str | None:
        """Ищет QR-код Pulse на снимке вкладки"""
        import zxingcpp
        from PIL import Image
        with Image.open(io.BytesIO(page.screenshot(timeout=10000))) as image:
            for code in zxingcpp.read_barcodes(image.convert("RGB")):
                token = qr_token(code.text)
                if token:
                    return token
        return None

    def approve_lecture(self, playwright, pulse, current: dict, token: str) -> tuple[bool, str]:
        """Подтверждает QR за открытый аккаунт и за остальные отмеченные профили"""
        approved, failure = 0, ""
        for account in self.enabled_accounts():
            if account["id"] == current["id"]:
                try:
                    ok, detail = self.approve_request(pulse, token)
                except PlaywrightError as exc:
                    ok, detail = False, self.short(exc)
            else:
                ok, detail = self.approve_account(playwright, account, token)
            if ok:
                approved += 1
                self.log("PULSE", f"{account['title']}: QR принят")
            else:
                failure = failure or detail
                self.log("PULSE", f"{account['title']}: отказ — {detail}")
        return approved > 0, failure

    def lecture_worker(self) -> None:
        """Ведёт вкладки лекции: подтверждает присутствие и ловит QR прямо в браузере"""
        confirmed_tokens: set[str] = set()
        qr_successes = 0
        try:
            with sync_playwright() as p:
                current = self.current_account()
                ctx = launch_context(p, headless=False, profile=profile_path(current["id"]))
                pulse = ctx.pages[0] if ctx.pages else ctx.new_page()
                pulse.goto(PULSE_HOME, wait_until="domcontentloaded", timeout=60000)
                lecture = ctx.new_page()
                lecture.goto(EDU_HOME, wait_until="domcontentloaded", timeout=60000)
                self.log("LECTURE", "Открыты вкладки СДО и Pulse. Войдите и запустите лекцию в этом окне")
                link = self.settings.get("lecture_url", "").strip()
                if link:
                    try:
                        ctx.new_page().goto(link, wait_until="domcontentloaded", timeout=60000)
                        self.log("LECTURE", "Открыта сохранённая ссылка занятия")
                    except PlaywrightError as exc:
                        self.log("LECTURE", "Ссылка занятия не открылась: " + self.short(exc))
                self.post("status", "Окно лекции открыто — следим за присутствием и QR")
                while self.lecture and not self.stop.is_set():
                    pages = [page for page in ctx.pages if not page.is_closed()]
                    if not pages:
                        self.log("LECTURE", "Окно лекции закрыто")
                        break
                    for page in pages:
                        if page is pulse:
                            continue
                        try:
                            button = self.presence_button(page)
                            if button:
                                if self.press_presence(button):
                                    self.log("PRESENCE", "Присутствие в онлайн-мероприятии подтверждено")
                                    self.post("status", "Присутствие в онлайн-мероприятии подтверждено")
                                continue
                            if time.time() - self.last_success < int(self.settings["cooldown_minutes"]) * 60:
                                continue
                            token = self.page_token(page)
                        except PlaywrightError as exc:
                            LOGGER.info("Page skipped: %s", exc)
                            continue
                        if not token or token in confirmed_tokens:
                            continue
                        attempt = qr_successes + 1
                        self.log("SCAN", f"Найден новый QR Pulse во вкладке. Подтверждение {attempt}/3")
                        ok, detail = self.approve_lecture(p, pulse, current, token)
                        if ok:
                            confirmed_tokens.add(token)
                            qr_successes += 1
                            if qr_successes == 3:
                                self.last_success = time.time()
                                confirmed_tokens.clear()
                                qr_successes = 0
                                self.log("PULSE", "Три разных QR подтверждены: 3/3. Включена пауза QR")
                                self.post("status", "Посещение подтверждено: 3/3")
                            else:
                                self.log("PULSE", f"Новый QR подтверждён: {qr_successes}/3")
                                self.post("status", f"Посещение подтверждено: {qr_successes}/3")
                        else:
                            confirmed_tokens.clear()
                            qr_successes = 0
                            self.log("PULSE", "Подтверждение отклонено: " + detail)
                            self.post("status", "Ошибка Pulse: " + detail)
                    self.stop.wait(2)
                ctx.close()
        except Exception as exc:
            LOGGER.exception("Lecture mode failure")
            self.log("LECTURE", "Режим лекции остановлен: " + self.short(exc))
            self.post("status", "Режим лекции недоступен: " + self.short(exc))
        finally:
            self.lecture = False
            self.browser_busy = False
            self.post("lecture", False)

    def scan_worker(self) -> None:
        try:
            import zxingcpp
        except Exception as exc:
            self.log("SCAN", "Не удалось загрузить модуль сканирования: " + self.short(exc))
            self.scanning = False
            self.post("scan", False)
            return
        if wayland_session():
            self.log("SCAN", "Сеанс Wayland: снимок экрана и автоклик работают только в сеансе X11")
        confirmed_tokens: set[str] = set()
        qr_successes = 0
        presence_pending = False
        while self.scanning and not self.stop.is_set():
            try:
                screenshot, origin_x, origin_y = grab_screens()
                presence_button = self.find_presence_button(screenshot)
                presence_activity = bool(presence_button)
                if presence_button:
                    self.click_screen_point(presence_button[0] + origin_x, presence_button[1] + origin_y)
                    presence_pending = True
                    self.post("status", "Подтверждаю присутствие в MTS Link…")
                elif presence_pending:
                    presence_pending = False
                    presence_activity = True
                    self.log("PRESENCE", "Присутствие в онлайн-мероприятии подтверждено")
                    self.post("status", "Присутствие в онлайн-мероприятии подтверждено")

                if time.time() - self.last_success >= int(self.settings["cooldown_minutes"]) * 60:
                    if self.last_success:
                        self.last_success = 0.0
                        confirmed_tokens.clear()
                        qr_successes = 0
                    codes = zxingcpp.read_barcodes(screenshot)
                    token = None
                    for code in codes:
                        token = qr_token(code.text)
                        if token:
                            break
                    if token and token not in confirmed_tokens:
                        attempt = qr_successes + 1
                        self.log("SCAN", f"Найден новый QR Pulse. Подтверждение {attempt}/3")
                        self.post("status", f"Найден новый QR — подтверждение {attempt}/3…")
                        ok, detail = self.approve(token)
                        if ok:
                            confirmed_tokens.add(token)
                            qr_successes += 1
                            if qr_successes == 3:
                                self.last_success = time.time()
                                self.log("PULSE", "Три разных QR подтверждены: 3/3. Включена пауза QR")
                                self.post("status", "Посещение подтверждено: 3/3")
                            else:
                                self.log("PULSE", f"Новый QR подтверждён: {qr_successes}/3")
                                self.post("status", f"Посещение подтверждено: {qr_successes}/3")
                        else:
                            confirmed_tokens.clear()
                            qr_successes = 0
                            self.log("PULSE", "Подтверждение отклонено: " + detail)
                            self.post("status", "Ошибка Pulse: " + detail)
                elif not presence_activity:
                    self.post("status", "Пауза после подтверждения QR")
                self.stop.wait(2)
            except Exception as exc:
                LOGGER.exception("Scanner failure")
                self.log("SCAN", "Ошибка сканирования: " + self.short(exc))
                self.stop.wait(2)

    @staticmethod
    def find_presence_button(image) -> tuple[int, int] | None:
        """Возвращает центр кнопки подтверждения MTS Link, если она видна
        В рабочем окне и демо используется ярко-фиолетовая кнопка
        Перед кликом проверяется сплошной фиолетовый прямоугольник в нескольких строках
        Обычные элементы интерфейса и QR-коды этому условию не соответствуют
        """
        rgb = image.convert("RGB")
        width, height = rgb.size
        pixels = rgb.load()
        candidates: list[tuple[int, int, int]] = []
        for y in range(0, height, 2):
            x = 0
            while x < width:
                red, green, blue = pixels[x, y]
                if not (130 <= red <= 190 and green <= 65 and 185 <= blue <= 255):
                    x += 2
                    continue
                start = x
                while x < width:
                    red, green, blue = pixels[x, y]
                    if not (130 <= red <= 190 and green <= 65 and 185 <= blue <= 255):
                        break
                    x += 2
                run = x - start
                if 70 <= run <= 220:
                    candidates.append((start, y, run))
                x += 2

        for start, y, run in candidates:
            supporting_rows = sum(
                1
                for sx, sy, sw in candidates
                if abs(sx - start) <= 8 and abs(sw - run) <= 16 and abs(sy - y) <= 24
            )
            if supporting_rows >= 5:
                center_x = start + run // 2
                # Кнопка находится в белом модальном окне
                # Это исключает аватары, бейджи и другие фиолетовые элементы страницы
                white_samples = 0
                for sample_x, sample_y in (
                    (center_x, y - 42),
                    (center_x - 80, y - 42),
                    (center_x + 80, y - 42),
                    (center_x - 110, y + 16),
                    (center_x + 110, y + 16),
                ):
                    if 0 <= sample_x < width and 0 <= sample_y < height:
                        red, green, blue = pixels[sample_x, sample_y]
                        white_samples += red >= 205 and green >= 205 and blue >= 205
                if white_samples >= 3:
                    return center_x, y + 18
        return None

    def click_screen_point(self, x: int, y: int) -> None:
        """Клик по точке виртуального рабочего стола"""
        if sys.platform == "win32":
            import ctypes
            user32 = ctypes.windll.user32
            user32.SetCursorPos(x, y)
            user32.mouse_event(0x0002, 0, 0, 0, 0)  # Нажатие левой кнопки
            user32.mouse_event(0x0004, 0, 0, 0, 0)  # Отпускание левой кнопки
            return
        if sys.platform == "linux":
            if click_x11(x, y) or click_xdotool(x, y):
                return
            self.log("PRESENCE", "Автоклик недоступен: нужен сеанс X11 или XWayland либо пакет xdotool")
            return
        self.log("PRESENCE", "Автоклик поддерживается только в Windows и Linux")

    @staticmethod
    def approve_request(page, token: str) -> tuple[bool, str]:
        """Отправляет подтверждение QR со страницы Pulse, где уже есть Cookie сессии"""
        result = page.evaluate("""async ({url, body}) => {
          const r = await fetch(url, {method: 'POST', credentials: 'include', headers: {
            'content-type': 'application/grpc-web+proto', 'x-grpc-web': '1',
            'x-requested-with': 'XMLHttpRequest', 'pulse-app-type': 'pulse'}, body: Uint8Array.from(body)});
          return {status:r.status, bytes:[...new Uint8Array(await r.arrayBuffer())]};
        }""", {"url": PULSE_RPC, "body": list(grpc_body(token))})
        grpc_status, grpc_message = grpc_web_trailer(bytes(result.get("bytes", [])))
        if result.get("status") == 200 and grpc_status == "0":
            return True, ""
        return False, grpc_message or f"HTTP {result.get('status')}; gRPC {grpc_status or 'не указан'}"

    def approve_account(self, playwright, account: dict, token: str) -> tuple[bool, str]:
        """Подтверждает QR в отдельном профиле аккаунта"""
        try:
            ctx = launch_context(playwright, headless=True, profile=profile_path(account["id"]))
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(PULSE_HOME, wait_until="domcontentloaded", timeout=60000)
            answer = self.approve_request(page, token)
            ctx.close()
            return answer
        except Exception as exc:
            LOGGER.exception("Approval failure")
            return False, self.short(exc)

    def approve(self, token: str) -> tuple[bool, str]:
        """Подтверждает QR за каждый отмеченный аккаунт"""
        if self.browser_busy:
            return False, "завершите вход в Pulse"
        approved, failure = 0, ""
        try:
            with sync_playwright() as p:
                for account in self.enabled_accounts():
                    ok, detail = self.approve_account(p, account, token)
                    if ok:
                        approved += 1
                        self.log("PULSE", f"{account['title']}: QR принят")
                    else:
                        failure = failure or detail
                        self.log("PULSE", f"{account['title']}: отказ — {detail}")
        except Exception as exc:
            LOGGER.exception("Approval failure")
            return False, self.short(exc)
        return approved > 0, failure

    def logout(self, account: dict | None = None) -> None:
        account = account or self.current_account()
        if self.browser_busy:
            messagebox.showinfo(APP_NAME, "Сначала завершите проверку или вход в Pulse")
            return
        if not messagebox.askyesno(APP_NAME, f"Сбросить сессию аккаунта «{account['title']}»? Потребуется войти заново"):
            return
        folder = profile_path(account["id"])
        try:
            shutil.rmtree(folder)
            folder.mkdir(parents=True, exist_ok=True)
            account["name"] = ""
            self.save_settings()
            self.post("accounts", None)
            if account["id"] == self.settings["current"]:
                self.post("account", "")
            self.post("status", f"Сессия аккаунта «{account['title']}» сброшена")
            self.log("AUTH", f"{account['title']}: сохранённая сессия удалена")
        except OSError as exc:
            messagebox.showerror(APP_NAME, "Не удалось удалить профиль. Закройте окно Chrome Pulse и повторите\n\n" + str(exc))

    @staticmethod
    def short(exc: BaseException) -> str:
        return str(exc).replace("\n", " ").strip()[:180] or exc.__class__.__name__

    def close(self) -> None:
        self.scanning = False
        self.lecture = False
        self.bot_running = False
        self.stop.set()
        self.log("UI", "Приложение закрыто")
        self.root.after(100, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()


if __name__ == "__main__":
    App().run()

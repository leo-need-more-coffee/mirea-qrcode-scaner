"""Окно приложения и сценарии работы с Pulse"""
from __future__ import annotations

import os
import queue
import secrets
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import font as tkfont, messagebox, simpledialog, ttk

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from . import APP_NAME, browser, login, passwords, paths, pulse, screen, settings as config, telegram
from .paths import LOGGER

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


class App:
    BG, CARD, FIELD = "#0f1929", "#192841", "#0a1424"

    TEXT, MUTED, BLUE, SECONDARY, RED = "#f4f7ff", "#9fc5ff", "#397cff", "#29466f", "#ca435b"

    def __init__(self) -> None:
        paths.DATA_DIR.mkdir(parents=True, exist_ok=True)
        paths.ACCOUNTS_DIR.mkdir(parents=True, exist_ok=True)
        paths.migrate_profile()
        self.settings = config.load_settings()
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop = threading.Event()
        self.scanning = False
        self.lecture = False
        self.bot = telegram.Bot(self)
        self.code_answer: queue.Queue | None = None
        self.login_attempts: dict[str, float] = {}
        self.code_window = None
        self.bot.pair_code = ""
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
        store = passwords.keyring_module()
        LOGGER.info("Keyring backend: %s", store.get_keyring() if store else "недоступно")
        self.root.after(150, self.pump)
        self.root.after(1000, self.check_session)
        if self.settings.get("telegram_enabled"):
            if not self.settings.get("telegram_chat"):
                # Код привязки живёт только в памяти, после перезапуска нужен новый
                self.bot.pair_code = f"{secrets.randbelow(1000000):06d}"
                self.log("BOT", "Отправьте боту команду /start " + self.bot.pair_code)
            self.bot.start()

    def set_icon(self) -> None:
        """Windows берёт значок из ICO, остальные системы — из PNG"""
        try:
            if sys.platform == "win32" and paths.APP_ICON.exists():
                self.root.iconbitmap(default=str(paths.APP_ICON))
            elif paths.APP_ICON_PNG.exists():
                self.window_icon = tk.PhotoImage(file=str(paths.APP_ICON_PNG))
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
        self.show_main()

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
        lecture_row.pack(fill="x", pady=(10, 0))
        self.lecture_button = self.button(lecture_row, text="Открыть лекцию", style="Secondary.TButton", command=self.toggle_lecture)
        self.lecture_button.pack(side="left")
        self.lecture_link = tk.StringVar(value=self.settings.get("lecture_url", ""))
        self.entry(lecture_row, textvariable=self.lecture_link).pack(side="left", fill="x", expand=True, padx=8)
        self.label(state, "Ссылка на занятие открывается вкладкой рядом с Pulse и СДО", 9, color=self.MUTED).pack(anchor="w", pady=(4, 0))
        logs = self.card(self.main_page)
        logs.pack(fill="both", expand=True)
        self.label(logs, "Журнал работы", 11, True).pack(anchor="w")
        self.label(logs, f"Файл: {paths.LOG_PATH}", 9, color=self.MUTED).pack(anchor="w", pady=(4, 8))
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
            return "Ожидаю команду /start " + (self.bot.pair_code or "в приложении")
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
        self.button(buttons, text=paths.OPEN_FOLDER_TEXT, style="Secondary.TButton", command=self.open_profile).pack(side="left")
        self.draw_accounts()
        telegram = self.card(self.settings_body)
        telegram.pack(fill="x", pady=(10, 0))
        self.label(telegram, "Управление из Telegram", 11, True).pack(anchor="w")
        self.label(telegram, "Бот присылает события и понимает команды /status, /scan, /stop, /lecture, /close.", 9, color=self.MUTED).pack(anchor="w", pady=(6, 6))
        self.telegram_label = self.label(telegram, self.telegram_status(), 9, True, "#cfe1ff")
        self.telegram_label.pack(anchor="w")
        telegram_row = tk.Frame(telegram, bg=self.CARD)
        telegram_row.pack(anchor="w", pady=(8, 0))
        self.button(telegram_row, text="Подключить бота", command=self.bot.connect).pack(side="left")
        self.button(telegram_row, text="Отключить", style="Danger.TButton", command=self.bot.disconnect).pack(side="left", padx=8)

    def show_main(self) -> None:
        self.settings_page.pack_forget()
        self.main_page.pack(fill="both", expand=True)

    def show_settings(self) -> None:
        self.main_page.pack_forget()
        self.settings_page.pack(fill="both", expand=True)

    def log(self, tag: str, message: str) -> None:
        LOGGER.info("[%s] %s", tag, message)
        self.events.put(("log", (tag, message)))
        if tag in telegram.NOTIFY_TAGS:
            self.bot.notify(f"{tag}: {message}")

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
        return config.find_account(self.settings, account_id)

    def enabled_accounts(self) -> list[dict]:
        return config.enabled_accounts(self.settings)

    def save_settings(self) -> None:
        config.save_settings(self.settings)

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

    def edit_credentials(self, account: dict) -> None:
        """Сохраняет логин и пароль аккаунта в системном хранилище"""
        if not passwords.keyring_module():
            self.show_info("Системное хранилище паролей недоступно.\n"
                           "В Linux нужен KWallet или GNOME Keyring, иначе входите вручную.")
            return
        name = self.ask_text(f"Логин для «{account['title']}»", account.get("login", ""))
        if name is None:
            return
        name = name.strip()
        if not name:
            account["login"] = ""
            passwords.forget_password(account["id"])
            self.save_settings()
            self.post("accounts", None)
            self.log("AUTH", f"{account['title']}: сохранённые данные входа удалены")
            return
        password = self.ask_text(f"Пароль для «{account['title']}»", secret=True)
        if password is None:
            return
        if not passwords.save_password(account["id"], password):
            self.show_error("Не удалось сохранить пароль в системном хранилище")
            return
        account["login"] = name
        self.save_settings()
        self.post("accounts", None)
        self.log("AUTH", f"{account['title']}: данные входа сохранены, пароль лежит в системном хранилище")

    def add_account(self) -> None:
        title = self.ask_text("Название аккаунта")
        if not title or not title.strip():
            return
        account = config.new_account(title.strip()[:40])
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
        shutil.rmtree(paths.profile_path(account["id"]).parent, ignore_errors=True)
        passwords.forget_password(account["id"])
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

    def copy_profile(self) -> None:
        self.root.clipboard_clear()
        self.root.clipboard_append(str(paths.profile_path(self.settings["current"])))
        self.post("status", "Путь профиля скопирован")

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
                        ctx = browser.launch_context(p, headless=True, profile=paths.profile_path(account["id"]))
                        name, logged = pulse.read_state(ctx.pages[0] if ctx.pages else ctx.new_page())
                        ctx.close()
                    except Exception as exc:
                        LOGGER.exception("Session check failure")
                        account["name"] = ""
                        self.log("AUTH", f"{account['title']}: проверка недоступна — " + self.short(exc))
                        continue
                    account["name"] = name if logged else ""
                    self.log("AUTH", f"{account['title']}: " + (f"сессия активна, {name}" if logged else "вход не выполнен"))
                for account in self.settings["accounts"]:
                    if self.needs_login(account):
                        self.relogin(p, account)
            self.save_settings()
            self.post("accounts", None)
            self.post("account", self.current_account()["name"])
            logged_in = [item for item in self.settings["accounts"] if item["name"]]
            self.post("status", f"Сессий активно: {len(logged_in)} из {len(self.settings['accounts'])}" if logged_in else "Войдите в Pulse")
        finally:
            self.browser_busy = False

    def needs_login(self, account: dict) -> bool:
        """Сессии нет, но есть сохранённые данные для входа"""
        return not account["name"] and bool(account.get("login")) and bool(passwords.load_password(account["id"]))

    def relogin(self, playwright, account: dict) -> bool:
        """Восстанавливает сессию без участия человека за компьютером

        Окно браузера не открывается: пароль берётся из хранилища, а код 2FA
        приходит из Telegram или из окна приложения.
        """
        if time.time() - self.login_attempts.get(account["id"], 0) < 600:
            return False
        self.login_attempts[account["id"]] = time.time()
        self.log("AUTH", f"{account['title']}: сессии нет, вхожу с сохранённым паролем")
        try:
            ctx = browser.launch_context(playwright, headless=True, profile=paths.profile_path(account["id"]))
        except PlaywrightError as exc:
            self.log("AUTH", f"{account['title']}: браузер не запустился — " + self.short(exc))
            return False
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(pulse.PULSE_HOME, wait_until="domcontentloaded", timeout=60000)
            self.autologin(page, account)
            skips = 0
            for _ in range(90):
                if self.stop.wait(1):
                    break
                if skips < 3 and login.skip_optional_step(
                        page, lambda message: self.log("AUTH", f"{account['title']}: {message}")):
                    skips += 1
                    self.stop.wait(3)
                    continue
                try:
                    state = page.evaluate(pulse.STATE_JS)
                except PlaywrightError:
                    continue
                name = str(state.get("name", "")).strip()
                if name and state.get("onPulse"):
                    account["name"] = name
                    self.save_settings()
                    self.post("accounts", None)
                    self.post("account", name)
                    self.log("AUTH", f"{account['title']}: вход выполнен автоматически, {name}")
                    return True
            self.log("AUTH", f"{account['title']}: автоматический вход не удался — {browser.page_summary(page)}")
            return False
        finally:
            ctx.close()

    def open_login(self) -> None:
        if self.browser_busy:
            self.post("status", "Уже выполняется проверка или вход…")
            return
        self.browser_busy = True
        self.post("status", f"Завершите вход и 2FA в окне Chrome: {self.current_account()['title']}")
        self.log("AUTH", f"{self.current_account()['title']}: открыто официальное окно Pulse для входа")
        threading.Thread(target=self.login_worker, daemon=True).start()

    def code_dialog(self, title: str, answer: queue.Queue) -> None:
        """Окно ввода кода, которое закрывается само, если код пришёл из Telegram"""
        window = tk.Toplevel(self.root)
        window.title(APP_NAME)
        window.configure(bg=self.BG, padx=18, pady=14)
        window.transient(self.root)
        self.code_window = window
        tk.Label(window, text=f"Код двухфакторной проверки для «{title}»", bg=self.BG, fg=self.TEXT,
                 font=(self.font, 10)).pack(anchor="w")
        tk.Label(window, text="Код можно прислать и боту в Telegram", bg=self.BG, fg=self.MUTED,
                 font=(self.font, 9)).pack(anchor="w", pady=(2, 8))
        field = ttk.Entry(window, width=24, font=(self.font, 11))
        field.pack(anchor="w")
        field.focus_set()
        row = tk.Frame(window, bg=self.BG)
        row.pack(anchor="w", pady=(10, 0))

        def accept(_event=None) -> None:
            if answer.empty():
                answer.put(field.get())

        field.bind("<Return>", accept)
        ttk.Button(row, text="Отправить", command=accept).pack(side="left")
        ttk.Button(row, text="Отмена", style="Secondary.TButton",
                   command=lambda: answer.empty() and answer.put("")).pack(side="left", padx=8)
        window.protocol("WM_DELETE_WINDOW", lambda: answer.empty() and answer.put(""))

    def close_code_dialog(self) -> None:
        window, self.code_window = self.code_window, None
        if window:
            try:
                window.destroy()
            except tk.TclError:
                pass

    def ask_code(self, title: str) -> str:
        """Ждёт код 2FA из окна приложения или из чата Telegram — что придёт первым"""
        answer: queue.Queue[str] = queue.Queue(maxsize=1)
        self.code_answer = answer
        self.bot.notify(f"Нужен код двухфакторной проверки для «{title}». Отправьте код сообщением.")
        self.root.after(0, lambda: self.code_dialog(title, answer))
        try:
            code = answer.get(timeout=300)
        except queue.Empty:
            code = ""
        self.code_answer = None
        self.root.after(0, self.close_code_dialog)
        return code.strip()

    def autologin(self, page, account: dict, attempts: int = 30, quiet: bool = False) -> None:
        """Вход сохранёнными данными аккаунта на открытой странице"""
        stored = passwords.load_password(account["id"]) if account.get("login") else ""
        if not stored:
            return
        login.autologin(
            page, account.get("login", ""), stored,
            log=lambda message: self.log("AUTH", f"{account['title']}: {message}"),
            ask_code=lambda: self.ask_code(account["title"]),
            wait=self.stop.wait, attempts=attempts, quiet=quiet)

    def login_worker(self) -> None:
        try:
            with sync_playwright() as p:
                account = self.current_account()
                ctx = browser.launch_context(p, headless=False, profile=paths.profile_path(account["id"]))
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(pulse.PULSE_HOME, wait_until="domcontentloaded", timeout=60000)
                self.autologin(page, account)
                for _ in range(600):
                    if self.stop.wait(1):
                        break
                    try:
                        state = page.evaluate(pulse.STATE_JS)
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
        link = self.lecture_link.get().strip()
        if link and not link.startswith(("http://", "https://")):
            messagebox.showerror(APP_NAME, "Ссылка должна начинаться с http:// или https://")
            return
        if link != self.settings.get("lecture_url"):
            self.settings["lecture_url"] = link
            self.save_settings()
        self.lecture = True
        self.browser_busy = True
        self.post("lecture", True)
        self.post("status", "Открываю СДО и Pulse…")
        threading.Thread(target=self.lecture_worker, daemon=True).start()

    def press_presence(self, button) -> bool:
        if browser.press(button):
            return True
        self.log("PRESENCE", "Не удалось нажать кнопку подтверждения")
        return False

    def approve_lecture(self, playwright, pulse, current: dict, token: str) -> tuple[bool, str]:
        """Подтверждает QR за открытый аккаунт и за остальные отмеченные профили"""
        approved, failure = 0, ""
        for account in self.enabled_accounts():
            if account["id"] == current["id"]:
                try:
                    ok, detail = pulse.approve_request(pulse, token)
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
                ctx = browser.launch_context(p, headless=False, profile=paths.profile_path(current["id"]))
                pulse = ctx.pages[0] if ctx.pages else ctx.new_page()
                pulse.goto(pulse.PULSE_HOME, wait_until="domcontentloaded", timeout=60000)
                lecture = ctx.new_page()
                lecture.goto(pulse.EDU_HOME, wait_until="domcontentloaded", timeout=60000)
                self.autologin(lecture, current, attempts=8, quiet=True)
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
                            button = browser.presence_button(page)
                            if button:
                                if self.press_presence(button):
                                    self.log("PRESENCE", "Присутствие в онлайн-мероприятии подтверждено")
                                    self.post("status", "Присутствие в онлайн-мероприятии подтверждено")
                                continue
                            if time.time() - self.last_success < int(self.settings["cooldown_minutes"]) * 60:
                                continue
                            token = browser.page_token(page)
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
        if screen.wayland_session():
            self.log("SCAN", "Сеанс Wayland: снимок экрана и автоклик работают только в сеансе X11")
        confirmed_tokens: set[str] = set()
        qr_successes = 0
        presence_pending = False
        while self.scanning and not self.stop.is_set():
            try:
                screenshot, origin_x, origin_y = screen.grab_screens()
                presence_button = screen.find_presence_button(screenshot)
                presence_activity = bool(presence_button)
                if presence_button:
                    clicked, reason = screen.click_screen_point(presence_button[0] + origin_x,
                                                                presence_button[1] + origin_y)
                    if not clicked:
                        self.log("PRESENCE", "Автоклик недоступен: " + reason)
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

    def approve_account(self, playwright, account: dict, token: str) -> tuple[bool, str]:
        """Подтверждает QR в отдельном профиле аккаунта"""
        try:
            ctx = browser.launch_context(playwright, headless=True, profile=paths.profile_path(account["id"]))
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(pulse.PULSE_HOME, wait_until="domcontentloaded", timeout=60000)
            answer = pulse.approve_request(page, token)
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
        folder = paths.profile_path(account["id"])
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
        self.bot.running = False
        self.stop.set()
        self.log("UI", "Приложение закрыто")
        self.root.after(100, self.root.destroy)

    def run(self) -> None:
        self.root.mainloop()

    def show_info(self, text: str) -> None:
        messagebox.showinfo(APP_NAME, text)

    def show_error(self, text: str) -> None:
        messagebox.showerror(APP_NAME, text)

    def ask_text(self, question: str, initial: str = "", secret: bool = False) -> str | None:
        return simpledialog.askstring(APP_NAME, question, initialvalue=initial,
                                      show="*" if secret else "", parent=self.root)

    def open_profile(self) -> None:
        """Открывает папку профиля активного аккаунта"""
        try:
            paths.open_folder(paths.profile_path(self.settings["current"]))
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.exception("Profile folder cannot be opened")
            self.log("UI", "Не удалось открыть папку профиля: " + self.short(exc))

"""Телеграм-бот: уведомления, команды и кнопки управления

Бот работает длинным опросом Bot API через urllib, без сторонних библиотек.
Экземпляр один на приложение: параллельный опрос одним токеном Telegram
отклоняет ошибкой 409.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.error
import urllib.request

from . import APP_NAME
from .passwords import TELEGRAM_KEY, forget_password, keyring_module, load_password, save_password
from .paths import LOGGER

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
NOTIFY_TAGS = ("PULSE", "PRESENCE", "AUTH", "LECTURE")


def telegram_call(token: str, method: str, payload: dict | None = None, timeout: int = 40) -> dict:
    """Вызов Bot API без сторонних библиотек"""
    data = json.dumps(payload or {}).encode("utf-8")
    request = urllib.request.Request(TELEGRAM_API.format(token=token, method=method), data=data,
                                     headers={"content-type": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class Bot:
    """Мост между приложением и чатом: экран с кнопками, команды и уведомления"""

    def __init__(self, app) -> None:
        self.app = app
        self.running = False
        self.thread: threading.Thread | None = None
        self.pair_code = ""
        self.link_waiting = False

    def notify(self, text: str) -> None:
        """Отправляет сообщение в привязанный чат, не задерживая работу приложения"""
        chat = self.app.settings.get("telegram_chat")
        if not chat or not self.app.settings.get("telegram_enabled"):
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

    def start(self) -> None:
        """Запускает бота в единственном экземпляре: два опроса одним токеном дают ошибку 409"""
        token = load_password(TELEGRAM_KEY)
        if not token:
            self.app.log("BOT", "Токен бота не сохранён")
            return
        threading.Thread(target=self.restart, args=(token,), daemon=True).start()

    def restart(self, token: str) -> None:
        previous = self.thread
        self.running = False
        if previous and previous.is_alive():
            previous.join(timeout=60)
        self.thread = threading.current_thread()
        self.running = True
        self.worker(token)

    def alive(self) -> bool:
        return self.running and self.thread is threading.current_thread() and not self.app.stop.is_set()

    def worker(self, token: str) -> None:
        """Слушает команды Telegram длинным опросом"""
        offset = 0
        conflict_at = 0.0
        try:
            # Вебхук и опрос вместе не работают, поэтому вебхук снимается
            telegram_call(token, "deleteWebhook", {"drop_pending_updates": False}, timeout=15)
            telegram_call(token, "setMyCommands", {"commands": [
                {"command": "menu", "description": "Кнопки управления"},
                {"command": "status", "description": "Что сейчас происходит"},
                {"command": "login", "description": "Проверить сессии и войти"},
                {"command": "scan", "description": "Искать QR на экранах"},
                {"command": "stop", "description": "Остановить поиск QR"},
                {"command": "lecture", "description": "Открыть занятие"},
                {"command": "close", "description": "Закрыть окно лекции"}]}, timeout=15)
        except (urllib.error.URLError, OSError, ValueError):
            LOGGER.info("Bot setup call failed")
        self.app.log("BOT", "Бот слушает команды")
        while self.alive():
            try:
                answer = telegram_call(token, "getUpdates", {"timeout": 25, "offset": offset,
                                                            "allowed_updates": ["message", "callback_query"]})
            except urllib.error.HTTPError as exc:
                if exc.code == 409:
                    if time.time() - conflict_at > 60:
                        conflict_at = time.time()
                        self.app.log("BOT", "Этот бот уже опрашивается другой программой, жду освобождения")
                    self.app.stop.wait(15)
                    continue
                if exc.code == 401:
                    self.app.log("BOT", "Telegram не принял токен, подключите бота заново")
                    break
                self.app.log("BOT", "Telegram ответил ошибкой: " + self.app.short(exc))
                self.app.stop.wait(10)
                continue
            except (urllib.error.URLError, OSError, ValueError) as exc:
                self.app.log("BOT", "Связь с Telegram потеряна: " + self.app.short(exc))
                self.app.stop.wait(10)
                continue
            for update in answer.get("result", []):
                offset = update.get("update_id", 0) + 1
                try:
                    self.handle_update(token, update)
                except Exception:
                    LOGGER.exception("Telegram update failed")
        if self.thread is threading.current_thread():
            self.running = False
            self.thread = None
            self.app.log("BOT", "Бот остановлен")

    def text(self) -> str:
        """Короткая сводка для экрана бота"""
        logged = [item["title"] for item in self.app.settings["accounts"] if item["name"]]
        enabled = [item["title"] for item in self.app.enabled_accounts()]
        link = self.app.settings.get("lecture_url") or "не задана"
        lines = [f"{APP_NAME}",
                 "",
                 f"Вошли: {', '.join(logged) if logged else 'никто'}",
                 f"Подтверждают QR: {', '.join(enabled) if enabled else 'никто'}",
                 f"Поиск QR на экранах: {'идёт' if self.app.scanning else 'остановлен'}",
                 f"Окно лекции: {'открыто' if self.app.lecture else 'закрыто'}",
                 f"Ссылка занятия: {link}"]
        return "\n".join(lines)

    def keyboard(self) -> dict:
        scan = "Остановить поиск QR" if self.app.scanning else "Искать QR на экранах"
        lecture = "Закрыть лекцию" if self.app.lecture else "Открыть лекцию"
        return {"inline_keyboard": [
            [{"text": lecture, "callback_data": "lecture"}],
            [{"text": scan, "callback_data": "scan"}],
            [{"text": "Ссылка занятия", "callback_data": "link"},
             {"text": "Аккаунты", "callback_data": "accounts"}],
            [{"text": "Войти заново", "callback_data": "login"},
             {"text": "Обновить", "callback_data": "refresh"}],
        ]}

    def accounts_text(self) -> str:
        return (f"{APP_NAME}\n\nОтмеченные аккаунты подтверждают найденный QR.\n"
                "Нажмите на аккаунт, чтобы включить или выключить его.")

    def accounts_keyboard(self) -> dict:
        rows = []
        for account in self.app.settings["accounts"]:
            mark = "✅" if account.get("enabled") else "⬜"
            state = account["name"] or ("автовход" if account.get("login") else "нет входа")
            rows.append([{"text": f"{mark} {account['title']} · {state}", "callback_data": "toggle:" + account["id"]}])
        rows.append([{"text": "Назад", "callback_data": "back"}])
        return {"inline_keyboard": rows}

    def send(self, token: str, method: str, payload: dict) -> None:
        """Вызов Bot API, который не должен ронять обработку обновления

        Telegram отвечает 400, если текст и кнопки экрана не изменились,
        и это нормальная ситуация: нажали «Обновить», а менять нечего.
        """
        try:
            telegram_call(token, method, payload)
        except urllib.error.HTTPError as exc:
            if exc.code != 400:
                LOGGER.info("Telegram %s failed: %s", method, exc)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            LOGGER.info("Telegram %s failed: %s", method, exc)

    def menu(self, token: str, chat: int) -> None:
        self.send(token, "sendMessage", {"chat_id": chat, "text": self.text(),
                                                  "reply_markup": self.keyboard()})

    def action(self, data: str) -> str:
        """Выполняет нажатие кнопки и возвращает короткий ответ"""
        if data == "refresh":
            return "Обновлено"
        if data == "link":
            self.link_waiting = True
            return "Пришлите ссылку сообщением"
        if data == "login":
            return self.command("/login")
        if data == "scan":
            return self.command("/stop" if self.app.scanning else "/scan")
        if data == "lecture":
            return self.command("/close" if self.app.lecture else "/lecture")
        if data.startswith("toggle:"):
            for account in self.app.settings["accounts"]:
                if account["id"] == data.split(":", 1)[1]:
                    account["enabled"] = not account.get("enabled")
                    self.app.save_settings()
                    self.app.post("accounts", None)
                    return f"{account['title']}: {'подтверждает QR' if account['enabled'] else 'не участвует'}"
        return ""

    def handle_callback(self, token: str, callback: dict) -> None:
        """Обрабатывает нажатие кнопки под сообщением"""
        message = callback.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        if chat != self.app.settings.get("telegram_chat"):
            LOGGER.info("Callback from unknown chat %s", chat)
            return
        data = callback.get("data", "")
        LOGGER.info("Callback %s from chat %s", data, chat)
        answer = self.action(data)
        self.send(token, "answerCallbackQuery",
                           {"callback_query_id": callback.get("id"), "text": answer[:190]})
        # Действие выполняется в потоке интерфейса, поэтому экран рисуется после него
        self.app.stop.wait(0.7)
        accounts_screen = data == "accounts" or data.startswith("toggle:")
        self.send(token, "editMessageText", {
            "chat_id": chat, "message_id": message.get("message_id"),
            "text": self.accounts_text() if accounts_screen else self.text(),
            "reply_markup": self.accounts_keyboard() if accounts_screen else self.keyboard()})

    def handle_update(self, token: str, update: dict) -> None:
        """Привязывает чат по коду и отвечает только привязанному чату"""
        LOGGER.info("Telegram update: %s", ", ".join(key for key in update if key != "update_id"))
        if update.get("callback_query"):
            self.handle_callback(token, update["callback_query"])
            return
        message = update.get("message") or {}
        chat = (message.get("chat") or {}).get("id")
        text = (message.get("text") or "").strip()
        if not chat or not text:
            return
        known = self.app.settings.get("telegram_chat")
        if not known:
            if self.pair_code and text.startswith("/start") and self.pair_code in text:
                self.app.settings["telegram_chat"] = chat
                self.app.settings["telegram_enabled"] = True
                self.pair_code = ""
                self.app.save_settings()
                self.app.post("telegram", None)
                self.app.log("BOT", "Чат привязан, команды доступны")
                self.menu(token, chat)
            return
        if chat != known:
            return
        if text.startswith("/start") or text.startswith("/menu"):
            self.menu(token, chat)
            return
        self.send(token, "sendMessage", {"chat_id": chat, "text": self.command(text)})

    def command(self, text: str) -> str:
        """Выполняет команду чата и возвращает ответ"""
        waiting = self.app.code_answer
        if waiting is not None and not text.startswith("/"):
            if waiting.empty():
                waiting.put(text)
            return "Код принят"
        if self.link_waiting and not text.startswith("/"):
            self.link_waiting = False
            if not text.startswith(("http://", "https://")):
                return "Ссылка должна начинаться с http:// или https://"
            self.app.settings["lecture_url"] = text
            self.app.save_settings()
            self.app.root.after(0, lambda: self.app.lecture_link.set(text))
            return "Ссылка сохранена. Нажмите «Открыть лекцию»"
        command = text.split()[0].lower().split("@")[0]
        if command == "/status":
            logged = [item["title"] for item in self.app.settings["accounts"] if item["name"]]
            lines = [f"Аккаунтов: {len(self.app.settings['accounts'])}, вошли: {len(logged) or 0}",
                     "Подтверждают QR: " + ", ".join(item["title"] for item in self.app.enabled_accounts()),
                     "Поиск QR на экранах: " + ("идёт" if self.app.scanning else "остановлен"),
                     "Режим лекции: " + ("открыт" if self.app.lecture else "закрыт"),
                     "Ссылка занятия: " + (self.app.settings.get("lecture_url") or "не задана")]
            return "\n".join(lines)
        if command == "/login":
            pending = [item["title"] for item in self.app.settings["accounts"] if self.app.needs_login(item)]
            if self.app.browser_busy:
                return "Браузер сейчас занят, попробуйте через минуту"
            self.app.root.after(0, self.app.check_session)
            if not pending:
                return "Проверяю сессии. Аккаунтов с сохранённым паролем и без входа нет"
            self.app.login_attempts.clear()
            return "Вхожу в аккаунты: " + ", ".join(pending)
        if command == "/scan":
            if self.app.scanning:
                return "Поиск QR уже идёт"
            self.app.root.after(0, self.app.toggle_scan)
            return "Запускаю поиск QR на экранах"
        if command == "/stop":
            if not self.app.scanning:
                return "Поиск QR не запущен"
            self.app.root.after(0, self.app.toggle_scan)
            return "Останавливаю поиск QR"
        if command == "/lecture":
            parts = text.split(maxsplit=1)
            if len(parts) > 1:
                link = parts[1].strip()
                if not link.startswith(("http://", "https://")):
                    return "Ссылка должна начинаться с http:// или https://"
                self.app.settings["lecture_url"] = link
                self.app.save_settings()
                self.app.root.after(0, lambda: self.app.lecture_link.set(link))
                if self.app.lecture:
                    return "Ссылка сохранена. Закройте окно командой /close и откройте заново"
            if self.app.lecture:
                return "Окно лекции уже открыто"
            self.app.root.after(0, self.app.toggle_lecture)
            link = self.app.settings.get("lecture_url", "")
            return "Открываю занятие: " + link if link else "Открываю СДО и Pulse"
        if command == "/close":
            if not self.app.lecture:
                return "Окно лекции не открыто"
            self.app.root.after(0, self.app.toggle_lecture)
            return "Закрываю окно лекции"
        return ("Кнопки управления: /menu\n\nКоманды:\n/status — что сейчас происходит\n"
                "/login — проверить сессии и войти\n/scan — начать поиск QR\n"
                "/stop — остановить поиск\n/lecture — открыть занятие\n"
                "/lecture <ссылка> — запомнить ссылку и открыть её\n/close — закрыть окно лекции")

    def connect(self) -> None:
        """Сохраняет токен бота и ждёт команду привязки чата"""
        if not keyring_module():
            self.app.show_info("Системное хранилище паролей недоступно, токен бота сохранить негде")
            return
        token = self.app.ask_text("Токен бота от @BotFather")
        if token is None:
            return
        token = token.strip()
        if not token:
            return
        if not save_password(TELEGRAM_KEY, token):
            self.app.show_error("Не удалось сохранить токен в системном хранилище")
            return
        self.app.settings["telegram_chat"] = 0
        self.app.settings["telegram_enabled"] = True
        self.app.save_settings()
        self.pair_code = f"{secrets.randbelow(1000000):06d}"
        self.start()
        self.app.post("telegram", None)
        self.app.log("BOT", "Отправьте боту команду /start " + self.pair_code)
        threading.Thread(target=self.check_token, args=(token,), daemon=True).start()
        self.app.show_info("Откройте своего бота в Telegram и отправьте ему:\n\n/start " + self.pair_code)

    def check_token(self, token: str) -> None:
        """Показывает имя бота, чтобы сразу было видно, принят ли токен"""
        try:
            answer = telegram_call(token, "getMe", timeout=15)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            self.app.log("BOT", "Проверить токен не удалось: " + self.app.short(exc))
            return
        username = (answer.get("result") or {}).get("username", "")
        if username:
            self.app.log("BOT", f"Бот @{username} на связи, отправьте ему /start {self.pair_code}")
        else:
            self.app.log("BOT", "Telegram не принял токен")

    def disconnect(self) -> None:
        self.running = False
        self.pair_code = ""
        self.app.settings["telegram_chat"] = 0
        self.app.settings["telegram_enabled"] = False
        self.app.save_settings()
        forget_password(TELEGRAM_KEY)
        self.app.post("telegram", None)
        self.app.log("BOT", "Телеграм отключён")

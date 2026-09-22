"""Телеграм-бот: привязка чата, команды, кнопки"""
import queue
import threading
import urllib.error

import pytest

from shalost import telegram


class FakeRoot:
    def after(self, delay, func=None):
        if func:
            func()


class FakeVar:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class FakeApp:
    """Приложение, каким его видит бот"""

    def __init__(self):
        self.root = FakeRoot()
        self.stop = threading.Event()
        self.scanning = self.lecture = self.browser_busy = False
        self.code_answer: queue.Queue | None = None
        self.login_attempts: dict[str, float] = {}
        self.lecture_link = FakeVar()
        self.messages: list[str] = []
        self.settings = {"telegram_chat": 555, "telegram_enabled": True, "lecture_url": "", "accounts": [
            {"id": "a1", "title": "лк", "enabled": True, "name": "Капустин Л. А.", "login": "s1"},
            {"id": "a2", "title": "второй", "enabled": False, "name": "", "login": ""}]}

    def log(self, tag, message):
        self.messages.append(f"{tag}: {message}")

    def post(self, kind, value):
        pass

    def save_settings(self):
        pass

    def short(self, exc):
        return str(exc)

    def enabled_accounts(self):
        return [item for item in self.settings["accounts"] if item["enabled"]]

    def needs_login(self, account):
        return not account["name"] and bool(account.get("login"))

    def check_session(self):
        self.messages.append("AUTH: проверка сессий")

    def toggle_scan(self):
        self.scanning = not self.scanning

    def toggle_lecture(self):
        self.lecture = not self.lecture


@pytest.fixture
def bot(monkeypatch):
    sent = []
    monkeypatch.setattr(telegram, "telegram_call",
                        lambda token, method, payload=None, timeout=40: sent.append((method, payload)) or {"ok": True, "result": []})
    instance = telegram.Bot(FakeApp())
    instance.sent = sent
    return instance


def press(bot, data, chat=555):
    bot.handle_update("token", {"callback_query": {"id": "1", "data": data,
                                                   "message": {"message_id": 7, "chat": {"id": chat}}}})


def write(bot, text, chat=555):
    bot.handle_update("token", {"message": {"chat": {"id": chat}, "text": text}})


def buttons(payload):
    return [button["text"] for row in payload["reply_markup"]["inline_keyboard"] for button in row]


def test_menu_shows_buttons(bot):
    write(bot, "/menu")
    method, payload = bot.sent[-1]
    assert method == "sendMessage"
    assert "Открыть лекцию" in buttons(payload)


def test_scan_button_starts_search_and_renames_itself(bot):
    press(bot, "scan")
    assert bot.app.scanning is True
    assert "Остановить поиск QR" in buttons(bot.sent[-1][1])


def test_accounts_screen_marks_participants(bot):
    press(bot, "accounts")
    rows = buttons(bot.sent[-1][1])
    assert any(row.startswith("✅ лк") for row in rows)
    assert any(row.startswith("⬜ второй") for row in rows)


def test_account_button_toggles_participation(bot):
    press(bot, "toggle:a2")
    assert bot.app.settings["accounts"][1]["enabled"] is True


def test_foreign_chat_is_ignored(bot):
    before = len(bot.sent)
    press(bot, "scan", chat=777)
    write(bot, "/scan", chat=777)
    assert len(bot.sent) == before
    assert bot.app.scanning is False


def test_link_button_waits_for_address(bot):
    press(bot, "link")
    assert bot.command("https://my.mts-link.ru/j/12345") == "Ссылка сохранена. Нажмите «Открыть лекцию»"
    assert bot.app.settings["lecture_url"].endswith("12345")
    assert bot.app.lecture_link.value.endswith("12345")


def test_link_button_rejects_junk(bot):
    press(bot, "link")
    assert "http" in bot.command("просто текст")
    assert bot.app.settings["lecture_url"] == ""


def test_code_message_goes_to_login_form(bot):
    answer: queue.Queue = queue.Queue(maxsize=1)
    bot.app.code_answer = answer
    assert bot.command("123456") == "Код принят"
    assert answer.get_nowait() == "123456"


def test_command_stays_command_while_code_expected(bot):
    bot.app.code_answer = queue.Queue(maxsize=1)
    assert "Аккаунтов" in bot.command("/status")


def test_pairing_requires_code(bot):
    bot.app.settings["telegram_chat"] = 0
    bot.pair_code = "424242"
    write(bot, "/start", chat=999)
    assert bot.app.settings["telegram_chat"] == 0
    write(bot, "/start 424242", chat=999)
    assert bot.app.settings["telegram_chat"] == 999


def test_unchanged_screen_answer_is_not_an_error(bot, monkeypatch):
    def refuse(token, method, payload=None, timeout=40):
        bot.sent.append((method, payload))
        if method == "editMessageText":
            raise urllib.error.HTTPError("url", 400, "message is not modified", {}, None)
        return {"ok": True, "result": []}

    monkeypatch.setattr(telegram, "telegram_call", refuse)
    press(bot, "refresh")  # не должно поднять исключение


def test_status_lists_state(bot):
    answer = bot.command("/status")
    assert "лк" in answer and "Поиск QR" in answer

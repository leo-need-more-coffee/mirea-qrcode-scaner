"""Работа с браузером: запуск профиля и поиск элементов на страницах

Приложение не знает вёрстку Pulse, СДО и MTS Link, поэтому ищет элементы
по назначению: поле пароля, поле одноразового кода, кнопку входа и так далее.
Так вход и подтверждение присутствия переживают изменения вёрстки сайтов.
"""
from __future__ import annotations

import io
import re
import shutil
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from .paths import LOGGER
from .pulse import qr_token

PRESENCE_TEXT = re.compile(r"подтверждаю", re.IGNORECASE)
ENTRY_TEXT = re.compile(r"войти|вход|sign in|log in", re.IGNORECASE)
SKIP_TEXT = re.compile(r"пропустить|позже|не сейчас|skip|later", re.IGNORECASE)


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


def login_fields(page):
    """Видимые поля логина и пароля; SSO может показывать их на разных шагах"""
    login_field = password_field = None
    for frame in page.frames:
        try:
            if password_field is None:
                password = frame.locator("input[type=password]:visible, input[name*='pass' i]:visible")
                if password.count():
                    password_field = password.first
            if login_field is None:
                login = frame.locator("input[type=text]:visible, input[type=email]:visible, "
                                      "input[type=tel]:visible, input[name*='login' i]:visible, "
                                      "input[name*='user' i]:visible")
                if login.count():
                    login_field = login.first
        except PlaywrightError:
            continue
        if password_field is not None and login_field is not None:
            break
    return login_field, password_field


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


def entry_button(page):
    """Кнопка или ссылка входа на странице, где формы ещё нет"""
    for frame in page.frames:
        for locator in (frame.get_by_role("button", name=ENTRY_TEXT),
                        frame.get_by_role("link", name=ENTRY_TEXT),
                        frame.get_by_text(ENTRY_TEXT)):
            try:
                if locator.count() and locator.first.is_visible():
                    return locator.first
            except PlaywrightError:
                continue
    return None


def skip_button(page):
    """Кнопка пропуска необязательного шага входа, например настройки аккаунта в Keycloak"""
    for frame in page.frames:
        try:
            named = frame.locator("input[name=skip]:visible, button[name=skip]:visible")
            if named.count():
                return named.first
            for locator in (frame.get_by_role("button", name=SKIP_TEXT), frame.get_by_role("link", name=SKIP_TEXT)):
                if locator.count() and locator.first.is_visible():
                    return locator.first
        except PlaywrightError:
            continue
    return None


def page_summary(page) -> str:
    """Куда попала страница, какие поля и кнопки на ней видны — без значений"""
    try:
        summary = page.evaluate(r"""() => {
            const visible = item => item.offsetParent !== null;
            const fields = [...document.querySelectorAll('input')].filter(visible)
                .map(item => (item.type || 'text') + (item.name ? ':' + item.name : '')).slice(0, 8);
            const buttons = [...document.querySelectorAll('button, a, [role=button]')].filter(visible)
                .map(item => (item.innerText || '').trim().replace(/\s+/g, ' ').slice(0, 24))
                .filter(Boolean).slice(0, 8);
            return {fields, buttons};
        }""")
    except PlaywrightError:
        summary = {}
    fields = ", ".join(summary.get("fields") or []) or "нет"
    buttons = ", ".join(summary.get("buttons") or []) or "нет"
    return f"{page.url} · поля: {fields} · кнопки: {buttons}"


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


def press(element) -> bool:
    """Нажимает элемент

    В неактивной вкладке Chrome не доставляет настоящие события мыши,
    поэтому запасной вариант — событие click из самой страницы.
    """
    try:
        element.click(timeout=3000)
        return True
    except PlaywrightError:
        LOGGER.info("Click timed out, falling back to DOM event")
    try:
        element.dispatch_event("click")
        return True
    except PlaywrightError as exc:
        LOGGER.info("DOM click failed: %s", exc)
        return False


def page_token(page) -> str | None:
    """Ищет QR-код Pulse на снимке вкладки"""
    import zxingcpp
    from PIL import Image

    with Image.open(io.BytesIO(page.screenshot(timeout=10000))) as image:
        for code in zxingcpp.read_barcodes(image.convert("RGB")):
            token = qr_token(code.text)
            if token:
                return token
    return None

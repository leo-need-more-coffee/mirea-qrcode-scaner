"""Вход в Pulse и СДО сохранёнными данными

Оба сервиса живут за одним SSO, поэтому сценарий один: дойти до формы,
заполнить её — логин и пароль могут спрашивать на разных экранах — и,
если сайт просит одноразовый код, получить его от пользователя.
"""
from __future__ import annotations

from typing import Callable

from playwright.sync_api import Error as PlaywrightError

from . import browser

Log = Callable[[str], None]
AskCode = Callable[[], str]
Wait = Callable[[float], bool]


def short(exc: BaseException) -> str:
    return str(exc).replace("\n", " ").strip()[:180] or exc.__class__.__name__


def autologin(page, login: str, password: str, *, log: Log, ask_code: AskCode, wait: Wait,
              attempts: int = 30, quiet: bool = False) -> bool:
    """Проходит вход на странице; возвращает True, если данные были отправлены

    `wait` повторяет поведение threading.Event.wait: возвращает True, когда пора
    прекратить работу. `quiet` нужен вкладке с готовой сессией, где формы не будет.
    """
    if not login or not password:
        return False
    login_field = password_field = None
    login_sent = False
    entry_clicks = 0
    for _ in range(attempts):
        login_field, password_field = browser.login_fields(page)
        if password_field:
            break
        if login_field is None and entry_clicks < 2:
            entry = browser.entry_button(page)
            if entry is not None and browser.press(entry):
                entry_clicks += 1
                log("открываю страницу входа")
                wait(3)
                continue
        if login_field and not login_sent:
            try:
                login_field.fill(login)
                login_field.press("Enter")
                login_sent = True
                log("логин отправлен, жду поле пароля")
            except PlaywrightError:
                pass
        if wait(1):
            return False
    if not password_field:
        if not quiet:
            log(f"форма входа не найдена — {browser.page_summary(page)}")
        return False
    try:
        if login_field and not login_sent:
            login_field.fill(login)
        password_field.fill(password)
        password_field.press("Enter")
        log("данные входа отправлены")
    except PlaywrightError as exc:
        log("не удалось заполнить форму — " + short(exc))
        return False
    wait(4)
    if browser.login_fields(page)[1] is not None and not quiet:
        log(f"сайт не принял данные входа — {browser.page_summary(page)}")
    submit_code(page, log=log, ask_code=ask_code, wait=wait)
    return True


def submit_code(page, *, log: Log, ask_code: AskCode, wait: Wait, attempts: int = 20) -> bool:
    """Ждёт поле одноразового кода и отправляет полученный код"""
    for _ in range(attempts):
        if wait(1):
            return False
        field = browser.code_field(page)
        if not field:
            continue
        code = ask_code()
        if not code:
            log("код не введён, завершите вход в окне браузера")
            return False
        try:
            field.fill(code)
            field.press("Enter")
            log("код двухфакторной проверки отправлен")
            return True
        except PlaywrightError as exc:
            log("не удалось отправить код — " + short(exc))
            return False
    return False


def skip_optional_step(page, log: Log) -> bool:
    """Пропускает необязательный шаг после входа, например настройку аккаунта в Keycloak"""
    skip = browser.skip_button(page)
    if skip is not None and browser.press(skip):
        log("пропускаю необязательный шаг входа")
        return True
    return False

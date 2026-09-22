"""Вход сохранёнными данными на страницах, похожих на SSO МИРЭА"""
import pytest

from shalost import login

PORTAL = """<!doctype html><meta charset=utf-8><title>сервисы</title>
<label><input type=radio name=":rr:">Студент</label>
<button onclick="location.href='/step1'">Войти</button>"""
STEP1 = """<!doctype html><meta charset=utf-8><title>логин</title>
<form method=post action=/step1><input type=text name=username><button>далее</button></form>"""
STEP2 = """<!doctype html><meta charset=utf-8><title>пароль</title>
<form method=post action=/step2><input type=password name=password><button>войти</button></form>"""
CODE = """<!doctype html><meta charset=utf-8><title>код</title>
<form method=post action=/code><input autocomplete=one-time-code name=otp><button>дальше</button></form>"""
DONE = """<!doctype html><meta charset=utf-8><title>готово</title><h1>сессия активна</h1>"""
WRONG = """<!doctype html><meta charset=utf-8><title>ошибка</title>
<form method=post action=/step2><input type=password name=password><b>неверный пароль</b></form>"""


def routes(path, server):
    if path == "/step1":
        return "/step2"
    if path == "/step2":
        return "/code" if server.posted["/step2"].get("password") == "s3cret" else "/wrong"
    return "/done"


PAGES = {"/": PORTAL, "/step1": STEP1, "/step2": STEP2, "/code": CODE, "/done": DONE, "/wrong": WRONG}


@pytest.fixture
def messages():
    return []


def logger(messages):
    return lambda text: messages.append(text)


def test_two_step_form_with_code(page, site, messages, quick_wait):
    server = site(PAGES, routes)
    page.goto(server.url + "/step1")
    assert login.autologin(page, "student01", "s3cret", log=logger(messages),
                           ask_code=lambda: "1234", wait=quick_wait)
    page.wait_for_load_state("networkidle")
    assert server.posted["/step1"]["username"] == "student01"
    assert server.posted["/step2"]["password"] == "s3cret"
    assert server.posted["/code"]["otp"] == "1234"
    assert page.url.endswith("/done")
    assert "код двухфакторной проверки отправлен" in messages


def test_entry_button_opens_form(page, site, messages, quick_wait):
    server = site(PAGES, routes)
    page.goto(server.url + "/")
    assert login.autologin(page, "student01", "s3cret", log=logger(messages),
                           ask_code=lambda: "1234", wait=quick_wait)
    assert "открываю страницу входа" in messages
    assert server.posted["/step2"]["password"] == "s3cret"


def test_wrong_password_is_reported(page, site, messages, quick_wait):
    server = site(PAGES, routes)
    page.goto(server.url + "/step1")
    login.autologin(page, "student01", "мимо", log=logger(messages), ask_code=lambda: "", wait=quick_wait)
    assert any("не принял данные входа" in text for text in messages)


def test_page_without_form_stays_quiet(page, site, messages, quick_wait):
    server = site({"/": DONE})
    page.goto(server.url + "/")
    assert login.autologin(page, "student01", "s3cret", log=logger(messages), ask_code=lambda: "",
                           wait=quick_wait, attempts=2, quiet=True) is False
    assert messages == []


def test_page_without_form_reports_where_it_stopped(page, site, messages, quick_wait):
    server = site({"/": DONE})
    page.goto(server.url + "/")
    login.autologin(page, "student01", "s3cret", log=logger(messages), ask_code=lambda: "",
                    wait=quick_wait, attempts=2)
    assert any("форма входа не найдена" in text for text in messages)


def test_empty_credentials_do_nothing(page, site, messages, quick_wait):
    server = site(PAGES, routes)
    page.goto(server.url + "/step1")
    assert login.autologin(page, "", "", log=logger(messages), ask_code=lambda: "", wait=quick_wait) is False
    assert server.posted == {}


def test_optional_step_is_skipped(page, site, messages, quick_wait):
    server = site({"/": """<!doctype html><meta charset=utf-8><form method=post action=/skip>
        <input type=submit name=retry value="Повторить"><input type=submit name=skip value="Пропустить"></form>"""})
    page.goto(server.url + "/")
    assert login.skip_optional_step(page, logger(messages)) is True
    assert "пропускаю необязательный шаг входа" in messages

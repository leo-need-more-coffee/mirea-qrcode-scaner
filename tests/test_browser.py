"""Поиск элементов и QR прямо во вкладке браузера"""
from shalost import browser, pulse

TOKEN = "3f2b6c1e-8a4d-4b7e-9c1f-2d5a7e8b4c60"

LECTURE = """<!doctype html><meta charset=utf-8><title>лекция</title>
<body style="margin:0;background:#101317">
<img src="/pulse-qr.png" width="320" alt="QR Pulse">
<iframe src="/presence-demo.html" style="width:1100px;height:700px;border:0"></iframe>"""


def test_qr_is_read_from_tab(page, site):
    server = site({"/": LECTURE})
    page.goto(server.url + "/")
    page.wait_for_selector("img")
    assert browser.page_token(page) == TOKEN


def test_presence_button_is_found_inside_frame(page, site):
    server = site({"/": LECTURE})
    page.goto(server.url + "/")
    page.wait_for_selector("iframe")
    frame = page.frame_locator("iframe")
    frame.locator("#presenceDialog.visible").wait_for(timeout=30000)
    button = browser.presence_button(page)
    assert button is not None
    assert browser.press(button)
    page.wait_for_timeout(500)
    assert page.frames[1].inner_text("#notice") == "Присутствие подтверждено"


def test_press_works_in_background_tab(page, site, chromium):
    """Chrome не доставляет настоящие события мыши в неактивную вкладку"""
    server = site({"/": """<!doctype html><meta charset=utf-8>
        <button id=go onclick="document.title='нажато'">ПОДТВЕРЖДАЮ</button>"""})
    page.goto(server.url + "/")
    other = page.context.new_page()
    other.goto("about:blank")
    other.bring_to_front()
    assert browser.press(browser.presence_button(page))
    page.wait_for_timeout(300)
    assert page.title() == "нажато"
    other.close()


def test_entry_and_skip_buttons_are_recognised(page, site):
    server = site({"/": """<!doctype html><meta charset=utf-8>
        <a href="/login">Войти</a><input type=submit name=skip value="Пропустить">"""})
    page.goto(server.url + "/")
    assert browser.entry_button(page) is not None
    assert browser.skip_button(page) is not None


def test_page_summary_lists_fields_and_buttons(page, site):
    server = site({"/": """<!doctype html><meta charset=utf-8>
        <input type=text name=username><input type=password name=password><button>Войти</button>"""})
    page.goto(server.url + "/")
    summary = browser.page_summary(page)
    assert "text:username" in summary and "password:password" in summary
    assert "Войти" in summary


def test_approve_request_reads_pulse_answer(page, site, monkeypatch):
    import struct

    def frame(payload: bytes, flags: int) -> bytes:
        return bytes([flags]) + struct.pack(">I", len(payload)) + payload

    answers = {"/ok": frame(b"\x00", 0) + frame(b"grpc-status:0\r\n", 0x80),
               "/fail": frame(b"grpc-status:7\r\ngrpc-message:QR+already+used\r\n", 0x80)}
    server = site({"/": "<title>pulse</title>"})

    # ответ gRPC-Web отдаётся тем же сервером
    server.pages["/ok"] = answers["/ok"]
    server.pages["/fail"] = answers["/fail"]
    page.goto(server.url + "/")

    monkeypatch.setattr(pulse, "PULSE_RPC", server.url + "/ok")
    assert pulse.approve_request(page, TOKEN) == (True, "")
    monkeypatch.setattr(pulse, "PULSE_RPC", server.url + "/fail")
    assert pulse.approve_request(page, TOKEN) == (False, "QR already used")

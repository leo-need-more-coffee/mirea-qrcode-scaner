"""Общие приспособления тестов: браузер и локальный сайт-заглушка"""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from shalost import browser as browser_module

DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="session")
def chromium():
    """Браузер для интеграционных тестов: системный или встроенный в Playwright"""
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as driver:
        executable = browser_module.system_browser()
        options = {"executable_path": executable} if executable else {}
        try:
            instance = driver.chromium.launch(headless=True, **options)
        except Exception as exc:  # браузера в системе может не быть
            pytest.skip(f"браузер недоступен: {exc}")
        yield instance
        instance.close()


@pytest.fixture
def page(chromium):
    context = chromium.new_context(viewport={"width": 1280, "height": 900})
    item = context.new_page()
    yield item
    context.close()


class Site:
    """Маленький сайт для проверки сценариев: отдаёт страницы и копит запросы"""

    def __init__(self, pages: dict, post_handler=None) -> None:
        self.pages = pages
        self.posted: dict[str, dict] = {}
        self.post_handler = post_handler
        site = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self, body: bytes, code: int = 200, location: str | None = None,
                      kind: str = "text/html; charset=utf-8") -> None:
                self.send_response(code)
                self.send_header("content-type", kind)
                if location:
                    self.send_header("location", location)
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                path = self.path.split("?")[0]
                page = site.pages.get(path)
                if page is None:
                    file = DATA / path.lstrip("/")
                    if not file.exists():
                        self.reply(b"not found", 404)
                        return
                    kind = "image/png" if file.suffix == ".png" else "text/html; charset=utf-8"
                    self.reply(file.read_bytes(), kind=kind)
                    return
                body = page(self) if callable(page) else page
                self.reply(body if isinstance(body, bytes) else body.encode("utf-8"))

            def do_POST(self) -> None:
                from urllib.parse import parse_qs

                size = int(self.headers.get("content-length", 0))
                raw = self.rfile.read(size)
                site.posted[self.path] = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
                answer = site.pages.get(self.path)
                if isinstance(answer, bytes):
                    # готовый ответ, например кадры gRPC-Web
                    self.reply(answer, kind="application/grpc-web+proto")
                    return
                location = site.post_handler(self.path, site) if site.post_handler else "/"
                self.reply(b"", 303, location)

            def log_message(self, *args) -> None:
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def site():
    created: list[Site] = []

    def build(pages: dict, post_handler=None) -> Site:
        item = Site(pages, post_handler)
        created.append(item)
        return item

    yield build
    for item in created:
        item.close()


@pytest.fixture
def quick_wait():
    """Ожидание без задержек: тесты не должны спать по-настоящему"""
    return lambda seconds: False

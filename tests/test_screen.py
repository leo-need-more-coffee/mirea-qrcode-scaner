"""Поиск кнопки подтверждения присутствия на снимке экрана"""
from PIL import Image, ImageDraw

from shalost import screen


def make_screen(button: tuple[int, int, int, int] | None, dialog: bool = True) -> Image.Image:
    """Рисует тёмный экран с белым окном и фиолетовой кнопкой внутри"""
    image = Image.new("RGB", (1280, 800), (18, 21, 28))
    draw = ImageDraw.Draw(image)
    if dialog:
        draw.rectangle((420, 260, 860, 520), fill=(255, 255, 255))
    if button:
        draw.rectangle(button, fill=(169, 0, 255))
    return image


def test_button_inside_dialog_is_found():
    left, top, right, bottom = 600, 430, 740, 464
    point = screen.find_presence_button(make_screen((left, top, right, bottom)))
    assert point is not None
    assert left <= point[0] <= right
    assert top - 10 <= point[1] <= bottom + 20


def test_no_button_no_answer():
    assert screen.find_presence_button(make_screen(None)) is None


def test_purple_without_white_dialog_is_ignored():
    """Фиолетовая плашка на тёмной странице — не кнопка присутствия"""
    assert screen.find_presence_button(make_screen((600, 430, 740, 464), dialog=False)) is None


def test_narrow_purple_strip_is_ignored():
    """Узкие элементы вроде бейджей не считаются кнопкой"""
    assert screen.find_presence_button(make_screen((600, 430, 640, 464))) is None


def test_wayland_session_detected(monkeypatch):
    monkeypatch.setattr(screen.sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    assert screen.wayland_session() is True
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert screen.wayland_session() is False

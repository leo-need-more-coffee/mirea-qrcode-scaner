"""Снимок экранов и клик по точке рабочего стола

Pillow умеет снимать все экраны только в Windows, поэтому в Linux работает mss.
Клик в Linux идёт через XTEST, а без него — через xdotool; в сеансе Wayland
ни то, ни другое недоступно.
"""
from __future__ import annotations

import os
import subprocess
import sys

from .paths import LOGGER

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


def click_screen_point(x: int, y: int) -> tuple[bool, str]:
    """Клик по точке виртуального рабочего стола; возвращает успех и причину отказа"""
    if sys.platform == "win32":
        import ctypes

        user32 = ctypes.windll.user32
        user32.SetCursorPos(x, y)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # Нажатие левой кнопки
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # Отпускание левой кнопки
        return True, ""
    if sys.platform == "linux":
        if click_x11(x, y) or click_xdotool(x, y):
            return True, ""
        return False, "нужен сеанс X11 или XWayland либо пакет xdotool"
    return False, "автоклик поддерживается только в Windows и Linux"

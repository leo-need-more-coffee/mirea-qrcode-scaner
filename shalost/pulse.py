"""Обмен с Pulse: разбор QR и вызов подтверждения присутствия

Подтверждение уходит запросом gRPC-Web со страницы Pulse, где уже есть Cookie
сессии, поэтому ни токены, ни заголовки авторизации приложению не нужны.
"""
from __future__ import annotations

import struct
from urllib.parse import parse_qs, unquote, unquote_plus, urlparse
from uuid import UUID

PULSE_HOME = "https://pulse.mirea.ru/"
EDU_HOME = "https://online-edu.mirea.ru/"
PULSE_RPC = ("https://pulse.mirea.ru/rtu_tc.attendance.api.AttendanceService"
             "/SelfApproveAttendanceThroughQRCode")

STATE_JS = """() => {
  const settingsLinks = [...document.querySelectorAll('a[href*="/settings"]')];
  const values = settingsLinks.map(x => (x.innerText || '').trim())
    .filter(x => x && !/настройки|settings/i.test(x));
  return {onPulse: location.hostname === 'pulse.mirea.ru', name: values.at(-1) || ''};
}"""

APPROVE_JS = """async ({url, body}) => {
  const r = await fetch(url, {method: 'POST', credentials: 'include', headers: {
    'content-type': 'application/grpc-web+proto', 'x-grpc-web': '1',
    'x-requested-with': 'XMLHttpRequest', 'pulse-app-type': 'pulse'}, body: Uint8Array.from(body)});
  return {status:r.status, bytes:[...new Uint8Array(await r.arrayBuffer())]};
}"""


def qr_token(value: str) -> str | None:
    """Идентификатор занятия из QR-кода: и ссылка, и голый UUID"""
    value = unquote(value.strip())
    if "token=" in value:
        value = parse_qs(urlparse(value).query).get("token", [""])[0]
    try:
        return str(UUID(value))
    except (ValueError, AttributeError):
        return None


def grpc_body(token: str) -> bytes:
    raw = token.encode("utf-8")
    proto = b"\x0a" + bytes([len(raw)]) + raw
    return b"\x00" + struct.pack(">I", len(proto)) + proto


def grpc_web_trailer(data: bytes) -> tuple[str | None, str]:
    """Извлекает grpc-status и grpc-message из кадров gRPC-Web в теле ответа"""
    offset = 0
    status: str | None = None
    message = ""
    while offset + 5 <= len(data):
        flags = data[offset]
        size = struct.unpack(">I", data[offset + 1:offset + 5])[0]
        offset += 5
        if offset + size > len(data):
            break
        payload = data[offset:offset + size]
        offset += size
        if flags & 0x80:
            for line in payload.decode("utf-8", errors="replace").splitlines():
                key, separator, value = line.partition(":")
                if not separator:
                    continue
                if key.lower() == "grpc-status":
                    status = value.strip()
                elif key.lower() == "grpc-message":
                    message = unquote_plus(value.strip())
    return status, message


def approve_request(page, token: str) -> tuple[bool, str]:
    """Отправляет подтверждение QR со страницы Pulse"""
    result = page.evaluate(APPROVE_JS, {"url": PULSE_RPC, "body": list(grpc_body(token))})
    grpc_status, grpc_message = grpc_web_trailer(bytes(result.get("bytes", [])))
    if result.get("status") == 200 and grpc_status == "0":
        return True, ""
    return False, grpc_message or f"HTTP {result.get('status')}; gRPC {grpc_status or 'не указан'}"


def read_state(page, timeout: int = 60000) -> tuple[str, bool]:
    """Имя пользователя и признак активной сессии на странице Pulse"""
    import time

    page.goto(PULSE_HOME, wait_until="domcontentloaded", timeout=timeout)
    time.sleep(2)
    state = page.evaluate(STATE_JS)
    name = str(state.get("name", "")).strip()
    return name, bool(name) and bool(state.get("onPulse"))

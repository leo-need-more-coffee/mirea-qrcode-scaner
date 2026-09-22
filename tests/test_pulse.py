"""Разбор QR-кодов и ответов Pulse"""
import struct

import pytest

from shalost import pulse

TOKEN = "3f2b6c1e-8a4d-4b7e-9c1f-2d5a7e8b4c60"


@pytest.mark.parametrize("value", [
    TOKEN,
    f"https://pulse.mirea.ru/?token={TOKEN}",
    f"https://pulse.mirea.ru/attendance?foo=bar&token={TOKEN}",
    f"  {TOKEN}  ",
])
def test_qr_token_recognises_pulse_codes(value):
    assert pulse.qr_token(value) == TOKEN


@pytest.mark.parametrize("value", ["", "просто текст", "https://example.com/", "token=нет"])
def test_qr_token_ignores_foreign_codes(value):
    assert pulse.qr_token(value) is None


def test_grpc_body_carries_token():
    body = pulse.grpc_body(TOKEN)
    assert body[0] == 0
    assert struct.unpack(">I", body[1:5])[0] == len(body) - 5
    assert TOKEN.encode() in body


def frame(payload: bytes, flags: int) -> bytes:
    return bytes([flags]) + struct.pack(">I", len(payload)) + payload


def test_trailer_reads_success():
    data = frame(b"\x00", 0) + frame(b"grpc-status:0\r\n", 0x80)
    assert pulse.grpc_web_trailer(data) == ("0", "")


def test_trailer_reads_error_message():
    data = frame(b"grpc-status:7\r\ngrpc-message:QR+already+used\r\n", 0x80)
    status, message = pulse.grpc_web_trailer(data)
    assert status == "7"
    assert message == "QR already used"


def test_trailer_survives_truncated_answer():
    assert pulse.grpc_web_trailer(b"\x80\x00\x00\x10") == (None, "")

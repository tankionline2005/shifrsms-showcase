"""Одноразовые коды аутентификатора (Google Authenticator и подобные), RFC 6238: 6 цифр, шаг 30 с.

Секрет хранится у аккаунта (TwoFactor.totp_secret). Принятый код запоминается шагом (totp_step) —
повторно тот же код не примут. Резервные коды — на случай потери телефона, каждый одноразовый."""

import base64
import hashlib
import hmac
import io
import json
import secrets
import struct
import time
from urllib.parse import quote

import qrcode
import qrcode.image.svg

ISSUER = "Shifr SMS"
STEP, DIGITS, WINDOW = 30, 6, 1  # окно ±1 шаг — на случай, если часы телефона чуть спешат или отстают
RECOVERY_COUNT = 10


def new_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode()


def _code(secret: str, step: int) -> str:
    key = base64.b32decode(secret)
    digest = hmac.new(key, struct.pack(">Q", step), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** DIGITS).zfill(DIGITS)


def now_step() -> int:
    return int(time.time()) // STEP


def match(secret: str, code: str, last_step: int | None) -> int | None:
    """-> шаг совпавшего кода или None. Код с шагом не новее last_step уже использован."""
    code = "".join(ch for ch in code if ch.isdigit())
    if len(code) != DIGITS:
        return None
    current = now_step()
    for step in range(current - WINDOW, current + WINDOW + 1):
        if (last_step is None or step > last_step) and hmac.compare_digest(_code(secret, step), code):
            return step
    return None


def uri(secret: str, account: str) -> str:
    label = quote(f"{ISSUER}:{account}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&digits={DIGITS}&period={STEP}"


def qr_svg(text: str) -> str:
    """QR-код как встроенный SVG (белый фон — камеры телефонов плохо читают светлое на тёмном)."""
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathFillImage, box_size=8, border=2)
    out = io.BytesIO()
    img.save(out)
    svg = out.getvalue().decode()
    return svg[svg.index("<svg"):]


# ---------------------------------------------------------------- резервные коды


def _hash(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").lower().encode()).hexdigest()


def new_recovery() -> tuple[list[str], str]:
    """-> (коды — показать один раз, JSON хешей — сохранить)."""
    codes = []
    for _ in range(RECOVERY_COUNT):
        raw = secrets.token_hex(4)
        codes.append(f"{raw[:4]}-{raw[4:]}")
    return codes, json.dumps([_hash(c) for c in codes])


def use_recovery(stored: str | None, code: str) -> str | None:
    """Код подошёл -> обновлённый JSON хешей (код вычеркнут); не подошёл -> None."""
    try:
        hashes = json.loads(stored or "[]")
    except ValueError:
        return None
    h = _hash(code.strip())
    if h not in hashes:
        return None
    hashes.remove(h)
    return json.dumps(hashes)


def recovery_left(stored: str | None) -> int:
    try:
        return len(json.loads(stored or "[]"))
    except ValueError:
        return 0

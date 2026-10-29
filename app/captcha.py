"""Капча регистрации на сайте: объёмные буквы на рифлёной стене (как классическая «3D-капча»).

Буквы рендерятся по-настоящему в 3D: контур шрифта выдавливается в толщину, с фаской на лицевой стороне, поворачивается
в пространстве и рисуется облаком точек с z-буфером; свет сверху справа даёт блики, тени падают на стену с бороздами,
поверх — лучи света и блик. Ответ — без учёта регистра.
"""

import hashlib
import hmac
import io
import math
import random
import secrets
import time
from functools import lru_cache
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.config import config

FONT = Path(__file__).parent / "fonts" / "ArchivoBlack-Regular.ttf"
W, H = 380, 120          # размер на странице
SS = 2                   # рендер в 2 раза крупнее — для чёткости на экранах с высокой плотностью
LENGTH = (5, 6)
# без похожих друг на друга и на себя после поворота: O/0/Q/D, I/1/L/J, S/5, Z/2, 6/9, M/W, N, U/V
ALPHABET = "ABCEFGHKPRTXY3478"
TTL = 600                # секунд
SESSION_KEY = "captcha"

# цвета сайта: стена — фиолетовая, буквы — розовые (ступени палитры по яркости 0…1)
WALL = ((0, (10, 4, 18)), (.3, (52, 24, 74)), (.5, (98, 58, 128)), (.7, (160, 118, 190)), (.9, (226, 204, 242)), (1, (255, 246, 255)))
INK = ((0, (34, 6, 36)), (.3, (124, 40, 120)), (.55, (210, 108, 200)), (.8, (248, 192, 242)), (1, (255, 244, 254)))


def _norm(v) -> np.ndarray:
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


def _palette(x: np.ndarray, stops) -> np.ndarray:
    pos = [p for p, _ in stops]
    return np.stack([np.interp(x, pos, [c[k] for _, c in stops]) for k in range(3)], -1)


def _lights(rnd) -> list:
    """Три источника с разных сторон: (направление на свет, сила, сила тени). Главный — сверху справа, как лучи."""
    return [(_norm([rnd.uniform(.35, .6), rnd.uniform(-.65, -.45), -.7]), .50, .40),
            (_norm([rnd.uniform(-.75, -.5), rnd.uniform(-.35, .05), -.7]), .26, .28),
            (_norm([rnd.uniform(-.2, .3), rnd.uniform(.45, .7), -.75]), .14, .2)]


def new_text() -> str:
    return "".join(secrets.choice(ALPHABET) for _ in range(secrets.choice(LENGTH)))


# nonce -> (HMAC ответа, когда истекает). Ответ — только на сервере: сессия сайта — подписанная, но читаемая cookie,
# и в ней лежит лишь nonce; капча одноразовая — старую cookie с решённой капчей повторно не использовать.
_pending: dict[str, tuple[str, float]] = {}
MAX_PENDING = 20_000


def _hash(text: str) -> str:
    return hmac.new(config.session_secret.encode(), text.strip().upper().encode(), hashlib.sha256).hexdigest()


def issue(session: dict) -> str:
    """Новая капча для этой сессии (прежняя перестаёт действовать) -> текст для картинки."""
    now = time.time()
    if len(_pending) > MAX_PENDING:
        for k in [k for k, (_, exp) in _pending.items() if exp < now] or list(_pending)[:MAX_PENDING // 2]:
            _pending.pop(k, None)
    _pending.pop(session.get(SESSION_KEY) or "", None)
    text, nonce = new_text(), secrets.token_urlsafe(12)
    _pending[nonce] = (_hash(text), now + TTL)
    session[SESSION_KEY] = nonce
    return text


def check(session: dict, answer: str) -> bool:
    """Проверить ответ; после любой проверки нужна новая картинка."""
    item = _pending.pop(session.pop(SESSION_KEY, None) or "", None)
    if not item or time.time() > item[1] or not (answer or "").strip():
        return False
    return hmac.compare_digest(_hash(answer), item[0])


# ---------------------------------------------------------------- рендер


@lru_cache(maxsize=4)
def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT), size)


def _blur(a: np.ndarray, r: float) -> np.ndarray:
    img = Image.fromarray(np.clip(a * 255, 0, 255).astype(np.uint8))
    return np.asarray(img.filter(ImageFilter.GaussianBlur(r)), dtype=np.float32) / 255


@lru_cache(maxsize=64)
def _glyph(ch: str, size: int) -> tuple:
    """Точки поверхности выдавленной буквы: (координаты N×3, нормали N×3); центр — в начале координат."""
    font = _font(size)
    img = Image.new("L", (size * 2, size * 2))
    ImageDraw.Draw(img).text((size, size), ch, font=font, anchor="mm", fill=255)
    m = np.asarray(img, dtype=np.float32) / 255
    ys, xs = np.nonzero(m > 0.5)
    y0, y1, x0, x1 = ys.min() - 4, ys.max() + 5, xs.min() - 4, xs.max() + 5
    m = m[y0:y1, x0:x1]
    solid = m > 0.5
    soft = _blur(solid.astype(np.float32), size * 0.022)
    gy, gx = np.gradient(soft)
    cx, cy = (x1 - x0) / 2, (y1 - y0) / 2
    depth = size * 0.28
    pts, nrm = [], []
    # лицевая сторона с фаской: нормаль наклоняется к краю
    ys, xs = np.nonzero(solid)
    bevel = 5.0
    n = np.stack([-gx[ys, xs] * bevel * size / 40, -gy[ys, xs] * bevel * size / 40, -np.ones(len(xs))], 1)
    pts.append(np.stack([xs - cx, ys - cy, np.zeros(len(xs))], 1))
    nrm.append(n)
    # задняя сторона
    pts.append(np.stack([xs - cx, ys - cy, np.full(len(xs), depth)], 1))
    nrm.append(np.tile([0, 0, 1.0], (len(xs), 1)))
    # боковые стенки: по контуру, слоями в глубину
    inner = np.zeros_like(solid)
    inner[1:-1, 1:-1] = solid[1:-1, 1:-1] & solid[:-2, 1:-1] & solid[2:, 1:-1] & solid[1:-1, :-2] & solid[1:-1, 2:]
    ys, xs = np.nonzero(solid & ~inner)
    side = np.stack([-gx[ys, xs], -gy[ys, xs], np.zeros(len(xs))], 1)
    side /= np.linalg.norm(side, axis=1, keepdims=True) + 1e-6
    for z in np.arange(0.5, depth, 0.7):
        pts.append(np.stack([xs - cx, ys - cy, np.full(len(xs), z)], 1))
        nrm.append(side)
    p, n = np.concatenate(pts).astype(np.float32), np.concatenate(nrm).astype(np.float32)
    n /= np.linalg.norm(n, axis=1, keepdims=True) + 1e-6
    p[:, 2] -= depth / 2
    return p, n


def _rot(yaw: float, pitch: float, roll: float) -> np.ndarray:
    cy, sy, cp, sp, cr, sr = math.cos(yaw), math.sin(yaw), math.cos(pitch), math.sin(pitch), math.cos(roll), math.sin(roll)
    ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]])
    rx = np.array([[1, 0, 0], [0, cp, -sp], [0, sp, cp]])
    rz = np.array([[cr, -sr, 0], [sr, cr, 0], [0, 0, 1]])
    return (rz @ rx @ ry).astype(np.float32)


def _wall(w: int, h: int, rnd) -> np.ndarray:
    """Стена с вертикальными бороздами, светлее к источнику света."""
    x = np.arange(w, dtype=np.float32)
    period = rnd.uniform(18, 26) * SS
    phase = (x + rnd.uniform(0, period)) % period / period
    groove = 0.035 * np.cos(phase * 2 * math.pi)
    groove -= 0.09 * np.exp(-((phase - 0.03) ** 2) / 0.0006)   # тёмная щель
    groove += 0.05 * np.exp(-((phase - 0.07) ** 2) / 0.0008)   # светлая кромка рядом
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    light = 0.42 + 0.14 * (xx / w) - 0.08 * (yy / h)
    return light + groove[None, :]


def _rays(w: int, h: int, rnd) -> np.ndarray:
    """Лучи света из правого верхнего угла."""
    layer = Image.new("L", (w, h))
    d = ImageDraw.Draw(layer)
    ox, oy = w * rnd.uniform(0.75, 1.05), -h * rnd.uniform(0.3, 0.8)
    for _ in range(rnd.randint(4, 6)):
        a = rnd.uniform(math.radians(100), math.radians(165))
        spread = rnd.uniform(0.015, 0.05)
        far = w * 2
        d.polygon([(ox, oy), (ox + far * math.cos(a - spread), oy + far * math.sin(a - spread)),
                   (ox + far * math.cos(a + spread), oy + far * math.sin(a + spread))], fill=rnd.randint(30, 70))
    return np.asarray(layer.filter(ImageFilter.GaussianBlur(5 * SS)), dtype=np.float32) / 255


def render(text: str) -> bytes:
    rnd = random.Random(secrets.randbits(64))
    w, h = W * SS, H * SS
    lights = _lights(rnd)
    half = _norm(lights[0][0] + np.array([0, 0, -1.0]))
    wall = _wall(w, h, rnd)
    color = np.zeros((h, w), np.float32)
    zbuf = np.full((h, w), np.inf, np.float32)
    shadows = [np.zeros((h, w), np.float32) for _ in lights]
    f = 900.0 * SS
    n = len(text)
    span = w * 0.80
    x = (w - span) / 2 + span / (2 * n)
    base_y = h * rnd.uniform(0.48, 0.56)
    wave_a, wave_f, wave_p = h * rnd.uniform(0.08, 0.16), rnd.uniform(0.6, 1.4), rnd.uniform(0, math.tau)
    for i, ch in enumerate(text):
        size = int(rnd.uniform(0.46, 0.54) * h)
        pts, nrm = _glyph(ch, size)
        r = _rot(rnd.uniform(-0.6, 0.6), rnd.uniform(-0.5, 0.5), rnd.uniform(-0.4, 0.4))
        p, nn = pts @ r.T, nrm @ r.T
        keep = nn[:, 2] < 0.35                        # обращённые от камеры точки не нужны
        p, nn = p[keep], nn[keep]
        z0 = rnd.uniform(-20, 30) * SS
        cx = x + i * span / n + rnd.uniform(-0.12, 0.12) * span / n
        cy = base_y + wave_a * math.sin(wave_p + wave_f * i) + rnd.uniform(-0.06, 0.06) * h
        Z = p[:, 2] + z0 + f
        sx, sy = cx + p[:, 0] * f / Z, cy + p[:, 1] * f / Z
        shade = 0.08 + sum(k * np.clip(nn @ d, 0, 1) for d, k, _ in lights) + 0.14 * np.clip(nn @ half, 0, 1) ** 24
        # z-буфер: ближние точки рисуются последними
        order = np.argsort(-Z)
        sx, sy, Z, shade = sx[order], sy[order], Z[order], shade[order]
        for dx in (0, 1):
            for dy in (0, 1):
                ix, iy = (sx + dx).astype(np.int32), (sy + dy).astype(np.int32)
                ok = (ix >= 0) & (ix < w) & (iy >= 0) & (iy < h)
                ix, iy, zz, sh = ix[ok], iy[ok], Z[ok], shade[ok]
                closer = zz < zbuf[iy, ix]
                zbuf[iy[closer], ix[closer]] = zz[closer]
                color[iy[closer], ix[closer]] = sh[closer]
        # тени на стене — от каждого источника в свою сторону; буквы почти у стены, поэтому тени близко и чёткие
        gap = 34 * SS - (p[order][:, 2] + z0) * 0.25
        for (d, _, _), layer in zip(lights, shadows):
            tx = (sx + gap * -d[0] / -d[2]).astype(np.int32)
            ty = (sy + gap * -d[1] / -d[2]).astype(np.int32)
            ok = (tx >= 0) & (tx < w) & (ty >= 0) & (ty < h)
            layer[ty[ok], tx[ok]] = 1
    for (_, _, k), layer in zip(lights, shadows):
        wall = wall * (1 - k * np.clip(_blur(np.clip(_blur(layer, 0.8 * SS) * 3, 0, 1), 1.6 * SS), 0, 1))
    obj = np.isfinite(zbuf)
    ink = _blur(obj.astype(np.float32), 0.5)          # мягкий край букв
    lum = np.where(obj, color, wall)
    rays = _rays(w, h, rnd) * 0.85
    gx_, gy_ = w * rnd.uniform(0.25, 0.75), h * rnd.uniform(0.92, 1.08)
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    rr = ((xx - gx_) ** 2 + (yy - gy_) ** 2) / (h * 0.13) ** 2
    glow = rays + 0.6 * np.exp(-rr) + 0.16 * np.exp(-rr / 8)   # лучи и блик
    lum = (lum + glow) * (1 - 0.18 * (((xx / w - 0.5) * 2) ** 2))
    lum += np.asarray(np.random.default_rng(secrets.randbits(32)).normal(0, 0.012, lum.shape), np.float32)
    lum = np.clip(lum, 0, 1)
    rgb = _palette(lum, WALL) * (1 - ink[..., None]) + _palette(lum, INK) * ink[..., None]
    out = Image.fromarray(np.clip(rgb, 0, 255).astype(np.uint8), "RGB")
    buf = io.BytesIO()
    out.save(buf, "WEBP", quality=80, method=4)
    return buf.getvalue()

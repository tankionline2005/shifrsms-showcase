"""Логотипы сервисов для сайта: плитка «как иконка приложения».

Самые популярные — векторные (Simple Icons, CC0; Google и Microsoft — нарисованы здесь). Остальные — картинки из icons/
(76×76 WEBP: иконки сервисов Grizzly и знаки Simple Icons на фирменном цвете), какой сервис какой — icons/map.json
(по id сервиса, для новых сервисов с тем же названием — по ключу). Без иконки — ничего: в списке только название."""

import json
import re
from functools import lru_cache
from pathlib import Path

from markupsafe import Markup

DIR = Path(__file__).parent / "logos"
ICONS = Path(__file__).parent / "icons"
_MAP = json.loads((ICONS / "map.json").read_text(encoding="utf-8"))
BY_ID: dict[str, str] = _MAP["by_id"]
BY_KEY: dict[str, str] = _MAP["by_key"]
ICON_RE = re.compile(r"^[a-z0-9-]{1,64}$")
GOOGLE = ('<svg viewBox="0 0 48 48" style="width:62%;height:62%"><path fill="#EA4335" d="M24 9.5c3.54 0 6.71 1.22 9.21 3.6l6.85-6.85'
          'C35.9 2.38 30.47 0 24 0 14.62 0 6.51 5.38 2.56 13.22l7.98 6.19C12.43 13.72 17.74 9.5 24 9.5z"/><path fill="#4285F4" '
          'd="M46.98 24.55c0-1.57-.15-3.09-.38-4.55H24v9.02h12.94c-.58 2.96-2.26 5.48-4.78 7.18l7.73 6c4.51-4.18 7.09-10.36 7.09-17.65z"/>'
          '<path fill="#FBBC05" d="M10.53 28.59c-.48-1.45-.76-2.99-.76-4.59s.27-3.14.76-4.59l-7.98-6.19C.92 16.46 0 20.12 0 24c0 3.88.92 '
          '7.54 2.56 10.78l7.97-6.19z"/><path fill="#34A853" d="M24 48c6.48 0 11.93-2.13 15.89-5.81l-7.73-6c-2.15 1.45-4.92 2.3-8.16 '
          '2.3-6.26 0-11.57-4.22-13.47-9.91l-7.98 6.19C6.51 42.62 14.62 48 24 48z"/></svg>')
MICROSOFT = ('<svg viewBox="0 0 23 23" style="width:58%;height:58%"><path fill="#f25022" d="M1 1h10v10H1z"/><path fill="#7fba00" '
             'd="M12 1h10v10H12z"/><path fill="#00a4ef" d="M1 12h10v10H1z"/><path fill="#ffb900" d="M12 12h10v10H12z"/></svg>')
# ключ сервиса (Service.key) -> (фон плитки, файл логотипа | готовый svg, цвет знака)
TILES = {
    "telegram": ("#ffffff", "telegram", "#26a5e4"), "whatsapp": ("#25d366", "whatsapp", "#fff"),
    "google": ("#ffffff", GOOGLE, None), "gmail": ("#ffffff", GOOGLE, None), "youtube": ("#ffffff", GOOGLE, None),
    "instagram": ("radial-gradient(circle at 30% 107%,#fdf497 0%,#fd5949 45%,#d6249f 60%,#285aeb 90%)", "instagram", "#fff"),
    "discord": ("#5865f2", "discord", "#fff"), "openai": ("#202123", "openai", "#fff"), "chatgpt": ("#202123", "openai", "#fff"),
    "tinder": ("linear-gradient(135deg,#ff6036,#fd267a)", "tinder", "#fff"), "microsoft": ("#ffffff", MICROSOFT, None),
}


@lru_cache(maxsize=64)
def _glyph(name: str, color: str) -> str:
    svg = (DIR / f"{name}.svg").read_text(encoding="utf-8")
    vb = re.search(r'viewBox="([^"]+)"', svg).group(1)
    body = re.sub(r"<title>.*?</title>", "", svg.split(">", 1)[1].rsplit("</svg>", 1)[0], flags=re.S)
    body = re.sub(r'\sfill="[^"]*"', "", body).replace("<path", f'<path fill="{color}"')
    pad = "16%" if name == "telegram" else "22%"
    return f'<svg viewBox="{vb}" style="width:calc(100% - 2*{pad});height:calc(100% - 2*{pad})">{body}</svg>'


def icon(service_id: int, key: str) -> str | None:
    """Имя картинки сервиса в icons/ (без .webp) или None."""
    return BY_ID.get(str(service_id)) or BY_KEY.get(re.sub(r"[^0-9a-zа-яё]", "", (key or "").lower()))


def tile(service_id: int, key: str, name: str) -> Markup:
    t = TILES.get((key or "").lower())
    if t:
        bg, glyph, color = t
        inner = glyph if glyph.startswith("<svg") else _glyph(glyph, color)
        return Markup(f'<i class="avatar logo-t" style="background:{bg}">{inner}</i>')
    f = icon(service_id, key)
    if f:
        return Markup(f'<img class="avatar" src="/shop/ico/{f}.webp" alt="" loading="lazy" decoding="async" width="38" height="38">')
    return Markup("")

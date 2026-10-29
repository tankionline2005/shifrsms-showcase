"""Общее для страниц сайта /shop: шаблоны, язык, вход, защита форм (CSRF), всплывающие сообщения."""

import re
import secrets
from decimal import Decimal
from pathlib import Path

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from markupsafe import Markup

from app import catalog, pycountry_names, shop_accounts
from app import settings_store as st
from app.config import config
from app.models import User
from app.web.shop import logos
from app.web.shop.i18n import FAQ, LANGS, pick_lang, t

P = "/shop"
SESSION_KEY = "shop"
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def rub(x) -> str:
    x = Decimal(x)
    s = f"{x:,.2f}".replace(",", " ").replace(".00", "")
    return f"{s} ₽"


def qty(n: int, lang: str) -> str:
    unit = t(lang, "pcs")
    return f"10 000+ {unit}" if n > 10_000 else f"{n:,} {unit}".replace(",", " ")


def country_title(c, lang: str) -> str:
    return f"{c.flag} {country_name(c, lang)}"


def flag(c) -> Markup:
    """Флаг картинкой flagcdn.com: эмодзи-флаги Windows рисует буквами."""
    cc = (c.key or "").split("_")[0].lower()
    if not re.fullmatch(r"[a-z]{2}", cc):
        return Markup("")
    return Markup(f'<img class="flag" src="https://flagcdn.com/w40/{cc}.png" srcset="https://flagcdn.com/w80/{cc}.png 2x" '
                  f'width="22" height="16" alt="" onerror="this.remove()">')


def country_name(c, lang: str) -> str:
    return pycountry_names.english(c.key, c.name) if lang == "en" else c.name


templates.env.filters["rub"] = rub
templates.env.globals["P"] = P
templates.env.globals["logo"] = lambda s: logos.tile(s.id, s.key, s.name)  # плитка сервиса: логотип или буква


def lang_of(request: Request) -> str:
    return pick_lang(request.cookies.get("lang"), request.headers.get("accept-language"))


async def current_user(request: Request) -> User | None:
    data = request.session.get(SESSION_KEY)
    if not isinstance(data, dict) or not isinstance(data.get("uid"), int):
        return None
    user = await shop_accounts.get(data["uid"])
    if not user or user.is_blocked or (user.session_ver or 1) != data.get("v"):
        request.session.pop(SESSION_KEY, None)
        return None
    return user


def sign_in(request: Request, user: User) -> None:
    request.session.pop("shop_auth", None)
    request.session[SESSION_KEY] = {"uid": user.id, "v": user.session_ver or 1}


def sign_out(request: Request) -> None:
    request.session.pop(SESSION_KEY, None)


def csrf_token(request: Request) -> str:
    tok = request.session.get("shop_csrf")
    if not tok:
        tok = request.session["shop_csrf"] = secrets.token_urlsafe(16)
    return tok


def csrf_ok(request: Request, token: str | None) -> bool:
    return bool(token) and secrets.compare_digest(str(token), request.session.get("shop_csrf") or "")


def flash(request: Request, text: str, kind: str = "ok") -> None:
    request.session["shop_flash"] = [kind, text]


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def safe_next(url: str | None) -> str:
    return url if url and url.startswith(P) and not url.startswith("//") else P


def render(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
    lang = lang_of(request)
    flash_msg = request.session.pop("shop_flash", None)
    page = templates.TemplateResponse(request, name, {
        "lang": lang, "langs": LANGS, "user": user, "flash": flash_msg, "csrf": csrf_token(request),
        "tr": lambda key, **fmt: t(lang, key, **fmt), "qty": lambda n: qty(n, lang),
        "ctitle": lambda c: flag(c) + " " + country_name(c, lang), "flag": flag, "cname": lambda c: country_name(c, lang),
        "ename": lambda c: country_name(c, "en"),
        "is_tg": shop_accounts.is_tg, "public_url": config.public_url, "faq": FAQ[lang],
        "terms_url": st.raw("terms_url"), "privacy_url": st.raw("privacy_url"),
        "path": request.url.path, **ctx})
    page.headers["Cache-Control"] = "no-store"
    return page


def svc(sid: int):
    s = catalog.SERVICES.get(sid)
    return s if s and not s.is_hidden else None

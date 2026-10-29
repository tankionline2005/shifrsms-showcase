"""Публичные лендинги сайта. / — главная (что за сервис, кнопка «Открыть бота», один экран; app/web/landing/home.html,
шрифты и иконка — общие с /partnership). /partnership — партнёрская программа для владельцев каналов (рассылаем крупным каналам).
Прежний адрес /become-partner (и его файлы) перенаправляется сюда.

Страница — статичный HTML (app/web/landing/partnership.html); домен в ней не пишется: адрес для превью
в Telegram берётся из config.public_url (DOMAIN в .env)."""

import logging
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from app import partner_signup as signup
from app import user_refs
from app import settings_store as st
from app.config import config
from app.web import referral as cabinet
from app.web.referral import P as CABINET

router = APIRouter()
log = logging.getLogger(__name__)
DIR = Path(__file__).parent / "landing"
P = "/partnership"
OLD_P = "/become-partner"

# кнопка «Стать партнёром» — в личные сообщения с готовым текстом (если в странице есть {{CTA_URL}})
PARTNER_CONTACT = "Kindergarten4631"
PARTNER_TEXT = "Здравствуйте! У меня есть свой канал, который я хочу подключить к партнёрской программе ShifrSMS."

FONTS = {"bitcell.woff", "plex-mono-400.woff", "plex-mono-500.woff"}


@router.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def home(request: Request):
    log.info("home %s %s", request.method, request.headers.get("user-agent", "")[:120])
    page = (DIR / "home.html").read_text(encoding="utf-8")
    # браузер пришёл по ссылке-приглашению (/r/<код>) — бот тоже получит код, если человек выберет бота
    invite = user_refs.clean(request.cookies.get(user_refs.COOKIE))
    bot_url = "https://t.me/ShifrSMSbot" + (f"?start={user_refs.PREFIX}{invite}" if invite else "")
    page = (page.replace("{{PAGE_URL}}", f"{config.public_url}/").replace("{{BOT_URL}}", bot_url)
            .replace("{{TERMS_URL}}", st.raw("terms_url")).replace("{{PRIVACY_URL}}", st.raw("privacy_url")))
    return HTMLResponse(page, headers={"Cache-Control": "no-cache, private"})


@router.api_route("/og.jpg", methods=["GET", "HEAD"])
async def home_preview(request: Request):
    """Картинка превью ссылки на главную (og:image, 1200×630)."""
    log.info("home og.jpg %s %s", request.method, request.headers.get("user-agent", "")[:120])
    return FileResponse(DIR / "home-og.jpg", media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


# HEAD — тоже: ботам превью ссылок (Telegram и др.) нужен ответ и на него
@router.api_route(P, methods=["GET", "HEAD"], response_class=HTMLResponse)
async def partnership(request: Request):
    log.info("landing %s %s %s", request.method, request.url.query, request.headers.get("user-agent", "")[:120])  # видно ботов превью
    page = (DIR / "partnership.html").read_text(encoding="utf-8")
    page = (page.replace("{{CTA_URL}}", f"https://t.me/{PARTNER_CONTACT}?text={quote(PARTNER_TEXT)}")
            .replace("{{PAGE_URL}}", f"{config.public_url}{P}")
            .replace("{{MIN_SUBS}}", str(st.int_("partner_min_subscribers"))))
    return HTMLResponse(page, headers={"Cache-Control": "no-cache"})


@router.api_route(P + "/og.jpg", methods=["GET", "HEAD"])
async def partnership_preview(request: Request):
    """Картинка превью ссылки (og:image, 1200×630) — Telegram показывает её, когда ссылку кидают в чат."""
    log.info("landing og.jpg %s %s", request.method, request.headers.get("user-agent", "")[:120])
    return FileResponse(DIR / "og.jpg", media_type="image/jpeg", headers={"Cache-Control": "public, max-age=86400"})


@router.api_route(P + "/icon.png", methods=["GET", "HEAD"])
async def partnership_icon():
    """Иконка вкладки браузера (скруглённая, прозрачные углы)."""
    return FileResponse(DIR / "icon.png", media_type="image/png", headers={"Cache-Control": "public, max-age=86400"})


@router.api_route(P + "/fonts/{name}", methods=["GET", "HEAD"])
async def partnership_font(name: str):
    """Шрифты лендинга — отдельными файлами, а не внутри HTML: так страница лёгкая и ботам превью ссылок,
    которые читают только начало страницы, хватает."""
    if name not in FONTS:
        raise HTTPException(404)
    return FileResponse(DIR / "fonts" / name, media_type="font/woff",
                        headers={"Cache-Control": "public, max-age=2592000"})


# ---------------------------------------------------------------- подключение канала (окно на странице)


def _fail(e: signup.SignupError) -> JSONResponse:
    return JSONResponse({"ok": False, "error": str(e)})


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):  # только JSON с этой страницы: обычную форму с чужого сайта так не отправить
        raise HTTPException(400)
    return data


def _draft(request: Request) -> signup.Draft:
    d = signup.draft(request.session.get("signup"))
    if d is None:
        raise signup.SignupError("Сессия устарела — начните сначала.")
    return d


def _ip(request: Request) -> str:
    return request.client.host if request.client else "?"


@router.post(P + "/api/check")
async def signup_check(request: Request):
    """Шаг 1: ссылка на канал — подходит ли он."""
    data = await _json(request)
    try:
        signup.limit("check", _ip(request))
        token, d = await signup.check(str(data.get("link") or ""))
    except signup.SignupError as e:
        return _fail(e)
    request.session["signup"] = token
    return {"ok": True, "title": d.title, "username": d.username, "members": d.members}


@router.post(P + "/api/code")
async def signup_code(request: Request):
    """Шаг 2: код подтверждения — создаётся только сейчас, для канала из шага 1 и этого браузера."""
    await _json(request)
    try:
        signup.limit("code", _ip(request))
        d = _draft(request)
    except signup.SignupError as e:
        return _fail(e)
    ttl = signup.new_code(d)
    return {"ok": True, "code": d.code, "ttl": ttl, "username": d.username}


@router.post(P + "/api/verify")
async def signup_verify(request: Request):
    """Шаг 2 → 3: код в описании канала — канал становится партнёром, партнёр сразу входит в кабинет."""
    await _json(request)
    try:
        signup.limit("verify", _ip(request))
        d = _draft(request)
        ref, password = await signup.verify(request.session.get("signup"), d)
    except signup.SignupError as e:
        return _fail(e)
    request.session.pop("signup", None)
    cabinet.sign_in(request, ref)  # вход в кабинет — сразу
    return {"ok": True, "title": d.title, "login": ref.login, "password": password,
            "cabinet": f"{config.public_url}{CABINET}"}


@router.api_route(OLD_P, methods=["GET", "HEAD"])
@router.api_route(OLD_P + "/{rest:path}", methods=["GET", "HEAD"])
async def old_partnership(request: Request, rest: str = ""):
    """Прежний адрес лендинга — на новый (ссылки, которые уже разослали)."""
    q = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(P + (f"/{rest}" if rest else "") + q, status_code=301)

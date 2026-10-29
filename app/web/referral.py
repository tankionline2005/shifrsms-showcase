"""Кабинет партнёра (реферала): /partner-dashboard (старые ссылки /referral… перенаправляются — server.py). Вкладки «Каналы» — только своя связка (сообщения, процент,
баланс и его история) — и «Вывести» (вывод баланса на кошелёк USDT TRC20 с кодом аутентификатора).
Вход — по логину и паролю, которые выдаёт владелец в «Каналах»."""

import asyncio

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app import accounts, referrals
from app.db import Session
from app.models import Referral
from app.web import channel_pages, money_pages, ratelimit
from app.web.admin import templates

P = "/partner-dashboard"
OLD_P = "/referral"  # прежний адрес кабинета — ссылки, которые партнёры уже получили
router = APIRouter(prefix=P)


class NeedRefLogin(Exception):
    pass


async def require_ref(request: Request) -> None:
    ref_id = request.session.get("ref")
    async with Session() as s:
        ref = await s.get(Referral, ref_id) if isinstance(ref_id, int) else None
    # реферала удалили, вышли «на всех устройствах» или сменили пароль — кабинет закрывается сразу
    if not ref or request.session.get("ref_v") != (ref.session_ver or 1):
        request.session.pop("ref", None)
        request.session.pop("ref_v", None)
        raise NeedRefLogin()
    request.state.ref = ref


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"active": name.split(".")[0], "refresh": 0,
                                                      "A": P, "is_ref": True, **ctx})


@router.get("/login")
async def login_page(request: Request):
    return render(request, "login.html", error=None)


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    keys = (f"ref-ip:{ratelimit.ip(request)}", f"ref-login:{username.strip().lower()}")
    if ratelimit.blocked(*keys):
        await asyncio.sleep(1)
        return render(request, "login.html", error=ratelimit.MESSAGE)
    ref = await referrals.login(username, password)
    if not ref:
        ratelimit.failed(*keys)
        await asyncio.sleep(1)
        return render(request, "login.html", error="Неверный логин или пароль")
    sign_in(request, ref)
    return RedirectResponse(f"{P}/channels", status_code=303)


def sign_in(request: Request, ref: Referral) -> None:
    request.session["ref"], request.session["ref_v"] = ref.id, ref.session_ver or 1


@router.get("/logout")
async def logout(request: Request):
    """Выход — на всех устройствах (прежние cookie кабинета больше не пускают)."""
    ref_id = request.session.get("ref")
    if isinstance(ref_id, int):
        async with Session() as s:
            ref = await s.get(Referral, ref_id)
        if ref and request.session.get("ref_v") == (ref.session_ver or 1):
            await referrals.sign_out_everywhere(ref_id)
    request.session.pop("ref", None)
    request.session.pop("ref_v", None)
    return RedirectResponse(f"{P}/login", status_code=303)


@router.get("", dependencies=[Depends(require_ref)])
async def home():
    return RedirectResponse(f"{P}/channels", status_code=303)


channel_pages.register(router, P, require_ref, lambda request: {request.state.ref.chat_id}, render)
money_pages.register(router, P, require_ref, lambda request: accounts.Acc("ref", request.state.ref.id),
                     lambda request: f"реферал {request.state.ref.login}", render)

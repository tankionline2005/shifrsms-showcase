"""Сайт /shop: каталог, активации, заказы, баланс, приглашения, поддержка, профиль, вход и регистрация.

Покупка, отмена и замена — те же функции, что у бота (app/orders.py), деньги — тот же баланс (User.balance)."""

import asyncio
import logging
import re
import time
from decimal import ROUND_CEILING, Decimal

from fastapi import APIRouter, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select

from app import captcha, catalog, orders, payments, shop_accounts, user_refs, wallet
from app import settings_store as st
from app.bot import support
from app.bot.ui import instruction_url
from app.config import config
from app.db import Session
from app.models import Order, Payment, SupportTicket, User
from app.web import ratelimit
from app.web.shop.core import (P, back, country_title, csrf_ok, current_user, flash, lang_of, render, rub, safe_next,
                               sign_in, sign_out, svc, templates)
from app.web.shop import logos
from app.web.shop.i18n import LANGS, order_message, t

log = logging.getLogger(__name__)
router = APIRouter()
ORDERS_PAGE = 20
SVC_PAGE = 150
ACTIVE = ("waiting", "received")
HIDDEN_ORDERS = ("cancelled", "expired", "replaced")
HIDDEN_PAYMENTS = ("canceled", "expired")


def _login_redirect(request: Request) -> RedirectResponse:
    return back(f"{P}/login?next={request.url.path}")


async def _need_user(request: Request):
    user = await current_user(request)
    return user, (None if user else _login_redirect(request))


def _bad_csrf(request: Request, url: str) -> RedirectResponse:
    flash(request, t(lang_of(request), "err_csrf"), "bad")
    return back(url)


# ---------------------------------------------------------------- язык


@router.get(P + "/lang/{lang}")
async def set_lang(request: Request, lang: str, next: str = P):
    lang = lang if lang in LANGS else "ru"
    user = await current_user(request)
    if user:
        await shop_accounts.set_lang(user.id, lang)
    resp = back(safe_next(next))
    resp.set_cookie("lang", lang, max_age=365 * 86400, samesite="lax", secure=config.public_url.startswith("https"))
    return resp


# ---------------------------------------------------------------- каталог


def _services(q: str) -> tuple[list, int]:
    """-> (сервисы по порядку, сколько показать сразу): без поиска сразу — только популярные, остальные — кнопкой."""
    if q:
        return catalog.search_services(q), SVC_PAGE
    popular = catalog.popular_services()
    seen = {s.id for s in popular}
    return popular + [s for s in catalog.all_services() if s.id not in seen], len(popular) or SVC_PAGE  # дальше — по числу активаций


@router.get(P, response_class=HTMLResponse)
async def catalog_page(request: Request, q: str = "", offset: int = 0, part: int = 0):
    """Каталог: сразу — популярные (при поиске — первые SVC_PAGE), дальше по SVC_PAGE кнопкой «Ещё сервисы»;
    part=1 — только следующая порция (кнопка и поиск без перезагрузки)."""
    items, first = _services(q.strip()[:64])
    offset = max(0, offset)
    end = offset + SVC_PAGE if offset else first
    chunk, more = items[offset:end], len(items) > end
    if part:
        page = templates.TemplateResponse(request, "_svc_items.html", {"items": chunk})
        page.headers["X-More"] = "1" if more else "0"
        page.headers["Cache-Control"] = "no-store"
        return page
    user = await current_user(request)
    return render(request, "catalog.html", user, items=items[:end], more=more, q=q,
                  loading=not catalog.SERVICES, active="catalog")


@router.get(P + "/ico/{name}.webp")
async def service_icon(name: str):
    path = logos.ICONS / f"{name}.webp"
    if not logos.ICON_RE.match(name) or not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "public, max-age=31536000, immutable"})


@router.get(P + "/s/{sid}", response_class=HTMLResponse)
async def service_page(request: Request, sid: int):
    user = await current_user(request)
    s = svc(sid)
    if not s:
        return back(P)
    lang = lang_of(request)
    return render(request, "service.html", user, s=s, levels=catalog.auto_choices(sid), guide=instruction_url(s.instruction),
                  note=s.note if lang == "ru" else None, active="catalog")


@router.get(P + "/s/{sid}/countries", response_class=HTMLResponse)
async def countries_page(request: Request, sid: int, q: str = "", sort: str = "popular"):
    user = await current_user(request)
    s = svc(sid)
    if not s:
        return back(P)
    lang = lang_of(request)
    rows = catalog.countries_for(sid)
    if q.strip():
        needle = q.strip().lower()
        rows = [r for r in rows if needle in r[0].name.lower() or needle in country_title(r[0], "en").lower()
                or needle in r[0].key.lower()]
    if sort == "cheap":
        rows = sorted(rows, key=lambda r: r[1].price)
    elif sort == "expensive":
        rows = sorted(rows, key=lambda r: -r[1].price)
    else:
        sort = "popular"
    return render(request, "countries.html", user, s=s, rows=rows, q=q, sort=sort, active="catalog")


@router.get(P + "/s/{sid}/c/{cid}", response_class=HTMLResponse)
async def country_page(request: Request, sid: int, cid: int):
    user = await current_user(request)
    s, c = svc(sid), catalog.COUNTRIES.get(cid)
    if not s or not c:
        return back(P)
    try:
        await catalog.refresh_pair(sid, cid)
    except Exception:
        log.exception("сайт: refresh_pair %s %s", sid, cid)
    qt = catalog.quote(sid, cid)
    if not qt:
        flash(request, t(lang_of(request), "err_no_numbers_plain"), "bad")
        return back(f"{P}/s/{sid}/countries")
    lang = lang_of(request)
    return render(request, "country.html", user, s=s, c=c, levels=qt.levels, note=s.note if lang == "ru" else None,
                  active="catalog")


@router.post(P + "/buy")
async def buy(request: Request, sid: int = Form(...), price: int = Form(...), cid: int | None = Form(None),
              csrf: str = Form("")):
    user, redirect = await _need_user(request)
    lang = lang_of(request)
    fail_url = f"{P}/s/{sid}/c/{cid}" if cid else f"{P}/s/{sid}"
    if redirect:
        flash(request, t(lang, "need_login"))
        return back(f"{P}/login?next={fail_url}")
    if not csrf_ok(request, csrf):
        return _bad_csrf(request, fail_url)
    amount = Decimal(price)
    try:
        order = await orders.buy(user.id, sid, cid, amount)
    except orders.PurchaseInProgress:
        msg = t(lang, "err_in_progress")
    except orders.PriceChanged:
        msg = t(lang, "err_price_changed")
    except orders.InsufficientFunds as e:
        need = (e.price - await wallet.get_balance(user.id)).to_integral_value(rounding=ROUND_CEILING)
        flash(request, t(lang, "err_funds", price=rub(e.price), need=rub(max(need, 1))), "bad")
        return back(f"{P}/balance?need={int(max(need, 1))}")
    except orders.ProviderUnavailable:
        msg = t(lang, "err_provider")
    except orders.NoNumbersAvailable:
        msg = t(lang, "err_no_numbers", price=rub(amount))
    except orders.OrderError as e:
        msg = order_message(lang, str(e)) or t(lang, "err_order")
    else:
        return back(f"{P}/orders/{order.id}")
    flash(request, msg, "bad")
    return back(fail_url)


# ---------------------------------------------------------------- заказы


async def _own_order(user, oid: int) -> Order | None:
    o = await orders.load(oid)
    return o if o and user and o.user_id == user.id else None


@router.get(P + "/orders", response_class=HTMLResponse)
async def orders_page(request: Request, q: str = "", sort: str = "new", show: str = "all", page: int = 0):
    user, redirect = await _need_user(request)
    if redirect:
        return redirect
    cond = [Order.user_id == user.id, Order.status.not_in(HIDDEN_ORDERS)]  # отменённые и истёкшие не показываем
    digits = re.sub(r"\D", "", q)
    if digits:
        cond.append(Order.phone.like(f"%{digits}%"))
    if show == "active":
        cond.append(Order.status.in_(ACTIVE))
    order_by = Order.id.asc() if sort == "old" else Order.id.desc()
    page = max(0, page)
    async with Session() as s:
        total = await s.scalar(select(func.count()).select_from(Order).where(*cond))
        rows = (await s.scalars(select(Order).where(*cond).order_by(Order.status.in_(ACTIVE).desc(), order_by)
                                .offset(page * ORDERS_PAGE).limit(ORDERS_PAGE))).all()
    return render(request, "orders.html", user, rows=rows, q=q, sort=sort, show=show, page=page,
                  pages=max(1, -(-total // ORDERS_PAGE)), active="orders")


@router.get(P + "/orders/{oid}", response_class=HTMLResponse)
async def order_page(request: Request, oid: int):
    user, redirect = await _need_user(request)
    if redirect:
        return redirect
    o = await _own_order(user, oid)
    if not o:
        return back(f"{P}/orders")
    return render(request, "order.html", user, o=o, active="orders", **_order_ctx(request, o))


def _order_ctx(request: Request, o: Order) -> dict:
    lang = lang_of(request)
    s = catalog.SERVICES.get(o.service_id)
    return {"note": (s.note if s and lang == "ru" and o.is_active else None),
            "note_sms": (o.service.note_sms if lang == "ru" and o.is_active and o.messages else None)}


@router.get(P + "/orders/{oid}/card", response_class=HTMLResponse)
async def order_card(request: Request, oid: int):
    """Карточка активации для обновления без перезагрузки (страница опрашивает её раз в несколько секунд)."""
    user = await current_user(request)
    o = await _own_order(user, oid)
    if not o:
        return HTMLResponse("", status_code=404)
    lang = lang_of(request)
    page = templates.TemplateResponse(request, "_order_card.html", {
        "o": o, "lang": lang, "tr": lambda key, **fmt: t(lang, key, **fmt), "ctitle": lambda c: country_title(c, lang),
        "csrf": request.session.get("shop_csrf") or "", **_order_ctx(request, o)})
    page.headers["X-Active"] = "1" if o.is_active else "0"
    page.headers["Cache-Control"] = "no-store"
    return page


@router.post(P + "/orders/{oid}/{action}")
async def order_action(request: Request, oid: int, action: str, csrf: str = Form("")):
    user, redirect = await _need_user(request)
    if redirect:
        return redirect
    url = f"{P}/orders/{oid}"
    if not csrf_ok(request, csrf):
        return _bad_csrf(request, url)
    lang = lang_of(request)
    o = await _own_order(user, oid)
    if not o:
        return back(f"{P}/orders")
    note, kind = None, "ok"
    if action == "cancel":
        note = await orders.cancel(oid, user.id)
        kind = "ok" if note.startswith("Активация отменена") else "bad"
    elif action == "replace":
        note, new = await orders.replace(oid, user.id)
        if new:
            return back(f"{P}/orders/{new.id}")
        kind = "bad"
    elif action == "finish":
        note = await orders.finish(oid, user.id)
    elif action == "another":
        note = await orders.request_another(oid, user.id)
    elif action == "refresh":
        try:
            await orders.refresh(oid)
        except Exception:
            pass
    elif action == "again":  # ещё одна такая же активация: «Авто» — к бюджету, страна — к ценам страны
        return back(f"{P}/s/{o.service_id}" + ("" if o.auto else f"/c/{o.country_id}"))
    if note:
        flash(request, order_message(lang, note), kind)
    return back(url)


# ---------------------------------------------------------------- баланс


@router.get(P + "/balance", response_class=HTMLResponse)
async def balance_page(request: Request, need: int = 0, paid: int = 0):
    user, redirect = await _need_user(request)
    if redirect:
        return redirect
    async with Session() as s:
        pays = (await s.scalars(select(Payment).where(Payment.user_id == user.id, Payment.status.not_in(HIDDEN_PAYMENTS))
                                .order_by(Payment.id.desc())
                                .limit(20))).all()
    methods = [(m, payments.limits(m), payments.amounts(m)) for m in payments.METHODS] if payments.enabled() else []
    return render(request, "balance.html", user, methods=methods, pays=pays, need=max(0, need), paid=paid,
                  active="balance", stars=payments.STARS)


@router.post(P + "/balance/topup")
async def topup(request: Request, method: int = Form(...), amount: str = Form(...), csrf: str = Form("")):
    user, redirect = await _need_user(request)
    if redirect:
        return redirect
    if not csrf_ok(request, csrf):
        return _bad_csrf(request, f"{P}/balance")
    lang = lang_of(request)
    value = payments.parse_amount(amount)
    if value is None:
        lo, hi = payments.limits(method) if method in payments.METHODS else (0, 0)
        flash(request, t(lang, "topup_limits", min=lo, max=hi), "bad")
        return back(f"{P}/balance")
    try:
        p = await payments.create(user.id, user.login or user.username, value, method,
                                  f"{config.public_url}{P}/balance?paid=1")
    except payments.PaymentError as e:
        flash(request, str(e) if lang == "ru" else t(lang, "err_try_again"), "bad")
        return back(f"{P}/balance")
    return RedirectResponse(p.url, status_code=303)


# ---------------------------------------------------------------- приглашения


@router.get(P + "/invite", response_class=HTMLResponse)
async def invite_page(request: Request):
    user, redirect = await _need_user(request)
    if redirect:
        return redirect
    code = await user_refs.code_of(user.id) if user_refs.enabled() else None
    stats = await user_refs.stats(user.id) if code else None
    return render(request, "invite.html", user, enabled=user_refs.enabled(), code=code, stats=stats,
                  percent=f"{st.dec('user_ref_revenue_percent'):g}", link=user_refs.link(code) if code else None,
                  active="invite")


async def _bot_username() -> str | None:
    from app.bot import channels
    if channels.bot is None:
        return None
    try:
        return (await channels.bot.me()).username
    except Exception:
        return None


@router.get("/r/{code}")
async def invite_link(request: Request, code: str):
    """Ссылка-приглашение: запомнить код в браузере на COOKIE_DAYS и открыть каталог. Засчитывается первый переход —
    код из следующих чужих ссылок прежний не перезаписывает. Код засчитают регистрация на сайте, вход через Telegram
    нового для бота человека и кнопка «Открыть Telegram-бота» на главной (?start=u_<код>)."""
    resp = back(P)
    kept = await user_refs.owner_of(request.cookies.get(user_refs.COOKIE))
    if not kept and await user_refs.owner_of(code):
        resp.set_cookie(user_refs.COOKIE, user_refs.clean(code), max_age=user_refs.COOKIE_DAYS * 86400, samesite="lax",
                        secure=config.public_url.startswith("https"))
    return resp


# ---------------------------------------------------------------- поддержка


async def _own_ticket(user, tid: int) -> SupportTicket | None:
    async with Session() as s:
        t_ = await s.get(SupportTicket, tid)
    return t_ if t_ and user and t_.user_id == user.id else None


@router.get(P + "/support", response_class=HTMLResponse)
async def support_page(request: Request):
    user, redirect = await _need_user(request)
    if redirect:
        return redirect
    tickets = await support.tickets_of(user.id)
    open_t = next((x for x in tickets if x.status == "open"), None)
    if open_t:
        return back(f"{P}/support/{open_t.id}")
    return render(request, "support.html", user, tickets=tickets, ticket=None, messages=[], active="support",
                  available=support.enabled())


@router.get(P + "/support/{tid}", response_class=HTMLResponse)
async def ticket_page(request: Request, tid: int):
    user, redirect = await _need_user(request)
    if redirect:
        return redirect
    ticket = await _own_ticket(user, tid)
    if not ticket:
        return back(f"{P}/support")
    tickets = await support.tickets_of(user.id)
    return render(request, "support.html", user, tickets=tickets, ticket=ticket,
                  messages=await support.messages_of(tid), active="support", available=support.enabled())


@router.get(P + "/support/{tid}/messages", response_class=HTMLResponse)
async def ticket_messages(request: Request, tid: int):
    user = await current_user(request)
    ticket = await _own_ticket(user, tid)
    if not ticket:
        return HTMLResponse("", status_code=404)
    lang = lang_of(request)
    page = templates.TemplateResponse(request, "_messages.html", {
        "messages": await support.messages_of(tid), "lang": lang, "tr": lambda key, **fmt: t(lang, key, **fmt)})
    page.headers["X-Open"] = "1" if ticket.status == "open" else "0"
    page.headers["Cache-Control"] = "no-store"
    return page


@router.post(P + "/support/send")
async def support_send(request: Request, text: str = Form(""), csrf: str = Form("")):
    user, redirect = await _need_user(request)
    if redirect:
        return redirect
    if not csrf_ok(request, csrf):
        return _bad_csrf(request, f"{P}/support")
    lang = lang_of(request)
    ticket, result = await support.web_send(user.id, text)
    if result != "ok":
        flash(request, t(lang, {"off": "support_off", "flood": "support_flood"}.get(result, "support_error")), "bad")
    return back(f"{P}/support/{ticket.id}" if ticket else f"{P}/support")


# ---------------------------------------------------------------- вход, регистрация, Telegram


@router.get(P + "/login", response_class=HTMLResponse)
async def login_page(request: Request, next: str = P):
    user = await current_user(request)
    if user:
        return back(safe_next(next))
    return render(request, "login.html", None, next=safe_next(next), mode="login")


@router.post(P + "/login")
async def login(request: Request, login_: str = Form(..., alias="login"), password: str = Form(...),
                next: str = Form(P), csrf: str = Form("")):
    lang = lang_of(request)
    if not csrf_ok(request, csrf):
        return _bad_csrf(request, f"{P}/login")
    keys = (f"shop-ip:{ratelimit.ip(request)}", f"shop-login:{shop_accounts.norm_login(login_)}")
    if ratelimit.blocked(*keys):
        return render(request, "login.html", None, next=safe_next(next), mode="login", error=t(lang, "err_rate"),
                      login_value=login_)
    user = await shop_accounts.authenticate(login_, password)
    if not user:
        ratelimit.failed(*keys)
        return render(request, "login.html", None, next=safe_next(next), mode="login", error=t(lang, "err_bad_login"),
                      login_value=login_)
    sign_in(request, user)
    return back(safe_next(next))


@router.get(P + "/register", response_class=HTMLResponse)
async def register_page(request: Request):
    if await current_user(request):
        return back(P)
    return render(request, "login.html", None, next=P, mode="register")


@router.get(P + "/captcha")
async def captcha_image(request: Request):
    """Картинка капчи регистрации; каждая загрузка — новая капча для этой сессии."""
    if not ratelimit.allow(f"shop-cap:{ratelimit.ip(request)}", 40, 600):
        return Response(status_code=429)
    data = await asyncio.to_thread(captcha.render, captcha.issue(request.session))
    return Response(data, media_type="image/webp", headers={"Cache-Control": "no-store"})


@router.post(P + "/register")
async def register(request: Request, login_: str = Form(..., alias="login"), password: str = Form(...),
                   password2: str = Form(...), agree: str | None = Form(None), csrf: str = Form(""),
                   code: str = Form("", alias="captcha")):
    lang = lang_of(request)
    if not csrf_ok(request, csrf):
        return _bad_csrf(request, f"{P}/register")

    def again(error: str):
        return render(request, "login.html", None, next=P, mode="register", error=error, login_value=login_)

    if not captcha.check(request.session, code):
        return again(t(lang, "err_captcha"))
    if not agree:
        return again(t(lang, "err_agree"))
    if password != password2:
        return again(t(lang, "err_password_match"))
    if not ratelimit.allow(f"shop-reg:{ratelimit.ip(request)}", 5, 3600):
        return again(t(lang, "err_rate"))
    try:
        user = await shop_accounts.register(login_, password, lang, request.cookies.get(user_refs.COOKIE))
    except shop_accounts.AccountError as e:
        return again(t(lang, str(e)))
    sign_in(request, user)
    flash(request, t(lang, "register_warn"), "info")
    return back(f"{P}/profile")


@router.post(P + "/logout")
async def logout(request: Request, csrf: str = Form("")):
    if csrf_ok(request, csrf):
        sign_out(request)
    return back(P)


@router.post(P + "/tg/start")
async def tg_start(request: Request):
    """Вход / привязка / сброс через Telegram: одноразовая ссылка на бота. JSON: {token, url} или {error}."""
    lang = lang_of(request)
    data = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    if not csrf_ok(request, data.get("csrf")):
        return JSONResponse({"error": t(lang, "err_csrf")})
    if not ratelimit.allow(f"shop-tg:{ratelimit.ip(request)}", 20, 600):
        return JSONResponse({"error": t(lang, "err_rate")})
    purpose = data.get("purpose")
    user = await current_user(request)
    target = None
    if purpose == "link":
        if not user or shop_accounts.is_tg(user.id):
            return JSONResponse({"error": t(lang, "err_try_again")})
        target = user.id
    elif purpose == "reset":
        async with Session() as s:
            u = (await s.scalars(select(User).where(User.login == shop_accounts.norm_login(data.get("login") or "")))).first()
        if not u:
            return JSONResponse({"error": t(lang, "reset_unknown")})
        if not shop_accounts.is_tg(u.id):
            return JSONResponse({"error": t(lang, "reset_no_tg")})
        target = u.id
    elif purpose != "login":
        return JSONResponse({"error": t(lang, "err_try_again")})
    bot = await _bot_username()
    if not bot:
        return JSONResponse({"error": t(lang, "err_try_again")})
    a = shop_accounts.new_auth(purpose, request.headers.get("user-agent", ""), target,
                               invite=request.cookies.get(user_refs.COOKIE),
                               country=request.headers.get("cf-ipcountry"))
    request.session["shop_auth"] = a.token  # результат получит только этот браузер
    return JSONResponse({"token": a.token, "url": f"https://t.me/{bot}?start={shop_accounts.AUTH_PREFIX}{a.token}"})


@router.get(P + "/tg/status")
async def tg_status(request: Request, token: str = ""):
    lang = lang_of(request)
    if token != request.session.get("shop_auth"):
        return JSONResponse({"status": "denied", "message": t(lang, "tg_expired")})
    a = shop_accounts.auth(token)
    if a is None:
        return JSONResponse({"status": "denied", "message": t(lang, "tg_expired")})
    if a.status == "pending":
        return JSONResponse({"status": "pending"})
    request.session.pop("shop_auth", None)
    if a.status == "denied":
        msg = {"expired": "tg_expired", "merge_tg_login": "tg_merge_login"}.get(a.error or "", "tg_denied")
        return JSONResponse({"status": "denied", "message": t(lang, msg)})
    if a.purpose == "login":
        user = await shop_accounts.get(a.tg_id)
        if not user or user.is_blocked:
            return JSONResponse({"status": "denied", "message": t(lang, "tg_denied")})
        sign_in(request, user)
        return JSONResponse({"status": "ok", "next": safe_next(request.query_params.get("next")) if
                             request.query_params.get("next") else P})
    if a.purpose == "link":
        user = await shop_accounts.get(a.tg_id)
        sign_in(request, user)  # теперь это пользователь бота
        flash(request, t(lang, "tg_linked_ok") + (" " + t(lang, "tg_linked_key", old=a.note) if a.note else ""))
        nxt = request.query_params.get("next")
        return JSONResponse({"status": "ok", "next": safe_next(nxt) if nxt else f"{P}/profile"})
    request.session["shop_reset"] = [a.user_id, int(time.time())]  # reset: можно задать новый пароль (RESET_TTL)
    return JSONResponse({"status": "ok", "next": f"{P}/reset"})


RESET_TTL = 600


def _reset_uid(request: Request) -> int | None:
    """Чей пароль можно задать в этом браузере: сброс подтверждён в боте не раньше RESET_TTL назад."""
    data = request.session.get("shop_reset")
    if isinstance(data, list) and len(data) == 2 and isinstance(data[0], int) and time.time() - data[1] < RESET_TTL:
        return data[0]
    request.session.pop("shop_reset", None)
    return None


@router.get(P + "/reset", response_class=HTMLResponse)
async def reset_page(request: Request):
    return render(request, "login.html", None, next=P, mode="reset", allowed=_reset_uid(request) is not None)


@router.post(P + "/reset")
async def reset(request: Request, password: str = Form(...), password2: str = Form(...), csrf: str = Form("")):
    lang = lang_of(request)
    uid = _reset_uid(request)
    if not csrf_ok(request, csrf) or uid is None:
        return _bad_csrf(request, f"{P}/reset")
    if password != password2:
        return render(request, "login.html", None, next=P, mode="reset", allowed=True, error=t(lang, "err_password_match"))
    try:
        await shop_accounts.change_password(uid, None, password)
    except shop_accounts.AccountError as e:
        return render(request, "login.html", None, next=P, mode="reset", allowed=True, error=t(lang, str(e)))
    request.session.pop("shop_reset", None)
    sign_in(request, await shop_accounts.get(uid))
    flash(request, t(lang, "reset_done"))
    return back(f"{P}/profile")


# ---------------------------------------------------------------- профиль


@router.get(P + "/profile", response_class=HTMLResponse)
async def profile_page(request: Request):
    user, redirect = await _need_user(request)
    if redirect:
        return redirect
    new_key = request.session.pop("shop_new_key", None)
    return render(request, "profile.html", user, active="profile", new_key=new_key,
                  can_api=await user_refs.has_topped_up(user.id))


@router.post(P + "/profile/credentials")
async def set_credentials(request: Request, login_: str = Form(..., alias="login"), password: str = Form(...),
                          password2: str = Form(...), csrf: str = Form("")):
    user, redirect = await _need_user(request)
    if redirect:
        return redirect
    lang = lang_of(request)
    if not csrf_ok(request, csrf):
        return _bad_csrf(request, f"{P}/profile")
    if password != password2:
        flash(request, t(lang, "err_password_match"), "bad")
        return back(f"{P}/profile")
    try:
        await shop_accounts.set_credentials(user.id, login_, password)
    except shop_accounts.AccountError as e:
        flash(request, t(lang, str(e)), "bad")
        return back(f"{P}/profile")
    flash(request, t(lang, "credentials_set"))
    return back(f"{P}/profile")


@router.post(P + "/profile/password")
async def change_password(request: Request, old: str = Form(...), password: str = Form(...),
                          password2: str = Form(...), csrf: str = Form("")):
    user, redirect = await _need_user(request)
    if redirect:
        return redirect
    lang = lang_of(request)
    if not csrf_ok(request, csrf):
        return _bad_csrf(request, f"{P}/profile")
    if password != password2:
        flash(request, t(lang, "err_password_match"), "bad")
        return back(f"{P}/profile")
    if not ratelimit.allow(f"shop-pw:{user.id}", 10, 900):
        flash(request, t(lang, "err_rate"), "bad")
        return back(f"{P}/profile")
    try:
        await shop_accounts.change_password(user.id, old, password)
    except shop_accounts.AccountError as e:
        flash(request, t(lang, str(e)), "bad")
        return back(f"{P}/profile")
    sign_in(request, await shop_accounts.get(user.id))  # этот вход остаётся, остальные закрываются
    flash(request, t(lang, "password_changed"))
    return back(f"{P}/profile")


@router.post(P + "/profile/apikey")
async def api_key(request: Request, csrf: str = Form("")):
    user, redirect = await _need_user(request)
    if redirect:
        return redirect
    lang = lang_of(request)
    if not csrf_ok(request, csrf):
        return _bad_csrf(request, f"{P}/profile")
    try:
        request.session["shop_new_key"] = await shop_accounts.new_api_key(user.id)
    except shop_accounts.AccountError as e:
        flash(request, t(lang, str(e)), "bad")
    return back(f"{P}/profile#api")


# ---------------------------------------------------------------- справка


@router.get(P + "/api", response_class=HTMLResponse)
async def api_docs(request: Request):
    return render(request, "api_docs.html", await current_user(request), active="api")


@router.get(P + "/faq", response_class=HTMLResponse)
async def faq_page(request: Request):
    return render(request, "faq.html", await current_user(request), active="faq")

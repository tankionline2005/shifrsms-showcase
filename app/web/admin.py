import asyncio
import secrets
import time
from datetime import datetime, timedelta
from decimal import ROUND_DOWN, Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import delete, func, or_, select
from sqlalchemy import update as sa_update

from app import accounts, catalog, country_match, owners, payments, payouts, pricing, quality, reconcile, totp, tron, wallet
from app import settings_store as st
from app import broadcasts, menu_images, partner_signup, referrals, textfmt, tgcheck, thumbs, user_refs, utm
from app.bot import channels, notify, support
from app.bot.ui import SERVICES_PAGE, rub
from app.config import config

A = config.admin_url_path  # секретный адрес админки (ADMIN_PATH в .env)
from app.db import Session, utcnow
from app.models import (
    NOT_POPULAR_SORT, AuditLog, BotChat, Broadcast, ChainTransfer, Country, DiscardedOrder, CountryLink, Order, Owner,
    Payout, Quarantine, Service, ServiceCountry, ServiceLink, TgChecker, Transaction, Payment, Referral, User,
    WebhookLog,
)
from app.providers import PROVIDERS, enabled
from app.web import channel_pages, localtime, money_pages, ratelimit, tables

router = APIRouter(prefix=A)
templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.globals["A"] = A  # начало адресов страниц: админка или кабинет реферала
templates.env.filters["rub"] = rub
# время — в поясе того, кто открыл панель (app/web/localtime.py); фильтр по старой памяти называется msk
templates.env.filters["msk"] = localtime.fmt
templates.env.globals["tz_label"] = localtime.label
TX_KINDS = {"purchase": "Покупка", "refund": "Возврат", "admin": "Админ", "topup": "Пополнение", "referral": "За приглашённого",
            "chargeback": "Возврат платежа", "topup_test": "Тестовое пополнение"}
templates.env.filters["tx_kind"] = lambda kind: TX_KINDS.get(kind, kind)
templates.env.filters["usd"] = channel_pages.money_usd
templates.env.filters["ver"] = thumbs.version  # метка версии картинки для её адреса


class NeedLogin(Exception):
    pass


async def require_admin(request: Request) -> None:
    """Вход владельца (пароль + код аутентификатора) или старый общий admin — пока он не закрыт."""
    owner_id = request.session.get("owner")
    if isinstance(owner_id, int):
        async with Session() as s:
            owner = await s.get(Owner, owner_id)
        if owner and owner.totp_secret and request.session.get("owner_v") == (owner.session_ver or 1):
            request.state.owner = owner
            request.state.me = {"login": owner.login,
                                "balance": await accounts.balance(accounts.Acc("owner", owner.id))}
            return
        request.session.pop("owner", None)
    if (request.session.get("admin") == config.admin_username and config.admin_password
            and await owners.admin_login_allowed()):
        request.state.owner = None
        return
    request.session.pop("admin", None)
    raise NeedLogin()


class NeedOwner(Exception):
    pass


async def require_owner(request: Request) -> None:
    """Деньги — только владельцам (у них есть аутентификатор), не старому общему admin."""
    await require_admin(request)
    if not request.state.owner:
        raise NeedOwner()


def actor(request: Request) -> str:
    owner = getattr(request.state, "owner", None)
    return owner.login if owner else "admin"


# Страницы с живыми данными — обновляются в браузере сами (секунды).
# На страницах с формами редактирования автообновления нет, чтобы не терять ввод.
AUTO_REFRESH = {"dashboard.html": 20, "orders.html": 20, "payments.html": 20, "users.html": 20, "user.html": 20,
                "user_orders.html": 20, "user_transactions.html": 20, "user_payments.html": 20,
                "webhooks.html": 20, "quarantine.html": 20, "quality.html": 60}


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(request, name, {"active": name.split(".")[0],
                                                      "refresh": AUTO_REFRESH.get(name, 0), **ctx})


def parse_amount(raw: str, allow_negative: bool) -> Decimal | None:
    """Число из формы. Decimal принимает «NaN» и «Infinity» — такие значения сломали бы
    расчёт цен и балансы, поэтому отсекаем их, а также слишком большие."""
    try:
        value = Decimal(raw.replace(",", ".").replace(" ", "").strip())
    except InvalidOperation:
        return None
    if not value.is_finite() or abs(value) > Decimal("10000000"):
        return None
    if value < 0 and not allow_negative:
        return None
    return Decimal(format(value.quantize(Decimal("0.0001")).normalize(), "f"))


def back(request: Request, default: str) -> RedirectResponse:
    return RedirectResponse(request.headers.get("referer") or default, status_code=303)


# ------------------------------------------------------------------ вход


@router.get("/login")
async def login_page(request: Request):
    return render(request, "login.html", error=None)


PENDING_TTL = 600  # пароль верный — 10 минут на код аутентификатора


@router.post("/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    keys = (f"admin-ip:{ratelimit.ip(request)}", f"admin-login:{username.strip().lower()}")
    if ratelimit.blocked(*keys):
        await asyncio.sleep(1)
        return render(request, "login.html", error=ratelimit.MESSAGE)
    owner = await owners.check_password(username, password)
    if owner:
        request.session.clear()
        request.session["pending"], request.session["pending_at"] = owner.id, time.time()
        return RedirectResponse(A + ("/login/code" if owner.totp_secret else "/login/setup"), status_code=303)
    legacy = (config.admin_password and secrets.compare_digest(username, config.admin_username)
              and secrets.compare_digest(password, config.admin_password))
    if legacy and not await owners.admin_login_allowed():
        return render(request, "login.html", error="Общий вход admin закрыт — войдите своим аккаунтом.")
    if not legacy:
        ratelimit.failed(*keys)
        await asyncio.sleep(1)
        return render(request, "login.html", error="Неверный логин или пароль")
    request.session.clear()
    request.session["admin"] = config.admin_username
    return RedirectResponse(A, status_code=303)


async def _pending(request: Request) -> Owner | None:
    owner_id = request.session.get("pending")
    if not isinstance(owner_id, int) or time.time() - request.session.get("pending_at", 0) > PENDING_TTL:
        return None
    async with Session() as s:
        return await s.get(Owner, owner_id)


def _signed_in(request: Request, owner: Owner) -> None:
    request.session.clear()
    request.session["owner"], request.session["owner_v"] = owner.id, owner.session_ver or 1


@router.get("/login/code")
async def login_code_page(request: Request):
    owner = await _pending(request)
    if not owner or not owner.totp_secret:
        return RedirectResponse(A + "/login", status_code=303)
    return render(request, "login_code.html", login=owner.login, error=None)


@router.post("/login/code")
async def login_code(request: Request, code: str = Form("")):
    owner = await _pending(request)
    if not owner or not owner.totp_secret:
        return RedirectResponse(A + "/login", status_code=303)
    if error := await accounts.check(accounts.Acc("owner", owner.id), code):
        await asyncio.sleep(1)
        return render(request, "login_code.html", login=owner.login, error=error)
    _signed_in(request, owner)
    return RedirectResponse(A, status_code=303)


@router.get("/login/setup")
async def login_setup_page(request: Request, error: str = ""):
    owner = await _pending(request)
    if not owner or owner.totp_secret:
        return RedirectResponse(A + "/login", status_code=303)
    return render(request, "login_setup.html", login=owner.login, error=error,
                  **money_pages.setup_ctx(request, owner.login))


@router.post("/login/setup")
async def login_setup(request: Request, code: str = Form("")):
    owner = await _pending(request)
    secret = request.session.get("totp_new")
    if not owner or owner.totp_secret or not secret:
        return RedirectResponse(A + "/login", status_code=303)
    codes = await accounts.bind(accounts.Acc("owner", owner.id), secret, code)
    if codes is None:
        return RedirectResponse(A + "/login/setup?error=" + quote_plus(
            "Код не подошёл. Проверьте, что время на телефоне точное, и введите свежий код."), status_code=303)
    await accounts.audit(owner.login, "привязал аутентификатор и вошёл впервые")
    _signed_in(request, owner)
    return render(request, "login_codes.html", login=owner.login, codes=codes)


@router.get("/logout")
async def logout(request: Request):
    """Выход — на всех устройствах: версия входа владельца растёт, прежние cookie больше не пускают."""
    owner_id = request.session.get("owner")
    if isinstance(owner_id, int) and request.session.get("owner_v") is not None:
        async with Session() as s:
            owner = await s.get(Owner, owner_id)
        if owner and request.session.get("owner_v") == (owner.session_ver or 1):  # выходит сам владелец
            await owners.sign_out_everywhere(owner_id)
            await accounts.audit(owner.login, "вышел (на всех устройствах)")
    request.session.clear()
    return RedirectResponse(A + "/login", status_code=303)


# ------------------------------------------------------------------ сводка


ANALYTICS_PERIODS = (7, 30, 90)
PAID = ("received", "completed")  # SMS пришла — деньги остались у нас; прочие заказы возвращены


def _totals(rows) -> dict:
    paid = [r for r in rows if r.status in PAID]
    revenue = sum((r.price for r in paid), Decimal(0))
    cost = sum((r.cost_usd * r.usd_rate for r in paid), Decimal(0))
    return {"started": len(rows), "ok": len(paid), "revenue": revenue, "cost": cost,
            "profit": revenue - cost, "avg": revenue / len(paid) if paid else None,
            "margin": (revenue - cost) / revenue * 100 if revenue else None,
            "rate": len(paid) * 100 / len(rows) if rows else None}


def _delta(cur, prev) -> float | None:
    """Изменение к прошлому периоду, %; None — сравнивать не с чем."""
    if cur is None or not prev:
        return None
    return float((cur - prev) / abs(prev) * 100)


def _top(rows, key, label, limit: int = 7) -> list[dict]:
    groups: dict = {}
    for r in rows:
        groups.setdefault(key(r), []).append(r)
    top = [{"name": label(k), **_totals(rs)} for k, rs in groups.items()]
    top = [t for t in top if t["ok"]]
    return sorted(top, key=lambda t: (-t["profit"], -t["ok"]))[:limit]


async def _analytics(days: int, since: datetime | None = None) -> dict:
    """Продажи за последние days календарных дней (в поясе смотрящего, включая сегодня) и такой же период до них.
    since — начало учёта прибыли: более ранние заказы (тесты до релиза) не считаются."""
    first = localtime.today() - timedelta(days=days - 1)
    start = localtime.day_start_utc(first)  # местная полночь в UTC
    prev_start = start - timedelta(days=days)
    async with Session() as s:
        rows = (await s.execute(select(Order.created_at, Order.status, Order.price, Order.cost_usd,
                                       Order.usd_rate, Order.service_id, Order.country_id)
                                .where(Order.created_at >= max(prev_start, since or prev_start)))).all()
        lost_n, lost_usd = (await s.execute(
            select(func.count(), func.coalesce(func.sum(DiscardedOrder.cost_usd), 0))
            .where(DiscardedOrder.lost.is_(True), DiscardedOrder.created_at >= max(start, since or start)))).one()
    cur = [r for r in rows if r.created_at >= start]
    cur_t, prev_t = _totals(cur), _totals([r for r in rows if r.created_at < start])

    by_day: dict = {first + timedelta(days=i): [] for i in range(days)}
    for r in cur:
        by_day.setdefault(localtime.local(r.created_at).date(), []).append(r)
    daily = []
    for day, rs in by_day.items():
        t = _totals(rs)
        daily.append({"label": day.strftime("%d.%m"), "revenue": float(t["revenue"]),
                      "cost": float(t["cost"]), "profit": float(t["profit"]),
                      "ok": t["ok"], "failed": t["started"] - t["ok"]})

    def service(i):
        svc = catalog.SERVICES.get(i)
        return svc.name if svc else f"#{i}"

    def country(i):
        c = catalog.COUNTRIES.get(i)
        return c.title if c else f"#{i}"

    return {
        "days": days, "total": cur_t, "daily": daily,
        # номера, которые не отдали пользователю и не смогли отменить у площадки
        "lost": {"n": lost_n, "usd": Decimal(lost_usd),
                 "rub": Decimal(lost_usd) * pricing.effective_rate()} if lost_n else None,
        "deltas": {k: _delta(cur_t[k], prev_t[k]) for k in ("revenue", "profit", "ok", "avg")},
        "top_services": _top(cur, lambda r: r.service_id, service),
        "top_countries": _top(cur, lambda r: r.country_id, country),
        "invites": await user_refs.total(max(start, since or start)),  # начислено пользователям за приглашённых
    }


@router.get("", dependencies=[Depends(require_admin)])
async def dashboard(request: Request, days: int = 30):
    days = days if days in ANALYTICS_PERIODS else 30
    # балансы из кэша (обновляется раз в минуту), чтобы автообновление не дёргало площадки
    if not catalog.balances:
        await catalog.refresh_balances()
    balances = {n: f"${catalog.balances[n]:.2f}" if n in catalog.balances else "нет данных"
                for n in PROVIDERS}
    since = owners.profit_since()  # «Начать учёт прибыли» на «Сверке»: заказы до него в «Сводке» не считаем
    async with Session() as s:
        users = await s.scalar(select(func.count()).select_from(User))
        active = select(func.count()).select_from(Order).where(Order.status.in_(("waiting", "received")))
        active_orders = await s.scalar(active.where(Order.created_at >= since) if since else active)
    return render(request, "dashboard.html", balances=balances, users=users, active_orders=active_orders,
                  stats=catalog.stats(), last_sync=catalog.last_sync, providers=PROVIDERS,
                  enabled_providers=set(enabled()), since=since,
                  rate=pricing.effective_rate(), a=await _analytics(days, since), periods=ANALYTICS_PERIODS)


@router.post("/sync", dependencies=[Depends(require_admin)])
async def sync_now(request: Request):
    asyncio.create_task(catalog.sync_all())
    return back(request, A)


# ------------------------------------------------------------------ настройки


async def support_chats() -> list[tuple[str, str]]:
    """Группы, где бот администратор, — выбор группы поддержки."""
    async with Session() as s:
        rows = (await s.scalars(select(BotChat).where(BotChat.type == "supergroup",
                                                      BotChat.status.in_(("administrator", "creator")))
                                .order_by(BotChat.title))).all()
    return [(str(c.id), c.title + (" (темы выключены)" if c.is_forum is False else "")) for c in rows]


@router.get("/settings", dependencies=[Depends(require_admin)])
async def settings_page(request: Request, saved: int = 0, image_error: str = "", error: str = ""):
    values = {d.key: st.raw(d.key) for d in st.DEFS}
    chats = await support_chats()
    if values["support_chat"] and values["support_chat"] not in {k for k, _ in chats}:
        chats.append((values["support_chat"], f"ID {values['support_chat']} — бота там больше нет"))
    return render(request, "settings.html", groups=st.grouped(), values=values, saved=saved, error=error,
                  chat_choices={"support_chat": [("", "Не выбрана — кнопки «Поддержка» нет")] + chats},
                  menu_items=menu_images.ITEMS,
                  menu_ver={k: thumbs.version(await menu_images.thumb(k)) for k in menu_images.ITEMS},
                  image_error=image_error,
                  example=[(c, pricing.sale_price(Decimal(c))) for c in ("0.01", "0.03", "0.05", "0.20", "0.50", "1.00")])


@router.post("/settings", dependencies=[Depends(require_admin)])
async def settings_save(request: Request):
    form = await request.form()
    values, errors = {}, []  # поле с ошибкой не сохраняется, остальные — сохраняются
    for d in st.DEFS:
        if d.secure:
            continue  # настройки денег — только на «Сверке», с кодом аутентификатора
        if d.type == "bool":
            values[d.key] = "1" if form.get(d.key) else "0"
        elif d.key in form:
            v = str(form[d.key]).strip()
            if d.type == "decimal":
                num = parse_amount(v, allow_negative=False)
                if num is None:
                    continue  # NaN, бесконечность, отрицательное — оставляем прежнее значение
                v = str(num)
            elif d.type == "int":
                if not v.isdigit():
                    continue
            elif d.type == "users":  # получатели уведомлений: только те, кто писал боту
                found, missing = await notify.find_users(notify.parse_users(v))
                if missing:
                    errors.append(f"{d.label.lower()} — не нашёл среди пользователей бота "
                                  + ", ".join("@" + m for m in missing)
                                  + ". Пусть каждый сначала напишет боту /start; проверьте написание @username")
                    continue
                v = ", ".join(f"@{u.username}" if u.username else str(u.id) for u in found)
            elif d.type == "chat" and v and v != st.raw(d.key):
                if not v.lstrip("-").isdigit():
                    continue
                if error := await support.check_chat(channels.bot, int(v)):
                    errors.append(f"{d.label.lower()} — {error}")
                    continue
            values[d.key] = v
    await st.save(values)
    await catalog.reload_meta()  # запрещённые страны, включение площадок меняют каталог
    if errors:
        return RedirectResponse(f"{A}/settings?error=" + quote_plus("; ".join(errors)), status_code=303)
    return RedirectResponse(A + "/settings?saved=1", status_code=303)


@router.post("/settings/menu-image/{key}", dependencies=[Depends(require_admin)])
async def menu_image_upload(request: Request, key: str, photo: UploadFile | None = File(None)):
    """Картинку выбрали — сразу сохраняем. Со страницы (X-Upload) — ответ JSON с адресами превью и исходника."""
    xhr = bool(request.headers.get("x-upload"))
    if key not in menu_images.ITEMS:
        return JSONResponse({"error": "Нет такого пункта меню."}) if xhr else RedirectResponse(f"{A}/settings", 303)
    data, kind, thumb, error = await channel_pages.read_image(photo)
    if error:
        return JSONResponse({"error": error}) if xhr else \
            RedirectResponse(f"{A}/settings?image_error={quote_plus(error)}#menu-images", status_code=303)
    await menu_images.put(key, data, kind, thumb)
    if xhr:
        return JSONResponse(channel_pages.photo_urls(f"{A}/settings/menu-image/{key}", thumb))
    return RedirectResponse(f"{A}/settings#menu-images", status_code=303)


@router.post("/settings/menu-image/{key}/delete", dependencies=[Depends(require_admin)])
async def menu_image_delete(request: Request, key: str):
    await menu_images.remove(key)
    if request.headers.get("x-upload"):
        return JSONResponse({"ok": True})
    return RedirectResponse(f"{A}/settings#menu-images", status_code=303)


@router.get("/settings/menu-image/{key}", dependencies=[Depends(require_admin)])
async def menu_image(request: Request, key: str, size: str = ""):
    """size=thumb — лёгкое превью для страницы, без него — исходник."""
    img = menu_images.get(key)
    if not img:
        return Response(status_code=404)
    if size == "thumb" and (thumb := await menu_images.thumb(key)):
        return channel_pages.image_response(request, thumb, "image/webp")
    return channel_pages.image_response(request, img.photo, img.photo_type)


# ------------------------------------------------------------------ сервисы


async def service_popularity(refresh: bool = True) -> dict[int, dict]:
    """Популярность сервиса — доля во всех активациях за catalog.POPULARITY_DAYS дней.
    -> {service_id: {"n", "pct", "title"}}. refresh=False — без пересчёта (подгрузка
    страницы должна продолжить тот же порядок, что был при её открытии)."""
    if refresh:
        await catalog.refresh_popularity()
    total = sum(catalog.popularity.values())
    result = {}
    for sid, n in catalog.popularity.items():
        share = n * 100 / total
        result[sid] = {"n": n, "pct": f"{share:.0f}%" if share >= 1 else "<1%",
                       "title": f"{n} из {total} активаций за {catalog.POPULARITY_DAYS} дней"}
    return result


SERVICES_FIRST, SERVICES_MORE = 50, 100  # сервисов без флагов: сразу / за одну подгрузку


async def _services_split(q: str) -> tuple[list[Service], list[Service], list[Service]]:
    """-> (популярные по «Сорт.», скрытые, остальные по популярности — самые покупаемые первыми)."""
    async with Session() as s:
        query = select(Service)
        if q:
            query = query.where(or_(Service.name.ilike(f"%{q}%"), Service.key.ilike(f"%{q}%")))
        services = (await s.scalars(query)).all()
    popular = sorted((x for x in services if x.is_popular), key=lambda x: (x.sort, x.name.lower()))
    hidden = sorted((x for x in services if x.is_hidden and not x.is_popular), key=lambda x: x.name.lower())
    # при равной популярности (чаще всего 0) выше те, у кого сейчас есть номера
    others = sorted((x for x in services if not x.is_popular and not x.is_hidden),
                    key=lambda x: (-catalog.popularity.get(x.id, 0), -_countries_count(x.id),
                                   x.name.lower()))
    return popular, hidden, others


async def _service_rows(services: list[Service]) -> list[dict]:
    """Строки таблицы — цены и страны считаются только для показанных сервисов."""
    async with Session() as s:
        links = (await s.scalars(select(ServiceLink).where(
            ServiceLink.service_id.in_([x.id for x in services])))).all()
    by_service: dict[int, list[ServiceLink]] = {}
    for link in links:
        by_service.setdefault(link.service_id, []).append(link)
    rows = []
    for svc in services:
        countries = catalog.countries_for(svc.id)
        rows.append({"svc": svc, "links": by_service.get(svc.id, []), "countries": len(countries),
                     "min_price": min((q.price for _c, q in countries), default=None)})
    return rows


@router.get("/services", dependencies=[Depends(require_admin)])
async def services_page(request: Request, q: str = ""):
    pop = await service_popularity()
    popular, hidden, others = await _services_split(q)
    return render(request, "services.html", q=q, pop=pop, pop_days=catalog.POPULARITY_DAYS,
                  total=len(popular) + len(hidden) + len(others),
                  popular=await _service_rows(popular), hidden_total=len(hidden),
                  # при поиске найденные скрытые показываем сразу, без сворачивания
                  hidden=await _service_rows(hidden) if q else [],
                  others=await _service_rows(others[:SERVICES_FIRST]),
                  others_total=len(others), shown=SERVICES_FIRST, step=SERVICES_MORE)


@router.get("/services/hidden", dependencies=[Depends(require_admin)])
async def services_hidden(request: Request, q: str = ""):
    """Скрытые сервисы — группа свёрнута по умолчанию и загружается по клику."""
    _p, hidden, _o = await _services_split(q)
    return render(request, "_service_rows.html", rows=await _service_rows(hidden),
                  pop=await service_popularity(refresh=False))


@router.get("/services/more", dependencies=[Depends(require_admin)])
async def services_more(request: Request, offset: int = 0, q: str = ""):
    """Следующие сервисы без флагов — подгружаются, когда админ долистал до конца страницы."""
    _p, _h, others = await _services_split(q)
    page = others[max(0, offset):max(0, offset) + SERVICES_MORE]
    return render(request, "_service_rows.html", rows=await _service_rows(page),
                  pop=await service_popularity(refresh=False))


@router.post("/services/{service_id}", dependencies=[Depends(require_admin)])
async def service_save(request: Request, service_id: int, name: str = Form(...),
                       sort: int = Form(100), is_popular: str | None = Form(None),
                       is_hidden: str | None = Form(None), highlight: str | None = Form(None), note: str = Form(""),
                       note_sms: str = Form(""), instruction: str = Form("")):
    async with Session() as s:
        svc = await s.get(Service, service_id)
        if svc:
            svc.name = name.strip()[:128] or svc.name
            svc.note = note.strip()[:1000] or None
            svc.note_sms = note_sms.strip()[:1000] or None
            svc.instruction = instruction.strip()[:512] or None
            svc.is_popular = bool(is_popular)
            svc.sort = sort if svc.is_popular else NOT_POPULAR_SORT
            svc.is_hidden = bool(is_hidden)
            svc.highlight = bool(highlight)
            await s.commit()
    await catalog.reload_meta()
    return back(request, A + "/services")


def _countries_count(service_id: int) -> int:
    """Сколько стран с номерами у сервиса — быстро, по индексу каталога (без расчёта цен)."""
    cids: set[int] = set()
    for name in PROVIDERS:
        cids |= catalog._index.get(name, {}).get(service_id, set())
    return len(cids)


@router.get("/popular", dependencies=[Depends(require_admin)])
async def popular_page(request: Request):
    visible = catalog._visible_service_ids()
    pop = await service_popularity()
    popular = sorted((s for s in catalog.SERVICES.values() if s.is_popular and not s.is_hidden),
                     key=lambda s: (s.sort, s.name.lower()))
    items = [{"id": s.id, "name": s.name, "visible": s.id in visible, "share": pop.get(s.id), "highlight": s.highlight}
             for s in popular]
    others = sorted(({"id": s.id, "name": s.name, "countries": _countries_count(s.id),
                      "share": pop.get(s.id)}
                     for s in catalog.SERVICES.values() if not s.is_popular and not s.is_hidden),
                    key=lambda x: x["name"].lower())
    # кандидаты в популярные — сервисы вне списка, которые берут чаще всего
    candidates = sorted((x for x in others if x["share"]), key=lambda x: -x["share"]["n"])[:8]
    return render(request, "popular.html", items=items, others=others, candidates=candidates,
                  page_size=SERVICES_PAGE, pop_days=catalog.POPULARITY_DAYS)


@router.post("/popular", dependencies=[Depends(require_admin)])
async def popular_save(request: Request):
    body = await request.json()
    ids = [int(x) for x in body.get("ids", []) if str(x).isdigit()]
    order = {sid: i for i, sid in enumerate(dict.fromkeys(ids))}  # без повторов, порядок сохранён
    async with Session() as s:
        for svc in (await s.scalars(select(Service).where(
                or_(Service.is_popular.is_(True), Service.id.in_(list(order)))))).all():
            if svc.id in order:
                svc.is_popular, svc.sort = True, order[svc.id]
            else:
                svc.is_popular, svc.sort = False, NOT_POPULAR_SORT
        await s.commit()
    await catalog.reload_meta()
    return {"ok": True, "count": len(order)}


@router.get("/services/{service_id}/countries", dependencies=[Depends(require_admin)])
async def service_countries_page(request: Request, service_id: int, saved: int = 0):
    async with Session() as s:
        svc = await s.get(Service, service_id)
        if not svc:
            return RedirectResponse(A + "/services", status_code=303)
        countries = (await s.scalars(select(Country).order_by(Country.name))).all()
        chosen = set((await s.scalars(select(ServiceCountry.country_id)
                                      .where(ServiceCountry.service_id == service_id))).all())
    # цены без учёта ограничения — чтобы было видно, где номера вообще есть
    saved_allowed = catalog.ALLOWED.pop(service_id, None)
    try:
        prices = {c.id: q.price for c, q in catalog.countries_for(service_id)}
    finally:
        if saved_allowed is not None:
            catalog.ALLOWED[service_id] = saved_allowed
    rows = [{"c": c, "on": c.id in chosen, "price": prices.get(c.id)} for c in countries]
    rows.sort(key=lambda r: (not r["on"], r["price"] is None, r["c"].name))
    return render(request, "service_countries.html", svc=svc, rows=rows, saved=saved,
                  chosen=len(chosen), with_offers=len(prices))


@router.post("/services/{service_id}/countries", dependencies=[Depends(require_admin)])
async def service_countries_save(request: Request, service_id: int):
    form = await request.form()
    ids = {int(v) for v in form.getlist("country") if str(v).isdigit()}
    async with Session() as s:
        svc = await s.get(Service, service_id)
        if not svc:
            return RedirectResponse(A + "/services", status_code=303)
        svc.country_filter = bool(form.get("country_filter"))
        await s.execute(delete(ServiceCountry).where(ServiceCountry.service_id == service_id))
        existing = set((await s.scalars(select(Country.id).where(Country.id.in_(ids)))).all())
        s.add_all([ServiceCountry(service_id=service_id, country_id=cid) for cid in existing])
        await s.commit()
    await catalog.reload_meta()
    return RedirectResponse(f"{A}/services/{service_id}/countries?saved=1", status_code=303)


@router.post("/countries/match", dependencies=[Depends(require_admin)])
async def countries_match(request: Request):
    """Распознать вставленный список стран -> какие отметить."""
    body = await request.json()
    text = str(body.get("text", ""))[:50_000]
    async with Session() as s:
        countries = (await s.scalars(select(Country))).all()
        links = (await s.execute(select(CountryLink.country_id, CountryLink.provider_name))).all()
    names: dict[int, list[str]] = {}
    for cid, pname in links:
        if cid:
            names.setdefault(cid, []).append(pname)
    refs = [country_match.CountryRef(c.id, c.key, c.name, names.get(c.id, [])) for c in countries]
    matches, unknown, absent = country_match.match(text, refs)
    return {
        "matched": [{"id": m.country.id, "line": m.line, "name": m.country.name,
                     "fuzzy": m.how == "fuzzy"} for m in matches],
        "unknown": unknown,
        "absent": absent,
    }


@router.post("/services/{service_id}/merge", dependencies=[Depends(require_admin)])
async def service_merge(request: Request, service_id: int, target_id: int = Form(...)):
    """Перенести все коды площадок этого сервиса в сервис target_id."""
    async with Session() as s:
        target = await s.get(Service, target_id)
        links = (await s.scalars(select(ServiceLink).where(ServiceLink.service_id == service_id))).all()
        source = await s.get(Service, service_id)
        if not target or not source or target_id == service_id:
            return back(request, A + "/services")
        source.is_hidden = True
        await s.commit()
    for l in links:
        await catalog.relink_service(l.provider, l.code, target_id)
    await catalog.reload_meta()
    return back(request, A + "/services")


# ------------------------------------------------------------------ страны


@router.get("/countries", dependencies=[Depends(require_admin)])
async def countries_page(request: Request, q: str = ""):
    async with Session() as s:
        query = select(Country).order_by(Country.name)
        if q:
            query = query.where(or_(Country.name.ilike(f"%{q}%"), Country.key.ilike(f"%{q}%")))
        countries = (await s.scalars(query)).all()
    return render(request, "countries.html", countries=countries, q=q,
                  blocked=catalog.blocked_countries())


@router.post("/countries/{country_id}", dependencies=[Depends(require_admin)])
async def country_save(request: Request, country_id: int, name: str = Form(...),
                       is_hidden: str | None = Form(None)):
    async with Session() as s:
        c = await s.get(Country, country_id)
        if c:
            c.name = name.strip()[:128] or c.name
            c.is_hidden = bool(is_hidden)
            await s.commit()
    await catalog.reload_meta()
    return back(request, A + "/countries")


# ------------------------------------------------------------------ заказы и пользователи


LAZY_STEP = 50  # строк таблицы сразу и за одну подгрузку при прокрутке (шаблон _lazy.html)


def lazy(request: Request, offset: int, part: int, page: str, rows_template: str, **ctx) -> HTMLResponse:
    """part=1 — только следующие строки таблицы (подгрузка при прокрутке), иначе — вся страница."""
    if part:
        return render(request, rows_template, **ctx)
    return render(request, page, step=LAZY_STEP, **ctx)


@router.get("/orders", dependencies=[Depends(require_admin)])
async def orders_page(request: Request, status: str = "", user_id: int | None = None, offset: int = 0, part: int = 0):
    async with Session() as s:
        q = select(Order).order_by(Order.id.desc()).offset(max(0, offset)).limit(LAZY_STEP)
        if status:
            q = q.where(Order.status == status)
        if user_id:
            q = q.where(Order.user_id == user_id)
        rows = (await s.scalars(q)).all()
    return lazy(request, offset, part, "orders.html", "_order_rows.html", orders=rows, status=status)


@router.get("/users", dependencies=[Depends(require_admin)])
async def users_page(request: Request, q: str = "", offset: int = 0, part: int = 0):
    async with Session() as s:
        query = select(User).order_by(User.created_at.desc(), User.id.desc()).offset(max(0, offset)).limit(LAZY_STEP)
        if q.lower().startswith("inviter:") and q[8:].strip().isdigit():  # кого пригласил пользователь
            inviter_q = int(q[8:].strip())
            query = query.where(or_(User.inviter_id == inviter_q, User.inviter2_id == inviter_q))
        elif q.lower().startswith("utm:"):  # пришедшие по метке; без дефиса — по источнику (первой части метки)
            label = q[4:].strip().lower()
            query = query.where(or_(User.utm == label, User.utm.like(label.replace("_", "\\_") + "-%", escape="\\"))
                                if label else User.utm.is_(None))
        elif q:
            cond = [User.username.ilike(f"%{q}%"), User.first_name.ilike(f"%{q}%")]
            if q.isdigit():
                cond.append(User.id == int(q))
            query = query.where(or_(*cond))
        users = (await s.scalars(query)).all()
    return lazy(request, offset, part, "users.html", "_user_rows.html", users=users, q=q)


@router.get("/utm", dependencies=[Depends(require_admin)])
async def utm_page(request: Request, days: int = 30, group: str = "label", label: str = ""):
    days = days if days in dict(QUALITY_PERIODS) else 30
    label = label.strip().lower().removeprefix(utm.PREFIX)
    me = await channels.bot.me() if channels.bot else None
    return render(request, "utm.html", s=await utm.stats(days, group == "source"), periods=QUALITY_PERIODS,
                  f={"days": days, "group": group}, bot=me.username if me else "", label=label,
                  label_error=utm.check(label) if label else None, prefix=utm.PREFIX, max_label=utm.MAX_LABEL)


@router.get("/users/{user_id}", dependencies=[Depends(require_admin)])
async def user_page(request: Request, user_id: int, error: str = "", saved: str = ""):
    async with Session() as s:
        user = await s.get(User, user_id)
        tx_count = await s.scalar(select(func.count()).select_from(Transaction).where(Transaction.user_id == user_id))
        order_count = await s.scalar(select(func.count()).select_from(Order).where(Order.user_id == user_id))
        pay_count = await s.scalar(select(func.count()).select_from(Payment).where(Payment.user_id == user_id))
        # кто привёл: канал-реферал и/или пользователь; после склейки аккаунтов — до двух (доля пополам)
        came_from = []
        for kind, value in referrals.attributions(user):
            if kind == "channel":
                ref = await s.get(Referral, value)
                chat = await s.get(BotChat, ref.chat_id) if ref else None
                if ref:
                    came_from.append(("channel", f"{A}/channels/{ref.chat_id}/referral", chat.title if chat else ref.login))
            else:
                inviter = await s.get(User, value)
                if inviter:
                    came_from.append(("user", f"{A}/users/{inviter.id}",
                                      "@" + inviter.username if inviter.username else inviter.login or str(inviter.id)))
    if not user:
        return RedirectResponse(A + "/users", status_code=303)
    return render(request, "user.html", user=user, tx_count=tx_count, order_count=order_count, pay_count=pay_count,
                  came_from=came_from, invites=await user_refs.stats(user_id), error=error, saved=saved)


@router.get("/users/{user_id}/transactions", dependencies=[Depends(require_admin)])
async def user_transactions(request: Request, user_id: int, offset: int = 0, part: int = 0):
    async with Session() as s:
        user = await s.get(User, user_id)
        txs = (await s.scalars(select(Transaction).where(Transaction.user_id == user_id)
                               .order_by(Transaction.id.desc()).offset(max(0, offset)).limit(LAZY_STEP))).all()
    if not user:
        return RedirectResponse(A + "/users", status_code=303)
    return lazy(request, offset, part, "user_transactions.html", "_tx_rows.html", user=user, txs=txs)


@router.get("/users/{user_id}/orders", dependencies=[Depends(require_admin)])
async def user_orders(request: Request, user_id: int, offset: int = 0, part: int = 0):
    async with Session() as s:
        user = await s.get(User, user_id)
        rows = (await s.scalars(select(Order).where(Order.user_id == user_id)
                                .order_by(Order.id.desc()).offset(max(0, offset)).limit(LAZY_STEP))).all()
    if not user:
        return RedirectResponse(A + "/users", status_code=303)
    return lazy(request, offset, part, "user_orders.html", "_order_rows.html", user=user, orders=rows)


@router.get("/users/{user_id}/payments", dependencies=[Depends(require_admin)])
async def user_payments(request: Request, user_id: int, offset: int = 0, part: int = 0):
    async with Session() as s:
        user = await s.get(User, user_id)
        rows = (await s.scalars(select(Payment).where(Payment.user_id == user_id)
                                .order_by(Payment.id.desc()).offset(max(0, offset)).limit(LAZY_STEP))).all()
    if not user:
        return RedirectResponse(A + "/users", status_code=303)
    return lazy(request, offset, part, "user_payments.html", "_payment_rows.html", user=user, rows=rows,
                statuses=PAYMENT_STATUSES, methods=payments.TITLES)


PAYMENT_STATUSES = {"pending": "Ждёт оплаты", "confirmed": "Оплачен", "canceled": "Отменён",
                    "expired": "Не оплачен", "chargebacked": "Возврат плательщику"}


@router.get("/payments", dependencies=[Depends(require_admin)])
async def payments_page(request: Request, status: str = "", offset: int = 0, part: int = 0):
    async with Session() as s:
        q = select(Payment).order_by(Payment.id.desc()).offset(max(0, offset)).limit(LAZY_STEP)
        if status:
            q = q.where(Payment.status == status)
        rows = (await s.scalars(q)).all()
        ctx = {"rows": rows, "statuses": PAYMENT_STATUSES, "methods": payments.TITLES}
        if part:
            return render(request, "_payment_rows.html", **ctx)
        midnight = localtime.day_start_utc(localtime.today())

        async def paid_since(since):
            n, total = (await s.execute(select(func.count(), func.coalesce(func.sum(Payment.amount), 0))
                                        .where(Payment.status == "confirmed", Payment.paid_at >= since))).one()
            return {"n": n, "sum": Decimal(total)}

        today, month = await paid_since(midnight), await paid_since(utcnow() - timedelta(days=30))
        pending = await s.scalar(select(func.count()).select_from(Payment).where(Payment.status == "pending"))
    return render(request, "payments.html", step=LAZY_STEP, status=status, today=today, month=month, pending=pending,
                  enabled=payments.enabled(), callback_url=f"{config.public_url}/webhooks/platega", **ctx)


@router.post("/users/{user_id}/balance", dependencies=[Depends(require_owner)])
async def user_balance(request: Request, user_id: int, amount: str = Form(...),
                       comment: str = Form(""), code: str = Form("")):
    """Ручное изменение баланса — деньги владельцев (бонус — их расход): только с кодом аутентификатора."""
    page = f"/users/{user_id}"
    value = parse_amount(amount, allow_negative=True)
    if value is None or value == 0:
        return _to(page, "Сумма — число, не ноль: например 100 или -50.")
    async with Session() as s:
        if not await s.get(User, user_id):
            return RedirectResponse(A + "/users", status_code=303)
    if error := await _owner_code(request, code):
        return _to(page, error)
    await wallet.credit(user_id, value, "admin", comment=comment[:255] or "Корректировка админом")
    await owners.bonus_expense(user_id, value, actor(request))  # бонус — расход владельцев
    await accounts.audit(actor(request), f"изменил баланс пользователя {user_id} на {value:+} ₽"
                         + (f" ({comment[:100]})" if comment else ""))
    return _to(page, saved=f"Баланс изменён на {value:+} ₽.")


@router.post("/users/{user_id}/block", dependencies=[Depends(require_admin)])
async def user_block(request: Request, user_id: int):
    async with Session() as s:
        user = await s.get(User, user_id)
        if user:
            user.is_blocked = not user.is_blocked
            await s.commit()
    return back(request, f"{A}/users/{user_id}")


QUALITY_PERIODS = ((1, "Сутки"), (7, "7 дней"), (30, "30 дней"), (90, "90 дней"), (0, "Всё время"))
# группировка -> (название, измерения)
QUALITY_GROUPS = {
    "supplier": ("Поставщик", ("provider", "supplier")),
    "provider": ("Площадка", ("provider",)),
    "service": ("Сервис", ("service",)),
    "country": ("Страна", ("country",)),
    "pair": ("Сервис + страна", ("service", "country")),
    "supplier_pair": ("Поставщик + сервис + страна", ("provider", "supplier", "service", "country")),
    "price": ("Уровень цены", ("price",)),
}
QUALITY_DIMS = {
    "provider": ("Площадка", lambda r: r.provider),
    "supplier": ("Поставщик", lambda r: r.supplier or ""),
    "service": ("Сервис", lambda r: r.service_id),
    "country": ("Страна", lambda r: r.country_id),
    "price": ("Цена", lambda r: quality.price_bucket(r.price)),
}
QUALITY_SORTS = (("reliability", "Надёжнее"), ("volume", "Больше данных"), ("worst", "Хуже по SMS"), ("phantom", "Хуже по выдаче"))


def _dim_label(dim: str, value) -> str:
    if dim == "service":
        svc = catalog.SERVICES.get(value)
        return svc.name if svc else f"#{value}"
    if dim == "country":
        c = catalog.COUNTRIES.get(value)
        return c.title if c else f"#{value}"
    if dim == "price":
        return quality.price_bucket_title(value)
    return value or "—"


def _worst_first(rate: float | None, volume: int) -> tuple:
    return (rate if rate is not None else 2, -volume)  # без данных — в конец


@router.get("/quality", dependencies=[Depends(require_admin)])
async def quality_page(request: Request, days: int = 7, group: str = "supplier", provider: str = "",
                       service: int = 0, country: int = 0, sort: str = "reliability", min_n: int = 0):
    group = group if group in QUALITY_GROUPS else "supplier"
    acts, atts = await quality.load(utcnow() - timedelta(days=days) if days > 0 else None)
    services = sorted({r.service_id for r in (*acts, *atts)}, key=lambda i: _dim_label("service", i))
    countries = sorted({r.country_id for r in (*acts, *atts)}, key=lambda i: _dim_label("country", i))

    def keep(r) -> bool:
        return ((not provider or r.provider == provider) and (not service or r.service_id == service)
                and (not country or r.country_id == country))

    acts, atts = [a for a in acts if keep(a)], [a for a in atts if keep(a)]
    total = quality.aggregate(acts, atts, lambda r: ()).get((), quality.Stat())
    dims = QUALITY_GROUPS[group][1]
    stats = quality.aggregate(acts, atts, lambda r: tuple(QUALITY_DIMS[d][1](r) for d in dims))
    rows = [{"key": k, "labels": [_dim_label(d, v) for d, v in zip(dims, k)], "stat": s}
            for k, s in stats.items() if s.activations >= min_n]
    # надёжность строки сглажена к среднему по всему показанному — как в боте «к ступени выше»
    base = total.sms_rate(quality.SMS_PRIOR)
    for r in rows:
        r["rel"] = r["stat"].score(base)
    if sort == "reliability":
        rows.sort(key=lambda r: (-r["rel"], -r["stat"].counted))
    elif sort == "worst":
        rows.sort(key=lambda r: _worst_first(r["stat"].rate, r["stat"].counted))
    elif sort == "phantom":
        rows.sort(key=lambda r: _worst_first(r["stat"].buy_rate, r["stat"].attempts))
    elif group == "price":
        rows.sort(key=lambda r: r["key"])
    else:
        rows.sort(key=lambda r: -(r["stat"].activations + r["stat"].attempts))
    return render(request, "quality.html", rows=rows[:500], total=len(rows), stat=total, q=quality,
                  total_rel=total.score(quality.SMS_PRIOR),
                  heads=[QUALITY_DIMS[d][0] for d in dims],
                  f={"days": days, "group": group, "provider": provider, "service": service,
                     "country": country, "sort": sort, "min_n": min_n},
                  periods=QUALITY_PERIODS, groups=QUALITY_GROUPS, sorts=QUALITY_SORTS,
                  providers=[(name, getattr(p, "title", name)) for name, p in PROVIDERS.items()],
                  services=[(i, _dim_label("service", i)) for i in services],
                  countries=[(i, _dim_label("country", i)) for i in countries],
                  bot_days=st.int_("quality_days"), min_orders=st.int_("quality_min_orders"),
                  quick=st.int_("quality_quick_cancel_minutes"))


@router.get("/quarantine", dependencies=[Depends(require_admin)])
async def quarantine_page(request: Request, offset: int = 0, part: int = 0):
    async with Session() as s:
        rows = (await s.scalars(select(Quarantine).where(Quarantine.until > utcnow())
                                .order_by(Quarantine.until.desc(), Quarantine.id.desc())
                                .offset(max(0, offset)).limit(LAZY_STEP))).all()
        slinks = {(l.provider, l.code): l.service_id for l in (await s.scalars(select(ServiceLink))).all()}
        clinks = {(l.provider, l.code): l.country_id for l in (await s.scalars(select(CountryLink))).all()}
    items = []
    for r in rows:
        svc = catalog.SERVICES.get(slinks.get((r.provider, r.service_code)))
        country = catalog.COUNTRIES.get(clinks.get((r.provider, r.country_code)))
        items.append({"row": r, "service": svc.name if svc else r.service_code,
                      "country": country.title if country else r.country_code})
    return lazy(request, offset, part, "quarantine.html", "_quarantine_rows.html", items=items,
                minutes=st.int_("quarantine_minutes"))


@router.post("/quarantine/{quarantine_id}/remove", dependencies=[Depends(require_admin)])
async def quarantine_remove(request: Request, quarantine_id: int):
    await catalog.remove_quarantine(quarantine_id)
    return back(request, A + "/quarantine")


# ------------------------------------------------------------------ каналы и рефералы
# Связки, сообщения и история баланса реферала — общие с кабинетом реферала (channel_pages);
# здесь — только то, что доступно админу.

_flash: dict[str, tuple[float, dict]] = {}  # токен -> (когда, логин и пароль) — пароль показываем один раз


def _put_flash(request: Request, data: dict) -> None:
    token = secrets.token_urlsafe(16)
    now = time.monotonic()
    for k in [k for k, (t, _) in _flash.items() if now - t > 600]:
        del _flash[k]
    _flash[token] = (now, data)
    request.session["flash"] = token  # в cookie — только токен, пароль остаётся на сервере


async def _channels_extra(request: Request) -> dict:
    token = request.session.pop("flash", None)
    data = _flash.pop(token, (0, None))[1] if token else None
    from app.web.referral import P as partner_path  # не наверху: referral импортирует admin
    return {"flash": data, "ref_url": f"{config.public_url}{partner_path}"}


@router.post("/channels/add", dependencies=[Depends(require_admin)])
async def channels_add(ref: str = Form(...)):
    ref = ref.strip().removeprefix("https://").removeprefix("t.me/").strip("/")
    if not channels.bot or not ref:
        return RedirectResponse(A + "/channels?error=Бот+не+запущен", status_code=303)
    target = int(ref) if ref.lstrip("-").isdigit() else ("@" + ref.lstrip("@"))
    if not await channels.refresh_chat(channels.bot, target):
        return RedirectResponse(A + "/channels?error=Telegram+не+нашёл+такой+канал+или+группу",
                                status_code=303)
    return RedirectResponse(A + "/channels", status_code=303)


@router.post("/channels/refresh", dependencies=[Depends(require_admin)])
async def channels_refresh():
    if channels.bot:
        await channels.refresh_all(channels.bot)
    return RedirectResponse(A + "/channels", status_code=303)


@router.post("/channels/{chat_id}/referral/make", dependencies=[Depends(require_admin)])
async def referral_make(request: Request, chat_id: int):
    async with Session() as s:
        chat = await s.get(BotChat, chat_id)
    if chat and chat.type == "channel" and not await referrals.by_chat(chat_id):
        await partner_signup.unban(chat_id)  # сами вернули в программу — запрет снят
        ref, password = await referrals.create(chat)
        _put_flash(request, {"title": chat.title, "login": ref.login, "password": password, "new": True})
    return RedirectResponse(A + "/channels", status_code=303)


@router.post("/channels/{chat_id}/referral/delete", dependencies=[Depends(require_admin)])
async def referral_delete(chat_id: int):
    ref = await referrals.by_chat(chat_id)
    error = await referrals.remove(ref.id) if ref else None
    if ref and not error:
        await partner_signup.ban(chat_id)  # сам через страницу партнёрской программы больше не подключится
    if error:
        return RedirectResponse(f"{A}/channels?error={quote_plus(error)}", status_code=303)
    return RedirectResponse(A + "/channels", status_code=303)


@router.post("/channels/{chat_id}/referral/password", dependencies=[Depends(require_admin)])
async def referral_password(request: Request, chat_id: int):
    ref = await referrals.by_chat(chat_id)
    if ref:
        password = await referrals.reset_password(ref.id)
        async with Session() as s:
            chat = await s.get(BotChat, chat_id)
        _put_flash(request, {"title": chat.title if chat else "", "login": ref.login, "password": password})
    return RedirectResponse(A + "/channels", status_code=303)


@router.post("/channels/{chat_id}/referral/percent", dependencies=[Depends(require_owner)])
async def referral_percent(request: Request, chat_id: int, revenue_percent: str = Form(...), percent: str = Form(...),
                           code: str = Form("")):
    """Условия реферала: доля от выручки и максимальная доля от прибыли — деньги, поэтому с кодом аутентификатора."""
    revenue = parse_amount(revenue_percent, allow_negative=False)
    cap = parse_amount(percent, allow_negative=False)
    if revenue is None or cap is None or revenue > 100 or cap > 100:
        return RedirectResponse(A + "/channels?error=" + quote_plus("Проценты — числа от 0 до 100"), status_code=303)
    if error := await _owner_code(request, code):
        return RedirectResponse(A + "/channels?error=" + quote_plus(error), status_code=303)
    async with Session() as s:
        ref = (await s.scalars(select(Referral).where(Referral.chat_id == chat_id))).first()
        if ref:
            old = (ref.revenue_percent, ref.percent)
            ref.revenue_percent, ref.percent = revenue, cap
            await s.commit()
            await accounts.audit(actor(request), f"проценты реферала {ref.login}: {old[0]:g}%/{old[1]:g}% → "
                                 f"{revenue:g}%/{cap:g}%")
    return RedirectResponse(A + "/channels", status_code=303)


channel_pages.register(router, A, require_admin, lambda request: None, lambda *a, **k: render(*a, **k),
                       _channels_extra)


# ------------------------------------------------------------------ рассылки


@router.get("/broadcasts", dependencies=[Depends(require_admin)])
async def broadcasts_page(request: Request, error: str = "", repeat: int = 0, offset: int = 0, part: int = 0):
    async with Session() as s:
        items = (await s.scalars(select(Broadcast).order_by(Broadcast.id.desc())
                                 .offset(max(0, offset)).limit(LAZY_STEP))).all()
        source = await s.get(Broadcast, repeat) if repeat and not part else None  # «Повторить рассылку»
    rows_ctx = {"items": items, "stats": await broadcasts.stats([b.id for b in items]),
                "segments": broadcasts.SEGMENTS, "statuses": broadcasts.STATUS_TITLES}
    if part:
        return render(request, "_broadcast_rows.html", **rows_ctx)
    return render(request, "broadcasts.html", counts=await broadcasts.counts(), error=error, source=source,
                  test_username=request.session.get("test_username", ""), step=LAZY_STEP,
                  max_text=channels.MAX_TEXT, max_caption=channels.MAX_CAPTION, **rows_ctx)


BROADCAST_DRAFT = 0  # «канал» черновиков фото рассылок в channel_pages._drafts


async def _broadcast_form(text: str, photo: UploadFile | None, photo_from: str, photo_token: str = ""
                          ) -> tuple[str, bytes | None, str | None, str | None, bytes | None, str]:
    """-> (текст, фото, тип фото, file_id фото, превью, ошибка). photo_token — фото, загруженное заранее;
    photo_from — id рассылки, чьё фото взять (повтор рассылки), если новое не выбрано."""
    text = textfmt.clean(text)
    data, kind, error = await channel_pages._read_photo(photo)
    file_id = thumb = None
    draft = channel_pages._drafts.get(photo_token)
    if data is None and draft and draft.chat_id == BROADCAST_DRAFT:
        data, kind, thumb = draft.photo, draft.photo_type, draft.thumb
    elif data is None and photo_token:
        error = error or channel_pages.DRAFT_LOST
    elif data is None and photo_from.isdigit():
        async with Session() as s:
            src = await s.get(Broadcast, int(photo_from))
        if src and src.photo:
            data, kind, file_id, thumb = src.photo, src.photo_type, src.photo_file_id, src.thumb
    return text, data, kind, file_id, thumb, error or channel_pages._check(text, data is not None)


@router.post("/broadcasts/photo-draft", dependencies=[Depends(require_admin)])
async def broadcast_photo_draft(photo: UploadFile | None = File(None)):
    """Фото новой рассылки: загружается сразу при выборе, в рассылку попадает кнопкой «Отправить»."""
    data, kind, thumb, error = await channel_pages.read_image(photo)
    if error:
        return JSONResponse({"error": error})
    token = channel_pages._put_draft(channel_pages.Draft(BROADCAST_DRAFT, data, kind, thumb, time.monotonic()))
    return JSONResponse({"token": token, **channel_pages.photo_urls(f"{A}/broadcasts/photo-draft/{token}", thumb)})


@router.get("/broadcasts/photo-draft/{token}", dependencies=[Depends(require_admin)])
async def broadcast_photo_draft_get(request: Request, token: str, size: str = ""):
    d = channel_pages._drafts.get(token)
    if not d or d.chat_id != BROADCAST_DRAFT:
        return Response(status_code=404)
    return channel_pages.image_response(request, d.thumb, "image/webp") if size == "thumb" else \
        channel_pages.image_response(request, d.photo, d.photo_type)


@router.post("/broadcasts/test", dependencies=[Depends(require_admin)])
async def broadcast_test(request: Request, text: str = Form(""), photo: UploadFile | None = File(None),
                         photo_from: str = Form(""), photo_token: str = Form(""), test_username: str = Form("")):
    """Пробная отправка себе — из формы без перезагрузки страницы (fetch), ответ — JSON."""
    text, data, _kind, _file_id, _thumb, error = await _broadcast_form(text, photo, photo_from, photo_token)
    username = test_username.strip().removeprefix("https://t.me/").lstrip("@")
    if not error and not username:
        error = "Укажите свой @username в Telegram."
    if not error:
        async with Session() as s:
            user = (await s.scalars(select(User).where(func.lower(User.username) == username.lower()))).first()
        if not user:
            error = (f"@{username} не найден среди пользователей бота — напишите боту /start "
                     "(и проверьте, что username указан в настройках Telegram).")
    if not error:
        request.session["test_username"] = "@" + username
        error = await broadcasts.send_test(user.id, text, data)
    return {"ok": not error, "message": error or f"Отправлено @{username} — проверьте Telegram."}


@router.post("/broadcasts", dependencies=[Depends(require_admin)])
async def broadcast_start(text: str = Form(""), photo: UploadFile | None = File(None), photo_from: str = Form(""),
                          photo_token: str = Form(""), segment: str = Form("")):
    text, data, kind, file_id, thumb, error = await _broadcast_form(text, photo, photo_from, photo_token)
    if segment not in broadcasts.SEGMENTS:
        error = error or "Выберите, кому отправить."
    if data is not None and thumb is None and not error:
        try:
            thumb = await thumbs.make(data)
        except thumbs.BadImage as e:
            error = str(e)
    bid = None if error else await broadcasts.start(text, data, kind, segment, file_id, thumb)
    if bid:
        channel_pages._drafts.pop(photo_token, None)
    if not bid:
        return RedirectResponse(f"{A}/broadcasts?error={quote_plus(error or 'В этой группе сейчас никого нет.')}",
                                status_code=303)
    return RedirectResponse(f"{A}/broadcasts/{bid}", status_code=303)


@router.get("/broadcasts/{bid}", dependencies=[Depends(require_admin)])
async def broadcast_page(request: Request, bid: int, error: str = ""):
    async with Session() as s:
        b = await s.get(Broadcast, bid)
    if not b:
        return RedirectResponse(A + "/broadcasts", status_code=303)
    active = b.status in ("sending", "cancelling")
    return render(request, "broadcast.html", b=b, st=(await broadcasts.stats([bid]))[bid], error=error,
                  segment=broadcasts.SEGMENTS.get(b.segment, (b.segment, ""))[0],
                  statuses=broadcasts.STATUS_TITLES, can_cancel=broadcasts.can_cancel(b),
                  deadline=broadcasts.cancel_deadline(b), refresh=3 if active else 0)


@router.post("/broadcasts/{bid}/cancel", dependencies=[Depends(require_admin)])
async def broadcast_cancel(bid: int):
    error = await broadcasts.cancel(bid)
    return RedirectResponse(f"{A}/broadcasts/{bid}" + (f"?error={quote_plus(error)}" if error else ""),
                            status_code=303)


@router.get("/broadcasts/{bid}/photo", dependencies=[Depends(require_admin)])
async def broadcast_photo(request: Request, bid: int, size: str = ""):
    """Фото рассылки: size=thumb — лёгкое превью для страницы, без него — исходник."""
    if size == "thumb":
        async with Session() as s:
            thumb = await s.scalar(select(Broadcast.thumb).where(Broadcast.id == bid))
        if thumb:
            return channel_pages.image_response(request, thumb, "image/webp")
    async with Session() as s:
        b = await s.get(Broadcast, bid)
    if not b or not b.photo:
        return Response(status_code=404)
    if size != "thumb":
        return channel_pages.image_response(request, b.photo, b.photo_type or "image/jpeg")
    try:  # отправлена до появления превью — делаем сейчас
        thumb = await thumbs.make(b.photo)
    except thumbs.BadImage:
        return channel_pages.image_response(request, b.photo, b.photo_type or "image/jpeg")
    async with Session() as s:
        await s.execute(sa_update(Broadcast).where(Broadcast.id == bid).values(thumb=thumb))
        await s.commit()
    return channel_pages.image_response(request, thumb, "image/webp")


@router.get("/webhooks", dependencies=[Depends(require_admin)])
async def webhooks_page(request: Request, offset: int = 0, part: int = 0):
    async with Session() as s:
        rows = (await s.scalars(select(WebhookLog).order_by(WebhookLog.id.desc())
                                .offset(max(0, offset)).limit(LAZY_STEP))).all()
    return lazy(request, offset, part, "webhooks.html", "_webhook_rows.html", rows=rows, config=config)


# ------------------------------------------------------------------ деньги: баланс владельца, вывод, «Сверка»


money_pages.register(router, A, require_owner, lambda request: accounts.Acc("owner", request.state.owner.id),
                     actor, lambda *a, **k: render(*a, **k))


def _to(path: str, error: str = "", saved: str = "") -> RedirectResponse:
    q = "&".join(f"{k}={quote_plus(v)}" for k, v in (("error", error), ("saved", saved)) if v)
    return RedirectResponse(f"{A}{path}" + (f"?{q}" if q else ""), status_code=303)


@router.get("/balance", dependencies=[Depends(require_owner)])
async def owner_balance(request: Request, error: str = "", saved: str = ""):
    owner = request.state.owner
    return render(request, "owner_balance.html", owner=owner, info=await owners.summary(owner.id),
                  t=await tables.owner_history_table(owner.id, owners.KIND_TITLES), kinds=owners.KIND_TITLES,
                  since=owners.profit_since(), error=error, saved=saved)


@router.get("/balance/history", dependencies=[Depends(require_owner)])
async def owner_balance_history(request: Request, q: str = "", page: int = 1):
    return render(request, "_tbl_owner_history.html", kinds=owners.KIND_TITLES,
                  t=await tables.owner_history_table(request.state.owner.id, owners.KIND_TITLES, q, page))


@router.post("/balance/password", dependencies=[Depends(require_owner)])
async def owner_password(request: Request, current: str = Form(""), new: str = Form(""), code: str = Form("")):
    owner = request.state.owner
    if not await owners.check_password(owner.login, current):
        return _to("/balance", "Текущий пароль неверный.")
    if len(new) < 10:
        return _to("/balance", "Новый пароль — не короче 10 символов.")
    if error := await accounts.check(accounts.Acc("owner", owner.id), code):
        return _to("/balance", error)
    await owners.set_password(owner.id, new)
    await accounts.audit(owner.login, "сменил пароль")
    async with Session() as s:
        _signed_in(request, await s.get(Owner, owner.id))  # этот вход остаётся, остальные закрыты
    return _to("/balance", saved="Пароль изменён. На других устройствах нужно войти заново.")


@router.get("/owners", dependencies=[Depends(require_admin)])
async def owners_page(request: Request, error: str = "", saved: str = ""):
    return render(request, "owners.html", owners_=await owners.all_(), error=error, saved=saved,
                  admin_open=await owners.admin_login_allowed())


@router.post("/owners/{owner_id}/password", dependencies=[Depends(require_admin)])
async def owners_set_password(request: Request, owner_id: int, password: str = Form("")):
    """Первый пароль владельца. Когда аутентификатор привязан, пароль меняет только сам владелец."""
    async with Session() as s:
        row = await s.get(Owner, owner_id)
    if not row:
        return _to("/owners")
    if row.totp_secret:
        return _to("/owners", "Аутентификатор уже привязан — пароль меняет сам владелец на своём «Балансе».")
    if len(password) < 10:
        return _to("/owners", "Пароль — не короче 10 символов.")
    await owners.set_password(owner_id, password)
    await accounts.audit(actor(request), f"задал пароль владельцу {row.login}")
    return _to("/owners", saved=f"Пароль для {row.login} задан. При первом входе он привяжет аутентификатор.")


async def _payouts_ctx(request: Request, q: str = "", page: int = 1) -> dict:
    """Страница таблицы «Переводы из кошелька» и всё, что нужно для её строк."""
    t = await tables.payouts_table(q, page)
    names = {}
    for p in t["rows"]:
        if p.account and (p.account, p.account_id) not in names:
            names[(p.account, p.account_id)] = await accounts.name(accounts.Acc(p.account, p.account_id))
    return {"t": t, "names": names, "statuses": payouts.STATUS_TITLES, "purposes": payouts.PURPOSE_TITLES,
            "provider_titles": payouts.PROVIDER_TITLES, "tx_url": tron.tx_url,
            "is_owner": bool(getattr(request.state, "owner", None))}


@router.get("/reconcile/table/{name}", dependencies=[Depends(require_admin)])
async def reconcile_table(request: Request, name: str, q: str = "", page: int = 1):
    """Страница одной таблицы «Сверки» — для поиска и переключения страниц без перезагрузки."""
    if name == "payouts":
        return render(request, "_tbl_payouts.html", **await _payouts_ctx(request, q, page))
    if name == "transfers":
        return render(request, "_tbl_transfers.html", t=await tables.transfers_table(q, page), tx_url=tron.tx_url)
    if name == "audit":
        return render(request, "_tbl_audit.html", t=await tables.audit_table(q, page))
    return Response(status_code=404)


@router.get("/reconcile", dependencies=[Depends(require_admin)])
async def reconcile_page(request: Request, error: str = "", saved: str = ""):
    data = await reconcile.compute()
    pay = await _payouts_ctx(request)
    address = tron.address()
    providers = [{"key": k, "title": payouts.PROVIDER_TITLES[k], "balance": catalog.balances.get(k),
                  "enabled": st.bool_(f"{k}_enabled")}
                 for k in payouts.PROVIDER_TITLES]
    return render(request, "reconcile.html", r=data, payouts_t=pay.pop("t"), **pay,
                  transfers_t=await tables.transfers_table(), audit_t=await tables.audit_table(),
                  providers=providers, address=address,
                  qr=totp.qr_svg(address) if address else "", network=config.tron_network,
                  address_url=tron.address_url, since=owners.profit_since(),
                  values={d.key: st.raw(d.key) for d in st.DEFS if d.secure},
                  error=error, saved=saved, trongrid_key=bool(config.trongrid_api_key))


async def _owner_code(request: Request, code: str) -> str | None:
    return await accounts.check(accounts.Acc("owner", request.state.owner.id), code)


@router.post("/reconcile/start", dependencies=[Depends(require_owner)])
async def reconcile_start(request: Request, code: str = Form("")):
    """День релиза: с этого момента прибыль идёт владельцам. Всё, что сверх долгов сейчас, — оборотные средства."""
    if owners.profit_since():
        return _to("/reconcile", "Учёт прибыли уже идёт.")
    if error := await _owner_code(request, code):
        return _to("/reconcile", error)
    data = await reconcile.compute()
    if not data["complete"]:
        return _to("/reconcile", "Не все балансы удалось получить — попробуйте через минуту: "
                   + "; ".join(data["errors"]))
    capital = (st.dec("working_capital") + data["diff"]).quantize(Decimal("0.01"))
    await st.save({"profit_since": utcnow().isoformat(timespec="seconds"), "working_capital": str(capital)})
    await accounts.audit(actor(request), f"начал учёт прибыли; оборотные средства {payouts.money(capital)}")
    await reconcile.compute()
    return _to("/reconcile", saved=f"Учёт прибыли начат. Оборотные средства — {payouts.money(capital)}.")


def _cents(value: Decimal) -> Decimal:
    """К центам в сторону нуля: распределяем не больше, чем есть."""
    return value.quantize(Decimal("0.01"), rounding=ROUND_DOWN)


@router.post("/reconcile/distribute", dependencies=[Depends(require_owner)])
async def reconcile_distribute(request: Request, code: str = Form("")):
    if not owners.profit_since():
        return _to("/reconcile", "Сначала начните учёт прибыли.")
    if error := await _owner_code(request, code):
        return _to("/reconcile", error)
    data = await reconcile.compute()
    if not data["complete"]:
        return _to("/reconcile", "Не все балансы удалось получить — распределять по неполным данным нельзя.")
    diff = _cents(data["diff"])
    if diff == 0:
        return _to("/reconcile", saved="Разницы нет.")
    await owners.distribute(diff, actor(request), f"разница «Сверки» {diff:+.2f} $")
    await reconcile.compute()
    return _to("/reconcile", saved=f"Разница {diff:+.2f} $ распределена на владельцев.")


@router.post("/reconcile/capital", dependencies=[Depends(require_owner)])
async def reconcile_capital(request: Request, capital: str = Form(""), code: str = Form("")):
    """Оборотные средства — деньги владельцев в площадках и кошельке, не прибыль (не распределяются)."""
    try:
        value = Decimal(capital.replace(",", ".").replace(" ", "").replace("$", "") or "0")
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite() or value < 0:
        return _to("/reconcile", "Оборотные средства — сумма в долларах.")
    if error := await _owner_code(request, code):
        return _to("/reconcile", error)
    old = st.dec("working_capital")
    await st.save({"working_capital": str(_cents(value))})
    await accounts.audit(actor(request), f"оборотные средства: {payouts.money(old)} → {payouts.money(_cents(value))}")
    await reconcile.compute()
    return _to("/reconcile", saved="Оборотные средства сохранены.")


@router.post("/reconcile/topup", dependencies=[Depends(require_owner)])
async def reconcile_topup(request: Request, provider: str = Form(""), address: str = Form(""),
                          amount: str = Form(""), code: str = Form("")):
    if provider not in payouts.PROVIDER_TITLES:
        return _to("/reconcile")
    value = money_pages.parse_usd(amount)
    if value is None:
        return _to("/reconcile", "Укажите сумму в долларах.")
    if error := await _owner_code(request, code):
        return _to("/reconcile", error)
    try:
        p = await payouts.create_transfer("provider", address, value, actor(request), provider=provider)
    except payouts.PayoutError as e:
        return _to("/reconcile", str(e))
    return _to("/reconcile", saved=f"Перевод #{p.id} на {payouts.PROVIDER_TITLES[provider]} создан.")


@router.post("/reconcile/trx", dependencies=[Depends(require_owner)])
async def reconcile_trx(request: Request, address: str = Form(""), amount: str = Form(""), code: str = Form("")):
    """Пополнить TRX: оплатить из кошелька системы счёт обменника (USDT → TRX на адрес кошелька)."""
    value = money_pages.parse_usd(amount)
    if value is None:
        return _to("/reconcile", "Укажите сумму в USDT — ровно как в счёте обменника.")
    if error := await _owner_code(request, code):
        return _to("/reconcile", error)
    try:
        p = await payouts.create_transfer("trx", address, value, actor(request))
    except payouts.PayoutError as e:
        return _to("/reconcile", str(e))
    return _to("/reconcile", saved=f"Перевод #{p.id} — оплата счёта обменника за TRX — создан.")


@router.post("/reconcile/payouts/{payout_id}/cancel", dependencies=[Depends(require_owner)])
async def reconcile_payout_cancel(request: Request, payout_id: int):
    error = await payouts.cancel(payout_id, actor(request))
    return _to("/reconcile", error or "", "" if error else f"Перевод #{payout_id} отменён.")


@router.post("/reconcile/payouts/{payout_id}/credited", dependencies=[Depends(require_owner)])
async def reconcile_payout_credited(request: Request, payout_id: int):
    await payouts.mark_credited(payout_id, actor(request))
    return _to("/reconcile", saved=f"Пополнение #{payout_id} отмечено зачисленным.")


# ------------------------------------------------------------------ проверка номеров Telegram


def _tg_to(error: str = "", saved: str = "", login: str = "") -> RedirectResponse:
    q = "&".join(f"{k}={quote_plus(v)}" for k, v in (("error", error), ("saved", saved), ("login", login)) if v)
    return RedirectResponse(f"{A}/tgcheck" + (f"?{q}" if q else ""), status_code=303)


@router.get("/tgcheck", dependencies=[Depends(require_owner)])
async def tgcheck_page(request: Request, error: str = "", saved: str = "", login: str = ""):
    async with Session() as s:
        rows = list((await s.scalars(select(TgChecker).order_by(TgChecker.id))).all())
    today = utcnow().strftime("%Y-%m-%d")
    pending = tgcheck.login_state(login) if login else None
    return render(request, "tgcheck.html", rows=rows, today=today, now=utcnow(), error=error, saved=saved,
                  login=login if pending else "", pending=pending, enabled=tgcheck.enabled(),
                  on=st.bool_("tg_check_enabled"), limit=st.int_("tg_check_daily"), pause=st.int_("tg_check_pause"))


@router.post("/tgcheck/add", dependencies=[Depends(require_owner)])
async def tgcheck_add(request: Request, phone: str = Form("")):
    try:
        token = await tgcheck.start_login(phone)
    except tgcheck.LoginError as e:
        return _tg_to(str(e))
    return _tg_to(login=token)


@router.post("/tgcheck/code", dependencies=[Depends(require_owner)])
async def tgcheck_code(request: Request, login: str = Form(""), code: str = Form("")):
    try:
        step = await tgcheck.submit_code(login, code)
    except tgcheck.LoginError as e:
        return _tg_to(str(e), login=login if tgcheck.login_state(login) else "")
    if step == "password":
        return _tg_to(login=login)
    await accounts.audit(actor(request), "добавил аккаунт проверки номеров Telegram")
    return _tg_to(saved="Аккаунт добавлен — проверяет номера.")


@router.post("/tgcheck/password", dependencies=[Depends(require_owner)])
async def tgcheck_password(request: Request, login: str = Form(""), password: str = Form("")):
    try:
        await tgcheck.submit_password(login, password)
    except tgcheck.LoginError as e:
        return _tg_to(str(e), login=login if tgcheck.login_state(login) else "")
    await accounts.audit(actor(request), "добавил аккаунт проверки номеров Telegram")
    return _tg_to(saved="Аккаунт добавлен — проверяет номера.")


@router.post("/tgcheck/{account_id}/pause", dependencies=[Depends(require_owner)])
async def tgcheck_pause(request: Request, account_id: int, paused: str = Form("")):
    await tgcheck.set_paused(account_id, paused == "1")
    return _tg_to(saved="Аккаунт на паузе." if paused == "1" else "Аккаунт снова проверяет номера.")


@router.post("/tgcheck/{account_id}/delete", dependencies=[Depends(require_owner)])
async def tgcheck_delete(request: Request, account_id: int):
    await tgcheck.remove(account_id)
    await accounts.audit(actor(request), f"удалил аккаунт проверки номеров Telegram #{account_id}")
    return _tg_to(saved="Аккаунт удалён, сессия сервера в нём завершена.")

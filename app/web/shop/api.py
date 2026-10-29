"""API для покупки активаций (ключ — в профиле сайта, после первого пополнения).

Два формата:
- простой REST: /api/v1/... (JSON, ключ в заголовке «Authorization: Bearer <ключ>», «X-API-Key» или ?api_key=);
- совместимый с SMS-Activate: /stubs/handler_api.php?api_key=…&action=… — программы, умеющие работать с SMS-Activate,
  подключаются заменой адреса и ключа. Коды сервисов и стран — как у SMS-Activate (берём у Grizzly, у неё те же);
  у чего такого кода нет — наш: «s<id>» для сервиса, «c<id>» для страны.

Цена: без max_price/maxPrice — самый дешёвый уровень; с ним — самый дорогой (надёжный) уровень не дороже его.
Без страны — режим «Авто» (любая страна, самый надёжный поставщик в ценовой категории)."""

import time
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select

from app import catalog, orders, pycountry_names, shop_accounts, wallet
from app.db import Session
from app.models import CountryLink, Order, ServiceLink
from app.web import ratelimit

router = APIRouter()
RATE = (20, 1.0)  # запросов в секунду на ключ


# ---------------------------------------------------------------- коды сервисов и стран


_codes: dict = {"at": 0.0}


async def _maps() -> dict:
    """{svc_code: id, svc_id: code, cty_code: id, cty_id: code} — коды Grizzly (= SMS-Activate), кэш 5 минут."""
    if time.monotonic() - _codes["at"] < 300:
        return _codes
    async with Session() as s:
        sl = (await s.execute(select(ServiceLink.code, ServiceLink.service_id).where(ServiceLink.provider == "grizzly"))).all()
        cl = (await s.execute(select(CountryLink.code, CountryLink.country_id).where(
            CountryLink.provider == "grizzly", CountryLink.country_id.is_not(None)))).all()
    _codes.update(svc_code={c: i for c, i in sl}, svc_id={}, cty_code={c: i for c, i in cl}, cty_id={}, at=time.monotonic())
    for c, i in sl:
        _codes["svc_id"].setdefault(i, c)
    for c, i in cl:
        _codes["cty_id"].setdefault(i, c)
    return _codes


async def svc_code(sid: int) -> str:
    return (await _maps())["svc_id"].get(sid) or f"s{sid}"


async def cty_code(cid: int) -> str:
    return (await _maps())["cty_id"].get(cid) or f"c{cid}"


async def svc_by_code(code: str | None) -> int | None:
    code = (code or "").strip()
    if not code:
        return None
    m = await _maps()
    sid = m["svc_code"].get(code)
    if sid is None and code[:1] == "s" and code[1:].isdigit():
        sid = int(code[1:])
    if sid is None and code.isdigit() and int(code) in catalog.SERVICES:
        sid = int(code)
    s = catalog.SERVICES.get(sid) if sid is not None else None
    return sid if s and not s.is_hidden else None


async def cty_by_code(code: str | None) -> int | None | bool:
    """-> id страны, None — не указана («Авто»), False — неизвестный код."""
    code = (code or "").strip()
    if code == "" or code.lower() in ("any", "auto"):
        return None
    m = await _maps()
    cid = m["cty_code"].get(code)
    if cid is None and code[:1] == "c" and code[1:].isdigit():
        cid = int(code[1:])
    return cid if cid in catalog.COUNTRIES and not catalog.COUNTRIES[cid].is_hidden else False


def _money(x) -> str:
    return f"{Decimal(x):.2f}"


def _level_price(sid: int, cid: int | None, max_price: Decimal | None) -> tuple[Decimal | None, bool]:
    """-> (цена уровня для orders.buy, up_to) или (None, _) — купить нечего. Ошибку «дорого» решает вызывающий."""
    levels = catalog.auto_choices(sid) if cid is None else (catalog.quote(sid, cid).levels if catalog.quote(sid, cid) else [])
    if not levels:
        return None, False
    if max_price is None:
        return levels[0].price, False
    fitting = [lv for lv in levels if lv.price <= max_price]
    return (fitting[-1].price, False) if fitting else (None, True)


async def _order_json(o: Order) -> dict:
    return {"id": o.id, "status": o.status, "phone": o.phone.lstrip("+"), "price": _money(o.price), "currency": "RUB",
            "service": {"code": await svc_code(o.service_id), "name": o.service.name},
            "country": {"code": await cty_code(o.country_id), "name": pycountry_names.english(o.country.key, o.country.name),
                        "iso": o.country.key[:2]},
            "sms": [{"code": m.code, "text": m.text, "received_at": m.received_at.isoformat() + "Z"} for m in o.messages],
            "can_get_another": bool(o.can_get_another),
            "created_at": o.created_at.isoformat() + "Z", "expires_at": o.expires_at.isoformat() + "Z",
            "cancel_available_at": o.cancel_available_at.isoformat() + "Z"}


# ---------------------------------------------------------------- ключ


def _key_from(request: Request) -> str:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.headers.get("x-api-key") or request.query_params.get("api_key") or ""


async def _user(request: Request, key: str | None = None):
    key = key if key is not None else _key_from(request)
    user = await shop_accounts.by_api_key(key)
    if user and not ratelimit.allow(f"api:{user.id}", *RATE):
        return user, "rate"
    return user, None


# ---------------------------------------------------------------- REST /api/v1


def _err(code: str, message: str, status: int = 400, **extra) -> JSONResponse:
    return JSONResponse({"ok": False, "error": code, "message": message, **extra}, status_code=status)


async def _rest_user(request: Request):
    user, problem = await _user(request)
    if not user:
        return None, _err("BAD_KEY", "Invalid or missing API key", 401)
    if problem == "rate":
        return None, _err("RATE_LIMIT", f"Too many requests: max {RATE[0]} per second", 429)
    return user, None


@router.get("/api/v1/balance")
async def rest_balance(request: Request):
    user, err = await _rest_user(request)
    if err:
        return err
    return {"ok": True, "balance": _money(user.balance), "currency": "RUB"}


@router.get("/api/v1/services")
async def rest_services(request: Request):
    user, err = await _rest_user(request)
    if err:
        return err
    return {"ok": True, "services": [{"code": await svc_code(s.id), "name": s.name, "popular": s.is_popular}
                                     for s in catalog.all_services()]}


@router.get("/api/v1/services/{code}/prices")
async def rest_prices(request: Request, code: str):
    user, err = await _rest_user(request)
    if err:
        return err
    sid = await svc_by_code(code)
    if sid is None:
        return _err("BAD_SERVICE", "Unknown service code", 404)
    return {"ok": True, "currency": "RUB", "auto": [{"price": _money(lv.price)} for lv in catalog.auto_choices(sid)],
            "countries": [{"code": await cty_code(c.id), "iso": c.key[:2], "name": pycountry_names.english(c.key, c.name),
                           "levels": [{"price": _money(lv.price), "count": lv.count} for lv in q.levels]}
                          for c, q in catalog.countries_for(sid)]}


async def _buy(user, sid: int, cid: int | None, max_price: Decimal | None):
    """-> (заказ, None) или (None, (код, сообщение, http))."""
    price, too_cheap = _level_price(sid, cid, max_price)
    if price is None:
        if too_cheap:
            lv = catalog.auto_choices(sid) if cid is None else catalog.quote(sid, cid).levels
            return None, ("WRONG_MAX_PRICE", f"Minimum price is {_money(lv[0].price)}", 400, _money(lv[0].price))
        return None, ("NO_NUMBERS", "No numbers available", 409, None)
    try:
        return await orders.buy(user.id, sid, cid, price), None
    except orders.InsufficientFunds:
        return None, ("NO_BALANCE", "Insufficient balance", 402, None)
    except orders.PurchaseInProgress:
        return None, ("IN_PROGRESS", "Another purchase is in progress", 409, None)
    except orders.ProviderUnavailable:
        return None, ("PROVIDER_ERROR", "Supplier is not responding, money returned; try again later", 503, None)
    except (orders.NoNumbersAvailable, orders.PriceChanged):
        return None, ("NO_NUMBERS", "No numbers available", 409, None)
    except orders.OrderError as e:
        if "незавершённых" in str(e):
            return None, ("TOO_MANY_ACTIVE", "Too many unfinished activations", 409, None)
        return None, ("ERROR", "Could not start the activation, money returned", 500, None)


def _parse_price(raw) -> Decimal | None | bool:
    if raw in (None, ""):
        return None
    try:
        v = Decimal(str(raw).strip())
    except InvalidOperation:
        return False
    return v if v.is_finite() and 0 < v <= 1_000_000 else False


@router.post("/api/v1/orders")
async def rest_create(request: Request):
    user, err = await _rest_user(request)
    if err:
        return err
    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return _err("BAD_REQUEST", "JSON body expected")
    sid = await svc_by_code(str(data.get("service") or ""))
    if sid is None:
        return _err("BAD_SERVICE", "Unknown service code")
    cid = await cty_by_code(str(data.get("country") or ""))
    if cid is False:
        return _err("BAD_COUNTRY", "Unknown country code")
    max_price = _parse_price(data.get("max_price"))
    if max_price is False:
        return _err("BAD_PRICE", "max_price must be a positive number")
    order, problem = await _buy(user, sid, cid, max_price)
    if problem:
        code, message, status, minimum = problem
        return _err(code, message, status, **({"min_price": minimum} if minimum else {}))
    return {"ok": True, "order": await _order_json(order)}


@router.get("/api/v1/orders")
async def rest_orders(request: Request, active: int = 0, limit: int = 50):
    user, err = await _rest_user(request)
    if err:
        return err
    cond = [Order.user_id == user.id] + ([Order.status.in_(("waiting", "received"))] if active else [])
    async with Session() as s:
        rows = (await s.scalars(select(Order).where(*cond).order_by(Order.id.desc()).limit(max(1, min(limit, 200))))).all()
    return {"ok": True, "orders": [await _order_json(o) for o in rows]}


async def _own(user, oid: int) -> Order | None:
    o = await orders.load(oid)
    return o if o and o.user_id == user.id else None


@router.get("/api/v1/orders/{oid}")
async def rest_order(request: Request, oid: int):
    user, err = await _rest_user(request)
    if err:
        return err
    o = await _own(user, oid)
    if not o:
        return _err("NO_ACTIVATION", "Order not found", 404)
    return {"ok": True, "order": await _order_json(o)}


@router.post("/api/v1/orders/{oid}/{action}")
async def rest_action(request: Request, oid: int, action: str):
    user, err = await _rest_user(request)
    if err:
        return err
    o = await _own(user, oid)
    if not o:
        return _err("NO_ACTIVATION", "Order not found", 404)
    if action == "cancel":
        note = await orders.cancel(oid, user.id)
        ok = note.startswith("Активация отменена")
    elif action == "finish":
        note = await orders.finish(oid, user.id)
        ok = note.startswith("Готово")
    elif action == "another":
        note = await orders.request_another(oid, user.id)
        ok = note.startswith("Ждём")
    elif action == "replace":
        note, new = await orders.replace(oid, user.id)
        if new:
            return {"ok": True, "order": await _order_json(new), "replaced": oid}
        ok = False
    else:
        return _err("BAD_ACTION", "Unknown action", 404)
    o = await orders.load(oid)
    if not ok:
        return _err("ACTION_FAILED", _english(note), 409, order=await _order_json(o))
    return {"ok": True, "order": await _order_json(o)}


def _english(note: str | None) -> str:
    from app.web.shop.i18n import order_message
    return order_message("en", note) or "Failed"


# ---------------------------------------------------------------- совместимый с SMS-Activate


def _status_text(o: Order) -> str:
    code = next((m.code or m.text for m in reversed(o.messages)), None)
    if o.status == "waiting":
        return "STATUS_WAIT_CODE"
    if o.status in ("received", "completed") and code:
        return f"STATUS_OK:{code}"
    return "STATUS_CANCEL"


@router.api_route("/stubs/handler_api.php", methods=["GET", "POST"])
@router.api_route("/api/handler_api.php", methods=["GET", "POST"])
async def compat(request: Request):
    params = dict(request.query_params)
    if request.method == "POST":
        try:
            params.update({k: str(v) for k, v in (await request.form()).items()})
        except Exception:
            pass
    action = params.get("action", "")
    user, problem = await _user(request, params.get("api_key", ""))
    if not user:
        return PlainTextResponse("BAD_KEY")
    if problem == "rate":
        return PlainTextResponse("ERROR_RATE_LIMIT")
    text = PlainTextResponse

    if action == "getBalance":
        return text(f"ACCESS_BALANCE:{_money(user.balance)}")

    if action in ("getNumber", "getNumberV2"):
        sid = await svc_by_code(params.get("service"))
        if sid is None:
            return text("BAD_SERVICE")
        cid = await cty_by_code(params.get("country"))
        if cid is False:
            return text("BAD_DATA")
        max_price = _parse_price(params.get("maxPrice"))
        if max_price is False:
            return text("BAD_DATA")
        order, problem = await _buy(user, sid, cid, max_price)
        if problem:
            code, _msg, _st, minimum = problem
            if code == "WRONG_MAX_PRICE":
                return text(f"WRONG_MAX_PRICE:{minimum}")
            return text({"NO_BALANCE": "NO_BALANCE", "NO_NUMBERS": "NO_NUMBERS"}.get(code, "ERROR_SQL"))
        phone = order.phone.lstrip("+")
        if action == "getNumber":
            return text(f"ACCESS_NUMBER:{order.id}:{phone}")
        return JSONResponse({"activationId": order.id, "phoneNumber": phone, "activationCost": _money(order.price),
                             "currency": 643, "countryCode": await cty_code(order.country_id),
                             "canGetAnotherSms": bool(order.can_get_another),
                             "activationTime": order.created_at.strftime("%Y-%m-%d %H:%M:%S"),
                             "activationEndTime": order.expires_at.strftime("%Y-%m-%d %H:%M:%S"),
                             "activationOperator": "any"})

    if action in ("getStatus", "getStatusV2", "setStatus"):
        oid = params.get("id", "")
        o = await _own(user, int(oid)) if oid.isdigit() else None
        if not o:
            return text("NO_ACTIVATION")
        if action == "getStatus":
            return text(_status_text(o))
        if action == "getStatusV2":
            last = o.messages[-1] if o.messages else None
            return JSONResponse({"verificationType": 0, "sms": {"dateTime": last.received_at.strftime("%Y-%m-%d %H:%M:%S"),
                                                                "code": last.code, "text": last.text} if last else None,
                                 "call": None})
        status = params.get("status", "")
        if status == "8":
            note = await orders.cancel(o.id, user.id)
            if note.startswith("Активация отменена"):
                return text("ACCESS_CANCEL")
            return text("EARLY_CANCEL_DENIED" if "через" in note else "ERROR_SQL")
        if status == "6":
            await orders.finish(o.id, user.id)
            return text("ACCESS_ACTIVATION")
        if status == "3":
            note = await orders.request_another(o.id, user.id)
            return text("ACCESS_RETRY_GET" if note.startswith("Ждём") else "WRONG_ACTIVATION_ID")
        if status == "1":
            return text("ACCESS_READY")
        return text("BAD_STATUS")

    if action == "getActiveActivations":
        async with Session() as s:
            rows = (await s.scalars(select(Order).where(Order.user_id == user.id, Order.status.in_(("waiting", "received")))
                                    .order_by(Order.id.desc()).limit(100))).all()
        if not rows:
            return JSONResponse({"status": "error", "error": "NO_ACTIVATIONS"})
        return JSONResponse({"status": "success", "activeActivations": [
            {"activationId": str(o.id), "serviceCode": await svc_code(o.service_id), "phoneNumber": o.phone.lstrip("+"),
             "activationCost": _money(o.price), "activationStatus": "4" if o.status == "waiting" else "1",
             "smsCode": [m.code for m in o.messages if m.code] or None, "smsText": [m.text for m in o.messages] or None,
             "activationTime": o.created_at.strftime("%Y-%m-%d %H:%M:%S"), "countryCode": await cty_code(o.country_id)}
            for o in rows]})

    if action == "getPrices":
        want_sid = await svc_by_code(params.get("service")) if params.get("service") else None
        want_cid = await cty_by_code(params.get("country")) if params.get("country") else None
        out: dict = {}
        services = [catalog.SERVICES[want_sid]] if want_sid else catalog.all_services()
        for s in services:
            for c, q in catalog.countries_for(s.id):
                if want_cid and c.id != want_cid:
                    continue
                out.setdefault(await cty_code(c.id), {})[await svc_code(s.id)] = {
                    "cost": float(q.levels[0].price), "count": q.count}
        return JSONResponse(out)

    if action == "getCountries":
        return JSONResponse({await cty_code(c.id): {"id": await cty_code(c.id), "rus": c.name,
                                                     "eng": pycountry_names.english(c.key, c.name), "visible": 1}
                             for c in catalog.COUNTRIES.values() if not c.is_hidden})

    if action == "getServicesList":
        return JSONResponse({"status": "success", "services": [{"code": await svc_code(s.id), "name": s.name}
                                                               for s in catalog.all_services()]})

    return text("BAD_ACTION")

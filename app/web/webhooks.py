"""Вебхуки площадок. Площадки не подписывают запросы, поэтому адрес содержит секрет."""

import asyncio
import json
import logging
import secrets

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import delete, select

from app import orders, payments
from app.config import config
from app.db import Session
from app.models import WebhookLog
from app.providers.base import Sms, Status

log = logging.getLogger(__name__)
router = APIRouter()
_tasks: set[asyncio.Task] = set()  # иначе asyncio может собрать незавершённую задачу


def _background(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _payload(request: Request) -> dict:
    data: dict = dict(request.query_params)
    body = await request.body()
    if body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                data.update(parsed)
            else:
                data["_body"] = parsed
        except ValueError:
            try:
                form = await request.form()
                data.update({k: str(v) for k, v in form.items()})
            except Exception:
                data["_raw"] = body.decode(errors="replace")[:2000]
    return data


async def _log(provider: str, payload: dict, result: str) -> None:
    async with Session() as s:
        s.add(WebhookLog(provider=provider, payload=json.dumps(payload, ensure_ascii=False)[:5000],
                         result=result[:255]))
        await s.commit()
        # храним только последние 500 записей
        old = (await s.scalars(select(WebhookLog.id).order_by(WebhookLog.id.desc()).offset(500))).all()
        if old:
            await s.execute(delete(WebhookLog).where(WebhookLog.id.in_(old)))
            await s.commit()


def _check(secret: str, expected: str) -> None:
    if not secrets.compare_digest(secret, expected):
        raise HTTPException(404)


def _first(data: dict, *keys):
    for k in keys:
        if data.get(k) not in (None, ""):
            return str(data[k])
    return None


@router.api_route("/webhooks/smspool/{secret}", methods=["POST", "GET"])
async def smspool_webhook(secret: str, request: Request):
    _check(secret, config.smspool_webhook_secret)
    data = await _payload(request)
    order_ref = _first(data, "orderid", "order_id", "order_code")
    text = _first(data, "full_sms", "sms")
    result = "ignored"
    if order_ref:
        order = await orders.find_by_provider_id("smspool", order_ref)
        if order and text:
            await orders.apply_status(order.id, Status("sms", [Sms(_first(data, "sms"), text)]))
            result = f"sms -> order {order.id}"
        else:
            result = "order not found" if not order else "no text"
    await _log("smspool", data, result)
    return PlainTextResponse("OK")


@router.api_route("/webhooks/onlinesim/{secret}", methods=["POST", "GET"])
async def onlinesim_webhook(secret: str, request: Request):
    """{operation_id, message, code, number, …} — приходит на каждую SMS."""
    if not config.onlinesim_webhook_secret:
        raise HTTPException(404)
    _check(secret, config.onlinesim_webhook_secret)
    data = await _payload(request)
    ref = _first(data, "operation_id", "tzid")
    text = _first(data, "message", "msg")
    result = "ignored"
    order = await orders.find_by_provider_id("onlinesim", ref) if ref else None
    if order and text:
        # «code» у Onlinesim — номер SMS, а не код подтверждения: код бот извлечёт из текста
        await orders.apply_status(order.id, Status("sms", [Sms(None, text)]))
        result = f"sms -> order {order.id}"
    elif ref:
        result = "order not found" if not order else "no text"
    await _log("onlinesim", data, result)
    return PlainTextResponse("OK")


@router.post("/webhooks/platega")
async def platega_webhook(request: Request):
    """Статус платежа Platega: {id, amount, currency, status, paymentMethod, payload}. Подлинность —
    по заголовкам X-MerchantId и X-Secret (наши же данные); статус перепроверяется у Platega."""
    if not payments.check_headers(request.headers.get("X-MerchantId"), request.headers.get("X-Secret")):
        raise HTTPException(401)
    data = await _payload(request)
    try:
        result = await payments.callback(data)
    except Exception:
        log.exception("callback platega")
        await _log("platega", data, "error")
        raise HTTPException(500)  # Platega повторит запрос через 5 минут
    await _log("platega", data, result)
    return PlainTextResponse("OK")


@router.api_route("/webhooks/grizzly/{secret}", methods=["POST", "GET"])
async def grizzly_webhook(secret: str, request: Request):
    """Формат вебхука Grizzly публично не описан — принимаем любые поля
    и в любом случае перепроверяем статус через API."""
    _check(secret, config.grizzly_webhook_secret)
    data = await _payload(request)
    ref = _first(data, "activationId", "activation_id", "activationID", "id")
    code = _first(data, "code", "smsCode")
    text = _first(data, "text", "smsText", "sms", "message")
    result = "poll all"
    order = await orders.find_by_provider_id("grizzly", ref) if ref else None
    if order:
        if code or text:
            await orders.apply_status(order.id, Status("sms", [Sms(code, text or code)]))
            result = f"sms -> order {order.id}"
        else:
            _background(orders.refresh(order.id))
            result = f"refresh order {order.id}"
    else:
        _background(orders.poll_active())
    await _log("grizzly", data, result)
    return PlainTextResponse("OK")

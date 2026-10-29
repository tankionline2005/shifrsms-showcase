"""Пополнение баланса через Platega (https://docs.platega.io).

Создание: запись Payment (pending) -> транзакция Platega с выбранным способом оплаты (METHODS: СБП или
криптовалюта; сумма всегда в рублях, пересчёт в крипту делает Platega) -> пользователю ссылка на оплату. Деньги зачисляются, когда Platega подтвердит оплату:
  • callback на /webhooks/platega (адрес задаётся в ЛК Platega: Настройки → Callback URLs) — заголовки
    X-MerchantId и X-Secret должны совпасть с нашими, а статус перепроверяется запросом к API;
  • фоновый опрос незавершённых платежей (poll) — если callback потерялся;
  • кнопка «Проверить оплату» в боте.
Зачисление — атомарная смена статуса pending -> confirmed: один платёж не зачислится дважды, сколько бы
раз ни пришло подтверждение. Возврат денег плательщику (CHARGEBACKED) списывает сумму с баланса."""

import asyncio
import logging
import secrets
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Awaitable, Callable

import httpx
from sqlalchemy import func, select, update

from app import settings_store as st
from app import wallet
from app.config import config
from app.db import Session, utcnow
from app.models import Payment

log = logging.getLogger(__name__)

URL = "https://app.platega.io"
SBP, CRYPTO = 2, 13  # PaymentMethodInt Platega
# способ оплаты -> (кнопка в боте, подпись в журнале баланса)
METHODS = {SBP: ("СБП", "СБП"), CRYPTO: ("Криптовалюта", "криптовалютой")}
STARS = 100  # наш код способа «звёзды Telegram» (app/stars.py), у Platega такого нет
TITLES = {**METHODS, STARS: ("Telegram Stars", "звёздами Telegram")}  # для панели: все способы
POLL_AFTER = timedelta(minutes=1)  # не опрашиваем совсем свежие — человек ещё платит
EXPIRE_AFTER = timedelta(hours=24)  # pending дольше — считаем брошенным
MAX_PENDING = 5  # незавершённых платежей у человека за час — дальше новые не создаём

# Подписчик (бот): (платёж, событие confirmed | chargebacked) -> None
Notifier = Callable[[Payment, str], Awaitable[None]]
_notifier: Notifier | None = None
_user_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


class PaymentError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


def enabled() -> bool:
    return bool(config.platega_merchant_id and config.platega_api_key)


def set_notifier(fn: Notifier) -> None:
    global _notifier
    _notifier = fn


def _headers() -> dict:
    return {"X-MerchantId": config.platega_merchant_id, "X-Secret": config.platega_api_key}


def check_headers(merchant_id: str | None, secret: str | None) -> bool:
    """Callback действительно от Platega: она присылает наши же MerchantId и ключ."""
    return enabled() and secrets.compare_digest(merchant_id or "", config.platega_merchant_id) \
        and secrets.compare_digest(secret or "", config.platega_api_key)


def limits(method: int) -> tuple[int, int]:
    """(минимум, максимум) пополнения ₽: у криптовалюты свой минимум — перевод стоит около $1."""
    lo = st.int_("topup_min_crypto") if method == CRYPTO else st.int_("topup_min")
    return lo, st.int_("topup_max")


def amounts(method: int = SBP) -> list[int]:
    """Кнопки сумм из настроек — только в пределах min..max."""
    result = []
    lo, hi = limits(method)
    for x in st.raw("topup_amounts_crypto" if method == CRYPTO else "topup_amounts").replace(" ", "").split(","):
        if x.isdigit() and lo <= int(x) <= hi and int(x) not in result:
            result.append(int(x))
    return result


def parse_amount(text: str) -> int | None:
    """«500», «1 000», «500 ₽», «500р» -> 500; не целое число рублей -> None."""
    digits = "".join(text.replace("₽", "").replace("руб", "").replace("р", "").split())
    return int(digits) if digits.isdigit() and len(digits) <= 9 else None


def check_amount(amount: int, method: int = SBP) -> str | None:
    lo, hi = limits(method)
    if amount < lo:
        return f"Минимальная сумма пополнения — {lo} ₽."
    if amount > hi:
        return f"Максимальная сумма пополнения — {hi} ₽."
    return None


# ---------------------------------------------------------------- API Platega


async def _request(method: str, path: str, **kwargs) -> dict:
    try:
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.request(method, f"{URL}{path}", headers=_headers(), **kwargs)
    except httpx.HTTPError as e:
        raise PaymentError(f"platega {path}: {e!r}") from e
    if r.status_code != 200:
        raise PaymentError(f"platega {path}: HTTP {r.status_code} {r.text[:300]!r}")
    try:
        return r.json()
    except ValueError:
        raise PaymentError(f"platega {path}: не JSON {r.text[:300]!r}")


async def balances() -> dict[str, Decimal]:
    """Деньги, накопленные в Platega (ещё не выведенные): {"RUB": ..., "USDT": ...}; замороженное — тоже наше.
    amount у Platega — уже весь баланс: available + frozenBalance (вывод в обработке) + disputeFrozenBalance.
    Замороженное к нему не прибавлять — иначе вывод, пока он идёт, считается дважды."""
    data = await _request("GET", "/balance/all")
    out: dict[str, Decimal] = {}
    for row in data if isinstance(data, list) else []:
        cur = str(row.get("currency", "")).upper()
        if row.get("amount") is not None:
            total = Decimal(str(row["amount"]))
        else:  # без amount — собираем из частей
            total = sum((Decimal(str(row.get(k) or 0)) for k in ("available", "frozenBalance", "disputeFrozenBalance")),
                        Decimal(0))
        out[cur] = out.get(cur, Decimal(0)) + total
    return out


async def remote_status(external_id: str) -> str:
    """Статус транзакции у Platega: PENDING | CONFIRMED | CANCELED | CHARGEBACKED."""
    data = await _request("GET", f"/transaction/{external_id}")
    return str(data.get("status") or "").upper()


# ---------------------------------------------------------------- создание


async def create(user_id: int, username: str | None, amount: int, method: int, return_url: str) -> Payment:
    """Новый платёж и ссылка на оплату. PaymentError — показать пользователю."""
    if not enabled():
        raise PaymentError("Пополнение временно недоступно.")
    if method not in METHODS:
        raise PaymentError("Неизвестный способ оплаты.")
    if error := check_amount(amount, method):
        raise PaymentError(error)
    async with _user_locks[user_id]:
        async with Session() as s:
            recent = await s.scalar(select(func.count()).select_from(Payment).where(
                Payment.user_id == user_id, Payment.status == "pending",
                Payment.created_at >= utcnow() - timedelta(hours=1)))
            if recent >= MAX_PENDING:
                raise PaymentError("Слишком много неоплаченных счетов. Оплатите один из них или попробуйте позже.")
            p = Payment(user_id=user_id, amount=Decimal(amount), method=method, status="pending")
            s.add(p)
            await s.commit()
        body = {
            "paymentMethod": method,
            "paymentDetails": {"amount": amount, "currency": "RUB"},
            "description": f"Пополнение баланса Shifr SMS, счёт №{p.id}",
            "return": return_url,
            "failedUrl": return_url,
            "payload": str(p.id),
            "orderId": str(p.id),
            # для антифрода Platega: кто платит
            "metadata": {"userId": str(user_id), "userName": f"@{username}" if username else str(user_id)},
        }
        try:
            data = await _request("POST", "/transaction/process", json=body)
            external_id, url = str(data.get("transactionId") or ""), str(data.get("redirect") or data.get("url") or "")
            if not external_id or not url:
                raise PaymentError(f"platega: в ответе нет ссылки {str(data)[:300]}")
        except PaymentError as e:
            log.error("платёж %s: %s", p.id, e)
            async with Session() as s:
                await s.execute(update(Payment).where(Payment.id == p.id).values(status="canceled"))
                await s.commit()
            raise PaymentError("Не удалось создать счёт на оплату. Попробуйте через минуту.") from e
        async with Session() as s:
            await s.execute(update(Payment).where(Payment.id == p.id).values(external_id=external_id, url=url))
            await s.commit()
    log.info("платёж %s: %s ₽ (%s) пользователя %s, транзакция %s", p.id, amount, METHODS[method][0], user_id,
             external_id)
    return await get(p.id)


async def get(payment_id: int) -> Payment | None:
    async with Session() as s:
        return await s.get(Payment, payment_id, populate_existing=True)


async def set_message(payment_id: int, chat_id: int, message_id: int) -> None:
    async with Session() as s:
        await s.execute(update(Payment).where(Payment.id == payment_id).values(chat_id=chat_id, message_id=message_id))
        await s.commit()


# ---------------------------------------------------------------- статусы


async def apply(payment_id: int, status: str) -> Payment | None:
    """Статус от Platega -> наш. Зачисление и списание — ровно один раз."""
    status = status.upper()
    now = utcnow()
    event = None
    async with Session() as s:
        if status == "CONFIRMED":  # и после нашей «отмены по сроку»: деньги всё-таки пришли
            res = await s.execute(update(Payment).where(
                Payment.id == payment_id, Payment.status.in_(("pending", "canceled", "expired")))
                .values(status="confirmed", paid_at=now))
            event = "confirmed" if res.rowcount == 1 else None
        elif status == "CANCELED":
            await s.execute(update(Payment).where(Payment.id == payment_id, Payment.status == "pending")
                            .values(status="canceled"))
        elif status == "CHARGEBACKED":
            res = await s.execute(update(Payment).where(Payment.id == payment_id, Payment.status == "confirmed")
                                  .values(status="chargebacked"))
            event = "chargebacked" if res.rowcount == 1 else None
        await s.commit()
    p = await get(payment_id)
    if event == "confirmed":
        how = METHODS.get(p.method, ("", "онлайн"))[1]
        await wallet.credit(p.user_id, p.amount, "topup", comment=f"Пополнение {how}, счёт №{p.id}")
        log.info("платёж %s: зачислено %s ₽ пользователю %s", p.id, p.amount, p.user_id)
    elif event == "chargebacked":
        # деньги вернули плательщику — забираем их с баланса (баланс может уйти в минус)
        await wallet.credit(p.user_id, -p.amount, "chargeback", comment=f"Возврат платежа, счёт №{p.id}")
        log.warning("платёж %s: возврат плательщику, списано %s ₽ у %s", p.id, p.amount, p.user_id)
    if event and _notifier:
        try:
            await _notifier(p, event)
        except Exception:
            log.exception("уведомление о платеже %s", p.id)
    return p


async def by_external(external_id: str) -> Payment | None:
    async with Session() as s:
        return (await s.scalars(select(Payment).where(Payment.external_id == external_id))).first()


async def callback(data: dict) -> str:
    """Callback Platega (заголовки уже проверены). -> текст для журнала вебхуков."""
    external_id, status = str(data.get("id") or ""), str(data.get("status") or "").upper()
    p = await by_external(external_id) if external_id else None
    if not p:
        return "payment not found"
    try:  # перепроверяем у Platega — статус берём оттуда
        status = await remote_status(external_id) or status
    except PaymentError as e:
        log.warning("платёж %s: не удалось перепроверить статус: %s", p.id, e)
    await apply(p.id, status)
    return f"{status} -> payment {p.id}"


async def refresh(payment_id: int) -> Payment | None:
    """Кнопка «Проверить оплату» и фоновый опрос."""
    p = await get(payment_id)
    if p and p.external_id and p.status == "pending":
        try:
            status = await remote_status(p.external_id)
        except PaymentError as e:
            log.info("платёж %s: %s", p.id, e)
            return p
        if status in ("CONFIRMED", "CANCELED", "CHARGEBACKED"):
            return await apply(p.id, status)
    return p


async def notify(p: Payment, event: str) -> None:
    """Сообщить пользователю о зачислении / возврате (бот)."""
    if _notifier:
        try:
            await _notifier(p, event)
        except Exception:
            log.exception("уведомление о платеже %s", p.id)


async def poll() -> None:
    """Незавершённые платежи: спросить статус; брошенные дольше суток — закрыть. Счета в звёздах — в app/stars.py."""
    from app import stars
    await stars.expire_old()
    if not enabled():
        return
    now = utcnow()
    async with Session() as s:
        rows = (await s.scalars(select(Payment).where(
            Payment.provider == "platega", Payment.status == "pending", Payment.created_at <= now - POLL_AFTER))).all()
    for p in rows:
        if not p.external_id or p.created_at <= now - EXPIRE_AFTER:
            async with Session() as s:
                await s.execute(update(Payment).where(Payment.id == p.id, Payment.status == "pending")
                                .values(status="expired"))
                await s.commit()
            continue
        try:
            await refresh(p.id)
        except Exception:
            log.exception("опрос платежа %s", p.id)

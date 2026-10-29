"""Жизненный цикл заказа: покупка, приём SMS, отмена, замена номера, завершение, истечение."""

import asyncio
import logging
import re
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal
from typing import Awaitable, Callable

from sqlalchemy import func, select, update

from app import catalog, owners, pricing, tgcheck, wallet
from app.textfmt import sms_text
from app import settings_store as st
from app.db import Session, utcnow
from app.models import BuyAttempt, DiscardedOrder, Order, OrderSms, Transaction
from app.providers import PROVIDERS
from app.providers.base import (
    NoNumbers, NoProviderBalance, PriceTooHigh, ProviderError, ServiceBlocked, Sms, Status,
)

log = logging.getLogger(__name__)

EXPIRY_GRACE = timedelta(minutes=2)
BUY_ATTEMPTS = 5  # поставщиков за одну покупку: прайсы врут, но дольше пользователь ждать не должен
# Площадки, где покупка у конкретного поставщика часто даёт «нет номеров», а без указания
# поставщика номер находится: после первого такого отказа пробуем один раз «любого».
ANY_SUPPLIER_FALLBACK = {"grizzly"}
TG_BUSY_RETRY = 2  # номер уже в Telegram — ещё столько номеров у того же поставщика, потом следующий
DISCARD_GIVE_UP = timedelta(minutes=30)  # сколько пытаемся отменить номер, который не отдали


class OrderError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


class NoNumbersAvailable(OrderError):
    """Номеров не оказалось — деньги уже возвращены."""


class ProviderUnavailable(OrderError):
    """Номера есть, но площадка не ответила (сбой у неё) — деньги уже возвращены."""


class InsufficientFunds(OrderError):
    def __init__(self, price: Decimal):
        super().__init__("Недостаточно средств на балансе.")
        self.price = price


class PriceChanged(OrderError):
    """Уровня с такой ценой больше нет — показать актуальные цены."""


class PurchaseInProgress(OrderError):
    pass


# Подписчик на изменения заказа (бот): (order, event) -> None
Notifier = Callable[[Order, str], Awaitable[None]]
_notifier: Notifier | None = None
_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
# одна покупка пользователя за раз: двойное нажатие не должно дать два номера
_buy_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)


def set_notifier(fn: Notifier) -> None:
    global _notifier
    _notifier = fn


async def _notify(order: Order, event: str) -> None:
    if _notifier:
        try:
            await _notifier(order, event)
        except Exception:
            log.exception("notify order %s", order.id)


async def load(order_id: int) -> Order | None:
    async with Session() as s:
        return await s.get(Order, order_id, populate_existing=True)


def extract_code(text: str) -> str | None:
    m = re.search(r"(?<!\d)(\d{3}[- ]?\d{3}|\d{4,8})(?!\d)", text or "")
    return m.group(1) if m else None


# ------------------------------------------------------------------ покупка


async def buy(user_id: int, service_id: int, country_id: int | None, price: Decimal, *,
              up_to: bool = False, avoid: tuple[str, str | None] | None = None) -> Order:
    """Активация по уровню цены price ₽.
    country_id None — режим «Авто»: самый надёжный поставщик среди всех стран (catalog.auto_levels).
    up_to — взять самый дорогой (надёжный) уровень не дороже price: так берётся замена номера.
    avoid — (площадка, поставщик), к которому обращаться в последнюю очередь."""
    lock = _buy_locks[user_id]
    if lock.locked():
        raise PurchaseInProgress("Активация уже запускается — подождите пару секунд.")
    async with lock:
        return await _buy(user_id, service_id, country_id, price, up_to, avoid)


def _pick_level(levels: list, price: Decimal, up_to: bool):
    if up_to:  # самый дорогой (надёжный) уровень не дороже price
        fitting = [lv for lv in levels if lv.price <= price]
        return fitting[-1] if fitting else None
    return next((lv for lv in levels if lv.price == price), None)


async def _buy(user_id: int, service_id: int, country_id: int | None, price: Decimal, up_to: bool,
               avoid: tuple[str, str | None] | None) -> Order:
    if country_id is None:  # «Авто»: всё, что не дороже цены кнопки, в любой стране
        level = catalog.auto_level_at(service_id, price)
        available = level is not None or bool(catalog.auto_levels(service_id))
    else:
        q = catalog.quote(service_id, country_id)
        level = _pick_level(q.levels if q else [], price, up_to)
        available = q is not None
    if not level:
        if available and not up_to:
            raise PriceChanged()
        raise NoNumbersAvailable("Активаций сейчас нет.")
    price = level.price

    async with Session() as s:
        active = await s.scalar(select(func.count()).select_from(Order).where(
            Order.user_id == user_id, Order.status.in_(("waiting", "received"))))
    if active >= st.int_("max_active_orders"):
        raise OrderError("Слишком много незавершённых активаций. Завершите или отмените текущие.")

    tx_id = await wallet.debit(user_id, price, "purchase")
    if not tx_id:
        raise InsufficientFunds(price)

    # (страна, кандидат) в порядке попытки покупки
    candidates = level.candidates if country_id is None else [(country_id, c) for c in level.candidates]
    if avoid:  # сортировка устойчивая: остальной порядок сохраняется
        candidates = sorted(candidates, key=lambda x: (x[1].offer.provider, x[1].offer.supplier) == avoid)
    purchase, used, used_country, sold_out = None, None, None, False
    attempts: list[BuyAttempt] = []  # для статистики «реально ли поставщик выдаёт номера»

    any_tried: set[str] = set()  # площадки, у которых уже пробовали «любого поставщика»

    def attempt(offer, cid: int, supplier: str | None, result: str) -> None:
        attempts.append(BuyAttempt(provider=offer.provider, supplier=supplier,
                                   service_id=service_id, country_id=cid,
                                   price=price, result=result))

    tried = 0
    for cid, cand in candidates:
        if tried == BUY_ATTEMPTS:
            break
        if not any(p <= cand.max_price for p, _ in cand.offer.tiers):
            continue  # спрятан в карантин за время этой покупки (площадка закрыла сервис и т.п.)
        tried += 1
        offer, supplier_id, supplier = cand.offer, cand.offer.supplier_id, cand.offer.supplier
        busy_here = 0
        while True:
            try:
                purchase = await PROVIDERS[offer.provider].buy(
                    offer.service_code, offer.country_code, cand.max_price, supplier_id)
                purchase.supplier = purchase.supplier or supplier
                used, used_country = offer, cid
                catalog.spend(offer.provider, purchase.cost_usd)
                attempt(offer, cid, supplier, "ok")
                if tgcheck.applies(service_id) and await tgcheck.check(purchase.phone) == "busy":
                    # на номере уже есть аккаунт Telegram — не выдаём, отменим у площадки в фоне
                    log.info("номер %s %s уже в Telegram — не выдаём", offer.provider, purchase.phone)
                    await _quietly(discard(offer.provider, purchase))
                    purchase = used = used_country = None
                    sold_out = True
                    if busy_here < TG_BUSY_RETRY:
                        busy_here += 1
                        continue  # ещё номер у этого же поставщика
            except NoNumbers as e:
                log.info("нет номеров: %s [%s]", e, supplier or "-")
                if isinstance(e, PriceTooHigh):  # номер выдан дороже лимита — не отдаём, отменим в фоне
                    catalog.spend(offer.provider, e.purchase.cost_usd)
                    await _quietly(discard(offer.provider, e.purchase))
                sold_out = True
                attempt(offer, cid, supplier, "no_numbers")
                if supplier_id == offer.supplier_id:  # карантин — только за отказ из прайса
                    await _quietly(catalog.drop_tiers(offer, cand.max_price, str(e)))
                if supplier_id and offer.provider in ANY_SUPPLIER_FALLBACK \
                        and offer.provider not in any_tried:
                    # поставщик «пуст» — та же цена, но пусть площадка выберет поставщика сама
                    any_tried.add(offer.provider)
                    supplier_id = supplier = None
                    continue
            except NoProviderBalance as e:  # наша проблема, не поставщика — в статистику не идёт
                log.error("НЕТ БАЛАНСА НА ПЛОЩАДКЕ: %s", e)
                catalog.mark_no_balance(offer.provider)
            except ServiceBlocked as e:
                attempt(offer, cid, supplier, "error")
                await _quietly(catalog.block_service(offer.provider, offer.service_code, str(e)))
            except Exception as e:
                # ошибка площадки или неожиданный ответ: пробуем следующего, деньги не теряем; этого
                # поставщика — в карантин, чтобы следующие нажатия не упирались в ту же ошибку
                if isinstance(e, ProviderError):
                    log.warning("ошибка покупки: %s", e)
                else:
                    log.exception("неожиданная ошибка покупки у %s", offer.provider)
                attempt(offer, cid, supplier, "error")
                if supplier_id == offer.supplier_id:
                    await _quietly(catalog.drop_tiers(offer, cand.max_price, f"ошибка: {e}"[:255]))
            break
        if purchase:
            break
    await _save_attempts(attempts)

    if not purchase:
        await wallet.credit(user_id, price, "refund", comment="Активация не запущена")
        if not sold_out:
            # никто не сказал «нет номеров» — это сбой площадки (HTTP 500 и т.п.)
            raise ProviderUnavailable("Поставщик сейчас не отвечает — активация не запущена, "
                                      "деньги возвращены на баланс.")
        if country_id is not None:
            await catalog.refresh_pair(service_id, country_id)
        raise NoNumbersAvailable(f"Активация за {price:.0f} ₽ сейчас недоступна — "
                                 "деньги возвращены на баланс.")

    try:
        order_id = await _save_order(user_id, service_id, used_country, price, tx_id, used, purchase,
                                     auto=country_id is None)
    except Exception:
        log.exception("не удалось сохранить заказ %s %s — отменяем", used.provider, purchase.order_id)
        try:
            await PROVIDERS[used.provider].cancel(purchase.order_id)
        except Exception:
            log.exception("не удалось отменить %s %s", used.provider, purchase.order_id)
        await wallet.credit(user_id, price, "refund", comment="Ошибка запуска активации")
        raise OrderError("Не удалось запустить активацию. Деньги возвращены на баланс, "
                         "попробуйте ещё раз.")
    log.info("заказ %s: %s %s за %s₽ (закупка $%s)%s", order_id, used.provider,
             purchase.phone, price, purchase.cost_usd, " [авто]" if country_id is None else "")
    return await load(order_id)


async def _quietly(coro) -> None:
    """Служебный шаг покупки (карантин, отложенная отмена): его сбой не должен прервать покупку —
    деньги пользователя уже списаны и вернутся, только если цикл покупки дойдёт до конца."""
    try:
        await coro
    except Exception:
        log.exception("служебный шаг покупки")


async def _save_attempts(attempts: list[BuyAttempt]) -> None:
    if not attempts:
        return
    try:
        async with Session() as s:
            s.add_all(attempts)
            await s.commit()
    except Exception:
        log.exception("не удалось записать попытки покупки")  # статистика не должна ломать покупку


async def _save_order(user_id, service_id, country_id, price, tx_id, used, purchase, auto=False) -> int:
    now = utcnow()
    async with Session() as s:
        order = Order(
            user_id=user_id, service_id=service_id, country_id=country_id,
            provider=used.provider, provider_order_id=purchase.order_id,
            supplier=purchase.supplier,
            phone=purchase.phone, cost_usd=purchase.cost_usd,
            usd_rate=pricing.effective_rate().quantize(Decimal("0.0001")), price=price,
            status="waiting", can_get_another=purchase.can_get_another, created_at=now, auto=auto,
            cancel_available_at=now + timedelta(seconds=purchase.cancel_after_sec),
            expires_at=now + timedelta(seconds=purchase.lifetime_sec),
        )
        s.add(order)
        await s.flush()
        await s.execute(update(Transaction).where(Transaction.id == tx_id)
                        .values(order_id=order.id))
        await s.commit()
        return order.id


async def set_message(order_id: int, chat_id: int, message_id: int) -> None:
    async with Session() as s:
        await s.execute(update(Order).where(Order.id == order_id)
                        .values(chat_id=chat_id, message_id=message_id))
        await s.commit()


# ------------------------------------------------------------------ SMS и статусы


async def _add_sms(order_id: int, sms_list: list[Sms]) -> bool:
    """Сохраняет новые SMS. True — если появилось что-то новое."""
    added = False
    async with Session() as s:
        order = await s.get(Order, order_id, populate_existing=True)
        if not order or order.status not in ("waiting", "received"):
            return False
        known = {sms_text(m.text) for m in order.messages}
        for sms in sms_list:
            text = sms_text(sms.text or sms.code or "")
            if not text or text in known:
                continue
            s.add(OrderSms(order_id=order_id, text=text, code=sms.code or extract_code(text)))
            known.add(text)
            added = True
        if added:
            order.status = "received"
        await s.commit()
    return added


REFUND_COMMENTS = {"cancelled": "Отмена", "replaced": "Замена номера", "expired": "SMS не пришла"}


async def _close(order: Order, status: str, refund: bool) -> Order:
    async with Session() as s:
        res = await s.execute(update(Order)
                              .where(Order.id == order.id, Order.status.in_(("waiting", "received")))
                              .values(status=status, closed_at=utcnow()))
        await s.commit()
    if res.rowcount == 1 and refund:
        await wallet.credit(order.user_id, order.price, "refund", order_id=order.id,
                            comment=REFUND_COMMENTS[status])
    return await load(order.id)


async def apply_status(order_id: int, status: Status) -> None:
    async with _locks[order_id]:
        order = await load(order_id)
        if not order or not order.is_active:
            return
        if status.state == "sms":
            if await _add_sms(order_id, status.sms):
                if order.status == "waiting":  # первая SMS — деньги наши: доля рефералу, остаток владельцам
                    await _quietly(owners.on_sms(order_id))
                await _notify(await load(order_id), "sms")
        elif status.state == "closed":
            if order.status == "waiting":
                await _notify(await _close(order, "expired", refund=True), "expired")
            else:
                await _notify(await _close(order, "completed", refund=False), "completed")


async def refresh(order_id: int) -> None:
    order = await load(order_id)
    if order and order.is_active:
        status = await PROVIDERS[order.provider].get_status(order.provider_order_id)
        await apply_status(order_id, status)


async def find_by_provider_id(provider: str, provider_order_id: str) -> Order | None:
    async with Session() as s:
        return (await s.scalars(select(Order).where(
            Order.provider == provider, Order.provider_order_id == provider_order_id))).first()


# ------------------------------------------------------------------ действия пользователя


CANCEL_MESSAGES = {
    "too_early": "Отмена пока невозможна. Попробуйте через минуту.",
    "busy": "Отмена пока не прошла. Попробуйте ещё раз через минуту.",
}


async def _cancellable(order_id: int, user_id: int) -> tuple[Order | None, str | None]:
    """-> (заказ, None), если его можно отменить сейчас, иначе (None, причина)."""
    order = await load(order_id)
    if not order or order.user_id != user_id:
        return None, "Активация не найдена."
    if order.status != "waiting":
        return None, "Эту активацию уже нельзя отменить."
    left = (order.cancel_available_at - utcnow()).total_seconds()
    if left > 0:
        return None, f"Отмена станет доступна через {int(left // 60)}:{int(left % 60):02d}."
    return order, None


async def _provider_cancel(order: Order) -> str:
    """Отмена у площадки -> closed | sms | busy | too_early | failed.
    Площадка может ответить «отменён», не закрыв заказ (так было у SMSPool), поэтому
    closed — только после проверки статуса: иначе SMS придёт на «отменённый» номер за наш счёт."""
    provider = PROVIDERS[order.provider]
    try:
        result = await provider.cancel(order.provider_order_id)
    except ProviderError as e:
        log.warning("cancel %s: %s", order.id, e)
        result = "failed"
    log.info("отмена заказа %s (%s %s): %s", order.id, order.provider,
             order.provider_order_id, result)
    if result != "ok":
        return "too_early" if result == "too_early" else "failed"
    try:
        check = await provider.get_status(order.provider_order_id)
    except ProviderError as e:
        log.warning("проверка отмены %s: %s", order.id, e)
        return "closed"
    if check.state == "waiting":
        log.warning("заказ %s: площадка ответила «отменён», но заказ активен", order.id)
        return "busy"
    return "sms" if check.state == "sms" else "closed"


async def _cancel_failed(order_id: int, result: str) -> str:
    if result in CANCEL_MESSAGES:
        return CANCEL_MESSAGES[result]
    await refresh(order_id)  # возможно, SMS уже пришла
    return "Не удалось отменить активацию. Если SMS пришла — она появится в карточке."


async def cancel(order_id: int, user_id: int) -> str:
    async with _locks[order_id]:
        order, denied = await _cancellable(order_id, user_id)
        if denied:
            return denied
        result = await _provider_cancel(order)
        if result == "closed":
            await _notify(await _close(order, "cancelled", refund=True), "cancelled")
            return "Активация отменена, деньги возвращены на баланс."
    return await _cancel_failed(order_id, result)


async def replace(order_id: int, user_id: int) -> tuple[str | None, Order | None]:
    """«Другой номер»: отменить текущий и сразу взять новый не дороже уплаченной цены,
    по возможности у другого поставщика (в режиме «Авто» — в любой стране).
    -> (сообщение, новая активация). Если новый номер взять не удалось — деньги за старый
    остаются на балансе."""
    async with _locks[order_id]:
        order, denied = await _cancellable(order_id, user_id)
        if denied:
            return denied, None
        result = await _provider_cancel(order)
        if result == "closed":
            await _close(order, "replaced", refund=True)
    if result != "closed":
        return await _cancel_failed(order_id, result), None
    try:
        new = await buy(user_id, order.service_id, None if order.auto else order.country_id,
                        order.price, up_to=True, avoid=(order.provider, order.supplier))
    except OrderError as e:
        return (f"Активация отменена, {order.price:.0f} ₽ возвращены на баланс.\n"
                f"Новую активацию запустить не удалось: {e}"), None
    return None, new


async def finish(order_id: int, user_id: int) -> str:
    async with _locks[order_id]:
        order = await load(order_id)
        if not order or order.user_id != user_id or order.status != "received":
            return "Активация уже завершена."
        try:
            await PROVIDERS[order.provider].finish(order.provider_order_id)
        except ProviderError as e:
            log.warning("finish %s: %s", order_id, e)
        await _notify(await _close(order, "completed", refund=False), "completed")
        return "Готово! Активация завершена."


async def request_another(order_id: int, user_id: int) -> str:
    async with _locks[order_id]:
        order = await load(order_id)
        if not order or order.user_id != user_id or order.status != "received":
            return "Активация уже завершена."
        try:
            ok = await PROVIDERS[order.provider].request_another(order.provider_order_id)
        except ProviderError as e:
            log.warning("another %s: %s", order_id, e)
            ok = False
        return ("Ждём следующую SMS по этой активации." if ok
                else "Для этой активации повторная SMS недоступна.")


# ------------------------------------------------------------------ номера, которые не отдали


def _digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


async def discard(provider: str, purchase) -> None:
    async with Session() as s:
        s.add(DiscardedOrder(provider=provider, provider_order_id=purchase.order_id,
                             phone=_digits(purchase.phone), cost_usd=purchase.cost_usd,
                             cancel_after=utcnow() + timedelta(seconds=purchase.cancel_after_sec)))
        await s.commit()


async def cancel_discarded() -> None:
    """Отменить у площадок номера, которые мы не отдали пользователям (площадка продала дороже
    лимита). Если отмена так и не прошла — закупка потеряна (lost).
    Часть площадок разрешает отмену только через пару минут — пробуем, пока не выйдет."""
    now = utcnow()
    async with Session() as s:
        rows = (await s.scalars(select(DiscardedOrder).where(
            DiscardedOrder.done.is_(False), DiscardedOrder.cancel_after <= now))).all()
    for row in rows:
        provider = PROVIDERS.get(row.provider)
        result = "failed"
        if provider:
            try:
                result = await provider.cancel(row.provider_order_id)
            except ProviderError as e:
                log.warning("отмена ненужного номера %s %s: %s", row.provider, row.provider_order_id, e)
        give_up = now - row.created_at > DISCARD_GIVE_UP
        if result == "ok" or give_up:
            if result != "ok":
                log.error("не удалось отменить ненужный номер %s %s — закупка потеряна",
                          row.provider, row.provider_order_id)
            async with Session() as s:
                await s.execute(update(DiscardedOrder).where(DiscardedOrder.id == row.id)
                                .values(done=True, lost=result != "ok"))
                await s.commit()


async def poll_active() -> None:
    try:
        await cancel_discarded()
    except Exception:
        log.exception("cancel_discarded")  # не должно мешать проверке активных заказов
    async with Session() as s:
        active = (await s.scalars(select(Order).where(
            Order.status.in_(("waiting", "received"))))).all()
    by_provider: dict[str, list[Order]] = defaultdict(list)
    for o in active:
        by_provider[o.provider].append(o)

    for name, orders in by_provider.items():
        provider = PROVIDERS.get(name)
        if not provider:
            continue
        try:
            statuses = await provider.get_active()
        except ProviderError as e:
            log.warning("poll %s: %s", name, e)
            statuses = {}
        for order in orders:
            try:
                status = statuses.get(order.provider_order_id)
                if status is None:
                    status = await provider.get_status(order.provider_order_id)
                await apply_status(order.id, status)
            except ProviderError as e:
                log.warning("poll order %s: %s", order.id, e)
            except Exception:
                log.exception("poll order %s", order.id)
            # срок проверяем всегда — даже если статус не удалось получить,
            # иначе заказ с «непонятным» статусом завис бы без возврата денег
            try:
                await _handle_expiry(order.id)
            except Exception:
                log.exception("expiry order %s", order.id)


async def _handle_expiry(order_id: int) -> None:
    async with _locks[order_id]:
        order = await load(order_id)
        if not order or not order.is_active or utcnow() < order.expires_at:
            return
        provider = PROVIDERS[order.provider]
        if order.status == "received":
            try:
                await provider.finish(order.provider_order_id)
            except ProviderError:
                pass
            await _notify(await _close(order, "completed", refund=False), "completed")
            return
        try:
            result = await provider.cancel(order.provider_order_id)
        except ProviderError:
            result = "failed"
        if result == "ok" or utcnow() > order.expires_at + EXPIRY_GRACE:
            if result != "ok":
                log.warning("заказ %s истёк, площадка не подтвердила отмену", order_id)
            await _notify(await _close(order, "expired", refund=True), "expired")

"""Пополнение баланса звёздами Telegram (валюта XTR, оплата внутри Telegram — без платёжки и ключей).

Курс зачисления (₽ за 1 звезду) = stars_usd × курс ЦБ × (1 − stars_margin %), где stars_usd — сколько Telegram
платит за звезду при выводе через Fragment (≈ $0,013, настройка). Курс ЦБ — тот же usd_rate, по которому «Сверка»
переводит балансы пользователей в доллары, — поэтому каждая оплаченная звезда даёт в «Сверке» +stars_usd в деньгах
и stars_usd × (1 − запас) в долгах: запас остаётся у нас, курс доллара на это не влияет. Надбавка к курсу для цен
активаций (usd_rate_extra) здесь НЕ участвует — иначе зачисляли бы больше, чем получим.

Сумма в рублях считается и записывается при создании счёта (Payment.amount, вниз до целого рубля); счёт принимается
час (pre_checkout проверяет статус, срок, валюту и число звёзд) — изменение курса после счёта ничего не сдвигает.
Зачисление — только по successful_payment, ровно один раз (атомарная смена статуса + уникальный
telegram_payment_charge_id в Payment.external_id). Звёзды копятся на балансе бота; вывод — владельцы бота через Fragment
(TON, через 21 день после оплаты), дальше в кошелёк системы. «Сверка» считает звёзды бота деньгами: баланс × stars_usd."""

import logging
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import func, select, update

from app import payments, wallet
from app import settings_store as st
from app.db import Session, utcnow
from app.models import Payment

log = logging.getLogger(__name__)

PROVIDER = "stars"
METHOD = payments.STARS
PAYLOAD = "stars:"
INVOICE_TTL = timedelta(hours=1)  # сколько принимаем оплату по счёту (курс зафиксирован при создании)


class StarsError(Exception):
    """Ошибка, текст которой можно показать пользователю."""


def rate() -> Decimal:
    """₽ на баланс за 1 звезду (без округления). 0 — курс не задан, пополнение звёздами недоступно."""
    usd, cbr = st.dec("stars_usd"), st.dec("usd_rate")
    margin = min(max(st.dec("stars_margin"), Decimal(0)), Decimal(90))
    if usd <= 0 or cbr <= 0:
        return Decimal(0)
    return usd * cbr * (1 - margin / 100)


def enabled() -> bool:
    return st.bool_("stars_enabled") and rate() > 0


def rub_for(stars: int) -> Decimal:
    """Сколько рублей зачислим за stars звёзд — вниз до целого рубля (остаток — в нашу пользу, меньше рубля)."""
    return (Decimal(stars) * rate()).quantize(Decimal(1), rounding=ROUND_DOWN)


def limits() -> tuple[int, int]:
    lo = max(1, st.int_("stars_min"))
    return lo, max(lo, st.int_("stars_max"))


def packs() -> list[int]:
    lo, hi = limits()
    out = []
    for x in st.raw("stars_packs").replace(" ", "").split(","):
        if x.isdigit() and lo <= int(x) <= hi and int(x) not in out:
            out.append(int(x))
    return out or [lo]


def parse(text: str) -> int | None:
    raw = (text or "").replace(" ", "").replace("⭐", "").strip()
    return int(raw) if raw.isdigit() else None


def check(stars: int) -> str | None:
    lo, hi = limits()
    if stars < lo:
        return f"Минимум — ⭐{lo}."
    if stars > hi:
        return f"Максимум — ⭐{hi}."
    return None


async def create(user_id: int, stars: int, chat_id: int) -> Payment:
    """Счёт в звёздах: рубли считаются и фиксируются сейчас."""
    if not enabled():
        raise StarsError("Пополнение звёздами временно недоступно.")
    if error := check(stars):
        raise StarsError(error)
    amount = rub_for(stars)
    if amount <= 0:
        raise StarsError("Пополнение звёздами временно недоступно.")
    async with Session() as s:
        recent = await s.scalar(select(func.count()).select_from(Payment).where(
            Payment.user_id == user_id, Payment.status == "pending",
            Payment.created_at >= utcnow() - timedelta(hours=1)))
        if recent >= payments.MAX_PENDING:
            raise StarsError("Слишком много неоплаченных счетов. Оплатите один из них или попробуйте позже.")
        p = Payment(user_id=user_id, provider=PROVIDER, method=METHOD, amount=amount, stars=stars,
                    status="pending", chat_id=chat_id)
        s.add(p)
        await s.commit()
        await s.refresh(p)
    log.info("счёт %s: ⭐%s → %s ₽ пользователю %s (курс %s ₽/⭐)", p.id, stars, amount, user_id,
             rate().quantize(Decimal("0.0001")))
    return p


def _payment_id(payload: str) -> int | None:
    if not (payload or "").startswith(PAYLOAD):
        return None
    rest = payload[len(PAYLOAD):]
    return int(rest) if rest.isdigit() else None


async def precheck(payload: str, currency: str, total: int, user_id: int) -> str | None:
    """Ответ на pre_checkout_query: None — можно платить, иначе текст отказа (Telegram покажет пользователю)."""
    pid = _payment_id(payload)
    async with Session() as s:
        p = await s.get(Payment, pid) if pid else None
    if not p or p.provider != PROVIDER or p.user_id != user_id:
        return "Счёт не найден. Создайте новый: «👤 Профиль» → «💰 Баланс» → «⭐ Telegram Stars»."
    if currency != "XTR" or total != p.stars:
        return "Сумма счёта не совпадает. Создайте новый счёт."
    if p.status != "pending" or utcnow() - p.created_at > INVOICE_TTL:
        return "Счёт устарел — создайте новый: «👤 Профиль» → «💰 Баланс» → «⭐ Telegram Stars»."
    return None


async def paid(payload: str, currency: str, total: int, charge_id: str, user_id: int) -> Payment | None:
    """successful_payment: звёзды уже у бота — зачисляем рубли ровно один раз. -> платёж, если зачислили сейчас."""
    if currency != "XTR" or total <= 0 or not charge_id:
        log.error("оплата звёздами: странные данные %s %s %s", currency, total, charge_id)
        return None
    async with Session() as s:
        if await s.scalar(select(func.count()).select_from(Payment).where(Payment.external_id == charge_id)):
            return None  # уже зачислено (повтор события)
    pid = _payment_id(payload)
    async with Session() as s:
        p = await s.get(Payment, pid) if pid else None
        ok = bool(p and p.provider == PROVIDER and p.user_id == user_id and p.stars == total)
        res = None
        if ok:
            res = await s.execute(update(Payment).where(
                Payment.id == p.id, Payment.status.in_(("pending", "expired", "canceled")))
                .values(status="confirmed", paid_at=utcnow(), external_id=charge_id))
        if not ok or res.rowcount != 1:
            # счёт не нашёлся, не совпал или уже оплачен (двойная оплата одного счёта), а звёзды Telegram уже
            # списал — это отдельное пополнение: по курсу счёта, если он есть, иначе по текущему
            amount = p.amount if ok else rub_for(total)
            log.warning("оплата звёздами %s (payload %r): отдельным пополнением на %s ₽", charge_id, payload, amount)
            p = Payment(user_id=user_id, provider=PROVIDER, method=METHOD, amount=amount, stars=total,
                        status="confirmed", paid_at=utcnow(), external_id=charge_id)
            s.add(p)
        await s.commit()
        await s.refresh(p)
    await wallet.credit(p.user_id, p.amount, "topup", comment=f"Пополнение звёздами Telegram (⭐{p.stars}), счёт №{p.id}")
    log.info("счёт %s: зачислено %s ₽ за ⭐%s пользователю %s", p.id, p.amount, p.stars, p.user_id)
    await payments.notify(p, "confirmed")
    return p


async def refunded(charge_id: str) -> None:
    """Звёзды вернули плательщику (refunded_payment) — забираем сумму с баланса (может уйти в минус)."""
    async with Session() as s:
        p = (await s.scalars(select(Payment).where(Payment.external_id == charge_id))).first()
        if not p:
            return
        res = await s.execute(update(Payment).where(Payment.id == p.id, Payment.status == "confirmed")
                              .values(status="chargebacked"))
        await s.commit()
    if res.rowcount == 1:
        await wallet.credit(p.user_id, -p.amount, "chargeback", comment=f"Возврат звёзд, счёт №{p.id}")
        log.warning("счёт %s: звёзды возвращены, списано %s ₽ у %s", p.id, p.amount, p.user_id)
        p.status = "chargebacked"
        await payments.notify(p, "chargebacked")


async def expire_old() -> None:
    """Неоплаченные счета в звёздах старше часа — закрыть (оплату по ним pre_checkout уже не пропустит)."""
    async with Session() as s:
        await s.execute(update(Payment).where(Payment.provider == PROVIDER, Payment.status == "pending",
                                              Payment.created_at < utcnow() - INVOICE_TTL).values(status="expired"))
        await s.commit()


async def bot_balance(bot) -> int:
    """Звёзды на балансе бота (включая ещё не доступные к выводу)."""
    return int((await bot.get_my_star_balance()).amount)

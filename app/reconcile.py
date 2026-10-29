"""«Сверка»: реальные деньги против того, что мы должны. Разница должна быть около нуля.

Деньги: кошелёк системы (USDT и TRX по курсу), Platega (₽ по курсу ЦБ + USDT), балансы площадок,
пополнения площадок в пути. TRX — тоже деньги: покупка TRX за USDT «Сверку» не сдвигает, а сожжённый
на комиссии TRX — расход (комиссию выводов оплачивают выводящие — их USDT остаются в кошельке).
Должны: балансы пользователей бота (₽ по курсу ЦБ), рефералам, владельцам, выводы в обработке
(уходящая сумма; комиссия, оплаченная выводящим, остаётся у нас), оборотные средства (деньги владельцев,
вложенные в площадки до начала учёта — не прибыль).

Плюс — лишние деньги (например, надбавка к курсу оказалась больше комиссий), минус — недостача.
Разницу распределяют на владельцев (owners.distribute). Пока на «Сверке» недостача, владельцы
не могут выводить — чтобы не вывести деньги, которых нет."""

import asyncio
import logging
import time
from decimal import Decimal

from sqlalchemy import func, select

from app import catalog, payments, payouts, stars, tron
from app import settings_store as st
from app.db import Session, utcnow
from app.models import Owner, OwnerTransaction, Payout, RefTransaction, User

log = logging.getLogger(__name__)
SHORTAGE_LIMIT = Decimal("1")  # недостача больше $1 — выводы владельцев на паузе
FRESH = 600  # расчёт старше 10 минут для проверки недостачи не годится

_last: dict = {}


async def _safe(coro, what: str, errors: list):
    try:
        return await coro
    except Exception as e:
        errors.append(f"{what}: {e}"[:200])
        log.warning("сверка, %s: %r", what, e)
        return None


async def compute() -> dict:
    rate = st.dec("usd_rate")
    errors: list[str] = []
    assets, debts = [], []

    wallet = trx = trx_price = None
    if tron.enabled():
        wallet, trx, trx_price = await asyncio.gather(_safe(tron.usdt_balance(), "кошелёк системы", errors),
                                                      _safe(tron.trx_balance(), "TRX", errors),
                                                      _safe(tron.trx_usd(), "курс TRX", errors))
        assets.append(("Кошелёк системы, USDT", wallet, ""))
        assets.append(("Кошелёк системы, TRX на комиссии",
                       trx * trx_price if trx is not None and trx_price is not None else None,
                       f"{trx:.1f} TRX × ${trx_price:.4f}" if trx is not None and trx_price is not None else ""))

    platega = await _safe(payments.balances(), "Platega", errors) if payments.enabled() else None
    if platega is not None:
        rub, usdt = platega.get("RUB", Decimal(0)), platega.get("USDT", Decimal(0))
        assets.append(("Platega", rub / rate + usdt, f"{rub:,.2f} ₽ + {usdt:,.2f} USDT".replace(",", " ")))

    if stars.enabled():  # звёзды на балансе бота — деньги по курсу вывода (новые — 21 день на удержании)
        from app.bot import channels
        n = await _safe(stars.bot_balance(channels.bot), "Telegram Stars", errors) if channels.bot else None
        if n is not None:
            usd = st.dec("stars_usd")
            assets.append(("Telegram Stars", Decimal(n) * usd,
                           f"⭐{n} × ${usd} — вывод через Fragment, новые звёзды доступны через 21 день"))

    for name, value in sorted(catalog.balances.items()):
        assets.append((f"Площадка {payouts.PROVIDER_TITLES.get(name, name)}", value, ""))

    async with Session() as s:
        in_flight = Decimal(await s.scalar(select(func.coalesce(func.sum(Payout.send_usd), 0)).where(
            Payout.purpose == "provider", Payout.status == "done", Payout.credited_at.is_(None))))
        users_rub = Decimal(await s.scalar(select(func.coalesce(func.sum(User.balance), 0))))
        refs = Decimal(await s.scalar(select(func.coalesce(func.sum(RefTransaction.amount_usd), 0))))
        owners = (await s.execute(select(Owner.login, func.coalesce(func.sum(OwnerTransaction.amount_usd), 0))
                                  .join(OwnerTransaction, OwnerTransaction.owner_id == Owner.id, isouter=True)
                                  .group_by(Owner.login).order_by(Owner.login))).all()
        leaving = Decimal(await s.scalar(select(func.coalesce(func.sum(Payout.send_usd), 0)).where(
            Payout.purpose == "withdrawal", Payout.status.in_(payouts.ACTIVE))))
    if in_flight:
        assets.append(("Пополнения площадок в пути", in_flight, "отправлены, площадка ещё не зачислила"))

    debts.append(("Балансы пользователей бота", users_rub / rate, f"{users_rub:,.2f} ₽".replace(",", " ")))
    debts.append(("Балансы рефералов", refs, ""))
    for login, total in owners:
        debts.append((f"Баланс {login}", Decimal(total), ""))
    if leaving:
        debts.append(("Выводы в обработке", leaving, "уходят из кошелька"))
    capital = st.dec("working_capital")
    if capital:
        debts.append(("Оборотные средства", capital, "вложены владельцами до начала учёта"))

    total_assets = sum((v for _, v, _ in assets if v is not None), Decimal(0))
    total_debts = sum((v for _, v, _ in debts), Decimal(0))
    diff = total_assets - total_debts
    result = {"assets": assets, "debts": debts, "total_assets": total_assets, "total_debts": total_debts,
              "diff": diff, "errors": errors, "complete": not errors, "rate": rate, "wallet": wallet, "trx": trx,
              "at": utcnow()}
    _last.update(result, mono=time.monotonic())
    return result


def shortage() -> Decimal | None:
    """Недостача по последнему полному расчёту (свежему) — или None, если её нет или расчёт неполный."""
    if not _last or not _last["complete"] or time.monotonic() - _last["mono"] > FRESH:
        return None
    return -_last["diff"] if _last["diff"] < -SHORTAGE_LIMIT else None

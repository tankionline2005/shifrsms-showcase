"""Владельцы: naomifontaine и kindergarten — вход в панель со всеми правами и доля прибыли.

Прибыль с активации (пришла SMS) после доли реферала делится между владельцами по share (50/50):
  прибыль ₽ = цена − закупка × курс покупки (с надбавкой к курсу — она страхует комиссии платёжки),
  владельцам $ = прибыль ₽ / курс ЦБ − доля реферала $.
Так же, как у рефералов, только остаток. Разницу с реальными деньгами (курс банка при выводе из
Platega, комиссии) показывает «Сверка», и её распределяют на владельцев той же пропорцией.

Ручной бонус пользователю (начисление в его карточке) — расход владельцев пропорцией.
Начисления идут с даты profit_since (день релиза); до неё — ничего."""

import logging
from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app import accounts, referrals, user_refs
from app import settings_store as st
from app.db import Session, utcnow
from app.models import Order, Owner, OwnerTransaction

log = logging.getLogger(__name__)
LOGINS = ("naomifontaine", "kindergarten")
USD = Decimal("0.0001")
KIND_TITLES = {"profit": "Прибыль", "bonus": "Бонус пользователю", "adjust": "Распределение разницы",
               "withdrawal": "Вывод", "withdrawal_back": "Вывод не прошёл — возврат"}


async def seed() -> None:
    """Создать владельцев, если их ещё нет (без пароля — его задают при первом входе через admin)."""
    async with Session() as s:
        have = set((await s.scalars(select(Owner.login))).all())
        for login in LOGINS:
            if login not in have:
                s.add(Owner(login=login, password_hash="", share=Decimal(100) / len(LOGINS)))
        await s.commit()


async def all_() -> list[Owner]:
    async with Session() as s:
        return list((await s.scalars(select(Owner).order_by(Owner.id))).all())


async def by_login(login: str) -> Owner | None:
    async with Session() as s:
        return (await s.scalars(select(Owner).where(Owner.login == login.strip().lower()))).first()


async def check_password(login: str, password: str) -> Owner | None:
    owner = await by_login(login)
    if owner and owner.password_hash and referrals.check_password(password, owner.password_hash):
        return owner
    return None


async def set_password(owner_id: int, password: str) -> None:
    """Новый пароль — и все прежние входы владельца закрыты."""
    async with Session() as s:
        row = await s.get(Owner, owner_id)
        row.password_hash = referrals.hash_password(password)
        row.session_ver = (row.session_ver or 1) + 1
        await s.commit()


async def sign_out_everywhere(owner_id: int) -> None:
    async with Session() as s:
        row = await s.get(Owner, owner_id)
        if row:
            row.session_ver = (row.session_ver or 1) + 1
            await s.commit()


async def admin_login_allowed() -> bool:
    """Общий вход admin живёт, пока оба владельца не привязали аутентификатор, — потом закрыт."""
    owners = await all_()
    return not owners or not all(o.totp_secret for o in owners)


# ---------------------------------------------------------------- прибыль


def profit_since() -> datetime | None:
    raw = st.raw("profit_since").strip()
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def _split(total: Decimal, owners: list[Owner]) -> list[tuple[Owner, Decimal]]:
    """Делим по share; последнему — остаток, чтобы сумма долей была ровно total."""
    parts, given = [], Decimal(0)
    for i, o in enumerate(owners):
        part = total - given if i == len(owners) - 1 else (total * o.share / 100).quantize(USD, rounding=ROUND_DOWN)
        given += part
        parts.append((o, part))
    return parts


async def on_sms(order_id: int) -> None:
    """Пришла SMS — деньги наши: доля реферала, остаток — владельцам (один раз на заказ)."""
    ref_usd = await referrals.accrue(order_id)
    invite_rub = await user_refs.accrue(order_id)  # пригласившему пользователю — на баланс бота, ₽
    since = profit_since()
    if not since or utcnow() < since:
        return
    owners = await all_()
    rate = st.dec("usd_rate")
    async with Session() as s:
        order = await s.get(Order, order_id)
        if not order or not owners or rate <= 0:
            return
        if await s.scalar(select(func.count()).select_from(OwnerTransaction)
                          .where(OwnerTransaction.order_id == order_id)):
            return
        profit_rub = order.price - order.cost_usd * order.usd_rate
        total = ((profit_rub - invite_rub) / rate - ref_usd).quantize(USD, rounding=ROUND_DOWN)
        if total == 0:
            return
        for owner, part in _split(total, owners):
            s.add(OwnerTransaction(owner_id=owner.id, kind="profit", amount_usd=part, order_id=order_id,
                                   profit_rub=profit_rub.quantize(Decimal("0.01")), usd_rate=rate))
        try:
            await s.commit()
        except IntegrityError:  # тот же заказ начислен параллельно
            return
    log.info("владельцам: %s$ с заказа %s", total, order_id)


async def bonus_expense(user_id: int, amount_rub: Decimal, actor: str) -> None:
    """Ручное начисление пользователю (бонус) — расход владельцев; списание с пользователя — наоборот."""
    since = profit_since()
    rate = st.dec("usd_rate")
    if not since or utcnow() < since or rate <= 0 or amount_rub == 0:
        return
    owners = await all_()
    total = -(amount_rub / rate).quantize(USD, rounding=ROUND_DOWN)
    async with Session() as s:
        for owner, part in _split(total, owners):
            s.add(OwnerTransaction(owner_id=owner.id, kind="bonus", amount_usd=part, usd_rate=rate, actor=actor,
                                   comment=f"пользователь {user_id}: {amount_rub:+.2f} ₽"))
        await s.commit()


async def distribute(diff_usd: Decimal, actor: str, comment: str) -> None:
    """Разница «Сверки» (+ лишние деньги или − недостача) — на балансы владельцев пропорцией."""
    owners = await all_()
    async with Session() as s:
        for owner, part in _split(diff_usd.quantize(USD, rounding=ROUND_DOWN), owners):
            s.add(OwnerTransaction(owner_id=owner.id, kind="adjust", amount_usd=part, actor=actor,
                                   comment=comment[:255]))
        await s.commit()
    await accounts.audit(actor, f"распределил разницу «Сверки»: {diff_usd:+.2f} $")


async def summary(owner_id: int) -> dict:
    async with Session() as s:
        earned = await s.scalar(select(func.coalesce(func.sum(OwnerTransaction.amount_usd), 0)).where(
            OwnerTransaction.owner_id == owner_id, OwnerTransaction.kind.in_(("profit", "bonus", "adjust"))))
        row = await s.get(Owner, owner_id)
    return {"balance": await accounts.balance(accounts.Acc("owner", owner_id)), "earned": Decimal(earned),
            "share": row.share if row else Decimal(0)}


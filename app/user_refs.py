"""Приглашения пользователей: обычный пользователь делится ссылкой на сайт <сайт>/r/<код> (link) и получает на баланс
(₽, вывести нельзя — только на активации) ту же долю, что каналы-партнёры (app/referrals.py): user_ref_revenue_percent %
цены активации, но не больше user_ref_profit_percent % нашей прибыли с неё — referrals.share. Начисление — когда
у приглашённого пришла SMS (orders.apply_status → owners.on_sms), один раз на заказ и пригласившего (UserRefReward),
уведомление пригласившему. Это наш расход: прибыль владельцев с заказа меньше на эту сумму (owners.on_sms).

Ссылка /r/<код> запоминает код в браузере на 90 дней (cookie invite, первый переход; app/web/shop/views.py) и ведёт в
каталог; засчитывается при регистрации на сайте, при входе через Telegram нового для бота человека и в боте —
кнопка «Открыть Telegram-бота» на главной несёт ?start=u_<код>. Старые ссылки t.me/бот?start=u_<код> работают.
Кодов у человека может быть несколько (RefCode: после склейки аккаунтов — коды обоих), показывается основной.

Ссылка появляется после первого пополнения баланса (подтверждённый платёж) — до этого меню просит пополнить.
За пригласившим закрепляется только НОВЫЙ пользователь — навсегда; человек привязан либо к каналу-партнёру,
либо к пользователю (кто первым привёл). Склейка аккаунтов может дать двух пригласивших — тогда каждому половина
(referrals.attributions)."""

import logging
import re
import secrets
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal
from typing import Awaitable, Callable

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app import referrals
from app import settings_store as st
from app.config import config
from app.db import Session, utcnow
from app.models import Order, Payment, RefCode, Transaction, User, UserRefReward

log = logging.getLogger(__name__)
PREFIX = "u_"
COOKIE = "invite"           # код из ссылки /r/<код> в браузере
COOKIE_DAYS = 90
KIND = "referral"  # Transaction.kind
CODE_ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"
CODE_LEN = 8
KOP = Decimal("0.01")

Notifier = Callable[[int, Decimal], Awaitable[None]]
_notifier: Notifier | None = None


def set_notifier(fn: Notifier) -> None:
    global _notifier
    _notifier = fn


def enabled() -> bool:
    return st.bool_("user_ref_enabled")


async def has_topped_up(user_id: int) -> bool:
    async with Session() as s:
        return bool(await s.scalar(select(func.count()).select_from(Payment).where(
            Payment.user_id == user_id, Payment.status == "confirmed")))


def link(code: str) -> str:
    """Ссылка-приглашение: на сайт, в каталог (app/web/shop/views.py::invite_link)."""
    return f"{config.public_url}/r/{code}"


def clean(code: str | None) -> str | None:
    code = (code or "").strip().lower()
    return code if re.fullmatch(r"[a-z0-9]{4,16}", code) else None


async def code_of(user_id: int) -> str | None:
    """Основной код ссылки пользователя (создаётся при первом запросе). None — ещё не пополнял баланс."""
    if not await has_topped_up(user_id):
        return None
    async with Session() as s:
        user = await s.get(User, user_id)
        if not user:
            return None
        if user.ref_code:
            if not await s.get(RefCode, user.ref_code):  # код старше таблицы ref_codes (обычно переносит db._migrate)
                s.add(RefCode(code=user.ref_code, user_id=user_id))
                await s.commit()
            return user.ref_code
        for _ in range(20):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
            if not await s.get(RefCode, code):
                break
        user.ref_code = code
        s.add(RefCode(code=code, user_id=user_id))
        await s.commit()
    return code


async def owner_of(code: str | None) -> int | None:
    """Чей код (любой из кодов человека, в т. ч. доставшийся при склейке). -> id пользователя или None."""
    code = clean(code)
    if not code:
        return None
    async with Session() as s:
        row = await s.get(RefCode, code)
        if row:
            return row.user_id
        return await s.scalar(select(User.id).where(User.ref_code == code).limit(1))


async def bind(user_id: int, start_arg: str | None) -> bool:
    """Новый пользователь пришёл по ссылке другого пользователя (start_arg = u_<код>) — закрепить навсегда."""
    if not enabled() or not start_arg or not start_arg.startswith(PREFIX):
        return False
    inviter_id = await owner_of(start_arg[len(PREFIX):])
    async with Session() as s:
        inviter = await s.get(User, inviter_id) if inviter_id else None
        user = await s.get(User, user_id)
        if not inviter or not user or inviter.id == user_id or referrals.attributions(user):
            return False
        user.inviter_id = inviter.id
        await s.commit()
    log.info("пользователь %s пришёл по приглашению %s", user_id, inviter.id)
    return True


async def accrue(order_id: int) -> Decimal:
    """По активации приглашённого пришла SMS — пригласившим их долю на баланс (один раз на заказ и пригласившего;
    пригласивших двое — каждому половина). -> начисленная сумма, ₽ (0 — нечего начислять)."""
    if not enabled():
        return Decimal(0)
    async with Session() as s:
        order = await s.get(Order, order_id)
        user = await s.get(User, order.user_id) if order else None
    attrs = referrals.attributions(user)
    total = Decimal(0)
    for inviter_id in [uid for kind, uid in attrs if kind == "user"]:
        async with Session() as s:
            inviter = await s.get(User, inviter_id)
            if not inviter or inviter.is_blocked:
                continue
            done = await s.get(UserRefReward, (order_id, inviter_id))
            if done:
                total += Decimal(done.amount)
                continue
            revenue_percent, profit_percent = st.dec("user_ref_revenue_percent"), st.dec("user_ref_profit_percent")
            profit = order.price - order.cost_usd * order.usd_rate
            rub, capped = referrals.share(order.price, profit, revenue_percent, profit_percent)
            amount = (rub / len(attrs)).quantize(KOP, rounding=ROUND_DOWN)
            if amount <= 0:
                continue
            s.add(UserRefReward(order_id=order_id, inviter_id=inviter.id, invitee_id=user.id, amount=amount,
                                price=order.price, profit=profit.quantize(KOP), revenue_percent=revenue_percent,
                                percent=profit_percent, capped=capped, split=len(attrs) if len(attrs) > 1 else None))
            await s.execute(update(User).where(User.id == inviter.id).values(balance=User.balance + amount))
            s.add(Transaction(user_id=inviter.id, amount=amount, kind=KIND, order_id=order_id,
                              comment=f"приглашённый {user.id}, активация #{order_id}"))
            try:
                await s.commit()
            except IntegrityError:  # тот же заказ начислен параллельно
                continue
        log.info("приглашение: +%s ₽ пользователю %s с заказа %s", amount, inviter_id, order_id)
        total += amount
        if _notifier:
            try:
                await _notifier(inviter_id, amount)
            except Exception:
                log.exception("уведомление о начислении за приглашение")
    return total


async def stats(user_id: int) -> dict:
    """Для меню «Пригласить друга»: сколько пригласил, сколько из них покупали, сколько заработал."""
    month_ago = utcnow() - timedelta(days=30)
    async with Session() as s:
        invited = await s.scalar(select(func.count()).select_from(User).where(
            or_(User.inviter_id == user_id, User.inviter2_id == user_id)))
        buyers = await s.scalar(select(func.count(func.distinct(UserRefReward.invitee_id)))
                                .where(UserRefReward.inviter_id == user_id))
        earned = await s.scalar(select(func.coalesce(func.sum(UserRefReward.amount), 0))
                                .where(UserRefReward.inviter_id == user_id))
        month = await s.scalar(select(func.coalesce(func.sum(UserRefReward.amount), 0))
                               .where(UserRefReward.inviter_id == user_id, UserRefReward.created_at >= month_ago))
    return {"invited": invited or 0, "buyers": buyers or 0, "earned": Decimal(earned), "month": Decimal(month)}


async def total(since=None) -> Decimal:
    """Всего начислено за приглашения (для «Сводки»)."""
    async with Session() as s:
        q = select(func.coalesce(func.sum(UserRefReward.amount), 0))
        if since:
            q = q.where(UserRefReward.created_at >= since)
        return Decimal(await s.scalar(q))

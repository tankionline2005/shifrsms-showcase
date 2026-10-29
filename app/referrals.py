"""Рефералы: друзья со своими каналами (см. models.Referral).

Привязка: кнопка «Открыть бота» под постами канала реферала ведёт на t.me/бот?start=ref_<code>.
За рефералом закрепляется только НОВЫЙ пользователь (впервые открыл бота) — навсегда; давний
клиент, случайно нажавший кнопку у друга, «его» не становится.

Начисление: когда по активации такого пользователя пришла SMS (деньги больше не вернутся),
рефералу идёт revenue_percent % цены активации, но не больше percent % нашей прибыли с неё (прибыль — цена минус
закупка по курсу на момент покупки): share(). Пример при 15% / 50%: цена 100, прибыль 10 → 5; прибыль 40 → 15.
Баланс реферала — в долларах по курсу ЦБ на момент начисления (без надбавки к курсу).

Склейка аккаунтов (app/shop_accounts.merge) может дать человеку двух пригласивших — каналы и/или пользователей
(attributions). Тогда каждый получает свою обычную долю, делённую на число пригласивших: общая доля с человека
не растёт."""

import hashlib
import hmac
import logging
import re
import secrets
from decimal import ROUND_DOWN, Decimal

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app import settings_store as st
from app.db import Session
from app.models import BotChat, Order, Payout, Referral, RefTransaction, SavedWallet, User

log = logging.getLogger(__name__)
START_PREFIX = "ref_"
DEFAULT_PERCENT = Decimal("50")  # максимум от прибыли
DEFAULT_REVENUE_PERCENT = Decimal("15")  # доля от выручки
_ITERATIONS = 200_000
USD = Decimal("0.0001")
MIN_LEFT = Decimal("0.01")  # остаток меньше цента удалению не мешает


# ---------------------------------------------------------------- пароли


def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), _ITERATIONS).hex()
    return f"pbkdf2${_ITERATIONS}${salt}${digest}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, digest = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iterations)).hex()
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got, digest)


def new_password() -> str:
    return secrets.token_urlsafe(9)  # 12 символов


# ---------------------------------------------------------------- управление


async def create(chat: BotChat) -> tuple[Referral, str]:
    """Сделать связку рефералом. -> (реферал, пароль — показать один раз)."""
    password = new_password()
    async with Session() as s:
        taken = set((await s.scalars(select(Referral.login))).all())
        # логин — username канала + его ID (ID канала отрицательный): naomifontainemommy-1004345594337
        base = (re.sub(r"[^a-z0-9_]", "", (chat.username or "").lower()) or "channel") + str(chat.id)
        login, n = base, 1
        while login in taken:
            n += 1
            login = f"{base}{n}"
        codes = set((await s.scalars(select(Referral.code))).all())
        code = secrets.token_hex(4)
        while code in codes:
            code = secrets.token_hex(4)
        ref = Referral(chat_id=chat.id, code=code, login=login, password_hash=hash_password(password),
                       percent=DEFAULT_PERCENT, revenue_percent=DEFAULT_REVENUE_PERCENT)
        s.add(ref)
        await s.commit()
    log.info("реферал %s (%s) для канала %s", ref.id, login, chat.id)
    return ref, password


async def reset_password(referral_id: int) -> str | None:
    password = new_password()
    async with Session() as s:
        ref = await s.get(Referral, referral_id)
        if not ref:
            return None
        ref.password_hash = hash_password(password)
        ref.session_ver = (ref.session_ver or 1) + 1  # прежние входы в кабинет закрыты
        await s.commit()
    return password


async def sign_out_everywhere(referral_id: int) -> None:
    async with Session() as s:
        ref = await s.get(Referral, referral_id)
        if ref:
            ref.session_ver = (ref.session_ver or 1) + 1
            await s.commit()


async def blocker(referral_id: int) -> str | None:
    """Почему реферала нельзя удалить: на балансе есть деньги (мы ему должны) или вывод ещё в пути
    (если сеть его отклонит, деньги вернутся на баланс). Остаток меньше цента не мешает."""
    left = await balance(referral_id)
    if left >= MIN_LEFT:
        return f"На балансе реферала ${left:.2f} — сначала он должен вывести их в своём кабинете."
    async with Session() as s:
        active = await s.scalar(select(func.count()).select_from(Payout).where(
            Payout.account == "ref", Payout.account_id == referral_id, Payout.status.in_(("queued", "signed"))))
    if active:
        return "Вывод реферала ещё выполняется — дождитесь, пока он завершится."
    return None


async def remove(referral_id: int) -> str | None:
    """Удалить реферала: связка снова обычная. Только когда мы ему ничего не должны (blocker). Его люди
    открепляются, история и сохранённые адреса стираются, кабинет закрывается. -> текст ошибки или None."""
    if error := await blocker(referral_id):
        return error
    async with Session() as s:
        await s.execute(delete(SavedWallet).where(SavedWallet.account == "ref", SavedWallet.account_id == referral_id))
        await s.execute(update(User).where(User.referral_id == referral_id).values(referral_id=None))
        await s.execute(update(User).where(User.referral2_id == referral_id).values(referral2_id=None))
        await s.execute(delete(RefTransaction).where(RefTransaction.referral_id == referral_id))
        await s.execute(delete(Referral).where(Referral.id == referral_id))
        await s.commit()
    log.info("реферал %s удалён", referral_id)
    return None


async def by_chat(chat_id: int) -> Referral | None:
    async with Session() as s:
        return (await s.scalars(select(Referral).where(Referral.chat_id == chat_id))).first()


async def login(login_: str, password: str) -> Referral | None:
    async with Session() as s:
        ref = (await s.scalars(select(Referral).where(Referral.login == login_.strip().lower()))).first()
    return ref if ref and check_password(password, ref.password_hash) else None


# ---------------------------------------------------------------- привязка и начисления


def attributions(user: User | None) -> list[tuple[str, int]]:
    """Кто привёл пользователя: [("channel", id реферала) | ("user", id пригласившего)] — не больше двух
    (второй — только после склейки аккаунтов); доля с его активаций делится между ними поровну."""
    out: list[tuple[str, int]] = []
    if user is not None:
        for kind, value in (("channel", user.referral_id), ("channel", user.referral2_id),
                            ("user", user.inviter_id), ("user", user.inviter2_id)):
            if value and (kind, value) not in out:
                out.append((kind, value))
    return out


async def bind(user_id: int, start_arg: str | None) -> bool:
    """Новый пользователь пришёл по ссылке реферала — закрепить за ним навсегда."""
    if not start_arg or not start_arg.startswith(START_PREFIX):
        return False
    code = start_arg[len(START_PREFIX):]
    async with Session() as s:
        ref = (await s.scalars(select(Referral).where(Referral.code == code))).first()
        user = await s.get(User, user_id)
        if not ref or not user or attributions(user):
            return False
        user.referral_id = ref.id
        await s.commit()
    log.info("пользователь %s пришёл от реферала %s", user_id, ref.id)
    return True


def share(price: Decimal, profit: Decimal, revenue_percent: Decimal, max_profit_percent: Decimal) -> tuple[Decimal, bool]:
    """Доля реферала с активации, ₽: revenue_percent % цены, но не больше max_profit_percent % прибыли.
    -> (сумма, упёрлись ли в потолок от прибыли). Нет прибыли — ничего."""
    if profit <= 0 or price <= 0:
        return Decimal(0), False
    by_revenue = price * revenue_percent / 100
    by_profit = profit * max_profit_percent / 100
    return (by_profit, True) if by_profit < by_revenue else (by_revenue, False)


async def accrue(order_id: int) -> Decimal:
    """По активации пришла SMS — начислить рефералам их долю нашей прибыли (один раз на заказ и реферала).
    -> начисленная рефералам сумма, $ (0 — рефералов нет или прибыли нет); владельцам — остальное."""
    async with Session() as s:
        order = await s.get(Order, order_id)
        user = await s.get(User, order.user_id) if order else None
    attrs = attributions(user)
    total = Decimal(0)
    for ref_id in [ref_id for kind, ref_id in attrs if kind == "channel"]:
        async with Session() as s:
            ref = await s.get(Referral, ref_id)
            if not ref:
                continue
            done = await s.scalar(select(RefTransaction.amount_usd).where(
                RefTransaction.order_id == order_id, RefTransaction.referral_id == ref_id))
            if done is not None:
                total += Decimal(done)  # уже начислено
                continue
            profit = order.price - order.cost_usd * order.usd_rate
            rate = st.dec("usd_rate")  # курс ЦБ, без нашей надбавки
            revenue_percent = ref.revenue_percent if ref.revenue_percent is not None else DEFAULT_REVENUE_PERCENT
            rub, capped = share(order.price, profit, revenue_percent, ref.percent)
            rub /= len(attrs)  # пригласивших двое — каждому половина его доли
            if rub <= 0 or rate <= 0:
                continue
            usd = (rub / rate).quantize(USD, rounding=ROUND_DOWN)
            if usd <= 0:
                continue
            s.add(RefTransaction(referral_id=ref.id, kind="accrual", amount_usd=usd, order_id=order_id,
                                 profit_rub=profit.quantize(Decimal("0.01")), percent=ref.percent, usd_rate=rate,
                                 revenue_percent=revenue_percent, price_rub=order.price,
                                 share_rub=rub.quantize(Decimal("0.01")), capped=capped,
                                 split=len(attrs) if len(attrs) > 1 else None))
            try:
                await s.commit()
            except IntegrityError:  # тот же заказ начислен параллельно
                continue
        log.info("реферал %s: +$%s с заказа %s", ref_id, usd, order_id)
        total += usd
    return total


async def balance(referral_id: int) -> Decimal:
    async with Session() as s:
        total = await s.scalar(select(func.coalesce(func.sum(RefTransaction.amount_usd), 0))
                               .where(RefTransaction.referral_id == referral_id))
    return Decimal(total)


async def summary(referral_id: int) -> dict:
    """Для карточки связки: баланс, всего заработано, сколько людей привёл."""
    async with Session() as s:
        earned = await s.scalar(select(func.coalesce(func.sum(RefTransaction.amount_usd), 0)).where(
            RefTransaction.referral_id == referral_id, RefTransaction.kind == "accrual"))
        users = await s.scalar(select(func.count()).select_from(User).where(
            or_(User.referral_id == referral_id, User.referral2_id == referral_id)))
    return {"balance": await balance(referral_id), "earned": Decimal(earned), "users": users,
            "blocker": await blocker(referral_id)}


KIND_TITLES = {"accrual": "Начисление", "payout": "Выплата", "withdrawal": "Вывод на кошелёк",
               "withdrawal_back": "Вывод не прошёл — возврат"}

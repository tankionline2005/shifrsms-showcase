"""UTM-метки в ссылках на бота: https://t.me/<бот>?start=utm_<метка>.

Telegram передаёт боту только один параметр start (до 64 символов: латиница, цифры, _ и -), поэтому метка —
одна строка. Договорённость: части через дефис — источник-кампания-объявление, например utm_tiktok-oct-video1.
Метка приводится к нижнему регистру. Новый пользователь закрепляется за первой меткой навсегда (User.utm);
каждый /start по такой ссылке, в том числе от уже знакомых, записывается в UtmStart — это «переходы».

Статистика (раздел панели «UTM-метки»): люди, пришедшие за период, и всё, что они сделали за всё время."""

import logging
import re
from collections import defaultdict
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select, true

from app.db import Session, utcnow
from app.models import Order, Payment, User, UtmStart

log = logging.getLogger(__name__)

PREFIX = "utm_"
MAX_LABEL = 64 - len(PREFIX)  # лимит Telegram на параметр start — 64 символа
LABEL_RE = re.compile(r"[a-z0-9_-]+")
SMS_STATUSES = ("received", "completed")
NO_LABEL = ""  # строка «Без метки» в статистике


def parse(start_arg: str | None) -> str | None:
    """Параметр /start -> метка или None (не UTM-ссылка или метка с недопустимыми символами)."""
    if not start_arg or not start_arg.lower().startswith(PREFIX):
        return None
    label = start_arg[len(PREFIX):].lower()
    return label if 0 < len(label) <= MAX_LABEL and LABEL_RE.fullmatch(label) else None


def check(label: str) -> str | None:
    """Метка из конструктора ссылки в панели. -> текст ошибки или None."""
    if not label:
        return "Напишите метку."
    if len(label) > MAX_LABEL:
        return f"Метка длиннее {MAX_LABEL} символов — Telegram такую ссылку обрежет."
    if not LABEL_RE.fullmatch(label):
        return "Только латинские буквы, цифры, «_» и «-» — без пробелов и кириллицы."
    return None


async def record(user_id: int, label: str, new: bool) -> None:
    async with Session() as s:
        s.add(UtmStart(label=label, user_id=user_id, new=new))
        if new:
            user = await s.get(User, user_id)
            if user and not user.utm:
                user.utm = label
        await s.commit()
    if new:
        log.info("пользователь %s пришёл по метке %s", user_id, label)


def source(label: str) -> str:
    return label.split("-", 1)[0] if label else label


async def stats(days: int, by_source: bool) -> dict:
    """Строки по меткам (или источникам — первая часть метки до дефиса) за период: days=0 — за всё время.
    Люди — пришедшие за период; их пополнения и активации — за всё время."""
    since = utcnow() - timedelta(days=days) if days else None
    new_user = User.created_at >= since if since else true()
    async with Session() as s:
        starts = (await s.execute(select(UtmStart.label, func.count())
                                  .where(UtmStart.created_at >= since if since else true())
                                  .group_by(UtmStart.label))).all()
        users = (await s.execute(select(User.utm, func.count()).where(new_user).group_by(User.utm))).all()
        pays = (await s.execute(
            select(User.utm, func.count(func.distinct(Payment.user_id)), func.coalesce(func.sum(Payment.amount), 0))
            .join(User, User.id == Payment.user_id)
            .where(new_user, Payment.status == "confirmed").group_by(User.utm))).all()
        orders = (await s.execute(
            select(User.utm, func.count(func.distinct(Order.user_id)), func.count(Order.id),
                   func.coalesce(func.sum(Order.price), 0),
                   func.coalesce(func.sum(Order.price - Order.cost_usd * Order.usd_rate), 0))
            .join(User, User.id == Order.user_id)
            .where(new_user, Order.status.in_(SMS_STATUSES)).group_by(User.utm))).all()
    key = source if by_source else (lambda x: x)
    rows: dict[str, dict] = defaultdict(lambda: {"starts": 0, "new": 0, "with_sms": 0, "payers": 0,
                                                  "paid": Decimal(0), "sms": 0, "revenue": Decimal(0),
                                                  "profit": Decimal(0)})
    for label, n in starts:
        rows[key(label)]["starts"] += n
    for label, n in users:
        rows[key(label or NO_LABEL)]["new"] += n
    for label, n, total in pays:
        r = rows[key(label or NO_LABEL)]
        r["payers"] += n
        r["paid"] += Decimal(total)
    for label, n, cnt, revenue, profit in orders:
        r = rows[key(label or NO_LABEL)]
        r["with_sms"] += n
        r["sms"] += cnt
        r["revenue"] += Decimal(revenue)
        r["profit"] += Decimal(profit)
    labeled = sorted(((k, v) for k, v in rows.items() if k != NO_LABEL),
                     key=lambda kv: (-kv[1]["new"], -kv[1]["starts"], kv[0]))
    total = {f: sum(v[f] for _, v in labeled) for f in ("starts", "new", "with_sms", "payers", "paid", "sms",
                                                         "revenue", "profit")}
    return {"rows": labeled, "total": total, "no_label": rows.get(NO_LABEL)}

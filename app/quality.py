"""Статистика качества: выдают ли поставщики номера и доходят ли SMS.

Два источника:
  • попытки покупки (buy_attempts) — поставщик выдал номер, ответил «нет номеров»
    («фантом»: в прайсе был, купить нельзя) или ошибкой;
  • активации (orders) — чем закончилась выданная активация.

Исход активации:
  ok      — пришла хотя бы одна SMS;
  no_sms  — срок истёк без SMS (провал);
  failed  — отмена или замена без SMS: замена — провал, отмена — провал, если ждали
            quality_quick_cancel_minutes и дольше;
  mind    — отмена раньше quality_quick_cancel_minutes — «передумал», не в счёт;
  active  — ещё ждёт SMS — не в счёт.
Успешность SMS = ok / (ok + no_sms + failed).
Выдача номера = выдано / (выдано + нет номеров + ошибки площадки); нехватка нашего баланса не в счёт.

Для бота статистика за quality_days пересчитывается фоновой задачей и лежит в памяти.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from statistics import median
from typing import Callable, Hashable

from sqlalchemy import delete, func, select

from app import settings_store as st
from app.db import Session, utcnow
from app.models import BuyAttempt, Order, OrderSms

ATTEMPTS_KEEP = timedelta(days=365)

# Надёжность = доля пришедших SMS × поправка на скорость. Доля сглажена к уровню «на ступень
# выше» (SMS_PRIOR_WEIGHT «виртуальных» активаций): поставщик в стране — к стране, страна — к
# сервису в целом, сервис — к 50%. Так «нет данных» означает «как в среднем», а не 50%, и
# 1 успех из 1 не выглядит как 100%. Чем больше своих данных, тем меньше влияние среднего.
# Фантомы (поставщик не выдал номер) в надёжность не входят: качество номеров они не
# характеризуют, а с пустыми прайсами справляется карантин (catalog.drop_tiers).
SMS_PRIOR, SMS_PRIOR_WEIGHT = 0.5, 5  # для сервиса без данных — как 5 активаций с 50%
FAST_SMS, SLOW_SMS, SLOW_PENALTY = 60, 300, 0.1  # сек: до FAST без штрафа, к SLOW — минус 10%
PRICE_BOUNDS = (40, 60, 80, 120, 200)  # границы уровней цены на странице «Качество», ₽


@dataclass
class Activation:
    provider: str
    supplier: str | None
    service_id: int
    country_id: int
    price: Decimal
    outcome: str
    sms_seconds: float | None


@dataclass
class Attempt:
    provider: str
    supplier: str | None
    service_id: int
    country_id: int
    price: Decimal
    result: str


@dataclass
class Stat:
    ok: int = 0
    no_sms: int = 0
    failed: int = 0
    mind: int = 0
    active: int = 0
    sms_seconds: list[float] = field(default_factory=list)
    bought: int = 0
    phantom: int = 0
    errors: int = 0

    @property
    def activations(self) -> int:
        return self.counted + self.mind + self.active

    @property
    def counted(self) -> int:
        """Активации, которые говорят о качестве поставщика: успех или провал."""
        return self.ok + self.no_sms + self.failed

    @property
    def rate(self) -> float | None:
        return self.ok / self.counted if self.counted else None

    @property
    def attempts(self) -> int:
        return self.bought + self.phantom + self.errors

    @property
    def buy_rate(self) -> float | None:
        return self.bought / self.attempts if self.attempts else None

    @property
    def median_sms_seconds(self) -> float | None:
        return median(self.sms_seconds) if self.sms_seconds else None

    def sms_rate(self, prior: float) -> float:
        """Доля пришедших SMS, сглаженная к prior — уровню на ступень выше."""
        return (self.ok + prior * SMS_PRIOR_WEIGHT) / (self.counted + SMS_PRIOR_WEIGHT)

    @property
    def speed(self) -> float:
        """Поправка на скорость: SMS до FAST_SMS — 1, к SLOW_SMS медианы — минус SLOW_PENALTY."""
        if self.median_sms_seconds is None:
            return 1.0
        slow = (self.median_sms_seconds - FAST_SMS) / (SLOW_SMS - FAST_SMS)
        return 1 - SLOW_PENALTY * min(1.0, max(0.0, slow))

    def score(self, prior: float) -> float:
        """Надёжность 0..1: шанс, что на выданный номер придёт код — по нашей статистике."""
        return self.sms_rate(prior) * self.speed

    def add_activation(self, a: Activation) -> None:
        setattr(self, a.outcome, getattr(self, a.outcome) + 1)
        if a.sms_seconds is not None:
            self.sms_seconds.append(a.sms_seconds)

    def add_attempt(self, a: Attempt) -> None:
        if a.result == "ok":
            self.bought += 1
        elif a.result == "no_numbers":
            self.phantom += 1
        elif a.result == "error":
            self.errors += 1


def classify(status: str, created_at: datetime, closed_at: datetime | None,
             first_sms: datetime | None) -> str:
    if first_sms is not None:
        return "ok"
    if status == "expired":
        return "no_sms"
    if status == "replaced":
        return "failed"
    if status == "cancelled":
        waited = ((closed_at or utcnow()) - created_at).total_seconds()
        return "mind" if waited < st.int_("quality_quick_cancel_minutes") * 60 else "failed"
    return "active"


async def load(since: datetime | None) -> tuple[list[Activation], list[Attempt]]:
    """Активации и попытки покупки с момента since (None — за всё время)."""
    first_sms = (select(OrderSms.order_id, func.min(OrderSms.received_at).label("first"))
                 .group_by(OrderSms.order_id).subquery())
    oq = (select(Order.provider, Order.supplier, Order.service_id, Order.country_id, Order.price,
                 Order.status, Order.created_at, Order.closed_at, first_sms.c.first)
          .outerjoin(first_sms, first_sms.c.order_id == Order.id))
    aq = select(BuyAttempt.provider, BuyAttempt.supplier, BuyAttempt.service_id,
                BuyAttempt.country_id, BuyAttempt.price, BuyAttempt.result)
    if since:
        oq = oq.where(Order.created_at >= since)
        aq = aq.where(BuyAttempt.created_at >= since)
    async with Session() as s:
        orders = (await s.execute(oq)).all()
        attempts = (await s.execute(aq)).all()
    acts = [Activation(p, sup, sid, cid, price, classify(status, created, closed, first),
                       (first - created).total_seconds() if first else None)
            for p, sup, sid, cid, price, status, created, closed, first in orders]
    return acts, [Attempt(*row) for row in attempts]


def aggregate(acts: list[Activation], atts: list[Attempt],
              key: Callable[[Activation | Attempt], Hashable]) -> dict[Hashable, Stat]:
    stats: dict[Hashable, Stat] = defaultdict(Stat)
    for a in acts:
        stats[key(a)].add_activation(a)
    for a in atts:
        stats[key(a)].add_attempt(a)
    return dict(stats)


def price_bucket(price: Decimal) -> int:
    """Номер уровня цены: 0 — до PRICE_BOUNDS[0] ₽ и т.д."""
    return next((i for i, b in enumerate(PRICE_BOUNDS) if price < b), len(PRICE_BOUNDS))


def price_bucket_title(i: int) -> str:
    if i == 0:
        return f"до {PRICE_BOUNDS[0]} ₽"
    if i == len(PRICE_BOUNDS):
        return f"от {PRICE_BOUNDS[-1]} ₽"
    return f"{PRICE_BOUNDS[i - 1]}–{PRICE_BOUNDS[i] - 1} ₽"


# ---------------------------------------------------------------- для выбора поставщика в боте

# Ступени оценки: сервис -> страна -> поставщик в стране
by_service: dict[Hashable, Stat] = {}  # service_id -> Stat
by_country: dict[Hashable, Stat] = {}  # (service_id, country_id) -> Stat
by_supplier_offer: dict[Hashable, Stat] = {}  # (provider, supplier, service_id, country_id) -> Stat


async def refresh() -> None:
    acts, atts = await load(utcnow() - timedelta(days=st.int_("quality_days")))
    for target, key in ((by_service, lambda r: r.service_id),
                        (by_country, lambda r: (r.service_id, r.country_id)),
                        (by_supplier_offer, lambda r: (r.provider, r.supplier, r.service_id, r.country_id))):
        target.clear()
        target.update(aggregate(acts, atts, key))
    async with Session() as s:
        await s.execute(delete(BuyAttempt).where(BuyAttempt.created_at < utcnow() - ATTEMPTS_KEEP))
        await s.commit()


def _service_sms(service_id: int) -> float:
    return (by_service.get(service_id) or Stat()).sms_rate(SMS_PRIOR)


def _country_sms(service_id: int, country_id: int) -> float:
    return (by_country.get((service_id, country_id)) or Stat()).sms_rate(_service_sms(service_id))


def reliability(provider: str, supplier: str | None, service_id: int, country_id: int) -> float:
    """Надёжность поставщика в стране. Без данных — уровень этой страны: поставщик ничем не
    хуже и не лучше остальных, пока сам себя не показал."""
    stat = by_supplier_offer.get((provider, supplier, service_id, country_id)) or Stat()
    country = by_country.get((service_id, country_id)) or Stat()
    speed = stat.speed if stat.sms_seconds else country.speed  # своей скорости нет — как у страны
    return stat.sms_rate(_country_sms(service_id, country_id)) * speed


def human_seconds(sec: float | None) -> str:
    if sec is None:
        return "—"
    if sec < 60:
        return f"{int(sec)} сек"
    return f"{sec / 60:.1f} мин".replace(".0 ", " ")

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

import httpx

USER_AGENT = "Mozilla/5.0 (compatible; ShifrSMS/1.0)"


class ProviderError(Exception):
    """Любая ошибка площадки."""


class NoNumbers(ProviderError):
    """Нет номеров под заданные условия (в т.ч. по цене)."""


class PriceTooHigh(NoNumbers):
    """Площадка без лимита цены выдала номер дороже лимита: по нашей цене номеров нет.
    Номер уже куплен, а сразу отменить его нельзя — purchase нужно отменить в фоне."""

    def __init__(self, message: str, purchase: "Purchase"):
        super().__init__(message)
        self.purchase = purchase


class ServiceBlocked(ProviderError):
    """Площадка закрыла нам этот сервис (например, SMSPool — за частые отмены), пока не
    напишем в её поддержку. Ждать минуту бессмысленно — сервис этой площадки прячем надолго."""


class NoProviderBalance(ProviderError):
    """На нашем счёте у площадки закончились деньги."""


@dataclass
class ServiceInfo:
    code: str
    name: str


@dataclass
class CountryInfo:
    code: str
    name_en: str
    name_ru: str | None = None
    iso: str | None = None


Tier = tuple[Decimal, int]  # (цена USD, сколько номеров по этой цене)


@dataclass
class PriceItem:
    service_code: str
    country_code: str
    tiers: list[Tier]  # только уровни с остатком > 0, по возрастанию цены
    # поставщик внутри площадки: id — для покупки у него, label — для статистики и панели.
    # None — площадка не раскрыла поставщиков для этой пары
    supplier_id: str | None = None
    supplier: str | None = None


@dataclass
class Purchase:
    order_id: str
    phone: str  # только цифры, с кодом страны
    cost_usd: Decimal
    lifetime_sec: int
    cancel_after_sec: int
    can_get_another: bool
    supplier: str | None = None  # поставщик внутри площадки, если площадка его сообщает


@dataclass
class Sms:
    code: str | None
    text: str


@dataclass
class Status:
    state: Literal["waiting", "sms", "closed"]
    sms: list[Sms] = field(default_factory=list)


CancelResult = Literal["ok", "too_early", "has_sms", "failed"]


class Provider:
    name: str
    title: str

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(30, read=90),
                                      headers={"User-Agent": USER_AGENT})

    async def close(self) -> None:
        await self.http.aclose()

    # --- справочники и цены ---
    async def get_services(self) -> list[ServiceInfo]: raise NotImplementedError
    async def get_countries(self) -> list[CountryInfo]: raise NotImplementedError
    async def get_prices(self) -> list[PriceItem]: raise NotImplementedError

    async def get_pair(self, service_code: str, country_code: str) -> list[PriceItem]:
        """Свежие цены/остатки одной пары по поставщикам — прямо перед покупкой
        (общий прайс быстро устаревает)."""
        raise NotImplementedError

    async def get_balance(self) -> Decimal: raise NotImplementedError

    # --- активации ---
    async def buy(self, service_code: str, country_code: str, max_cost: Decimal,
                  supplier_id: str | None = None) -> Purchase:
        """Покупка. supplier_id — купить именно у этого поставщика площадки."""
        raise NotImplementedError

    async def get_active(self) -> dict[str, Status]:
        """Статусы всех активных заказов одним запросом: {order_id: Status}."""
        raise NotImplementedError

    async def get_status(self, order_id: str) -> Status: raise NotImplementedError
    async def cancel(self, order_id: str) -> CancelResult: raise NotImplementedError
    async def finish(self, order_id: str) -> None: raise NotImplementedError
    async def request_another(self, order_id: str) -> bool: raise NotImplementedError

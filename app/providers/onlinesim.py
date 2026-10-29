"""Onlinesim — https://onlinesim.io/openapi_docs/Onlinesim-API-UN/info/

Одна цена на пару сервис+страна, поставщиков площадка не раскрывает, реальных остатков нет
(всегда 9999), лимита цены при покупке нет. Страна — телефонный код (62 — Индонезия).
Отмена без SMS — только после защитного интервала: 2 мин, для WhatsApp — 5 мин.
Цены и баланс — в долларах. SMS приходят вебхуком (адрес задаётся в профиле Onlinesim)."""

import asyncio
import logging
import re
from decimal import Decimal

import httpx

from app.providers.base import (
    CancelResult,
    CountryInfo,
    NoNumbers,
    NoProviderBalance,
    PriceItem,
    PriceTooHigh,
    Provider,
    ProviderError,
    Purchase,
    ServiceInfo,
    Sms,
    Status,
)

log = logging.getLogger(__name__)
URL = "https://onlinesim.io/api"
CANCEL_AFTER = {"whatsapp": 300}  # сек; остальные сервисы — 120
WAITING = {"TZ_NUM_PREPARE", "TZ_NUM_WAIT"}
CLOSED = {"TZ_OVER_EMPTY", "TZ_OVER_OK", "TZ_OVER_NR", "TZ_DELETED", "ERROR_NO_OPERATIONS"}


def _service_name(slug: str, title: str) -> str:
    """Имя для сопоставления с каталогом. Названия у Onlinesim с пояснениями в скобках
    («Google (Youtube, Gmail)») и бывают по-русски («Яндекс») — тогда лучше латинский slug."""
    plain = re.sub(r"\s*\(.*?\)", "", title or "").strip()
    if re.search("[a-zA-Z]", plain):
        return plain
    return slug if re.search("[a-zA-Z]", slug) else (title or slug)


def _messages(msg) -> list[Sms]:
    """msg — строка или список {service, msg}."""
    if not msg:
        return []
    if isinstance(msg, str):
        return [Sms(None, msg)]
    return [Sms(None, str(m.get("msg") if isinstance(m, dict) else m)) for m in msg if m]


def _status(op: dict) -> Status:
    state, sms = str(op.get("response")), _messages(op.get("msg"))
    if sms:
        return Status("sms", sms)
    if state in WAITING or state == "TZ_NUM_ANSWER":
        return Status("waiting")
    if state in CLOSED:
        return Status("closed")
    raise ProviderError(f"onlinesim state: unexpected {str(op)[:200]}")


class OnlineSim(Provider):
    name = "onlinesim"
    title = "Onlinesim"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self._countries: list[CountryInfo] | None = None
        self._services: dict[str, str] = {}  # slug -> имя, собирается из прайса

    async def _get(self, path: str, **params):
        try:
            r = await self.http.get(f"{URL}{path}", params={"apikey": self.api_key, **params})
            data = r.json()
        except (httpx.HTTPError, ValueError) as e:
            raise ProviderError(f"onlinesim {path}: {e!r}") from e
        if isinstance(data, dict):
            resp = str(data.get("response", "1"))
            if resp == "NO_NUMBER":
                raise NoNumbers("onlinesim: NO_NUMBER")
            if resp == "WARNING_LOW_BALANCE":
                raise NoProviderBalance("onlinesim: WARNING_LOW_BALANCE")
            if resp not in ("1", "ERROR_NO_OPERATIONS"):
                raise ProviderError(f"onlinesim {path}: {resp}")
        return data

    # --- справочники и цены ---

    async def get_countries(self) -> list[CountryInfo]:
        if self._countries is None:
            data = await self._get("/getTariffs.php")
            self._countries = [
                CountryInfo(code=str(c["code"]), name_en=str(c.get("original") or c["code"]).replace("_", " "),
                            name_ru=c.get("name"))
                for c in (data.get("countries") or {}).values() if c.get("enable", True)]
        return self._countries

    async def get_services(self) -> list[ServiceInfo]:
        return [ServiceInfo(code=slug, name=name) for slug, name in self._services.items()]

    async def _country_items(self, country: str) -> list[PriceItem]:
        data = await self._get("/getNumbersStats.php", country=country)
        items = []
        for s in (data.get("services") or {}).values():
            slug, price, count = str(s.get("slug") or ""), Decimal(str(s.get("price") or 0)), int(s.get("count") or 0)
            if slug and price > 0 and count > 0:
                self._services.setdefault(slug, _service_name(slug, s.get("service", "")))
                items.append(PriceItem(slug, country, [(price, count)]))
        return items

    async def get_prices(self) -> list[PriceItem]:
        countries = await self.get_countries()
        sem = asyncio.Semaphore(3)  # у Onlinesim лимит частоты запросов

        async def load(code: str) -> list[PriceItem]:
            async with sem:
                for attempt in range(3):
                    try:
                        return await self._country_items(code)
                    except ProviderError as e:
                        if "TRY_AGAIN_LATER" not in str(e) and "INTERVAL" not in str(e) or attempt == 2:
                            raise
                        await asyncio.sleep(1 + attempt)
            return []

        results = await asyncio.gather(*(load(c.code) for c in countries), return_exceptions=True)
        failed = [r for r in results if isinstance(r, Exception)]
        if failed:
            log.warning("onlinesim: %d из %d стран не загрузились: %s", len(failed), len(countries), failed[0])
        if len(failed) > len(countries) * 0.1:
            raise ProviderError(f"onlinesim: не загрузились {len(failed)} стран")
        return [x for r in results if not isinstance(r, Exception) for x in r]

    async def get_pair(self, service_code: str, country_code: str) -> list[PriceItem]:
        return [x for x in await self._country_items(country_code) if x.service_code == service_code]

    async def get_balance(self) -> Decimal:
        data = await self._get("/getBalance.php")
        return Decimal(str(data.get("balance", "0")))

    # --- активации ---

    async def buy(self, service_code: str, country_code: str, max_cost: Decimal,
                  supplier_id: str | None = None) -> Purchase:
        # Лимита цены у Onlinesim нет: фактическую цену берём из getState и, если она выше
        # max_cost, отказываемся от номера (PriceTooHigh).
        d = await self._get("/getNum.php", service=service_code, country=country_code, number="true")
        tzid = str(d.get("tzid") or "")
        if not tzid:
            raise ProviderError(f"onlinesim getNum: {str(d)[:200]}")
        phone, cost, lifetime = str(d.get("number") or ""), max_cost, 1200
        try:
            ops = await self._get("/getState.php", tzid=tzid, message_to_code=0)
            op = ops[0] if isinstance(ops, list) and ops else {}
            phone = phone or str(op.get("number") or "")
            cost = Decimal(str(op.get("sum"))) if op.get("sum") is not None else max_cost
            lifetime = int(op.get("time") or lifetime)
        except (ProviderError, ValueError, ArithmeticError) as e:
            log.warning("onlinesim: нет данных операции %s после покупки: %s", tzid, e)
        purchase = Purchase(
            order_id=tzid,
            phone=phone.lstrip("+"),
            cost_usd=cost,
            lifetime_sec=max(60, lifetime),
            cancel_after_sec=CANCEL_AFTER.get(service_code, 120),
            can_get_another=True,
        )
        if cost > max_cost:  # цена выросла после прайса — не продаём в минус, номер отменится в фоне
            log.warning("onlinesim %s: цена %s выше лимита %s", tzid, cost, max_cost)
            raise PriceTooHigh(f"onlinesim: цена {cost} выше лимита {max_cost}", purchase)
        return purchase

    async def get_active(self) -> dict[str, Status]:
        data = await self._get("/getState.php", message_to_code=0)
        if not isinstance(data, list):
            return {}
        result = {}
        for op in data:
            try:
                result[str(op["tzid"])] = _status(op)
            except (KeyError, ProviderError):
                continue  # незнакомый статус — такой заказ проверится отдельным запросом
        return result

    async def get_status(self, order_id: str) -> Status:
        data = await self._get("/getState.php", tzid=order_id, message_to_code=0)
        if isinstance(data, dict):  # ERROR_NO_OPERATIONS — операции больше нет
            return Status("closed")
        op = next((o for o in data if str(o.get("tzid")) == order_id), None)
        if op is None:
            return Status("closed")
        return _status(op)

    async def cancel(self, order_id: str) -> CancelResult:
        # без SMS setOperationOk закрывает операцию с возвратом; раньше защитного интервала — отказ
        try:
            d = await self._get("/setOperationOk.php", tzid=order_id)
        except ProviderError as e:
            log.warning("onlinesim cancel %s: %s", order_id, e)
            return "failed"
        log.info("onlinesim cancel %s: %s", order_id, str(d)[:300])
        return "ok"

    async def finish(self, order_id: str) -> None:
        await self._get("/setOperationOk.php", tzid=order_id)

    async def request_another(self, order_id: str) -> bool:
        await self._get("/setOperationRevise.php", tzid=order_id)
        return True

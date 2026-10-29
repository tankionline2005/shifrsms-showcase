"""5SIM — https://5sim.net/docs (API v1, авторизация Bearer).

Поставщик внутри площадки — оператор (virtual4, virtual34…): у каждого своя цена и остаток.
Цены и баланс — в долларах.
Вебхуков нет: статусы опрашиваются."""

import logging
from datetime import datetime, timezone
from decimal import Decimal

import httpx

from app.providers.base import (
    CancelResult,
    CountryInfo,
    NoNumbers,
    NoProviderBalance,
    PriceItem,
    Provider,
    ProviderError,
    Purchase,
    ServiceInfo,
    Sms,
    Status,
)

log = logging.getLogger(__name__)
URL = "https://5sim.net/v1"


def _items(prices: dict) -> list[PriceItem]:
    """{страна: {продукт: {оператор: {cost, count, rate}}}} -> предложения по операторам."""
    items = []
    for country, products in (prices or {}).items():
        for product, operators in (products or {}).items():
            for operator, p in (operators or {}).items():
                count, cost = int(p.get("count") or 0), Decimal(str(p.get("cost") or 0))
                if count > 0 and cost > 0:
                    items.append(PriceItem(product, country, [(cost, count)],
                                           supplier_id=operator, supplier=operator))
    return items


def _expires_in(order: dict) -> int:
    try:
        end = datetime.fromisoformat(str(order["expires"]).replace("Z", "+00:00"))
        return max(60, int((end - datetime.now(timezone.utc)).total_seconds()))
    except (KeyError, ValueError, TypeError):
        return 900  # по документации активация ждёт SMS до 15 минут


def _sms(order: dict) -> list[Sms]:
    return [Sms(str(m.get("code") or "") or None, str(m.get("text") or m.get("code") or ""))
            for m in order.get("sms") or []]


class FiveSim(Provider):
    name = "fivesim"
    title = "5SIM"

    def __init__(self, api_key: str):
        super().__init__(api_key)
        self.http.headers.update({"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        self._countries: dict[str, CountryInfo] | None = None

    async def _get(self, path: str, **params):
        try:
            r = await self.http.get(f"{URL}{path}", params=params)
        except httpx.HTTPError as e:
            raise ProviderError(f"5sim {path}: {e!r}") from e
        try:
            data = r.json()
        except ValueError:
            data = r.text
        if r.status_code == 200 and not isinstance(data, str):
            return data
        # ошибки 5SIM — строка («no free phones», «not enough user balance»…), простым текстом
        # или JSON-строкой
        text = str(data).strip().strip('"')
        low = text.lower()
        if "balance" in low:
            raise NoProviderBalance(f"5sim: {text[:200]}")
        if "no free phones" in low or "no product" in low:
            raise NoNumbers(f"5sim: {text[:200]}")
        raise ProviderError(f"5sim {path}: HTTP {r.status_code} {text[:200]!r}")

    # --- справочники и цены ---

    async def get_countries(self) -> list[CountryInfo]:
        if self._countries is None:
            data = await self._get("/guest/countries")
            self._countries = {
                code: CountryInfo(code=code, name_en=c.get("text_en") or code,
                                  name_ru=c.get("text_ru"),
                                  iso=next(iter(c.get("iso") or {}), "").upper() or None)
                for code, c in data.items()}
        return list(self._countries.values())

    async def get_services(self) -> list[ServiceInfo]:
        data = await self._get("/guest/products/any/any")
        return [ServiceInfo(code=k, name=k) for k in data]

    async def get_prices(self) -> list[PriceItem]:
        return _items(await self._get("/guest/prices"))

    async def get_pair(self, service_code: str, country_code: str) -> list[PriceItem]:
        data = await self._get("/guest/prices", country=country_code, product=service_code)
        # ответ бывает вложен и как {продукт: {страна: …}}, и как {страна: {продукт: …}}
        if service_code in data and country_code in (data[service_code] or {}):
            data = {country_code: {service_code: data[service_code][country_code]}}
        return [x for x in _items(data)
                if x.service_code == service_code and x.country_code == country_code]

    async def get_balance(self) -> Decimal:
        data = await self._get("/user/profile")
        return Decimal(str(data.get("balance", "0")))

    # --- активации ---

    async def buy(self, service_code: str, country_code: str, max_cost: Decimal,
                  supplier_id: str | None = None) -> Purchase:
        operator = supplier_id or "any"
        params = {"maxPrice": str(max_cost)} if operator == "any" else {}  # у 5SIM лимит — только для any
        d = await self._get(f"/user/buy/activation/{country_code}/{operator}/{service_code}", **params)
        if not isinstance(d, dict) or "id" not in d:
            raise ProviderError(f"5sim buy: {str(d)[:200]}")
        cost = Decimal(str(d.get("price", max_cost)))
        if cost > max_cost:  # оператор поднял цену между прайсом и покупкой — не продаём в минус
            await self.cancel(str(d["id"]))
            raise NoNumbers(f"5sim: цена {cost} выше лимита {max_cost}")
        return Purchase(
            order_id=str(d["id"]),
            phone=str(d.get("phone", "")).lstrip("+"),
            cost_usd=cost,
            lifetime_sec=_expires_in(d),
            cancel_after_sec=0,
            can_get_another=False,
            supplier=d.get("operator") or supplier_id,
        )

    async def get_active(self) -> dict[str, Status]:
        return {}  # общего списка активных у 5SIM нет — статусы по каждому заказу

    async def get_status(self, order_id: str) -> Status:
        d = await self._get(f"/user/check/{order_id}")
        if not isinstance(d, dict) or "status" not in d:
            raise ProviderError(f"5sim check {order_id}: {str(d)[:200]}")
        status, sms = d["status"], _sms(d)
        if sms:
            return Status("sms", sms)
        if status == "PENDING":
            return Status("waiting")
        if status in ("CANCELED", "TIMEOUT", "BANNED", "FINISHED"):
            return Status("closed")
        # RECEIVED без текста SMS и прочее неизвестное — не повод закрывать заказ
        raise ProviderError(f"5sim check {order_id}: unexpected {str(d)[:200]}")

    async def cancel(self, order_id: str) -> CancelResult:
        try:
            d = await self._get(f"/user/cancel/{order_id}")
        except ProviderError as e:
            log.warning("5sim cancel %s: %s", order_id, e)
            return "failed"
        log.info("5sim cancel %s: %s", order_id, str(d)[:300])
        return "ok" if isinstance(d, dict) and d.get("status") == "CANCELED" else "failed"

    async def finish(self, order_id: str) -> None:
        await self._get(f"/user/finish/{order_id}")

    async def request_another(self, order_id: str) -> bool:
        return False  # 5SIM присылает все SMS на номер сам, отдельного запроса нет

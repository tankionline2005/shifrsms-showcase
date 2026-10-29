"""Grizzly SMS — протокол sms-activate: https://grizzlysms.com/ru/docs"""

import json
import logging
from datetime import datetime
from decimal import Decimal

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
URL = "https://api.grizzlysms.com/stubs/handler_api.php"


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def _items(service: str, country: str, p: dict) -> list[PriceItem]:
    """Записи getPricesV3 -> предложения по поставщикам Grizzly (provider_id).
    Если разбивки нет — одно предложение без поставщика."""
    items = []
    if p.get("providers"):
        for pid, prov in p["providers"].items():
            prices = prov.get("price") or []
            count = int(prov.get("count") or 0)
            if prices and count > 0:
                sid = str(prov.get("provider_id") or pid)
                items.append(PriceItem(service, country, [(Decimal(str(min(prices))), count)],
                                       supplier_id=sid, supplier=sid))
    elif p.get("count") and p.get("price") is not None:
        items.append(PriceItem(service, country, [(Decimal(str(p["price"])), int(p["count"]))]))
    return items


class Grizzly(Provider):
    name = "grizzly"
    title = "Grizzly SMS"

    async def _call(self, action: str, **params) -> str:
        params = {"api_key": self.api_key, "action": action, **params}
        try:
            r = await self.http.get(URL, params=params)
        except Exception as e:
            raise ProviderError(f"grizzly {action}: {e!r}") from e
        text = r.text.strip()
        if text in ("BAD_KEY", "NO_KEY", "SERVICE_UNAVAILABLE_REGION", "ERROR_SQL", "BAD_ACTION"):
            raise ProviderError(f"grizzly {action}: {text}")
        return text

    async def _json(self, action: str, **params):
        text = await self._call(action, **params)
        try:
            return json.loads(text)
        except ValueError:
            raise ProviderError(f"grizzly {action}: unexpected response {text[:200]!r}")

    async def get_services(self) -> list[ServiceInfo]:
        data = await self._json("getServicesList")
        return [ServiceInfo(code=s["code"], name=s["name"]) for s in data["services"]]

    async def get_countries(self) -> list[CountryInfo]:
        data = await self._json("getCountries")
        return [
            CountryInfo(code=str(c["id"]), name_en=c["eng"], name_ru=c.get("rus"))
            for c in data.values()
            if c.get("visible", 1)
        ]

    async def get_prices(self) -> list[PriceItem]:
        # V3 даёт разбивку по поставщикам внутри Grizzly: реальные остатки по каждой цене.
        # Общий "count" верхнего уровня сильно завышен, поэтому при наличии разбивки берём её.
        data = await self._json("getPricesV3")
        items = []
        for country, services in data.items():
            for service, p in services.items():
                items.extend(_items(service, country, p))
        return items

    async def get_pair(self, service_code: str, country_code: str) -> list[PriceItem]:
        data = await self._json("getPricesV3", service=service_code, country=country_code)
        p = ((data or {}).get(country_code) or {}).get(service_code) or {}
        return _items(service_code, country_code, p)

    async def get_balance(self) -> Decimal:
        text = await self._call("getBalance")
        if not text.startswith("ACCESS_BALANCE:"):
            raise ProviderError(f"grizzly getBalance: {text}")
        return Decimal(text.split(":", 1)[1])

    async def buy(self, service_code: str, country_code: str, max_cost: Decimal,
                  supplier_id: str | None = None) -> Purchase:
        params = {"service": service_code, "country": country_code, "maxPrice": str(max_cost)}
        if supplier_id:
            params["providerIds"] = supplier_id  # только этот поставщик
        text = await self._call("getNumberV2", **params)
        if text == "NO_BALANCE":
            raise NoProviderBalance("grizzly: NO_BALANCE")
        if text.startswith("NO_NUMBERS") or text.startswith("WRONG_MAX_PRICE"):
            raise NoNumbers(f"grizzly: {text}")
        try:
            d = json.loads(text)
        except ValueError:
            raise ProviderError(f"grizzly getNumberV2: {text[:200]}")
        if not isinstance(d, dict) or "activationId" not in d:
            raise ProviderError(f"grizzly getNumberV2: {text[:200]}")
        # номер уже куплен — время жизни разбираем мягко, чтобы не потерять заказ из-за формата
        lifetime, cancel_after = 1200, 120
        try:
            start = _dt(d["activationTime"])
            lifetime = int((_dt(d["activationEnd"]) - start).total_seconds())
            cancel_after = max(0, int((_dt(d["activationCancel"]) - start).total_seconds()))
        except (KeyError, TypeError, ValueError):
            log.warning("grizzly: нет времени активации в ответе %s", text[:200])
        return Purchase(
            order_id=str(d["activationId"]),
            phone=str(d.get("phoneNumber", "")),
            cost_usd=Decimal(str(d.get("activationCost", max_cost))),
            lifetime_sec=lifetime if lifetime > 0 else 1200,
            cancel_after_sec=cancel_after,
            can_get_another=str(d.get("canGetAnotherSms")) == "1",
            supplier=supplier_id,  # Grizzly поставщика не сообщает — знаем, т.к. выбрали сами
        )

    async def get_active(self) -> dict[str, Status]:
        data = await self._json("getActiveActivations")
        result = {}
        for a in data or []:
            code, text = a.get("smsCode") or "", a.get("smsText") or ""
            if code or text:
                result[str(a["activationId"])] = Status("sms", [Sms(code or None, text or code)])
            else:
                result[str(a["activationId"])] = Status("waiting")
        return result

    async def get_status(self, order_id: str) -> Status:
        text = await self._call("getStatus", id=order_id)
        if text.startswith("STATUS_OK"):
            code = text.split(":", 1)[1] if ":" in text else ""
            full = code
            try:
                v2 = await self._json("getStatusV2", id=order_id)
                sms = v2.get("sms") or {}
                full = sms.get("text") or code
                code = sms.get("code") or code
            except ProviderError:
                pass
            return Status("sms", [Sms(code or None, full)])
        if text.startswith("STATUS_WAIT"):
            return Status("waiting")
        if text.startswith("STATUS_CANCEL") or text == "NO_ACTIVATION":
            return Status("closed")
        # всё прочее (сбой сети, страница ошибки) — не повод считать заказ закрытым:
        # иначе вернём деньги пользователю, а номер у площадки останется активным
        raise ProviderError(f"grizzly getStatus {order_id}: unexpected {text[:200]!r}")

    async def cancel(self, order_id: str) -> CancelResult:
        text = await self._call("setStatus", id=order_id, status=8)
        log.info("grizzly cancel %s: %s", order_id, text[:300])
        if text == "ACCESS_CANCEL":
            return "ok"
        if "EARLY" in text:
            return "too_early"
        log.warning("grizzly cancel %s: %s", order_id, text)
        return "failed"

    async def finish(self, order_id: str) -> None:
        await self._call("setStatus", id=order_id, status=6)

    async def request_another(self, order_id: str) -> bool:
        text = await self._call("setStatus", id=order_id, status=3)
        return text == "ACCESS_RETRY_GET"

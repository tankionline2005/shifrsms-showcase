"""SMSPool — https://documenter.getpostman.com/view/30155063/2s9YXmZ1JY"""

import asyncio
import logging
import re
import time
from collections import deque
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
    ServiceBlocked,
    ServiceInfo,
    Sms,
    Status,
)

log = logging.getLogger(__name__)
URL = "https://api.smspool.net"
# SMSPool пускает ~60 запросов в минуту на ключ (сверх — «Rate limit exceeded, please try again in 60 seconds»).
# Обновление прайса — 150+ запросов (по стране), поэтому оно идёт не быстрее BULK_PER_MIN, а запас до TOTAL_PER_MIN
# остаётся покупкам, отменам и проверке SMS — их пользователь ждёт.
WINDOW = 60.0
TOTAL_PER_MIN, BULK_PER_MIN = 55, 40
RATE_LIMIT_WAIT = 61.0


def _flatten(data):
    """all_stock отдаёт список списков — разворачиваем в плоский список строк."""
    rows = data
    while rows and isinstance(rows, list) and isinstance(rows[0], list):
        rows = [r for chunk in rows for r in chunk]
    if not isinstance(rows, list):
        raise ProviderError(f"smspool all_stock: {str(data)[:200]}")
    return rows


def _items(rows) -> list[PriceItem]:
    """Строки all_stock -> предложения по пулам (пул = поставщик SMSPool)."""
    items = []
    for r in rows:
        stock, price = int(r.get("stock") or 0), Decimal(str(r.get("price") or 0))
        if stock > 0 and price > 0:
            items.append(PriceItem(str(r["service"]), str(r["country"]), [(price, stock)],
                                   supplier_id=str(r.get("pool")),
                                   supplier=r.get("pool_name") or str(r.get("pool"))))
    return items


def _pool_name(d: dict) -> str | None:
    """Название пула из ответа покупки: «...from pool: Foxtrot for 0.24.»"""
    m = re.search(r"pool:\s*([A-Za-z0-9_-]+)", str(d.get("message", "")))
    if m:
        return m.group(1)
    return str(d["pool"]) if d.get("pool") not in (None, "") else None


class RateLimited(ProviderError):
    """SMSPool ответил «Rate limit exceeded» — минуту запросы не принимает."""


def _rate_limited(body) -> bool:
    return isinstance(body, dict) and "rate limit" in str(body.get("message", "")).lower()


class SmsPool(Provider):
    name = "smspool"
    title = "SMSPool"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sent: deque[float] = deque()  # время запросов за последнюю минуту
        self._blocked_until = 0.0  # SMSPool сказал «подождите минуту»

    async def _slot(self, bulk: bool) -> None:
        """Дождаться места в минутном лимите. bulk (обновление прайса) — с запасом для покупок."""
        cap = BULK_PER_MIN if bulk else TOTAL_PER_MIN
        while True:
            now = time.monotonic()
            while self._sent and now - self._sent[0] >= WINDOW:
                self._sent.popleft()
            wait = self._blocked_until - now if bulk else 0
            if wait <= 0 and len(self._sent) < cap:
                self._sent.append(now)
                return
            await asyncio.sleep(max(wait, self._sent[0] + WINDOW - now if len(self._sent) >= cap else 0, 0.05))

    async def _post(self, path: str, bulk: bool = False, **data):
        await self._slot(bulk)
        try:
            r = await self.http.post(f"{URL}{path}", data={"key": self.api_key, **data})
        except Exception as e:
            raise ProviderError(f"smspool {path}: {e!r}") from e
        try:
            body = r.json()
        except ValueError:
            raise ProviderError(f"smspool {path}: HTTP {r.status_code} {r.text[:200]!r}")
        if _rate_limited(body):
            self._blocked_until = time.monotonic() + RATE_LIMIT_WAIT
            raise RateLimited(f"smspool {path}: {body.get('message')}")
        if r.status_code == 403:
            raise ProviderError(f"smspool {path}: {body}")
        return body

    async def _bulk(self, path: str, **data):
        """Запрос обновления прайса: при «Rate limit» ждём минуту и повторяем (до 3 раз)."""
        for attempt in range(3):
            try:
                return await self._post(path, bulk=True, **data)
            except RateLimited:
                if attempt == 2:
                    raise
                log.info("smspool: лимит запросов — обновление прайса ждёт минуту")
                await asyncio.sleep(RATE_LIMIT_WAIT)

    async def get_services(self) -> list[ServiceInfo]:
        data = await self._bulk("/service/retrieve_all")
        return [ServiceInfo(code=str(s["ID"]), name=s["name"]) for s in data]

    async def get_countries(self) -> list[CountryInfo]:
        data = await self._bulk("/country/retrieve_all")
        return [CountryInfo(code=str(c["ID"]), name_en=c["name"], iso=c.get("short_name"))
                for c in data]

    async def get_prices(self) -> list[PriceItem]:
        # /sms/all_stock отдаёт цену и остаток по каждому пулу. Грузим по странам,
        # чтобы не держать в памяти 30 МБ JSON целиком; темп задаёт минутный лимит (_slot), всё обновление ≈ 4 мин.
        countries = await self.get_countries()
        # в остатках встречаются сервисы, которых нет в списке сервисов SMSPool
        # (приходят без названия) — это мусор, в каталог их не берём
        known_services = {s.code for s in await self.get_services()}
        sem = asyncio.Semaphore(4)
        items: list[PriceItem] = []

        async def load(country: str) -> None:
            async with sem:
                data = await self._bulk("/sms/all_stock", country=country)
            items.extend(_items(r for r in _flatten(data)
                                if str(r.get("service")) in known_services))

        results = await asyncio.gather(*(load(c.code) for c in countries),
                                       return_exceptions=True)
        failed = [r for r in results if isinstance(r, Exception)]
        if failed:
            log.warning("smspool all_stock: %d из %d стран не загрузились: %s",
                        len(failed), len(countries), failed[0])
        # если отвалилось много — лучше оставить прежний прайс, чем показать урезанный
        if len(failed) > len(countries) * 0.1:
            raise ProviderError(f"smspool all_stock: не загрузились {len(failed)} стран")
        return items

    async def get_pair(self, service_code: str, country_code: str) -> list[PriceItem]:
        data = await self._post("/sms/all_stock", country=country_code, service=service_code)
        return _items(r for r in _flatten(data) if str(r.get("service")) == service_code)

    async def get_balance(self) -> Decimal:
        data = await self._post("/request/balance")
        return Decimal(str(data.get("balance", "0")))

    async def buy(self, service_code: str, country_code: str, max_cost: Decimal,
                  supplier_id: str | None = None) -> Purchase:
        params = {"country": country_code, "service": service_code, "max_price": str(max_cost),
                  "pricing_option": 0, "quantity": 1}
        if supplier_id:
            params["pool"] = supplier_id  # только этот пул
        d = await self._post("/purchase/sms", **params)
        if not d.get("success"):
            kind = str(d.get("type", ""))
            msg = str(d.get("message", d))
            # Баланс проверяем первым: SMSPool перечисляет ошибки по каждому пулу, и если хоть
            # один пул отказал из-за баланса — это не «нет номеров» (иначе ложный карантин).
            low = msg.lower()
            if "insufficient balance" in low or "funds" in low or "balance" in kind.lower():
                raise NoProviderBalance(f"smspool: {msg[:200]}")
            if "not available for you" in low:  # закрыли нам сервис «due to repeated failed orders»
                raise ServiceBlocked(f"smspool: {msg[:200]}")
            if kind in ("OUT_OF_STOCK", "PRICE_NOT_FOUND") or "stock" in low:
                raise NoNumbers(f"smspool: {kind} {msg[:200]}")
            raise ProviderError(f"smspool purchase: {kind} {msg[:300]}")
        return Purchase(
            order_id=str(d["order_id"]),
            phone=str(d["number"]),
            cost_usd=Decimal(str(d["cost"])),
            lifetime_sec=int(d.get("expires_in") or 1200),
            cancel_after_sec=0,
            can_get_another=False,
            supplier=_pool_name(d),
        )

    @staticmethod
    def _has_code(code) -> bool:
        return code not in (None, "", "0", 0)

    async def get_active(self) -> dict[str, Status]:
        data = await self._post("/request/active")
        if not isinstance(data, list):
            raise ProviderError(f"smspool active: {data}")
        result = {}
        for o in data:
            if self._has_code(o.get("code")):
                result[str(o["order_code"])] = Status(
                    "sms", [Sms(str(o["code"]), o.get("full_code") or str(o["code"]))])
            else:
                result[str(o["order_code"])] = Status("waiting")
        return result

    # Статусы /sms/check: 1 — ожидание, 3 — SMS получена, 6 — возврат (задокументированы).
    # 2 — истёк, 5 — отменён, 4 — ожидание повторной SMS — по неофициальным описаниям API.
    # Незнакомый статус -> ошибка: заказ закроется по сроку, а не по догадке.
    WAITING, DONE, CLOSED = {1, 4}, {3}, {2, 5, 6}

    async def get_status(self, order_id: str) -> Status:
        d = await self._post("/sms/check", orderid=order_id)
        if not isinstance(d, dict):
            raise ProviderError(f"smspool check {order_id}: {str(d)[:200]}")
        try:
            status = int(d.get("status"))
        except (TypeError, ValueError):
            status = None
        if status in self.DONE or self._has_code(d.get("sms")):
            return Status("sms", [Sms(str(d.get("sms") or "") or None,
                                      d.get("full_sms") or str(d.get("sms")))])
        if status in self.WAITING:
            return Status("waiting")
        if status in self.CLOSED:
            return Status("closed")
        # лимит запросов, ошибка и т.п. — не повод закрывать заказ и возвращать деньги
        raise ProviderError(f"smspool check {order_id}: unexpected {str(d)[:200]}")

    async def cancel(self, order_id: str) -> CancelResult:
        d = await self._post("/sms/cancel", orderid=order_id)
        log.info("smspool cancel %s: %s", order_id, str(d)[:300])
        if d.get("success"):
            return "ok"
        msg = str(d.get("message", "")).lower()
        if "cannot be cancelled yet" in msg:
            return "too_early"
        log.warning("smspool cancel %s: %s", order_id, d)
        return "failed"

    async def finish(self, order_id: str) -> None:
        return None  # у SMSPool нет отдельного завершения

    async def request_another(self, order_id: str) -> bool:
        check = await self._post("/sms/check_resend", orderid=order_id)
        if not check.get("success") or Decimal(str(check.get("resendCost", 0))) > 0:
            return False
        d = await self._post("/sms/resend", orderid=order_id)
        return bool(d.get("success"))

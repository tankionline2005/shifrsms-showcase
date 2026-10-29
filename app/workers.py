import asyncio
import logging
from decimal import Decimal

import httpx

from app import catalog, orders, payments, payouts, quality, reconcile, tgcheck, tron
from app import settings_store as st
from app.bot import support

log = logging.getLogger(__name__)

CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"


async def _loop(name: str, interval, fn) -> None:
    while True:
        try:
            await fn()
        except asyncio.CancelledError:
            raise
        except tron.ChainError as e:  # сеть TRON или узел временно недоступны — попробуем в следующий раз
            log.warning("worker %s: %s", name, e)
        except Exception:
            log.exception("worker %s", name)
        await asyncio.sleep(interval() if callable(interval) else interval)


async def update_usd_rate() -> None:
    if not st.bool_("usd_rate_auto"):
        return
    async with httpx.AsyncClient(timeout=20) as http:
        data = (await http.get(CBR_URL)).json()
    rate = Decimal(str(data["Valute"]["USD"]["Value"])).quantize(Decimal("0.01"))
    if rate > 0 and str(rate) != st.raw("usd_rate"):
        await st.save({"usd_rate": str(rate)})
        log.info("курс USD обновлён: %s", rate)


def start_workers() -> list[asyncio.Task]:
    return [
        asyncio.create_task(_loop("rate", 3600, update_usd_rate)),
        asyncio.create_task(_loop("catalog", lambda: max(1, st.int_("catalog_sync_minutes")) * 60,
                                  catalog.sync_all)),
        asyncio.create_task(_loop("orders", lambda: max(5, st.int_("poll_seconds")),
                                  orders.poll_active)),
        asyncio.create_task(_loop("balances", 60, catalog.refresh_balances)),
        asyncio.create_task(_loop("quality", 300, quality.refresh)),
        asyncio.create_task(_loop("popularity", 300, catalog.refresh_popularity)),
        asyncio.create_task(_loop("payments", 60, payments.poll)),
        asyncio.create_task(_loop("support_cards", 60, support.refresh_cards)),  # карточки открытых обращений
        asyncio.create_task(_loop("tgcheck", 600, tgcheck.health)),  # живы ли аккаунты проверки Telegram
        # кошелёк системы: очередь переводов, автоматика, «Сверка»
        asyncio.create_task(_loop("payouts", 15, payouts.process)),
        asyncio.create_task(_loop("auto_withdraw", 600, payouts.auto_withdrawals)),
        asyncio.create_task(_loop("provider_credited", 300, payouts.check_credited)),
        asyncio.create_task(_loop("money_alerts", 900, payouts.alerts)),
        asyncio.create_task(_loop("chain", 120, payouts.sync_chain)),
        asyncio.create_task(_loop("reconcile", 120, reconcile.compute)),
    ]

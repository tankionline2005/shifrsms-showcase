import asyncio
import logging

import uvicorn
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from app import broadcasts, catalog, menu_images, orders, owners, payments, payouts, quality, tron, user_refs
from app import settings_store as st
from app.bot import channels, support
from app.bot.handlers import router
from app.bot.notify import make_invite_notifier, make_notifier, make_owner_notifier, make_payment_notifier
from app.config import config
from app.db import init_db
from app.web.server import create_app
from app.workers import start_workers

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("aiogram.event").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(logging.WARNING)  # подключения аккаунтов проверки Telegram — не в журнал
log = logging.getLogger("shifr")


async def main() -> None:
    await init_db()
    await st.load()
    await catalog.reload_meta()
    await menu_images.load()
    await owners.seed()
    await catalog.load_quarantine()
    await quality.refresh()
    await catalog.refresh_popularity()

    bot = Bot(config.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(channels.router)  # каналы и группы — раньше личных чатов
    dp.include_router(support.group)  # группа поддержки: ответы из тем, закрытие обращений
    dp.include_router(support.private)  # чат поддержки — раньше поиска сервиса по тексту
    dp.include_router(router)
    channels.bot = bot
    orders.set_notifier(make_notifier(bot))
    payments.set_notifier(make_payment_notifier(bot))
    payouts.set_notifier(make_owner_notifier(bot))
    user_refs.set_notifier(make_invite_notifier(bot))

    server = uvicorn.Server(uvicorn.Config(create_app(), host=config.web_host,
                                           port=config.web_port, log_level="warning",
                                           proxy_headers=True, forwarded_allow_ips="*"))
    workers = start_workers()
    await broadcasts.resume()  # рассылки, прерванные перезапуском
    log.info("Панель: %s%s, кабинет партнёров: %s/partner-dashboard", config.public_url, config.admin_url_path,
             config.public_url)
    log.info("Вебхук Grizzly: %s", config.grizzly_webhook_url)
    log.info("Вебхук SMSPool: %s", config.smspool_webhook_url)
    log.info("Вебхук Onlinesim: %s", config.onlinesim_webhook_url)
    log.info("Callback Platega: %s/webhooks/platega (%s)", config.public_url,
             "включён" if payments.enabled() else "ключей нет — пополнение недоступно")
    log.info("Кошелёк системы (%s): %s", config.tron_network, tron.address() or "не подключён (WALLET_PRIVATE_KEY)")

    async def serve() -> None:
        await server.serve()  # завершается по SIGTERM/SIGINT
        try:
            await dp.stop_polling()
        except RuntimeError:
            pass

    try:
        await asyncio.gather(dp.start_polling(bot, handle_signals=False), serve())
    finally:
        for t in workers:
            t.cancel()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

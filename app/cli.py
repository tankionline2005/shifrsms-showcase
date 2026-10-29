"""Служебные команды на сервере (в /opt/shifr):

  docker compose exec -T app python -m app.cli new-wallet >> .env   — создать кошелёк системы: ключ сразу
        уходит в .env и нигде не показывается (если кошелёк уже есть — команда ничего не делает)
  docker compose exec app python -m app.cli wallet                  — адрес кошелька и балансы
  docker compose exec app python -m app.cli reset-2fa <логин>       — сбросить аутентификатор (потерян телефон)
  docker compose exec app python -m app.cli owners                  — владельцы и их состояние
  docker compose exec -T app python -m app.cli notify "<текст>"     — системное уведомление в Telegram тем, кто указан
        в «Настройках» (notify_users); так сообщает об ошибке резервное копирование (/usr/local/sbin/shifr-backup)
"""

import asyncio
import sys

from sqlalchemy import select

from app import accounts, owners, tron
from app.config import config
from app.db import Session, init_db
from app.models import Owner, Referral


async def reset_2fa(login: str) -> None:
    async with Session() as s:
        owner = (await s.scalars(select(Owner).where(Owner.login == login))).first()
        ref = (await s.scalars(select(Referral).where(Referral.login == login))).first()
    if not owner and not ref:
        print(f"Нет владельца или реферала с логином {login}")
        sys.exit(1)
    acc = accounts.Acc("owner", owner.id) if owner else accounts.Acc("ref", ref.id)
    await accounts.reset_2fa(acc)
    await accounts.audit("сервер", f"сброшен аутентификатор {login}")
    print(f"Аутентификатор {login} сброшен (и автовывод выключен). При следующем входе / выводе — привязка заново.")


async def main(args: list[str]) -> None:
    cmd = args[0] if args else ""
    if cmd == "new-wallet":
        if config.wallet_private_key.strip():
            print("кошелёк уже есть — новый не создаю", file=sys.stderr)
            sys.exit(1)
        print(f"\nWALLET_PRIVATE_KEY={tron.new_private_key()}")
        print("ключ записан в .env; перезапустите: docker compose up -d app", file=sys.stderr)
        return
    await init_db()
    if cmd == "wallet":
        if not tron.enabled():
            print("кошелька нет")
            return
        print("сеть:", config.tron_network, "\nадрес:", tron.address())
        print("USDT:", await tron.usdt_balance(), "| TRX:", await tron.trx_balance())
    elif cmd == "reset-2fa" and len(args) == 2:
        await reset_2fa(args[1].strip().lower())
    elif cmd == "owners":
        await owners.seed()
        for o in await owners.all_():
            print(f"{o.login}: пароль {'задан' if o.password_hash else 'НЕ задан'}, "
                  f"аутентификатор {'привязан' if o.totp_secret else 'НЕ привязан'}, доля {o.share}%")
    elif cmd == "notify" and len(args) == 2:
        from aiogram import Bot
        from app import settings_store as st
        from app.bot.notify import make_owner_notifier
        await st.load()
        bot = Bot(config.bot_token)
        try:
            await make_owner_notifier(bot)(args[1])
        finally:
            await bot.session.close()
    else:
        print(__doc__)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1:]))

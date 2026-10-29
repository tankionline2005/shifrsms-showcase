import logging
import re
from html import escape

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import ReplyParameters
from sqlalchemy import func, select

from app import settings_store as st
from app import shop_accounts
from app.bot.ui import order_kb, order_text, rub
from app.db import Session
from app.textfmt import sms_text
from app.models import Order, User

log = logging.getLogger(__name__)


def make_notifier(bot: Bot):
    async def notify(order: Order, event: str) -> None:
        if not shop_accounts.is_tg(order.user_id):
            return  # пользователь только сайта — всё видит на сайте
        chat_id = order.chat_id or order.user_id
        if order.message_id:
            try:
                await bot.edit_message_text(order_text(order), chat_id=chat_id,
                                            message_id=order.message_id,
                                            reply_markup=order_kb(order))
            except TelegramBadRequest as e:
                if "not modified" not in str(e):
                    log.info("edit card %s: %s", order.id, e)

        reply = ({"reply_parameters": ReplyParameters(message_id=order.message_id,
                                                  allow_sending_without_reply=True)}
                 if order.message_id else {})
        if event == "sms" and order.messages:
            last = order.messages[-1]
            name = escape(order.service.name)
            if last.code:
                text = f"📩 {name} SMS code: <code>{escape(last.code)}</code>"
            else:  # код не распознан — показываем текст SMS целиком
                text = f"📩 {name} SMS: {escape(sms_text(last.text)[:300])}"
            await bot.send_message(chat_id, text, **reply)
        elif event == "expired":
            await bot.send_message(chat_id, f"⌛ SMS не пришла, активация {escape(order.service.name)} закрыта. "
                                            f"{rub(order.price)} возвращены на баланс.", **reply)

    return notify


def make_invite_notifier(bot: Bot):
    """Пригласившему — о каждом начислении за друга (app/user_refs.py)."""
    async def notify(user_id: int, amount) -> None:
        if not shop_accounts.is_tg(user_id):
            return
        try:
            await bot.send_message(user_id, f"🎉 +{rub(amount)}: ваш друг получил SMS")
        except TelegramAPIError as e:  # заблокировал бота и т. п. — деньги всё равно на балансе
            log.info("уведомление о начислении за приглашение %s: %s", user_id, e)
    return notify


def make_payment_notifier(bot: Bot):
    """Платёж зачислен (или возвращён плательщику): обновить сообщение со счётом и написать человеку."""
    from app import wallet
    from app.bot.handlers import invoice_view

    async def notify(p, event: str) -> None:
        if not shop_accounts.is_tg(p.user_id):
            return
        chat_id = p.chat_id or p.user_id
        if p.message_id:
            text, markup = invoice_view(p)
            try:
                await bot.edit_message_text(text, chat_id=chat_id, message_id=p.message_id, reply_markup=markup)
            except TelegramBadRequest as e:
                if "not modified" not in str(e):
                    log.info("edit invoice %s: %s", p.id, e)
        balance = rub(await wallet.get_balance(p.user_id))
        if event == "confirmed":
            paid = f" (⭐{p.stars})" if getattr(p, "stars", None) else ""
            text = f"✅ Баланс пополнен на {rub(p.amount)}{paid}.\nВаш баланс: <b>{balance}</b>"
        else:
            text = f"↩️ Платёж по счёту №{p.id} возвращён, {rub(p.amount)} списаны с баланса.\nВаш баланс: <b>{balance}</b>"
        try:
            await bot.send_message(chat_id, text)
        except TelegramBadRequest as e:
            log.info("payment message %s: %s", p.id, e)

    return notify


def parse_users(raw: str) -> list[str]:
    """«@a, @b c» / t.me/a / 123 -> ['a', 'b', 'c', '123'] (без @)."""
    out = []
    for token in re.split(r"[\s,;]+", raw or ""):
        name = token.strip().removeprefix("https://").removeprefix("t.me/").lstrip("@").strip("/")
        if name and name.lower() not in (x.lower() for x in out):
            out.append(name)
    return out


async def find_users(names: list[str]) -> tuple[list[User], list[str]]:
    """Пользователи бота по @username или Telegram ID -> (найденные, не найденные). Бот может писать только тем,
    кто ему писал, поэтому ищем среди его пользователей."""
    found, missing = [], []
    async with Session() as s:
        for name in names:
            user = await s.get(User, int(name)) if name.isdigit() else (await s.scalars(
                select(User).where(func.lower(User.username) == name.lower()))).first()
            (found.append(user) if user else missing.append(name))
    return found, missing


def make_owner_notifier(bot: Bot):
    """Системные уведомления (выводы, поступления, нехватка денег) — тем, кто указан в «Настройках»
    («Кому присылать системные уведомления», notify_users)."""

    async def notify(text: str) -> None:
        users, missing = await find_users(parse_users(st.raw("notify_users")))
        if missing:
            log.warning("уведомления: не нашёл среди пользователей бота %s", ", ".join(missing))
        for user in users:
            try:
                await bot.send_message(user.id, escape(text), disable_web_page_preview=True)
            except Exception as e:
                log.info("уведомление @%s: %s", user.username or user.id, e)

    return notify

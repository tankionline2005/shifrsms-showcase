"""Рассылки пользователям бота.

Запуск: получатели группы (SEGMENTS) фиксируются в broadcast_recipients, и фоновая задача шлёт
им сообщение (текст и/или фото) не быстрее RATE в секунду — лимит Telegram для ботов. Если сервер
перезапустился посреди рассылки, resume() продолжит с того же места.

Отмена: отправка останавливается, уже отправленные сообщения удаляются — Telegram разрешает боту
удалять свои сообщения только CANCEL_WINDOW после отправки, поэтому позже отменить нельзя.
Кто заблокировал бота, помечается (User.bot_blocked) и в рассылки не попадает, пока снова
не напишет боту."""

import asyncio
import logging
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import (
    TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter,
)
from aiogram.types import BufferedInputFile, LinkPreviewOptions
from sqlalchemy import and_, exists, func, or_, select, true, update

from app.bot import channels
from app.shop_accounts import WEB_ID_BASE
from app.db import Session, utcnow
from app.models import Broadcast, BroadcastRecipient, Order, Transaction, User

log = logging.getLogger(__name__)

RATE = 25  # сообщений в секунду (лимит Telegram — около 30)
CANCEL_WINDOW = timedelta(hours=47)  # Telegram даёт удалить сообщение бота 48 ч — берём с запасом
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)
# пополнение баланса: ручное начисление админом (пока платежей нет) и будущая оплата
TOPUP_KINDS = ("topup", "admin", "topup_test")
PAID = ("received", "completed")  # активация, по которой пришла SMS
STATUS_TITLES = {"sending": "отправляется", "done": "отправлена", "cancelling": "отменяется — удаляем",
                 "cancelled": "отменена"}

_tasks: set[asyncio.Task] = set()
_send_lock = asyncio.Lock()  # рассылки идут по очереди — лимит скорости общий


def _topup():
    return exists().where(Transaction.user_id == User.id, Transaction.kind.in_(TOPUP_KINDS), Transaction.amount > 0)


def _bought(since: datetime | None = None):
    cond = [Order.user_id == User.id, Order.status.in_(PAID)]
    if since:
        cond.append(Order.created_at >= since)
    return exists().where(*cond)


# ключ -> (название, пояснение, условие на User)
SEGMENTS = {
    "all": ("Все", "все пользователи бота", lambda now: true()),
    "new7": ("Новые за 7 дней", "открыли бота за последнюю неделю",
             lambda now: User.created_at >= now - timedelta(days=7)),
    "never_topup": ("Ни разу не пополняли", "баланс ни разу не пополнялся", lambda now: ~_topup()),
    "topup_no_buy": ("Пополнили, но не купили", "пополняли баланс, но ни разу не получили код",
                     lambda now: and_(_topup(), ~_bought())),
    "has_balance": ("Есть деньги на балансе", "баланс больше нуля — напомнить потратить",
                    lambda now: User.balance > 0),
    "buyers": ("Покупали", "хотя бы раз получили код", lambda now: _bought()),
    "lost_buyers": ("Давно не покупали", "получали код, но последние 30 дней ничего не брали",
                    lambda now: and_(_bought(), ~_bought(now - timedelta(days=30)))),
    "referral": ("Пришли от рефералов", "закреплены за любым рефералом",
                 lambda now: or_(User.referral_id.is_not(None), User.referral2_id.is_not(None))),
}


def _reachable():
    """Кому вообще можно писать: не заблокирован нами и сам не заблокировал бота."""
    return and_(User.is_blocked.is_(False), or_(User.bot_blocked.is_(None), User.bot_blocked.is_(False)),
                User.id < WEB_ID_BASE)  # пользователи только сайта — без Telegram


async def recipients(segment: str) -> list[int]:
    cond = SEGMENTS[segment][2](utcnow())
    async with Session() as s:
        return list((await s.scalars(select(User.id).where(_reachable(), cond).order_by(User.id))).all())


async def counts() -> dict[str, int]:
    now = utcnow()
    async with Session() as s:
        return {k: await s.scalar(select(func.count()).select_from(User).where(_reachable(), seg[2](now)))
                for k, seg in SEGMENTS.items()}


# ---------------------------------------------------------------- отправка


async def _send(bot: Bot, chat_id: int, text: str, photo: bytes | None, file_id: str | None,
                plain: bool = False) -> tuple[int, str | None]:
    """-> (id сообщения, file_id фото). Ошибка разметки — повтор без разметки."""
    extra = {"parse_mode": None} if plain else {}
    try:
        if photo:
            sent = await bot.send_photo(chat_id, file_id or BufferedInputFile(photo, "photo"),
                                        caption=text or None, **extra)
            return sent.message_id, sent.photo[-1].file_id if sent.photo else file_id
        sent = await bot.send_message(chat_id, text, link_preview_options=NO_PREVIEW, **extra)
        return sent.message_id, None
    except TelegramBadRequest as e:
        if "parse" in str(e).lower() and not plain:
            return await _send(bot, chat_id, text, photo, file_id, plain=True)
        raise


async def send_test(chat_id: int, text: str, photo: bytes | None) -> str | None:
    """Пробная отправка себе. -> текст ошибки или None."""
    if not channels.bot:
        return "Бот не запущен."
    try:
        await _send(channels.bot, chat_id, text, photo, None)
    except TelegramForbiddenError:
        return "Этот пользователь не запускал бота или заблокировал его — сначала напишите боту /start."
    except TelegramAPIError as e:
        return f"Telegram не принял сообщение: {e}"[:300]
    return None


async def start(text: str, photo: bytes | None, photo_type: str | None, segment: str,
                photo_file_id: str | None = None, thumb: bytes | None = None) -> int | None:
    """Создать рассылку и запустить отправку в фоне. -> id или None, если получателей нет."""
    users = await recipients(segment)
    if not users:
        return None
    async with Session() as s:
        b = Broadcast(text=text, photo=photo, photo_type=photo_type, photo_file_id=photo_file_id, thumb=thumb,
                      segment=segment, status="sending", total=len(users))
        s.add(b)
        await s.flush()
        s.add_all([BroadcastRecipient(broadcast_id=b.id, user_id=u) for u in users])
        await s.commit()
        bid = b.id
    log.info("рассылка %s: %s получателей (%s)", bid, len(users), segment)
    _spawn(_run(bid))
    return bid


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _status(bid: int) -> str | None:
    async with Session() as s:
        return await s.scalar(select(Broadcast.status).where(Broadcast.id == bid))


async def _run(bid: int) -> None:
    async with _send_lock:
        try:
            await _send_all(bid)
        except Exception:
            log.exception("рассылка %s", bid)


async def _send_all(bid: int) -> None:
    bot = channels.bot
    while bot is not None:
        if await _status(bid) != "sending":
            return  # отменили
        async with Session() as s:
            b = await s.get(Broadcast, bid)
            batch = (await s.scalars(select(BroadcastRecipient).where(
                BroadcastRecipient.broadcast_id == bid, BroadcastRecipient.status == "pending")
                .order_by(BroadcastRecipient.id).limit(100))).all()
        if not batch:
            break
        for r in batch:
            if await _status(bid) != "sending":
                return
            status, message_id = "failed", None
            while True:
                try:
                    message_id, file_id = await _send(bot, r.user_id, b.text, b.photo, b.photo_file_id)
                    status = "sent"
                    if file_id and file_id != b.photo_file_id:  # фото загружено — дальше по file_id
                        b.photo_file_id = file_id
                        async with Session() as s:
                            await s.execute(update(Broadcast).where(Broadcast.id == bid).values(photo_file_id=file_id))
                            await s.commit()
                except TelegramRetryAfter as e:  # Telegram просит притормозить
                    await asyncio.sleep(e.retry_after + 1)
                    continue
                except TelegramForbiddenError:
                    status = "blocked"
                    async with Session() as s:
                        await s.execute(update(User).where(User.id == r.user_id).values(bot_blocked=True))
                        await s.commit()
                except TelegramAPIError as e:
                    log.info("рассылка %s -> %s: %s", bid, r.user_id, e)
                break
            async with Session() as s:
                await s.execute(update(BroadcastRecipient).where(BroadcastRecipient.id == r.id).values(
                    status=status, message_id=message_id, sent_at=utcnow() if message_id else None))
                await s.commit()
            await asyncio.sleep(1 / RATE)
    async with Session() as s:
        await s.execute(update(Broadcast).where(Broadcast.id == bid, Broadcast.status == "sending")
                        .values(status="done", finished_at=utcnow()))
        await s.commit()
    log.info("рассылка %s отправлена", bid)


# ---------------------------------------------------------------- отмена


def cancel_deadline(b: Broadcast) -> datetime:
    return b.created_at + CANCEL_WINDOW


def can_cancel(b: Broadcast) -> bool:
    return b.status in ("sending", "done") and utcnow() < cancel_deadline(b)


async def cancel(bid: int) -> str | None:
    """Остановить рассылку и удалить отправленные сообщения. -> текст ошибки или None."""
    async with Session() as s:
        b = await s.get(Broadcast, bid)
        if not b or not can_cancel(b):
            return "Эту рассылку уже нельзя отменить: Telegram даёт удалить сообщения только в течение 48 часов."
        b.status, b.cancelled_at = "cancelling", utcnow()
        await s.commit()
    log.info("рассылка %s: отмена", bid)
    _spawn(_delete_sent(bid))
    return None


async def _delete_sent(bid: int) -> None:
    bot = channels.bot
    try:
        while bot is not None:
            async with Session() as s:
                batch = (await s.scalars(select(BroadcastRecipient).where(
                    BroadcastRecipient.broadcast_id == bid, BroadcastRecipient.status == "sent",
                    BroadcastRecipient.sent_at > utcnow() - timedelta(hours=48))
                    .order_by(BroadcastRecipient.id).limit(100))).all()
            if not batch:
                break
            for r in batch:
                try:
                    await bot.delete_message(r.user_id, r.message_id)
                except TelegramRetryAfter as e:
                    await asyncio.sleep(e.retry_after + 1)
                    continue  # повторим в следующей пачке
                except TelegramAPIError as e:
                    log.info("рассылка %s: не удалить у %s: %s", bid, r.user_id, e)  # человек сам удалил чат и т.п.
                async with Session() as s:
                    await s.execute(update(BroadcastRecipient).where(BroadcastRecipient.id == r.id)
                                    .values(status="deleted"))
                    await s.commit()
                await asyncio.sleep(1 / RATE)
        # кому ещё не успели отправить — уже не отправим
        async with Session() as s:
            await s.execute(update(BroadcastRecipient).where(BroadcastRecipient.broadcast_id == bid,
                                                             BroadcastRecipient.status == "pending")
                            .values(status="skipped"))
            await s.execute(update(Broadcast).where(Broadcast.id == bid).values(status="cancelled"))
            await s.commit()
        log.info("рассылка %s отменена", bid)
    except Exception:
        log.exception("отмена рассылки %s", bid)


async def resume() -> None:
    """После перезапуска: продолжить незаконченные рассылки и отмены."""
    async with Session() as s:
        rows = (await s.execute(select(Broadcast.id, Broadcast.status)
                                .where(Broadcast.status.in_(("sending", "cancelling"))))).all()
    for bid, status in rows:
        _spawn(_run(bid) if status == "sending" else _delete_sent(bid))


async def stats(bids: list[int]) -> dict[int, dict[str, int]]:
    """{рассылка: {статус получателя: сколько}}."""
    if not bids:
        return {}
    async with Session() as s:
        rows = (await s.execute(select(BroadcastRecipient.broadcast_id, BroadcastRecipient.status, func.count())
                                .where(BroadcastRecipient.broadcast_id.in_(bids))
                                .group_by(BroadcastRecipient.broadcast_id, BroadcastRecipient.status))).all()
    result: dict[int, dict[str, int]] = {b: {} for b in bids}
    for bid, status, n in rows:
        result[bid][status] = n
    return result

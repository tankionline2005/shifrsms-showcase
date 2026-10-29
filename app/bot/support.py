"""Поддержка через бота: каждое обращение — отдельная тема в группе поддержки (настройка support_chat).

Пользователь: «Информация» → «💬 Поддержка» — чат поддержки (состояние Support.chat). Первое сообщение
создаёт обращение и тему «№N · имя» с карточкой пользователя, дальше всё, что он пишет, бот копирует в тему.
Выйти из чата поддержки — любая кнопка меню; вернуться — «✍️ Ответить» под ответом поддержки или ответ
(reply) на него. Новое обращение — только кнопкой «Поддержка».

Группа: всё, что владельцы пишут в теме, бот отправляет пользователю от своего имени («💬 Поддержка»);
сообщение с // в начале — заметка для своих, не отправляется. Закрыть — кнопка на карточке, /close или
закрыть тему в Telegram; закрывают только владельцы. Открыли тему заново — обращение снова открыто.
Бот в группе — администратор с правом «Управление темами».
Карточка пользователя в теме обновляется сама раз в минуту, пока обращение открыто (refresh_cards — правится
только если данные изменились), и по кнопке «🔄 Обновить».

Сайт (/shop/support): web_send — сообщение с сайта в ту же тему (бот пишет его от себя с пометкой «С сайта»).
Вся переписка сохраняется в SupportMessage — сайт показывает историю обращений. Пользователь только сайта
(без Telegram) ответы читает на сайте: в Telegram ему не пишем."""

import asyncio
import logging
import time
from collections import defaultdict, deque
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ChatType, ContentType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, StateFilter
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.base import BaseStorage, StorageKey
from aiogram.types import (
    CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message, ReactionTypeEmoji,
)
from sqlalchemy import func, select, update

from app import settings_store as st
from app import shop_accounts
from app.bot.handlers import ensure_user
from app.bot.ui import (BTN_BALANCE, BTN_BUY, BTN_HELP, BTN_INVITE, BTN_ORDERS, BTN_PROFILE, BTN_WEB, LEGACY_BUTTONS, MSK,
                       STATUS_TITLES, SUPPORT, rub)
from app.db import Session, utcnow
from app.models import Country, Order, Payment, Service, SupportMessage, SupportTicket, User

log = logging.getLogger(__name__)

private = Router()  # пользователь в личке с ботом
private.message.filter(F.chat.type == ChatType.PRIVATE)
private.callback_query.filter(F.message.chat.type == ChatType.PRIVATE)
private.message.outer_middleware(ensure_user)
private.callback_query.outer_middleware(ensure_user)

group = Router()  # группа поддержки
group.message.filter(F.chat.type == ChatType.SUPERGROUP)
group.callback_query.filter(F.message.chat.type == ChatType.SUPERGROUP)

MENU_TEXTS = {BTN_BUY, BTN_WEB, BTN_HELP, BTN_PROFILE, BTN_BALANCE, BTN_ORDERS, BTN_INVITE, *LEGACY_BUTTONS}
HEADER = "💬 <b>Поддержка</b>"
REPLY_KB = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✍️ Ответить", callback_data=SUPPORT)]])
# эти сообщения можно подписать «Поддержка» (у остальных — стикер, кружок, геопозиция — подписи нет)
CAPTIONED = {ContentType.PHOTO, ContentType.VIDEO, ContentType.DOCUMENT, ContentType.AUDIO, ContentType.ANIMATION,
             ContentType.VOICE}
DELIVERABLE = CAPTIONED | {ContentType.TEXT, ContentType.STICKER, ContentType.VIDEO_NOTE, ContentType.LOCATION,
                           ContentType.CONTACT}
FLOOD_LIMIT, FLOOD_WINDOW = 20, 60  # сообщений в поддержку за минуту — фиксированно, без нарастания
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)

_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)  # альбом приходит пачкой — обращение одно
_recent: dict[int, deque] = {}
_warned: dict[int, float] = {}
_shown: dict[int, str] = {}  # обращение -> текст карточки, который сейчас в теме (не править без изменений)


class Support(StatesGroup):
    chat = State()  # всё, что пишет пользователь, уходит в его обращение


class CloseCb(CallbackData, prefix="sclose"):
    id: int


class RefreshCb(CallbackData, prefix="srefresh"):
    id: int


def chat_id() -> int | None:
    raw = st.raw("support_chat").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def enabled() -> bool:
    return chat_id() is not None


def topic_url(t: SupportTicket) -> str:
    return f"https://t.me/c/{str(t.chat_id).removeprefix('-100')}/{t.topic_id}"


def card_kb(ticket_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data=RefreshCb(id=ticket_id).pack()),
        InlineKeyboardButton(text="🔒 Закрыть обращение", callback_data=CloseCb(id=ticket_id).pack())]])


def who(user) -> str:
    if user is None:
        return "администратор"
    return f"@{user.username}" if user.username else (user.full_name or str(user.id))


async def check_chat(bot: Bot, cid: int) -> str | None:
    """Годится ли группа для поддержки. -> текст ошибки или None."""
    try:
        info = await bot.get_chat(cid)
        me = await bot.get_chat_member(cid, bot.id)
    except TelegramAPIError as e:
        return f"Telegram не отдаёт эту группу: {e}"
    if not info.is_forum:
        return "В группе выключены темы — включите «Темы» в настройках группы."
    status = str(getattr(me.status, "value", me.status))
    if status != "creator" and not (status == "administrator" and getattr(me, "can_manage_topics", False)):
        return "Сделайте бота администратором группы с правом «Управление темами»."
    return None


async def open_ticket(user_id: int) -> SupportTicket | None:
    async with Session() as s:
        return await s.scalar(select(SupportTicket).where(SupportTicket.user_id == user_id,
                                                          SupportTicket.status == "open")
                              .order_by(SupportTicket.id.desc()).limit(1))


async def by_topic(cid: int, topic_id: int | None) -> SupportTicket | None:
    if topic_id is None:
        return None
    async with Session() as s:
        return await s.scalar(select(SupportTicket).where(SupportTicket.chat_id == cid,
                                                          SupportTicket.topic_id == topic_id)
                              .order_by(SupportTicket.id.desc()).limit(1))


async def _set(ticket_id: int, **fields) -> None:
    async with Session() as s:
        await s.execute(update(SupportTicket).where(SupportTicket.id == ticket_id).values(**fields))
        await s.commit()


def _when(dt) -> str:
    return (dt + MSK).strftime("%d.%m.%Y %H:%M")


async def save_message(ticket_id: int, text: str, from_staff: bool, via: str | None = None) -> None:
    try:
        async with Session() as s:
            s.add(SupportMessage(ticket_id=ticket_id, text=(text or "")[:4000], from_staff=from_staff, via=via))
            await s.commit()
    except Exception:
        log.exception("поддержка: не сохранить сообщение обращения %s", ticket_id)


ATTACHMENTS = {ContentType.PHOTO: "фото", ContentType.VIDEO: "видео", ContentType.DOCUMENT: "файл",
               ContentType.VOICE: "голосовое", ContentType.AUDIO: "аудио", ContentType.STICKER: "стикер",
               ContentType.VIDEO_NOTE: "видеосообщение", ContentType.ANIMATION: "GIF",
               ContentType.LOCATION: "геопозиция", ContentType.CONTACT: "контакт"}


def message_text(message: Message) -> str:
    """Текст сообщения для истории на сайте; вложения — пометкой в квадратных скобках."""
    text = message.text or message.caption or ""
    kind = ATTACHMENTS.get(message.content_type)
    return f"[{kind}] {text}".strip() if kind else text


async def card_text(ticket_id: int, user_id: int) -> str:
    """Карточка пользователя в начале темы: кто это, деньги, последние активации, прошлые обращения."""
    async with Session() as s:
        u = await s.get(User, user_id)
        orders = (await s.execute(
            select(Order, Service.name, Country.name).join(Service, Service.id == Order.service_id)
            .join(Country, Country.id == Order.country_id).where(Order.user_id == user_id)
            .order_by(Order.id.desc()).limit(5))).all()
        n_orders = await s.scalar(select(func.count()).select_from(Order).where(Order.user_id == user_id))
        n_sms = await s.scalar(select(func.count()).select_from(Order).where(
            Order.user_id == user_id, Order.status.in_(("received", "completed"))))
        pay_n, pay_sum = (await s.execute(select(func.count(), func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.user_id == user_id, Payment.status == "confirmed"))).one()
        prev = list((await s.scalars(select(SupportTicket).where(
            SupportTicket.user_id == user_id, SupportTicket.id != ticket_id, SupportTicket.topic_id.is_not(None))
            .order_by(SupportTicket.id.desc()).limit(5))).all())
    name = escape((u.first_name if u else None) or str(user_id))
    if shop_accounts.is_tg(user_id):
        who_line = (f'👤 <a href="tg://user?id={user_id}">{name}</a>' + (f" @{escape(u.username)}" if u and u.username else "")
                    + f" · ID <code>{user_id}</code>" + (f" · на сайте <code>{escape(u.login)}</code>" if u and u.login else ""))
    else:
        who_line = (f"🌐 Пользователь сайта <code>{escape(u.login if u and u.login else '?')}</code> · ID <code>{user_id}</code>"
                    " · Telegram не привязан — ответы он читает на сайте")
    lines = [f"🆕 <b>Обращение №{ticket_id}</b>", "", who_line]
    if u:
        lines.append(f"В боте с {(u.created_at + MSK):%d.%m.%Y} · баланс <b>{rub(u.balance)}</b>"
                     + (" · ⛔️ заблокирован в боте" if u.is_blocked else ""))
    lines.append(f"Пополнения: {pay_n} на {rub(pay_sum)}" if pay_n else "Пополнений не было")
    lines.append(f"Активаций: {n_orders}, из них с SMS: {n_sms}")
    if orders:
        lines += ["", "<b>Последние активации</b>"]
        for o, service, country in orders:
            lines.append(f"• #{o.id} {escape(service)} · {escape(country)} · {rub(o.price)} · "
                         f"{STATUS_TITLES.get(o.status, o.status)} · {_when(o.created_at)} · +{escape(o.phone)}")
    if prev:
        lines += ["", "Прошлые обращения: " + ", ".join(f'<a href="{topic_url(t)}">№{t.id}</a>' for t in prev)]
    lines += ["", "<i>Карточка обновляется сама каждую минуту, пока обращение открыто. Ответ пользователю — просто "
                  "сообщение в эту тему. Начните сообщение с // — это заметка для своих, пользователь её не увидит. "
                  "Закрыть — кнопкой ниже или /close.</i>"]
    return "\n".join(lines)


async def refresh_card(bot: Bot, t: SupportTicket, final: bool = False) -> str:
    """Перерисовать карточку обращения. -> changed | same | error. final — обращение закрыто: без кнопок."""
    if not t.card_message_id:
        return "error"
    text = await card_text(t.id, t.user_id)
    if not final and _shown.get(t.id) == text:
        return "same"
    try:
        await bot.edit_message_text(text, chat_id=t.chat_id, message_id=t.card_message_id,
                                    reply_markup=None if final else card_kb(t.id), link_preview_options=NO_PREVIEW)
    except TelegramRetryAfter:
        raise
    except TelegramBadRequest as e:
        if "not modified" not in str(e).lower():
            log.info("поддержка: карточка обращения %s: %s", t.id, e)
            return "error"
        _shown[t.id] = text
        return "same"
    except TelegramAPIError as e:
        log.info("поддержка: карточка обращения %s: %s", t.id, e)
        return "error"
    if final:
        _shown.pop(t.id, None)
    else:
        _shown[t.id] = text
    return "changed"


async def refresh_cards() -> None:
    """Раз в минуту: карточки открытых обращений — правим только те, где что-то изменилось."""
    from app.bot import channels  # бот задаётся в main
    if channels.bot is None:
        return
    async with Session() as s:
        tickets = list((await s.scalars(select(SupportTicket).where(
            SupportTicket.status == "open", SupportTicket.card_message_id.is_not(None)))).all())
    for t in tickets:
        try:
            await refresh_card(channels.bot, t)
        except TelegramRetryAfter as e:  # Telegram просит притормозить — остальные в следующую минуту
            log.info("поддержка: обновление карточек — пауза %s с", e.retry_after)
            return


async def _create(bot: Bot, user=None, *, user_id: int | None = None, title: str | None = None) -> SupportTicket | None:
    """Новое обращение: строка в базе, тема в группе, карточка пользователя.
    user — пользователь Telegram; с сайта — user_id и title (название темы)."""
    cid = chat_id()
    if cid is None:
        return None
    uid = user.id if user else user_id
    async with Session() as s:
        t = SupportTicket(user_id=uid, chat_id=cid)
        s.add(t)
        await s.commit()
        await s.refresh(t)
    name = f"№{t.id} · " + (title or (f"{user.full_name or user.id}" + (f" @{user.username}" if user.username else "")))
    try:
        topic = await bot.create_forum_topic(cid, name[:128])
    except TelegramAPIError as e:
        log.warning("поддержка: не создать тему в %s: %s", cid, e)
        async with Session() as s:
            row = await s.get(SupportTicket, t.id)
            await s.delete(row)
            await s.commit()
        return None
    card = None
    try:
        text = await card_text(t.id, uid)
        card = await bot.send_message(cid, text, message_thread_id=topic.message_thread_id,
                                      reply_markup=card_kb(t.id), link_preview_options=NO_PREVIEW)
        _shown[t.id] = text
    except TelegramAPIError as e:
        log.warning("поддержка: карточка обращения %s: %s", t.id, e)
    await _set(t.id, topic_id=topic.message_thread_id, card_message_id=card.message_id if card else None)
    t.topic_id, t.card_message_id = topic.message_thread_id, card.message_id if card else None
    return t


async def _copy_in(bot: Bot, message: Message, t: SupportTicket) -> str:
    """Сообщение пользователя — в тему обращения. -> ok | gone (темы больше нет) | error."""
    try:
        await bot.copy_message(t.chat_id, message.chat.id, message.message_id, message_thread_id=t.topic_id)
        return "ok"
    except TelegramBadRequest as e:
        low = str(e).lower()
        log.warning("поддержка: сообщение в обращение %s: %s", t.id, e)
        return "gone" if "thread" in low or "topic" in low else "error"
    except TelegramAPIError as e:
        log.warning("поддержка: сообщение в обращение %s: %s", t.id, e)
        return "error"


def _flood(user_id: int) -> bool:
    now = time.monotonic()
    q = _recent.setdefault(user_id, deque())
    while q and now - q[0] > FLOOD_WINDOW:
        q.popleft()
    if len(q) >= FLOOD_LIMIT:
        return True
    q.append(now)
    return False


async def _react(bot: Bot, message: Message, emoji: str) -> None:
    try:
        await bot.set_message_reaction(message.chat.id, message.message_id, [ReactionTypeEmoji(emoji=emoji)])
    except TelegramAPIError:
        pass  # отметка «принято» — не обязательна


async def clear_mode(storage: BaseStorage, bot: Bot, user_id: int) -> None:
    """Обращение закрыто — пользователь выходит из чата поддержки (новое — только кнопкой)."""
    key = StorageKey(bot_id=bot.id, chat_id=user_id, user_id=user_id)
    if await storage.get_state(key) == Support.chat.state:
        await storage.set_state(key, None)


async def close(bot: Bot, storage: BaseStorage, t: SupportTicket, by: str, close_topic: bool = True) -> bool:
    async with Session() as s:
        res = await s.execute(update(SupportTicket).where(SupportTicket.id == t.id, SupportTicket.status == "open")
                              .values(status="closed", closed_at=utcnow(), closed_by=by[:64]))
        await s.commit()
    if res.rowcount != 1:
        return False  # уже закрыто
    try:  # последний раз обновить карточку и убрать кнопки — больше не нужны
        await refresh_card(bot, t, final=True)
    except TelegramAPIError:
        pass
    calls = []
    if close_topic:
        calls.append(lambda: bot.close_forum_topic(t.chat_id, t.topic_id))
    calls.append(lambda: bot.send_message(t.chat_id, f"🔒 Обращение закрыто — {escape(by)}.",
                                          message_thread_id=t.topic_id))
    for call in calls:
        try:
            await call()
        except TelegramAPIError:
            pass
    await save_message(t.id, "Обращение закрыто", from_staff=True, via="system")
    if shop_accounts.is_tg(t.user_id):
        try:
            await bot.send_message(t.user_id, f"✅ Обращение №{t.id} закрыто. Если появится новый вопрос — "
                                              "«ℹ️ Информация» → «💬 Поддержка».")
        except TelegramAPIError:
            pass
        await clear_mode(storage, bot, t.user_id)
    return True


# ------------------------------------------------------------------ пользователь


@private.callback_query(F.data == SUPPORT)
async def support_start(cb: CallbackQuery, state: FSMContext):
    t = await open_ticket(cb.from_user.id)
    if t is None and not enabled():
        await cb.answer("Поддержка сейчас недоступна, попробуйте позже.", show_alert=True)
        return
    await state.set_state(Support.chat)
    await cb.answer()
    if t:
        text = (f"💬 Обращение №{t.id} открыто — напишите сообщение, передадим в поддержку. Ответ придёт сюда.\n\n"
                "Вернуться к активациям — кнопками меню внизу.")
    else:
        text = ("💬 <b>Поддержка</b>\n\n"
                "Опишите вопрос — одним или несколькими сообщениями, можно со скриншотами. Если вопрос об активации, "
                "укажите номер телефона.\n\n"
                "Пока открыт чат поддержки, всё, что вы пишете, получаем мы. Вернуться к активациям — кнопками "
                "меню внизу.")
    await cb.message.answer(text)


def _not_menu(message: Message) -> bool:
    if message.successful_payment or message.refunded_payment:
        return False  # оплата звёздами — зачисление (handlers.stars_paid), не в поддержку
    text = message.text
    return not (text and (text in MENU_TEXTS or text.startswith("/")))


def _reply_to_support(message: Message) -> bool:
    """Ответ (reply) на сообщение поддержки — тоже в обращение, даже вне чата поддержки."""
    r = message.reply_to_message
    markup = r.reply_markup if r else None
    return bool(markup and any(b.callback_data == SUPPORT for row in markup.inline_keyboard for b in row))


@private.message(StateFilter(Support.chat), _not_menu)
@private.message(_reply_to_support, _not_menu)
async def from_user(message: Message, state: FSMContext, bot: Bot):
    await state.set_state(Support.chat)
    uid = message.from_user.id
    if _flood(uid):
        if time.monotonic() - _warned.get(uid, 0) > FLOOD_WINDOW:
            _warned[uid] = time.monotonic()
            await message.answer("Слишком много сообщений подряд — подождите минуту.")
        return
    async with _locks[uid]:
        t = await open_ticket(uid)
        if t and t.topic_id is None:  # тему так и не создали (сбой) — это обращение не считается
            await _set(t.id, status="closed", closed_at=utcnow(), closed_by="не создана тема")
            t = None
        created = t is None
        if created:
            t = await _create(bot, message.from_user)
        result = await _copy_in(bot, message, t) if t else "error"
        if result == "gone" and not created:  # тему удалили в группе — начинаем новое обращение
            await _set(t.id, status="closed", closed_at=utcnow(), closed_by="тема удалена")
            created, t = True, await _create(bot, message.from_user)
            result = await _copy_in(bot, message, t) if t else "error"
    if result == "ok":
        await save_message(t.id, message_text(message), from_staff=False, via="tg")
    if result != "ok":
        await message.answer("Не получилось передать сообщение в поддержку — попробуйте ещё раз через пару минут.")
    elif created:
        await message.answer(f"✅ Обращение №{t.id} создано. Ответ придёт сюда, в бот — можно дописать ещё сообщения.")
    else:
        await _react(bot, message, "👌")


# ------------------------------------------------------------------ группа поддержки


@group.callback_query(CloseCb.filter())
async def close_button(cb: CallbackQuery, callback_data: CloseCb, bot: Bot, fsm_storage: BaseStorage):
    async with Session() as s:
        t = await s.get(SupportTicket, callback_data.id)
    if t and t.chat_id == cb.message.chat.id and await close(bot, fsm_storage, t, who(cb.from_user)):
        await cb.answer("Обращение закрыто.")
    else:
        await cb.answer("Обращение уже закрыто.")


@group.callback_query(RefreshCb.filter())
async def refresh_button(cb: CallbackQuery, callback_data: RefreshCb, bot: Bot):
    async with Session() as s:
        t = await s.get(SupportTicket, callback_data.id)
    if not t or t.chat_id != cb.message.chat.id:
        await cb.answer()
        return
    if t.status != "open":
        await cb.answer("Обращение закрыто.")
        return
    t.card_message_id = cb.message.message_id  # кнопка нажата именно на этой карточке
    try:
        result = await refresh_card(bot, t)
    except TelegramRetryAfter as e:
        await cb.answer(f"Telegram просит подождать {e.retry_after} с.")
        return
    await cb.answer({"changed": "Обновлено.", "same": "Ничего не изменилось."}.get(result, "Не получилось обновить."))


@group.message(F.forum_topic_closed)
async def topic_closed(message: Message, bot: Bot, fsm_storage: BaseStorage):
    """Тему закрыли в Telegram — закрываем и обращение."""
    t = await by_topic(message.chat.id, message.message_thread_id)
    if t:
        await close(bot, fsm_storage, t, who(message.from_user), close_topic=False)


@group.message(F.forum_topic_reopened)
async def topic_reopened(message: Message, bot: Bot):
    """Тему открыли заново — обращение снова открыто (если у пользователя нет другого открытого)."""
    t = await by_topic(message.chat.id, message.message_thread_id)
    if t is None or t.status == "open":
        return
    other = await open_ticket(t.user_id)
    if other:
        await message.answer(f'У пользователя уже открыто <a href="{topic_url(other)}">обращение №{other.id}</a> — '
                             "отвечайте там. Эту тему закрываю.")
        try:
            await bot.close_forum_topic(t.chat_id, t.topic_id)
        except TelegramAPIError:
            pass
        return
    await message.answer("🔓 Обращение снова открыто: ваши ответы получит пользователь, его новые сообщения придут сюда.")
    text = await card_text(t.id, t.user_id)  # свежая карточка с кнопками — внизу темы
    card = await message.answer(text, reply_markup=card_kb(t.id), link_preview_options=NO_PREVIEW)
    _shown[t.id] = text
    await _set(t.id, status="open", closed_at=None, closed_by=None, card_message_id=card.message_id)
    await save_message(t.id, "Обращение снова открыто", from_staff=True, via="system")
    if shop_accounts.is_tg(t.user_id):
        try:
            await bot.send_message(t.user_id, f"💬 Поддержка снова открыла обращение №{t.id}.", reply_markup=REPLY_KB)
        except TelegramAPIError:
            pass


@group.message(Command("close"))
async def close_command(message: Message, bot: Bot, fsm_storage: BaseStorage):
    t = await by_topic(message.chat.id, message.message_thread_id if message.is_topic_message else None)
    if t is None:
        return
    if not await close(bot, fsm_storage, t, who(message.from_user)):
        await message.reply("Обращение уже закрыто.")


async def deliver(bot: Bot, message: Message, user_id: int) -> None:
    """Ответ из темы — пользователю от имени бота, с подписью «Поддержка» и кнопкой «Ответить»."""
    if message.content_type == ContentType.TEXT:
        body = f"{HEADER}\n\n{message.html_text}"
        if len(body) <= 4096:
            await bot.send_message(user_id, body, reply_markup=REPLY_KB, link_preview_options=NO_PREVIEW)
            return
    elif message.content_type in CAPTIONED:
        caption = HEADER + (f"\n\n{message.html_text}" if message.caption else "")
        if len(caption) <= 1024:
            await bot.copy_message(user_id, message.chat.id, message.message_id, caption=caption,
                                   reply_markup=REPLY_KB)
            return
    await bot.copy_message(user_id, message.chat.id, message.message_id, reply_markup=REPLY_KB)


@group.message(F.is_topic_message)
async def from_staff(message: Message, bot: Bot):
    if message.content_type not in DELIVERABLE:
        return  # служебные сообщения группы
    if message.from_user and message.from_user.is_bot and not message.sender_chat:
        return
    text = message.text or message.caption or ""
    if text.startswith("//") or (message.text or "").startswith("/"):
        return  # заметка для своих или команда
    t = await by_topic(message.chat.id, message.message_thread_id)
    if t is None:
        return  # тема не обращения
    if t.status != "open":
        await message.reply("🔒 Обращение закрыто — пользователь это сообщение не получил. Откройте тему заново, "
                            "чтобы ответить.")
        return
    await save_message(t.id, message_text(message), from_staff=True)  # на сайте ответ виден в любом случае
    if not shop_accounts.is_tg(t.user_id):
        await _react(bot, message, "👍")  # пользователь только сайта — прочитает там
        return
    try:
        await deliver(bot, message, t.user_id)
    except TelegramForbiddenError:
        await message.reply("⚠️ Не доставлено в Telegram: пользователь заблокировал бота (на сайте ответ виден).")
        async with Session() as s:
            await s.execute(update(User).where(User.id == t.user_id).values(bot_blocked=True))
            await s.commit()
        return
    except TelegramAPIError as e:
        await message.reply(f"⚠️ Не доставлено: {escape(str(e))}")
        return
    await _react(bot, message, "👍")


# ------------------------------------------------------------------ сайт


async def web_send(user_id: int, text: str) -> tuple[SupportTicket | None, str]:
    """Сообщение пользователя с сайта — в его открытое обращение (или новое). -> (обращение, ok|flood|off|error)."""
    from app.bot import channels  # бот задаётся в main
    bot = channels.bot
    text = (text or "").strip()[:3500]
    if not text:
        return None, "error"
    if bot is None or (await open_ticket(user_id) is None and not enabled()):
        return None, "off"
    if _flood(user_id):
        return None, "flood"
    async with _locks[user_id]:
        t = await open_ticket(user_id)
        if t and t.topic_id is None:
            await _set(t.id, status="closed", closed_at=utcnow(), closed_by="не создана тема")
            t = None
        async with Session() as s:
            u = await s.get(User, user_id)
        if shop_accounts.is_tg(user_id):
            title = f"{(u.first_name if u else None) or user_id}" + (f" @{u.username}" if u and u.username else "") + " (сайт)"
        else:
            title = f"{u.login if u and u.login else user_id} (сайт)"
        if t is None:
            t = await _create(bot, user_id=user_id, title=title)
        if t is None:
            return None, "error"
        body = f"🌐 <b>С сайта:</b>\n{escape(text)}"
        try:
            await bot.send_message(t.chat_id, body, message_thread_id=t.topic_id, link_preview_options=NO_PREVIEW)
        except TelegramBadRequest as e:
            low = str(e).lower()
            if "thread" not in low and "topic" not in low:
                log.warning("поддержка: сообщение с сайта в обращение %s: %s", t.id, e)
                return t, "error"
            await _set(t.id, status="closed", closed_at=utcnow(), closed_by="тема удалена")  # тему удалили — новое
            t = await _create(bot, user_id=user_id, title=title)
            if t is None:
                return None, "error"
            try:
                await bot.send_message(t.chat_id, body, message_thread_id=t.topic_id, link_preview_options=NO_PREVIEW)
            except TelegramAPIError:
                return t, "error"
        except TelegramAPIError as e:
            log.warning("поддержка: сообщение с сайта в обращение %s: %s", t.id, e)
            return t, "error"
    await save_message(t.id, text, from_staff=False, via="web")
    return t, "ok"


async def join_merged(bot: Bot, keep: SupportTicket, old: SupportTicket) -> None:
    """Склейка аккаунтов, а открытые обращения были у обоих: остаётся обращение из бота (keep), обращение с сайта (old)
    закрывается — в обеих темах ссылка друг на друга, переписка с сайта остаётся в истории закрытого обращения."""
    old.user_id = keep.user_id  # строка в базе уже переехала к пользователю бота
    async with Session() as s:
        res = await s.execute(update(SupportTicket).where(SupportTicket.id == old.id, SupportTicket.status == "open")
                              .values(status="closed", closed_at=utcnow(), closed_by="склейка аккаунтов"))
        await s.commit()
    if res.rowcount != 1:
        return
    for t in (old, keep):
        try:
            await refresh_card(bot, t, final=t is old)
        except TelegramAPIError:
            pass
    notes = ((old, f"🔗 Пользователь привязал Telegram — аккаунты сайта и бота объединены. Переписка продолжается "
                   f'в обращении <a href="{topic_url(keep)}">№{keep.id}</a>.'),
             (keep, f"🔗 Пользователь привязал Telegram к аккаунту сайта. Его обращение с сайта — "
                    f'<a href="{topic_url(old)}">№{old.id}</a>: закрыто, переписка осталась там.'))
    for t, text in notes:
        try:
            await bot.send_message(t.chat_id, text, message_thread_id=t.topic_id, link_preview_options=NO_PREVIEW)
        except TelegramAPIError:
            pass
    try:
        await bot.close_forum_topic(old.chat_id, old.topic_id)
    except TelegramAPIError:
        pass
    await save_message(old.id, f"Обращение объединено с №{keep.id}", from_staff=True, via="system")
    await save_message(keep.id, f"Присоединено обращение №{old.id}", from_staff=True, via="system")


async def tickets_of(user_id: int) -> list[SupportTicket]:
    async with Session() as s:
        return list((await s.scalars(select(SupportTicket).where(SupportTicket.user_id == user_id,
                                                                  SupportTicket.topic_id.is_not(None))
                                     .order_by(SupportTicket.id.desc()).limit(50))).all())


async def messages_of(ticket_id: int) -> list[SupportMessage]:
    async with Session() as s:
        return list((await s.scalars(select(SupportMessage).where(SupportMessage.ticket_id == ticket_id)
                                     .order_by(SupportMessage.id))).all())

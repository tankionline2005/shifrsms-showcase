"""Каналы и группы, где состоит бот, и рекламный комментарий под каждым новым постом канала.

Как это устроено в Telegram: новый пост канала Telegram сам пересылает в привязанную группу
обсуждения (сообщение с is_automatic_forward), а комментарии к посту — это ответы на эту копию.
Бот отвечает на неё случайным сообщением из настроенных для канала (ChannelMessage, до MAX_MESSAGES:
текст и/или фото) с кнопкой «Открыть бота». Чтобы видеть такие пересылки, бот должен быть администратором группы обсуждения
(обычный бот в группе получает только команды и обращения к себе).

Списка своих чатов Telegram боту не даёт — бот запоминает их сам: когда его добавляют, удаляют
или меняют права (my_chat_member), когда в канале выходит пост, или когда админ добавляет чат
в панели по @username / ID."""

import logging
import random
import time

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import (
    BufferedInputFile, ChatMemberUpdated, InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Message,
    ReplyParameters,
)

from sqlalchemy import func, select

from app import referrals
from app.db import Session, utcnow
from app.models import BotChat, ChannelMessage

log = logging.getLogger(__name__)
router = Router()

MAX_MESSAGES = 5
# «Шаблон сообщения» (панель → «Каналы»): ChannelMessage с этим chat_id. Каждому новому каналу он копируется
# первым сообщением — рефералу не обязательно что-то настраивать самому
TEMPLATE = 0
MAX_TEXT, MAX_CAPTION = 4096, 1024  # лимиты Telegram: текст сообщения и подпись к фото
ADMIN = ("creator", "administrator")
BUTTON = "🤖 Открыть бота"  # текст кнопки, если у сообщения не задан свой (ChannelMessage.button_text)
MAX_BUTTON = 64
NO_PREVIEW = LinkPreviewOptions(is_disabled=True)  # ссылки в комментарии — без карточки предпросмотра
bot: Bot | None = None  # задаётся в main — панели нужен бот, чтобы спрашивать Telegram о чатах
_albums: dict[str, float] = {}  # media_group_id -> когда прокомментировали (альбом — один комментарий)


async def messages_of(chat_id: int) -> list[ChannelMessage]:
    async with Session() as s:
        return list((await s.scalars(select(ChannelMessage).where(ChannelMessage.chat_id == chat_id)
                                     .order_by(ChannelMessage.id))).all())


def _status(member) -> str:
    status = member.status
    return str(getattr(status, "value", status))


async def _save(chat_id: int, **fields) -> BotChat:
    async with Session() as s:
        row = await s.get(BotChat, chat_id)
        created = row is None
        if created:
            row = BotChat(id=chat_id, comments_on=False)
            s.add(row)
        for k, v in fields.items():
            setattr(row, k, v)
        row.updated_at = utcnow()
        await s.commit()
    if created and row.type == "channel":
        await apply_template(chat_id)
    return row


async def apply_template(chat_id: int) -> bool:
    """Новый канал: скопировать «Шаблон сообщения» первым сообщением (если у канала ещё нет сообщений)."""
    try:
        async with Session() as s:
            if await s.scalar(select(func.count()).select_from(ChannelMessage).where(ChannelMessage.chat_id == chat_id)):
                return False
            tpl = list((await s.scalars(select(ChannelMessage).where(ChannelMessage.chat_id == TEMPLATE)
                                        .order_by(ChannelMessage.id))).all())
            for m in tpl[:MAX_MESSAGES]:
                s.add(ChannelMessage(chat_id=chat_id, text=m.text, photo=m.photo, photo_type=m.photo_type,
                                     photo_file_id=m.photo_file_id, thumb=m.thumb, button_text=m.button_text,
                                     button_url=m.button_url))
            await s.commit()
        if tpl:
            log.info("канал %s: скопирован шаблон сообщения", chat_id)
        return bool(tpl)
    except Exception:
        log.exception("канал %s: не удалось скопировать шаблон сообщения", chat_id)
        return False


async def refresh_chat(b: Bot, ref: int | str, linked: bool = True) -> BotChat | None:
    """Спросить у Telegram сведения о чате (название, связь канал ↔ обсуждение, права бота) и
    запомнить. ref — id или @username. linked — заодно обновить связанный чат. None — Telegram
    о таком чате ничего не сказал (бота там нет и чат закрытый, или опечатка)."""
    try:
        info = await b.get_chat(ref)
    except TelegramAPIError as e:
        log.info("чат %s: %s", ref, e)
        return None
    try:
        status = _status(await b.get_chat_member(info.id, b.id))
    except TelegramAPIError:
        status = "left"
    row = await _save(info.id, type=str(getattr(info.type, "value", info.type)),
                      title=(info.title or info.username or str(info.id))[:255],
                      username=info.username, status=status, linked_id=info.linked_chat_id,
                      is_forum=bool(info.is_forum))
    if linked and info.linked_chat_id:
        await refresh_chat(b, info.linked_chat_id, linked=False)
    return row


async def refresh_all(b: Bot) -> None:
    async with Session() as s:
        ids = (await s.scalars(select(BotChat.id))).all()
    for chat_id in ids:
        if not await refresh_chat(b, chat_id, linked=False):
            await _save(chat_id, status="left")  # Telegram больше не отдаёт чат — бота там нет


async def _send(b: Bot, chat_id: int, msg: ChannelMessage, reply: ReplyParameters, kb, plain: bool,
                upload: bool) -> str | None:
    """Отправить комментарий. -> file_id фото в Telegram (чтобы дальше не загружать файл)."""
    extra = {"parse_mode": None} if plain else {}
    if msg.photo:
        photo = msg.photo_file_id if msg.photo_file_id and not upload else BufferedInputFile(msg.photo, "photo")
        sent = await b.send_photo(chat_id, photo, caption=msg.text or None, reply_parameters=reply,
                                  reply_markup=kb, **extra)
        return sent.photo[-1].file_id if sent.photo else None
    await b.send_message(chat_id, msg.text, reply_parameters=reply, reply_markup=kb,
                         link_preview_options=NO_PREVIEW, **extra)
    return None


@router.my_chat_member(F.chat.type != ChatType.PRIVATE)
async def membership(event: ChatMemberUpdated, bot: Bot) -> None:
    """Бота добавили в канал/группу, удалили или поменяли ему права."""
    status = _status(event.new_chat_member)
    chat = event.chat
    log.info("бот в чате %s (%s): %s", chat.id, chat.title, status)
    await _save(chat.id, type=str(getattr(chat.type, "value", chat.type)),
                title=(chat.title or chat.username or str(chat.id))[:255],
                username=chat.username, status=status, is_forum=bool(getattr(chat, "is_forum", False)))
    if status not in ("left", "kicked"):
        await refresh_chat(bot, chat.id)  # связь с каналом / обсуждением


@router.message(F.migrate_to_chat_id)
async def migrated(message: Message, bot: Bot) -> None:
    """Группа стала супергруппой (например, включили темы) — у неё новый ID, старый больше не нужен."""
    await _save(message.chat.id, status="left")
    await refresh_chat(bot, message.migrate_to_chat_id)


@router.channel_post()
async def channel_post(message: Message, bot: Bot) -> None:
    """Пост в канале, где бот администратор: запоминаем канал, если ещё не знали
    (так находятся каналы, куда бота добавили до появления этой функции)."""
    async with Session() as s:
        known = await s.get(BotChat, message.chat.id)
    if known is None:
        await refresh_chat(bot, message.chat.id)


@router.message(F.is_automatic_forward, F.sender_chat)
async def new_post(message: Message, bot: Bot) -> None:
    """Копия нового поста канала в группе обсуждения — оставляем комментарий."""
    if message.media_group_id:  # альбом приходит несколькими сообщениями
        now = time.monotonic()
        for k in [k for k, t in _albums.items() if now - t > 600]:
            del _albums[k]
        if message.media_group_id in _albums:
            return
        _albums[message.media_group_id] = now
    channel_id = message.sender_chat.id
    async with Session() as s:
        channel = await s.get(BotChat, channel_id)
    if channel is None:
        channel = await refresh_chat(bot, channel_id)
    if channel is None or not channel.comments_on:
        return
    items = await messages_of(channel_id)
    if not items:
        return
    me = await bot.me()
    ref = await referrals.by_chat(channel_id)  # у канала реферала — ссылка с его кодом, всегда
    msg, error, plain, upload, file_id = random.choice(items), None, False, False, None
    if ref:
        url = f"https://t.me/{me.username}?start={referrals.START_PREFIX}{ref.code}"
    else:  # свою ссылку (например, с UTM-меткой) можно задать у сообщения
        url = msg.button_url or f"https://t.me/{me.username}"
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=msg.button_text or BUTTON, url=url)]])
    for _ in range(3):
        try:
            file_id = await _send(bot, message.chat.id, msg, ReplyParameters(message_id=message.message_id), kb,
                                  plain, upload)
            break
        except TelegramBadRequest as e:
            low = str(e).lower()
            if "parse" in low and not plain:
                plain = True  # в тексте ошибка разметки (например, незакрытый тег) — отправляем как есть
            elif msg.photo_file_id and not upload:
                upload = True  # сохранённое фото Telegram больше не узнаёт — загружаем файл заново
            else:
                error = str(e)[:255]
                break
        except TelegramAPIError as e:
            error = str(e)[:255]
            break
    if error:
        log.warning("комментарий к посту канала %s: %s", channel_id, error)
    if file_id and file_id != msg.photo_file_id:
        async with Session() as s:
            row = await s.get(ChannelMessage, msg.id)
            if row:
                row.photo_file_id = file_id
                await s.commit()
    if error:
        await _save(channel_id, last_error=error)
    else:
        await _save(channel_id, last_comment_at=utcnow(), last_error=None)

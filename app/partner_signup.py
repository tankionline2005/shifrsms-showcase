"""Самостоятельное подключение канала к партнёрской программе — окно на лендинге /partnership (app/web/landing.py).

1. Партнёр даёт ссылку на свой ПУБЛИЧНЫЙ канал — бот видит такой канал (описание, группу обсуждения, число
   подписчиков), не состоя в нём. Проверяем: это канал, подписчиков не меньше partner_min_subscribers, канал ещё
   не партнёр и не удалён нами из программы (PartnerBan). Группа обсуждения не обязательна — её можно привязать
   потом, шаги настройки в кабинете подскажут.
2. Код подтверждения создаётся только на этом шаге (заранее его не знает никто), привязан к каналу и к браузеру
   (черновик в памяти, ключ — в сессии) и живёт CODE_TTL. Партнёр вписывает код в описание канала — менять его
   могут только администраторы канала, это и есть проверка.
3. Код нашёлся в описании — канал становится рефералом (referrals.create), партнёр сразу входит в кабинет,
   владельцам — уведомление. Логин и пароль показываются один раз.

Черновики — в памяти: после перезапуска сервера код надо получить заново (живёт он всё равно 15 минут)."""

import asyncio
import html
import logging
import re
import secrets
import time
from dataclasses import dataclass

from aiogram.exceptions import TelegramAPIError

from app import payouts, referrals
from app import settings_store as st
from app.bot import channels
from app.db import Session
from app.models import PartnerBan

log = logging.getLogger(__name__)

CODE_TTL = 15 * 60
DRAFT_TTL = 60 * 60
MAX_DRAFTS = 2000
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без 0/O и 1/I — не перепутать при вводе
CODE_LEN = 6
# не больше N запросов одного вида с одного адреса за окно (секунд)
LIMITS = {"check": (20, 600), "code": (10, 600), "verify": (40, 600)}

_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]{3,31}$")


class SignupError(Exception):
    """Текст — для партнёра, показывается в окне как есть."""


@dataclass
class Draft:
    chat_id: int
    username: str
    title: str
    members: int
    created: float
    code: str | None = None
    code_until: float = 0


_drafts: dict[str, Draft] = {}
_hits: dict[tuple[str, str], list[float]] = {}
_lock = asyncio.Lock()  # подключение — по одному, чтобы один канал не стал партнёром дважды


def limit(kind: str, ip: str) -> None:
    n, window = LIMITS[kind]
    now = time.monotonic()
    hits = [t for t in _hits.get((kind, ip), []) if now - t < window]
    if len(hits) >= n:
        raise SignupError("Слишком много попыток — подождите несколько минут.")
    hits.append(now)
    _hits[(kind, ip)] = hits
    if len(_hits) > 20000:
        _hits.clear()


def parse_link(raw: str) -> str:
    """Ссылка или @username публичного канала -> username. Приглашения t.me/+… — закрытые каналы, не подходят."""
    s = (raw or "").strip()
    s = re.sub(r"^(https?://)?(www\.)?(t\.me|telegram\.me|telegram\.dog)/", "", s, flags=re.I)
    s = s.lstrip("@").split("?")[0].split("/")[0].strip()
    if s.startswith("+") or s.lower() == "joinchat":
        raise SignupError("Это ссылка-приглашение закрытого канала. Нужен публичный канал: t.me/имя_канала.")
    if s.lower().startswith("s/"):
        s = s[2:]
    if not _NAME.match(s):
        raise SignupError("Укажите ссылку на канал: t.me/имя_канала или @имя_канала.")
    return s


async def _channel(username_or_id: str | int):
    """Сведения о канале у Telegram -> (информация, число подписчиков) или SignupError."""
    bot = channels.bot
    if bot is None:
        raise SignupError("Сервис временно недоступен — попробуйте чуть позже.")
    ref = f"@{username_or_id}" if isinstance(username_or_id, str) else username_or_id
    try:
        info = await bot.get_chat(ref)
    except TelegramAPIError:
        raise SignupError("Канал не найден. Проверьте ссылку — канал должен быть публичным.") from None
    if str(getattr(info.type, "value", info.type)) != "channel":
        raise SignupError("Это не канал. Укажите ссылку на сам канал, а не на группу или бота.")
    if not info.username:
        raise SignupError("Канал должен быть публичным — с адресом t.me/имя_канала.")
    try:
        members = await bot.get_chat_member_count(info.id)
    except TelegramAPIError:
        members = 0
    return info, members


async def _eligible(info, members: int) -> None:
    """Можно ли этому каналу стать партнёром (проверяется и на шаге 1, и при подтверждении)."""
    async with Session() as s:
        banned = await s.get(PartnerBan, info.id)
    if banned:
        raise SignupError("Этот канал не может участвовать в партнёрской программе.")
    if await referrals.by_chat(info.id):
        raise SignupError("Канал уже подключён к партнёрской программе. Если вы потеряли доступ к кабинету — "
                          "напишите нам.")
    need = st.int_("partner_min_subscribers")
    if members < need:
        raise SignupError(f"Партнёрская программа — для каналов от {need} подписчиков. Сейчас у канала {members}.")


def _clean() -> None:
    now = time.monotonic()
    for k in [k for k, d in _drafts.items() if now - d.created > DRAFT_TTL]:
        del _drafts[k]
    while len(_drafts) >= MAX_DRAFTS:
        del _drafts[next(iter(_drafts))]


def draft(token: str | None) -> Draft | None:
    d = _drafts.get(token or "")
    if d and time.monotonic() - d.created > DRAFT_TTL:
        _drafts.pop(token, None)
        return None
    return d


async def check(link: str) -> tuple[str, Draft]:
    """Шаг 1. -> (ключ черновика для сессии, черновик)."""
    info, members = await _channel(parse_link(link))
    await _eligible(info, members)
    _clean()
    token = secrets.token_urlsafe(18)
    d = Draft(chat_id=info.id, username=info.username, title=info.title or info.username, members=members,
              created=time.monotonic())
    _drafts[token] = d
    return token, d


def new_code(d: Draft) -> int:
    """Шаг 2: новый код (прежний перестаёт действовать). -> сколько секунд он живёт."""
    d.code = "SHIFR-" + "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LEN))
    d.code_until = time.monotonic() + CODE_TTL
    return CODE_TTL


def code_left(d: Draft) -> int:
    return max(0, int(d.code_until - time.monotonic())) if d.code else 0


async def verify(token: str, d: Draft) -> tuple[referrals.Referral, str]:
    """Шаг 3: код в описании канала -> (новый реферал, пароль)."""
    if not d.code or not code_left(d):
        raise SignupError("Код устарел — получите новый.")
    async with _lock:
        info, members = await _channel(d.chat_id)
        await _eligible(info, members)
        if d.code.lower() not in (info.description or "").lower():
            raise SignupError("Код не найден в описании канала. Проверьте, что описание сохранено, и нажмите "
                              "«Проверить» ещё раз. Если только что сохранили — подождите полминуты.")
        chat = await channels.refresh_chat(channels.bot, info.id)  # канал и его обсуждение — в «Каналы»
        if chat is None:
            raise SignupError("Не удалось получить сведения о канале — попробуйте ещё раз.")
        ref, password = await referrals.create(chat)
        _drafts.pop(token, None)
    log.info("партнёр подключился сам: канал %s (@%s), %s подписчиков, логин %s", info.id, info.username,
             members, ref.login)
    await payouts.notify(f"🤝 Новый партнёр подключился сам: канал «{html.escape(info.title or '')}» (@{info.username}), "
                         f"подписчиков: {members}. Логин кабинета: {ref.login}. Удалить — «Каналы» в панели.")
    return ref, password


async def ban(chat_id: int) -> None:
    """Мы удалили реферала — канал больше не подключится сам (вернуть может только админ кнопкой в «Каналах»)."""
    async with Session() as s:
        if not await s.get(PartnerBan, chat_id):
            s.add(PartnerBan(chat_id=chat_id))
            await s.commit()


async def unban(chat_id: int) -> None:
    async with Session() as s:
        row = await s.get(PartnerBan, chat_id)
        if row:
            await s.delete(row)
            await s.commit()

"""Проверка номеров Telegram перед выдачей: нет ли уже аккаунта на номере.

Бот так не умеет — проверяют пользовательские аккаунты владельцев (TgChecker), подключённые к серверу
отдельной сессией (Telethon): номер → в панели код, который Telegram присылает внутрь Telegram, → облачный пароль,
если есть. Сессии в базе — зашифрованы (ключ из SESSION_SECRET), api_id/api_hash — TG_API_ID/TG_API_HASH в .env.

Проверка — contacts.resolvePhone (как нажатие на номер в Telegram): «номер не занят» — свободен; найден
пользователь, в том числе удалённый аккаунт, — занят. Аккаунты по очереди (дольше всех не проверявший — первым),
лимит в сутки и пауза — в «Настройках» (0 — без ограничений). Telegram попросил притормозить — аккаунт отдыхает
до указанного времени; сессия сброшена / аккаунт удалён или заблокирован — «отлетел», уведомление владельцам.
Нет ни одного рабочего аккаунта — номер выдаётся без проверки (и уведомление, если аккаунты вообще добавлены).

Ограничение Telegram: если владелец занятого номера скрыл «Кто может найти меня по номеру», ответ — как у свободного."""

import asyncio
import base64
import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from datetime import timedelta

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import func, select, update
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from telethon.tl.functions.contacts import ResolvePhoneRequest

from app import catalog, payouts
from app import settings_store as st
from app.config import config
from app.db import Session, utcnow
from app.models import TgChecker

log = logging.getLogger(__name__)

SERVICE_KEY = "telegram"  # Service.key сервиса Telegram в каталоге
CHECK_TIMEOUT = 15  # сек на один запрос к Telegram
LOGIN_TTL = 15 * 60  # сколько ждём код / пароль при добавлении аккаунта
class Broken(Exception):
    """Сохранённую сессию не расшифровать (сменился SESSION_SECRET) — аккаунт надо добавить заново."""


DEAD_ERRORS = (errors.AuthKeyUnregisteredError, errors.SessionRevokedError, errors.UserDeactivatedError,
               errors.UserDeactivatedBanError, errors.AuthKeyDuplicatedError, Broken)
DEVICE = {"device_model": "Shifr SMS", "system_version": "server", "app_version": "1.0", "lang_code": "ru",
          "system_lang_code": "ru"}

_clients: dict[int, TelegramClient] = {}  # TgChecker.id -> подключённый клиент


def enabled() -> bool:
    return bool(config.tg_api_id and config.tg_api_hash)


def applies(service_id: int) -> bool:
    """Номер этого сервиса проверяем перед выдачей."""
    meta = catalog.SERVICES.get(service_id)
    return bool(meta and meta.key == SERVICE_KEY and st.bool_("tg_check_enabled") and enabled())


def digits(phone: str) -> str:
    return re.sub(r"\D", "", phone or "")


# ------------------------------------------------------------------ сессии


def _fernet() -> Fernet:
    key = hashlib.sha256(b"shifr-tgcheck:" + config.session_secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def _seal(session: str) -> str:
    return _fernet().encrypt(session.encode()).decode()


def _open(sealed: str) -> str:
    return _fernet().decrypt(sealed.encode()).decode()


def _new_client(session: str = "") -> TelegramClient:
    return TelegramClient(StringSession(session), config.tg_api_id, config.tg_api_hash,
                          receive_updates=False, **DEVICE)


async def _client(row: TgChecker) -> TelegramClient:
    client = _clients.get(row.id)
    if client is None:
        try:
            session = _open(row.session)
        except InvalidToken:
            raise Broken() from None
        client = _new_client(session)
        _clients[row.id] = client
    if not client.is_connected():
        await client.connect()
    return client


async def _drop(account_id: int) -> None:
    client = _clients.pop(account_id, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass


# ------------------------------------------------------------------ проверка


def _short(row: TgChecker) -> str:
    return f"+{row.phone}" + (f" (@{row.username})" if row.username else "")


async def _available() -> list[TgChecker]:
    """Рабочие аккаунты, которые сейчас можно спросить: дольше всех не проверявший — первым."""
    now = utcnow()
    today = now.strftime("%Y-%m-%d")
    limit, pause = st.int_("tg_check_daily"), st.int_("tg_check_pause")
    async with Session() as s:
        rows = list((await s.scalars(select(TgChecker).where(TgChecker.status == "active"))).all())
    out = []
    for r in rows:
        if r.rest_until and r.rest_until > now:
            continue
        if limit > 0 and r.checks_day == today and (r.checks_today or 0) >= limit:
            continue
        if pause > 0 and r.last_check_at and (now - r.last_check_at).total_seconds() < pause:
            continue
        out.append(r)
    return sorted(out, key=lambda r: (r.last_check_at is not None, r.last_check_at or now, r.id))


async def _counted(row: TgChecker, busy: bool) -> None:
    now = utcnow()
    today = now.strftime("%Y-%m-%d")
    async with Session() as s:
        r = await s.get(TgChecker, row.id)
        if r is None:
            return
        r.checks_today = (r.checks_today or 0) + 1 if r.checks_day == today else 1
        r.checks_day = today
        r.checks_total = (r.checks_total or 0) + 1
        r.busy_total = (r.busy_total or 0) + (1 if busy else 0)
        r.last_check_at = now
        r.last_error = None
        await s.commit()


async def _rest(row: TgChecker, seconds: int, why: str) -> None:
    async with Session() as s:
        await s.execute(update(TgChecker).where(TgChecker.id == row.id).values(
            rest_until=utcnow() + timedelta(seconds=seconds), last_error=why[:255]))
        await s.commit()
    log.warning("проверка Telegram: аккаунт %s отдыхает %s с: %s", row.phone, seconds, why)


async def _dead(row: TgChecker, why: str) -> None:
    async with Session() as s:
        await s.execute(update(TgChecker).where(TgChecker.id == row.id).values(status="dead", last_error=why[:255]))
        await s.commit()
    await _drop(row.id)
    log.error("проверка Telegram: аккаунт %s отлетел: %s", row.phone, why)
    await payouts.notify(f"⚠️ Аккаунт проверки номеров Telegram {_short(row)} отлетел: {why}. "
                         "Добавьте его заново в панели: «Ещё» → «Проверка Telegram».", key=f"tgdead:{row.id}",
                         every=24 * 3600)


async def _error(row: TgChecker, why: str) -> None:
    async with Session() as s:
        await s.execute(update(TgChecker).where(TgChecker.id == row.id).values(last_error=why[:255]))
        await s.commit()


async def check(phone: str) -> str:
    """Есть ли на номере аккаунт Telegram. -> free | busy | unknown (проверить некем — выдаём без проверки)."""
    number = digits(phone)
    for row in await _available():
        try:
            client = await _client(row)
            res = await asyncio.wait_for(client(ResolvePhoneRequest(phone=number)), CHECK_TIMEOUT)
        except (errors.PhoneNotOccupiedError, errors.PhoneNumberUnoccupiedError):
            await _counted(row, busy=False)
            return "free"
        except errors.FloodWaitError as e:
            await _rest(row, int(e.seconds) + 5, f"Telegram попросил паузу {e.seconds} с")
            continue
        except DEAD_ERRORS as e:
            await _dead(row, f"сессия недействительна ({type(e).__name__})")
            continue
        except errors.PhoneNumberInvalidError:
            await _counted(row, busy=False)
            return "free"  # Telegram не считает номер номером — аккаунта на нём точно нет
        except Exception as e:  # сеть, таймаут, неожиданный ответ — спросим другой аккаунт
            log.warning("проверка Telegram: аккаунт %s: %r", row.phone, e)
            await _error(row, f"{type(e).__name__}: {e}"[:255])
            continue
        user = res.users[0] if getattr(res, "users", None) else None
        await _counted(row, busy=True)
        log.info("номер %s уже в Telegram%s", number, " (удалённый аккаунт)" if user and user.deleted else "")
        return "busy"
    async with Session() as s:
        added = await s.scalar(select(func.count()).select_from(TgChecker).where(TgChecker.status != "paused"))
    if not added:
        return "unknown"  # аккаунтов не добавляли (или все на паузе) — проверка просто не настроена
    await payouts.notify("⚠️ Проверка номеров Telegram: нет рабочих аккаунтов — номера Telegram выдаются без проверки. "
                         "Проверьте раздел панели «Ещё» → «Проверка Telegram».", key="tgcheck:none", every=3600)
    return "unknown"


async def health() -> None:
    """Раз в 10 минут: живы ли сессии рабочих аккаунтов (чтобы узнать об отлетевшем до покупки)."""
    if not enabled():
        return
    async with Session() as s:
        rows = list((await s.scalars(select(TgChecker).where(TgChecker.status == "active"))).all())
    for row in rows:
        try:
            client = await _client(row)
            if not await asyncio.wait_for(client.is_user_authorized(), CHECK_TIMEOUT):
                await _dead(row, "сессия завершена")
        except DEAD_ERRORS as e:
            await _dead(row, f"сессия недействительна ({type(e).__name__})")
        except Exception as e:
            log.info("проверка Telegram: аккаунт %s недоступен: %r", row.phone, e)


# ------------------------------------------------------------------ добавление аккаунта


@dataclass
class Login:
    phone: str
    client: TelegramClient
    code_hash: str = ""
    step: str = "code"  # code | password
    started: float = field(default_factory=time.monotonic)


_logins: dict[str, Login] = {}


class LoginError(Exception):
    pass


def login_state(token: str) -> Login | None:
    for t in [t for t, x in _logins.items() if time.monotonic() - x.started > LOGIN_TTL]:
        asyncio.create_task(_drop_login(t))
    return _logins.get(token)


async def _drop_login(token: str) -> None:
    x = _logins.pop(token, None)
    if x:
        try:
            await x.client.disconnect()
        except Exception:
            pass


async def start_login(phone: str) -> str:
    """Шаг 1: Telegram присылает код в приложение этого аккаунта. -> токен входа."""
    if not enabled():
        raise LoginError("Нет TG_API_ID и TG_API_HASH в .env на сервере.")
    number = digits(phone)
    if len(number) < 8:
        raise LoginError("Укажите номер аккаунта целиком, с кодом страны, например +79991234567.")
    client = _new_client()
    try:
        await client.connect()
        sent = await client.send_code_request("+" + number)
    except errors.PhoneNumberInvalidError:
        await client.disconnect()
        raise LoginError("Telegram не принял номер — проверьте, что он записан с кодом страны.")
    except errors.FloodWaitError as e:
        await client.disconnect()
        raise LoginError(f"Telegram просит подождать {e.seconds} с перед новой попыткой входа.")
    except Exception as e:
        await client.disconnect()
        raise LoginError(f"Не удалось запросить код: {e}")
    token = secrets.token_urlsafe(12)
    _logins[token] = Login(phone=number, client=client, code_hash=sent.phone_code_hash)
    return token


async def submit_code(token: str, code: str) -> str:
    """Шаг 2: код из Telegram. -> done | password (нужен облачный пароль)."""
    x = login_state(token)
    if not x:
        raise LoginError("Время на ввод кода вышло — начните заново.")
    try:
        await x.client.sign_in("+" + x.phone, re.sub(r"\D", "", code), phone_code_hash=x.code_hash)
    except errors.SessionPasswordNeededError:
        x.step = "password"
        return "password"
    except errors.PhoneCodeInvalidError:
        raise LoginError("Код не подошёл — проверьте и введите ещё раз.")
    except errors.PhoneCodeExpiredError:
        await _drop_login(token)
        raise LoginError("Код устарел — начните заново.")
    except Exception as e:
        raise LoginError(f"Не удалось войти: {e}")
    await _finish(token)
    return "done"


async def submit_password(token: str, password: str) -> None:
    """Шаг 3 (если есть облачный пароль)."""
    x = login_state(token)
    if not x or x.step != "password":
        raise LoginError("Время на вход вышло — начните заново.")
    try:
        await x.client.sign_in(password=password)
    except errors.PasswordHashInvalidError:
        raise LoginError("Облачный пароль неверный.")
    except Exception as e:
        raise LoginError(f"Не удалось войти: {e}")
    await _finish(token)


async def _finish(token: str) -> None:
    x = _logins.pop(token)
    me = await x.client.get_me()
    session = x.client.session.save()
    async with Session() as s:
        row = (await s.scalars(select(TgChecker).where(TgChecker.phone == x.phone))).first()
        if row is None:
            row = TgChecker(phone=x.phone)
            s.add(row)
        row.session, row.status, row.last_error, row.rest_until = _seal(session), "active", None, None
        row.tg_user_id, row.username = me.id, me.username
        row.name = " ".join(p for p in (me.first_name, me.last_name) if p)[:128] or None
        await s.commit()
        await s.refresh(row)
    await _drop(row.id)
    _clients[row.id] = x.client  # уже подключён — пользуемся этим соединением
    log.info("проверка Telegram: добавлен аккаунт %s", x.phone)


async def remove(account_id: int) -> None:
    """Удалить аккаунт: выйти из сессии сервера (в Telegram она пропадёт из «Устройств») и забыть её."""
    async with Session() as s:
        row = await s.get(TgChecker, account_id)
        if row is None:
            return
        if row.status != "dead":
            try:
                client = await _client(row)
                await asyncio.wait_for(client.log_out(), CHECK_TIMEOUT)
            except Exception as e:
                log.info("проверка Telegram: выход из %s: %r", row.phone, e)
        await s.delete(row)
        await s.commit()
    await _drop(account_id)


async def set_paused(account_id: int, paused: bool) -> None:
    async with Session() as s:
        row = await s.get(TgChecker, account_id)
        if row and row.status in ("active", "paused"):
            row.status = "paused" if paused else "active"
            await s.commit()

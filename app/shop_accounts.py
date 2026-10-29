"""Аккаунты сайта /shop: вход логином и паролем, вход и привязка через Telegram, склейка с аккаунтом бота, ключ API.

Пользователь один на сайт и бот (User). Зарегистрировался на сайте без Telegram — id из диапазона WEB_ID_BASE+
(не Telegram ID: бот ему не пишет, в рассылки не попадает). Вошёл через Telegram — это его пользователь бота (id =
Telegram ID), логин и пароль можно задать потом. Привязал Telegram к аккаунту сайта — аккаунты склеиваются (merge):
всё сайтовое переезжает к пользователю бота, балансы складываются; отвязать Telegram нельзя.

Вход через Telegram: сайт создаёт одноразовый TgAuth (живёт AUTH_TTL, только для этого браузера), ссылка
t.me/<бот>?start=web_<token> открывает бота, бот спрашивает «Войти на сайт?» с браузером и временем запроса,
и только после «✅ Да» (confirm) сайт — тот же браузер — входит. Тем же способом: привязка Telegram и сброс пароля."""

import hashlib
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app import referrals, user_refs
from app.db import Session
from app.models import (BroadcastRecipient, Order, Payment, RefCode, SupportTicket, Transaction, User, UserRefReward,
                        UtmStart)

log = logging.getLogger(__name__)

WEB_ID_BASE = 9_000_000_000_000_000  # больше любого Telegram ID (они до 2^52)
LOGIN_RE = re.compile(r"[a-z0-9][a-z0-9_.-]{2,31}")
MIN_PASSWORD = 8
AUTH_TTL = 5 * 60
AUTH_PREFIX = "web_"  # /start web_<token>


class AccountError(Exception):
    """Текст — ключ перевода (app/web/shop/i18n.py) или готовая фраза."""


def is_tg(user_id: int) -> bool:
    """Пользователь бота (есть Telegram) или только сайта."""
    return 0 < user_id < WEB_ID_BASE


def norm_login(login: str) -> str:
    return (login or "").strip().lower()


def check_login(login: str) -> str | None:
    if not LOGIN_RE.fullmatch(login):
        return "err_login_format"
    return None


def check_password(password: str) -> str | None:
    if len(password or "") < MIN_PASSWORD:
        return "err_password_short"
    if len(password) > 128:
        return "err_password_long"
    return None


async def login_taken(login: str) -> bool:
    async with Session() as s:
        return bool(await s.scalar(select(func.count()).select_from(User).where(User.login == login)))


async def register(login: str, password: str, lang: str, invite_code: str | None = None) -> User:
    login = norm_login(login)
    if err := check_login(login) or check_password(password):
        raise AccountError(err)
    for _ in range(5):
        async with Session() as s:
            if await s.scalar(select(func.count()).select_from(User).where(User.login == login)):
                raise AccountError("err_login_taken")
            last = await s.scalar(select(func.max(User.id)).where(User.id >= WEB_ID_BASE))
            user = User(id=(last or WEB_ID_BASE) + 1, first_name=login, login=login, balance=Decimal(0),
                        password_hash=referrals.hash_password(password), lang=lang, session_ver=1)
            s.add(user)
            try:
                await s.commit()
            except IntegrityError:  # одновременная регистрация: тот же id или логин — пробуем снова
                await s.rollback()
                continue
        if invite_code:
            await user_refs.bind(user.id, f"{user_refs.PREFIX}{invite_code}")
        log.info("сайт: регистрация %s (%s)", login, user.id)
        return user
    raise AccountError("err_try_again")


async def authenticate(login: str, password: str) -> User | None:
    async with Session() as s:
        user = (await s.scalars(select(User).where(User.login == norm_login(login)))).first()
    if user and user.password_hash and referrals.check_password(password, user.password_hash) and not user.is_blocked:
        return user
    return None


async def get(user_id: int) -> User | None:
    async with Session() as s:
        return await s.get(User, user_id)


async def set_credentials(user_id: int, login: str, password: str) -> None:
    """Пользователь бота задаёт логин и пароль для сайта (вошёл через Telegram)."""
    login = norm_login(login)
    if err := check_login(login) or check_password(password):
        raise AccountError(err)
    async with Session() as s:
        user = await s.get(User, user_id)
        if user.login:
            raise AccountError("err_login_set")
        if await s.scalar(select(func.count()).select_from(User).where(User.login == login)):
            raise AccountError("err_login_taken")
        user.login, user.password_hash = login, referrals.hash_password(password)
        user.session_ver = (user.session_ver or 1)
        try:
            await s.commit()
        except IntegrityError:
            raise AccountError("err_login_taken") from None


async def change_password(user_id: int, old: str | None, new: str) -> int:
    """old=None — сброс через Telegram. -> новая версия сессии (остальные входы закрываются)."""
    if err := check_password(new):
        raise AccountError(err)
    async with Session() as s:
        user = await s.get(User, user_id)
        if old is not None and not (user.password_hash and referrals.check_password(old, user.password_hash)):
            raise AccountError("err_old_password")
        user.password_hash = referrals.hash_password(new)
        user.session_ver = (user.session_ver or 1) + 1
        await s.commit()
        return user.session_ver


async def set_lang(user_id: int, lang: str) -> None:
    async with Session() as s:
        await s.execute(update(User).where(User.id == user_id).values(lang=lang))
        await s.commit()


# ---------------------------------------------------------------- ключ API


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


async def new_api_key(user_id: int) -> str:
    """Новый ключ (старый перестаёт работать). Только после первого пополнения. Ключ показывается один раз."""
    if not await user_refs.has_topped_up(user_id):
        raise AccountError("err_api_topup")
    key = secrets.token_urlsafe(30)
    async with Session() as s:
        await s.execute(update(User).where(User.id == user_id).values(api_key_hash=_key_hash(key), api_key_hint=key[-4:]))
        await s.commit()
    _key_cache.clear()
    return key


_key_cache: dict[str, tuple[float, int | None]] = {}


async def by_api_key(key: str) -> User | None:
    if not key or len(key) > 100:
        return None
    h = _key_hash(key)
    hit = _key_cache.get(h)
    if hit and time.monotonic() - hit[0] < 30:
        uid = hit[1]
        return await get(uid) if uid else None
    async with Session() as s:
        user = (await s.scalars(select(User).where(User.api_key_hash == h))).first()
    if len(_key_cache) > 5000:
        _key_cache.clear()
    _key_cache[h] = (time.monotonic(), user.id if user else None)
    return user if user and not user.is_blocked else None


# ---------------------------------------------------------------- вход через Telegram


@dataclass
class TgAuth:
    token: str
    purpose: str  # login | link | reset
    user_id: int | None  # link — аккаунт сайта; reset — чей пароль
    device: str
    created: float = field(default_factory=time.monotonic)
    status: str = "pending"  # pending | ok | denied
    tg_id: int | None = None
    error: str | None = None
    note: str | None = None  # link: подсказка ключа API, переставшего действовать при склейке
    invite: str | None = None  # код приглашения из браузера: новый для бота человек закрепится за пригласившим
    country: str | None = None  # страна запроса (CF-IPCountry от Cloudflare) — показать в боте при подтверждении


_auths: dict[str, TgAuth] = {}


def device_title(ua: str) -> str:
    """User-Agent -> «Chrome · Windows» — показать в боте, какой браузер просит вход."""
    ua = ua or ""
    browser = next((name for pat, name in ((r"Edg/", "Edge"), (r"OPR/|Opera", "Opera"), (r"YaBrowser", "Яндекс Браузер"),
                                            (r"Firefox/", "Firefox"), (r"Chrome/", "Chrome"), (r"Safari/", "Safari"))
                    if re.search(pat, ua)), "браузер")
    system = next((name for pat, name in ((r"iPhone|iPad", "iOS"), (r"Android", "Android"), (r"Windows", "Windows"),
                                           (r"Mac OS X|Macintosh", "macOS"), (r"Linux", "Linux"))
                   if re.search(pat, ua)), "")
    return f"{browser} · {system}" if system else browser


def new_auth(purpose: str, ua: str, user_id: int | None = None, invite: str | None = None,
             country: str | None = None) -> TgAuth:
    now = time.monotonic()
    for k in [k for k, a in _auths.items() if now - a.created > AUTH_TTL * 2]:
        del _auths[k]
    if len(_auths) > 20000:
        _auths.clear()
    country = (country or "").strip().upper()
    a = TgAuth(secrets.token_urlsafe(18), purpose, user_id, device_title(ua), invite=user_refs.clean(invite),
               country=country if re.fullmatch(r"[A-Z]{2}", country) and country not in ("XX", "T1") else None)
    _auths[a.token] = a
    return a


def auth(token: str | None) -> TgAuth | None:
    a = _auths.get(token or "")
    if a and time.monotonic() - a.created > AUTH_TTL and a.status == "pending":
        a.status, a.error = "denied", "expired"
    return a


async def confirm(token: str, tg_id: int, yes: bool) -> str:
    """Ответ в боте. -> текст для пользователя бота (по-русски — бот русский)."""
    a = auth(token)
    if a is None or (a.status == "denied" and a.error == "expired"):
        return "⌛ Запрос устарел. Начните заново на сайте."
    if a.status != "pending":
        return "Этот запрос уже обработан."
    if not yes:
        a.status, a.error = "denied", "denied"
        return "❌ Вход отклонён. Если это были не вы — просто ничего не делайте, доступа к аккаунту нет."
    if a.purpose == "login":
        a.status, a.tg_id = "ok", tg_id
        return "✅ Готово! Вернитесь на сайт — вход выполнен."
    if a.purpose == "reset":
        if a.user_id != tg_id:
            a.status, a.error = "denied", "reset_other"
            return "⚠️ Этот Telegram не привязан к аккаунту сайта, пароль которого сбрасывают."
        a.status, a.tg_id = "ok", tg_id
        return "✅ Подтверждено. Вернитесь на сайт и придумайте новый пароль."
    # link: склеиваем аккаунт сайта с этим Telegram
    info: dict = {}
    error = await merge(a.user_id, tg_id, info)
    if error:
        a.status, a.error = "denied", error
        return {"merge_tg_login": "⚠️ Этот Telegram уже привязан к другому аккаунту сайта. Войдите на сайт через "
                                  "Telegram — это и есть ваш аккаунт.",
                "merge_linked": "Аккаунт сайта уже привязан к Telegram."}.get(error, "⚠️ Не удалось привязать, "
                                                                                    "попробуйте ещё раз.")
    a.status, a.tg_id, a.note = "ok", tg_id, info.get("old_key")
    text = "✅ Telegram привязан: аккаунт сайта и бота теперь общий — баланс, заказы и обращения в одном месте."
    if a.note:
        text += (f"\n\n🔑 Ключ API, созданный раньше через Telegram (••••{a.note}), больше не действует — "
                 "работает ключ аккаунта сайта.")
    return text


def bot_question(a: TgAuth, login: str | None) -> str:
    """Вопрос в боте. Подтверждение отдаёт аккаунт тому браузеру, который начал запрос, — поэтому прямо говорим,
    что будет, и чей это аккаунт сайта: мошенник может начать вход у себя и прислать ссылку жертве."""
    who = f"«{login}»" if login else "на сайте"
    return {"login": "🔐 <b>Вход на сайт {site}</b>\n\nВойти в ваш аккаунт через этот Telegram? Браузер, который "
                     "запросил вход, получит доступ к вашему балансу и заказам.",
            "link": f"🔗 <b>Привязка Telegram</b>\n\nПривязать этот Telegram к аккаунту сайта {who}?\n\n"
                    f"Баланс, заказы и обращения объединятся с ботом, и у того, кто знает логин и пароль {who}, "
                    "будет доступ к ним. Отвязать Telegram потом нельзя.",
            "reset": f"🔑 <b>Сброс пароля</b>\n\nСбросить пароль от аккаунта сайта {who}? Новый пароль задаст тот, "
                     "кто сейчас на странице сброса."}[a.purpose]


# ---------------------------------------------------------------- склейка аккаунтов


async def merge(site_id: int, tg_id: int, info: dict | None = None) -> str | None:
    """Аккаунт сайта (без Telegram) -> к пользователю бота tg_id: всё его переезжает, балансы складываются.
    Ключ API — один на пользователя: если он был у обоих, остаётся ключ сайта (info["old_key"] — подсказка
    переставшего действовать). Заблокирован один из двух — заблокирован и общий. Приглашения: коды ссылок обоих
    продолжают работать; пригласившие обоих аккаунтов (каналы и пользователи) — пригласившие общего, даже если
    аккаунт Telegram давний (это один и тот же человек); разные — делят долю пополам (referrals.attributions).
    -> код ошибки или None."""
    info = {} if info is None else info
    if is_tg(site_id) or not is_tg(tg_id):
        return "merge_linked"
    async with Session() as s:
        # строки блокируются до конца склейки: зачисление на аккаунт сайта в этот момент подождёт
        src = await s.get(User, site_id, with_for_update=True)
        dst = await s.get(User, tg_id, with_for_update=True)
        if src is None or dst is None:
            return "merge_missing"
        if dst.login:
            return "merge_tg_login"
        for model, col in ((Transaction, Transaction.user_id), (Order, Order.user_id), (Payment, Payment.user_id),
                           (SupportTicket, SupportTicket.user_id), (UtmStart, UtmStart.user_id),
                           (BroadcastRecipient, BroadcastRecipient.user_id)):
            await s.execute(update(model).where(col == site_id).values({col.key: tg_id}))
        # кто привёл обоих — до изменений ниже (они переписывают ссылки на аккаунт сайта)
        attrs = [a for a in referrals.attributions(dst) + referrals.attributions(src)
                 if a not in (("user", site_id), ("user", tg_id))]
        attrs = list(dict.fromkeys(attrs))[:2]  # у каждого аккаунта — не больше одного
        # начисления за приглашённых: аккаунт сайта и Telegram пригласили одного и того же (склеенного) человека —
        # по одному заказу две записи; сумма уходит в запись Telegram
        both = (await s.scalars(select(UserRefReward).where(UserRefReward.inviter_id == site_id))).all()
        for r in both:
            twin = await s.get(UserRefReward, (r.order_id, tg_id))
            if twin:
                twin.amount += r.amount
                await s.delete(r)
        await s.flush()
        await s.execute(update(UserRefReward).where(UserRefReward.inviter_id == site_id).values(inviter_id=tg_id))
        await s.execute(update(UserRefReward).where(UserRefReward.invitee_id == site_id).values(invitee_id=tg_id))
        await s.execute(update(User).where(User.inviter_id == site_id).values(inviter_id=tg_id))
        await s.execute(update(User).where(User.inviter2_id == site_id).values(inviter2_id=tg_id))
        await s.execute(update(User).where(User.inviter2_id == User.inviter_id).values(inviter2_id=None)
                        .execution_options(synchronize_session=False))
        await s.execute(update(RefCode).where(RefCode.user_id == site_id).values(user_id=tg_id))
        login, pw, lang, key, hint = src.login, src.password_hash, src.lang, src.api_key_hash, src.api_key_hint
        src.login = src.api_key_hash = None
        await s.flush()  # уникальные логин и ключ освобождены — теперь их можно отдать пользователю бота
        await s.execute(update(User).where(User.id == tg_id).values(balance=User.balance + src.balance))
        dst.login, dst.password_hash = login, pw
        dst.lang = dst.lang or lang
        if key:
            if dst.api_key_hash:
                info["old_key"] = dst.api_key_hint or "?"
            dst.api_key_hash, dst.api_key_hint = key, hint
        dst.is_blocked = bool(dst.is_blocked or src.is_blocked)
        dst.ref_code = dst.ref_code or src.ref_code  # показывается один; коды обоих работают (RefCode)
        channels = [v for kind, v in attrs if kind == "channel"] + [None, None]
        people = [v for kind, v in attrs if kind == "user"] + [None, None]
        dst.referral_id, dst.referral2_id = channels[:2]
        dst.inviter_id, dst.inviter2_id = people[:2]
        dst.session_ver = max(dst.session_ver or 1, src.session_ver or 1)
        await s.delete(src)
        await s.commit()
    _key_cache.clear()
    log.info("сайт: аккаунт %s склеен с Telegram %s", site_id, tg_id)
    return None

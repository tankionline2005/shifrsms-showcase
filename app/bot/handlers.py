import logging
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from html import escape
from urllib.parse import quote

from aiogram import F, Router
from aiogram.enums import ButtonStyle, ChatType
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton, InputMediaPhoto, LabeledPrice, Message,
                           PreCheckoutQuery)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, or_, select, update

from app import catalog, menu_images, orders, payments, referrals, shop_accounts, stars, user_refs, utm, wallet
from app import settings_store as st
from app.bot.ui import (
    BTN_BALANCE, BTN_BUY, BTN_HELP, BTN_INVITE, BTN_ORDERS, BTN_PROFILE, BTN_WEB, LEGACY_BUTTONS, MAIN_KB, MENU_NEWS,
    MENU_VERSION, PAGE, PARTNER_BUTTON,
    SERVICES_PAGE, STATUS_TITLES, SUPPORT,
    AutoBuyCb, BuyCb, CountriesCb, CountryCb, OrderCb, OrdersCb, PayCheckCb, ServiceCb, ServicesCb, SortCb, StarsCb,
    TopupCb,
    instruction_url, order_kb, order_text, pager, phone_fmt, qty, rub, service_note,
)
from app.config import config
from app.db import Session, utcnow
from app.models import Order, User

log = logging.getLogger(__name__)
router = Router()
# Всё ниже — только для личных чатов: в группах (обсуждения каналов, app/bot/channels.py) бот
# не должен отвечать людям и заводить их в пользователи
router.message.filter(F.chat.type == ChatType.PRIVATE)


class Flow(StatesGroup):
    countries = State()  # карточка сервиса или список стран — текст ищет страну


async def is_blocked(user_id: int) -> bool:
    async with Session() as s:
        u = await s.get(User, user_id)
        return bool(u and u.is_blocked)


_created: dict[int, int | None] = {}  # пользователь -> обновление, при котором его создали


@router.message.outer_middleware()
@router.callback_query.outer_middleware()
async def ensure_user(handler, event, data):
    user = event.from_user
    if isinstance(event, Message) and event.chat.type != ChatType.PRIVATE:
        user = None  # сообщение из группы — его отсеет фильтр роутера
    if user:
        _, created = await wallet.get_or_create_user(user.id, user.username, user.first_name)
        # ensure_user стоит и у роутера поддержки (support.private), и здесь: одно сообщение проходит его дважды,
        # и второй раз пользователь уже есть в базе. «Новый» — если создан при обработке ЭТОГО же обновления
        update_id = getattr(data.get("event_update"), "update_id", None)
        if created:
            _created[user.id] = update_id
            if len(_created) > 10000:
                _created.clear()
        data["user_created"] = created or (update_id is not None and _created.get(user.id) == update_id)
        if await is_blocked(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ ограничен.", show_alert=True)
            return None
        if (isinstance(event, Message) and not data["user_created"] and not (event.text or "").startswith("/start")
                and await _menu_outdated(user.id)):
            await event.answer(MENU_NEWS, reply_markup=MAIN_KB)
    return await handler(event, data)


async def _menu_outdated(user_id: int) -> bool:
    """Нижнее меню у пользователя старое? Отмечаем сразу — присылаем новое ровно один раз."""
    async with Session() as s:
        res = await s.execute(update(User).where(User.id == user_id, or_(User.menu_ver.is_(None),
                                                                           User.menu_ver < MENU_VERSION))
                              .values(menu_ver=MENU_VERSION))
        await s.commit()
    return res.rowcount == 1


async def edit_or_send(target: Message | CallbackQuery, text: str, markup=None, image: str | None = None) -> None:
    """Нажатие кнопки — меняем это же сообщение (у сообщения с картинкой — подпись к ней, а если раздел
    с другой картинкой — и саму картинку);
    новое сообщение — с картинкой-оформлением пункта меню image, если она загружена."""
    if isinstance(target, CallbackQuery):
        msg = target.message
        wants_image = bool(image and menu_images.get(image))
        if bool(msg.photo) != wants_image:
            # картинка есть только на экране раздела (список сервисов): выбрали сервис — экран
            # без картинки; вернулись к списку — снова с картинкой. Подпись в текст не превратить,
            # поэтому старое сообщение убираем и присылаем новое
            try:
                await msg.delete()
            except TelegramBadRequest:
                pass
            await send_menu(msg, image if wants_image else None, text, markup)
            try:
                await target.answer()
            except TelegramBadRequest:
                pass
            return
        if wants_image and msg.photo[-1].file_unique_id != menu_images.unique_id(image) \
                and await swap_photo(msg, image, text, markup):
            try:  # другой раздел (профиль -> баланс) — другая картинка, а не только подпись
                await target.answer()
            except TelegramBadRequest:
                pass
            return
        try:
            if msg.photo:
                await msg.edit_caption(caption=text, reply_markup=markup)
            else:
                await msg.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" not in str(e):  # подпись длиннее 1024 и т.п. — новым сообщением
                await msg.answer(text, reply_markup=markup)
        try:
            await target.answer()
        except TelegramBadRequest:
            pass  # на нажатие уже ответили раньше (например, «Получаем номер…»)
    else:
        await send_menu(target, image, text, markup)


async def swap_photo(msg: Message, image: str, text: str, markup) -> bool:
    """Сменить картинку сообщения на картинку раздела image (с подписью и кнопками). False — не вышло."""
    img = menu_images.get(image)
    if not img or len(text) > 1024:
        return False
    for upload in (False, True):
        photo = img.file_id if img.file_id and not upload else BufferedInputFile(img.photo, "menu")
        try:
            sent = await msg.edit_media(InputMediaPhoto(media=photo, caption=text), reply_markup=markup)
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return True
            if upload or not img.file_id:
                return False
            continue  # сохранённая картинка устарела — загрузим файл заново
        if isinstance(sent, Message) and sent.photo:
            await menu_images.remember_file_id(image, sent.photo[-1].file_id, sent.photo[-1].file_unique_id)
        return True
    return False


async def send_menu(message: Message, image: str | None, text: str, markup=None) -> None:
    """Раздел меню: картинка-оформление с текстом подписью; без картинки — просто текст."""
    img = menu_images.get(image) if image else None
    if img and len(text) <= 1024:
        for upload in (False, True):
            photo = img.file_id if img.file_id and not upload else BufferedInputFile(img.photo, "menu")
            try:
                sent = await message.answer_photo(photo, caption=text, reply_markup=markup)
            except TelegramBadRequest:
                if upload or not img.file_id:
                    break  # картинку Telegram не принял — покажем текстом
                continue  # сохранённая картинка устарела — загрузим файл заново
            if sent.photo:
                await menu_images.remember_file_id(image, sent.photo[-1].file_id, sent.photo[-1].file_unique_id)
            return
    await message.answer(text, reply_markup=markup)


# ------------------------------------------------------------------ старт и меню


def docs_kb():
    """Кнопки-ссылки на политику конфиденциальности и пользовательское соглашение (/start и «Информация»)."""
    b = InlineKeyboardBuilder()
    for key, title in (("privacy_url", "🔒 Политика конфиденциальности"),
                       ("terms_url", "📄 Пользовательское соглашение")):
        url = st.raw(key).strip()
        if url.startswith(("https://", "http://")):
            b.button(text=title, url=url)
    b.adjust(1)
    return b.as_markup() if b.buttons else None


MSK_DELTA = timedelta(hours=3)


class WebAuthCb(CallbackData, prefix="wa"):
    yes: bool
    token: str


async def web_auth_prompt(message: Message, token: str) -> None:
    """/start web_<token> — сайт просит вход (привязку, сброс пароля) через этот Telegram: спросить подтверждение."""
    a = shop_accounts.auth(token)
    if a is None or a.status != "pending":
        await message.answer("⌛ Ссылка устарела. Начните заново на сайте.", reply_markup=MAIN_KB)
        return
    yes = {"login": "✅ Войти", "link": "✅ Привязать", "reset": "✅ Сбросить пароль"}[a.purpose]
    b = InlineKeyboardBuilder()
    b.button(text=yes, callback_data=WebAuthCb(yes=True, token=token))
    b.button(text="❌ Это не я", callback_data=WebAuthCb(yes=False, token=token))
    b.adjust(2)
    site = config.public_url.split("://", 1)[-1]
    when = (utcnow() + MSK_DELTA).strftime("%H:%M")
    login = None
    if a.user_id:  # привязка и сброс — чей аккаунт сайта
        async with Session() as s:
            target = await s.get(User, a.user_id)
        login = target.login if target else None
    where = ""
    if a.country:
        country = next((c.name for c in catalog.COUNTRIES.values() if c.key == a.country), a.country)
        where = f" · 🌍 {escape(country)}"
    # логин сайта — только латиница, цифры, «._-» (shop_accounts.LOGIN_RE): экранировать нечего
    await message.answer(shop_accounts.bot_question(a, login).format(site=site)
                         + f"\n\n🖥 {escape(a.device)}{where} · запрос в {when} МСК\n\n"
                           "⚠️ Нажимайте «✅», только если вы сами только что нажали кнопку на сайте. Если ссылку вам "
                           "прислали или просят что-то «подтвердить» — это мошенники: нажмите «Это не я».",
                         reply_markup=b.as_markup())


@router.callback_query(WebAuthCb.filter())
async def web_auth_answer(cb: CallbackQuery, callback_data: WebAuthCb):
    from app.bot import support  # support импортирует этот модуль
    a = shop_accounts.auth(callback_data.token)
    tickets = None  # привязка: открытые обращения обоих аккаунтов до склейки
    if a and a.purpose == "link" and a.status == "pending" and callback_data.yes and a.user_id:
        tickets = (await support.open_ticket(a.user_id), await support.open_ticket(cb.from_user.id))
    text = await shop_accounts.confirm(callback_data.token, cb.from_user.id, callback_data.yes)
    await cb.answer()
    try:
        await cb.message.edit_text(text)
    except TelegramBadRequest:
        await cb.message.answer(text)
    if tickets and all(tickets) and a.status == "ok":
        try:
            await support.join_merged(cb.bot, keep=tickets[1], old=tickets[0])
        except Exception:
            log.exception("поддержка: объединение обращений %s и %s", tickets[0].id, tickets[1].id)


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, command: CommandObject, user_created: bool = False):
    await state.clear()
    if (command.args or "").startswith(shop_accounts.AUTH_PREFIX):
        token = command.args[len(shop_accounts.AUTH_PREFIX):]
        if user_created:
            # вход с сайта нового для бота человека: приглашение, запомненное браузером, засчитывается здесь
            a = shop_accounts.auth(token)
            if a and a.invite:
                await user_refs.bind(message.from_user.id, f"{user_refs.PREFIX}{a.invite}")
            await message.answer("👋 Добро пожаловать в <b>Shifr SMS</b>!", reply_markup=MAIN_KB)
            await _menu_outdated(message.from_user.id)
        await web_auth_prompt(message, token)
        return
    if user_created:  # впервые открыл бота — по кнопке под постом канала реферала или по приглашению друга
        if not await referrals.bind(message.from_user.id, command.args):
            await user_refs.bind(message.from_user.id, command.args)
    if label := utm.parse(command.args):  # ссылка с UTM-меткой: ?start=utm_<метка>
        await utm.record(message.from_user.id, label, new=user_created)
    balance = await wallet.get_balance(message.from_user.id)
    # У сообщения может быть либо нижнее меню, либо кнопки под ним — поэтому меню
    # приходит с приветствием, а ссылки на документы — под основным текстом.
    await message.answer("👋 Добро пожаловать в <b>Shifr SMS</b>!", reply_markup=MAIN_KB)
    await _menu_outdated(message.from_user.id)  # новое меню только что пришло с приветствием
    await message.answer(
        "Регистрируйтесь в популярных сервисах, получая SMS на временный номер.\n\n"
        "1. Выберите сервис и запустите SMS-активацию.\n"
        "2. Введите временный номер в нужном сервисе.\n"
        "3. Код из SMS придёт сюда.\n\n"
        f"Ваш баланс: <b>{rub(balance)}</b>\n\n"
        "❗️Если SMS не придёт, деньги автоматически вернутся на ваш баланс.\n"
        "Мы не храним данные аккаунтов, которые вы регистрируете.",
        reply_markup=docs_kb(),
    )


def tg_url(key: str) -> str | None:
    """Контакт из настроек (@username, t.me/… или ссылка) -> ссылка для кнопки."""
    contact = st.raw(key).strip()
    if contact.startswith(("https://", "http://")):
        return contact
    name = contact.removeprefix("t.me/").lstrip("@").strip("/")
    return f"https://t.me/{name}" if name else None


@router.message(F.text == BTN_HELP)
@router.message(Command("help"))
async def help_(message: Message, state: FSMContext):
    await state.clear()
    b = InlineKeyboardBuilder()
    if st.raw("support_chat").strip():
        b.button(text="💬 Поддержка", callback_data=SUPPORT)
    if url := tg_url("channel_url"):
        b.button(text="📢 Наш канал", url=url)
    docs = docs_kb()
    for row in (docs.inline_keyboard if docs else []):
        b.row(*row)
    b.adjust(1)
    await send_menu(message, "info",
                    "<b>Как это работает</b>\n\n"
                    "• Одна активация — один код подтверждения. На активацию даётся ~20 минут.\n"
                    "• Оплата списывается при запуске активации. Если SMS не пришла — полный возврат на баланс.\n"
                    "• Отменить активацию можно, пока не пришла SMS (иногда — через пару минут после запуска).\n"
                    "• Для поиска сервиса просто напишите его название в чат.",
                    b.as_markup() if b.buttons else None)  # нижнее меню уже закреплено и остаётся на месте


def partner_url() -> str:
    return f"{config.public_url}/partnership"


@router.message(F.text == BTN_WEB)
async def web_version(message: Message, state: FSMContext):
    await state.clear()
    b = InlineKeyboardBuilder()
    b.button(text="🌐 Открыть сайт", url=f"{config.public_url}/shop")
    await send_menu(message, "web",
                    "🌐 <b>Веб-версия</b>\n\n"
                    "Всё то же самое — в браузере. Войти можно двумя способами:\n\n"
                    "• <b>Через Telegram</b> — баланс и заказы будут общими с ботом.\n"
                    "• <b>Анонимно</b> — просто придумайте логин и пароль.", b.as_markup())


# ------------------------------------------------------------------ профиль: баланс, заказы, приглашения


PROFILE = "profile"
INVITE = "invite"


def back_to_profile(b: InlineKeyboardBuilder) -> None:
    b.row(InlineKeyboardButton(text="⬅️ Профиль", callback_data=PROFILE))


@router.message(F.text == BTN_PROFILE)
async def profile(message: Message, state: FSMContext):
    await state.clear()
    await show_profile(message)


@router.callback_query(F.data == PROFILE)
async def profile_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_profile(cb)


async def show_profile(target) -> None:
    b = InlineKeyboardBuilder()
    b.button(text=BTN_BALANCE, callback_data=BALANCE)
    b.button(text=BTN_ORDERS, callback_data=OrdersCb(page=0))
    b.button(text=BTN_INVITE, callback_data=INVITE)
    b.adjust(1)
    await edit_or_send(target, f"👤 <b>Профиль</b>\n\n"
                               f"Ваш баланс: <b>{rub(await wallet.get_balance(target.from_user.id))}</b>",
                       b.as_markup(), image="profile")


# ------------------------------------------------------------------ пригласить друга (app/user_refs.py)


@router.message(F.text == BTN_INVITE)  # кнопка прежнего нижнего меню
async def invite(message: Message, state: FSMContext):
    await state.clear()
    await show_invite(message)


@router.callback_query(F.data == INVITE)
async def invite_cb(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_invite(cb)


async def show_invite(target) -> None:
    uid = target.from_user.id
    b = InlineKeyboardBuilder()
    percent = f"{st.dec('user_ref_revenue_percent'):g}"
    if not user_refs.enabled():
        text = "👥 Приглашения друзей сейчас недоступны."
    elif not (code := await user_refs.code_of(uid)):
        text = ("👥 <b>Приглашайте друзей</b>\n\n"
                f"С каждой успешной активации друга вы получаете {percent}% её стоимости на баланс — навсегда.\n\n"
                "Чтобы получить реферальную ссылку, пополните баланс на любую сумму.")
        b.button(text="💰 Пополнить баланс", callback_data=BALANCE)
    else:
        link = user_refs.link(code)
        s = await user_refs.stats(uid)
        text = ("👥 <b>Приглашайте друзей</b>\n\n"
                f"С каждой успешной активации друга вы получаете {percent}% её стоимости на баланс — навсегда. "
                "Друг должен впервые перейти по вашей ссылке — и зарегистрироваться на сайте или открыть бота.\n\n"
                f"Ваша ссылка:\n<code>{link}</code>\n\n"
                f"Приглашено: <b>{s['invited']}</b> · из них покупали: <b>{s['buyers']}</b>\n"
                f"Заработано: <b>{rub(s['earned'])}</b> · за 30 дней: <b>{rub(s['month'])}</b>")
        share = "SMS-активации: номер для регистрации в Telegram, WhatsApp и других сервисах"
        b.button(text="📤 Поделиться ссылкой",
                 url=f"https://t.me/share/url?url={quote(link, safe='')}&text={quote(share, safe='')}")
    b.button(text=PARTNER_BUTTON, url=partner_url())
    b.adjust(1)
    back_to_profile(b)
    await edit_or_send(target, text, b.as_markup(), image="invite")


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


# ------------------------------------------------------------------ выбор сервиса


def service_style(svc) -> str | None:
    """«Выделить синим» в панели — синяя кнопка сервиса (стиль кнопок Telegram)."""
    return ButtonStyle.PRIMARY if getattr(svc, "highlight", False) else None


def services_kb(items, mode: str, page: int, show_all_button: bool) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    page = min(page, max(0, (len(items) - 1) // SERVICES_PAGE))  # список мог сократиться
    for svc in items[page * SERVICES_PAGE:(page + 1) * SERVICES_PAGE]:
        b.button(text=svc.name, callback_data=ServiceCb(id=svc.id), style=service_style(svc))
    b.adjust(2)
    pager(b, page, len(items), lambda p: ServicesCb(mode=mode, page=p), SERVICES_PAGE)
    if show_all_button:
        b.row(InlineKeyboardButton(text="📚 Все сервисы",
                                   callback_data=ServicesCb(mode="all").pack()))
    if mode == "all":
        b.row(InlineKeyboardButton(text="⭐ К популярным",
                                   callback_data=ServicesCb(mode="popular").pack()))
    return b


@router.message(F.text.in_(LEGACY_BUTTONS))
async def legacy_button(message: Message, state: FSMContext):
    """Старая кнопка меню: обновляем клавиатуру и выполняем действие новой кнопки."""
    await message.answer("🔄 Меню обновлено", reply_markup=MAIN_KB)
    action = {BTN_BUY: buy_menu, BTN_ORDERS: my_orders, BTN_HELP: help_}[LEGACY_BUTTONS[message.text]]
    await action(message, state)


@router.message(F.text == BTN_BUY)
async def buy_menu(message: Message, state: FSMContext):
    await state.clear()
    await show_services(message, "popular", 0)


@router.callback_query(ServicesCb.filter())
async def services_page(cb: CallbackQuery, callback_data: ServicesCb, state: FSMContext):
    await state.clear()
    await show_services(cb, callback_data.mode, callback_data.page)


# Откуда пользователь пришёл к списку стран — чтобы «К сервисам» вернула туда же.
# user_id -> ("popular"|"all", page) или ("search", запрос). В памяти: после перезапуска — популярные.
_services_origin: dict[int, tuple[str, int | str]] = {}
BACK_TO_SERVICES = "svc_back"


async def show_services(target, mode: str, page: int):
    _services_origin[target.from_user.id] = (mode, page)
    items = catalog.popular_services() if mode == "popular" else catalog.other_services()
    if not items:
        await edit_or_send(target, "Каталог загружается, попробуйте через минуту.")
        return
    title = "Популярные сервисы" if mode == "popular" else f"Все сервисы ({len(items)})"
    kb = services_kb(items, mode, page, show_all_button=mode == "popular")
    await edit_or_send(target, f"<b>{title}</b>\n\nВыберите сервис или напишите его название "
                               "в чат для поиска 🔍", kb.as_markup(), image="buy")


# ------------------------------------------------------------------ выбор страны


# Страны идут по популярности (catalog.countries_for) — других сортировок нет, кнопок выбора тоже


def country_page(service_id: int, country_id: int) -> int:
    """Страница полного списка стран, на которой находится страна — чтобы вернуть туда же."""
    for i, (country, _q) in enumerate(country_rows(service_id)):
        if country.id == country_id:
            return i // PAGE
    return 0


def country_rows(service_id: int, query: str | None = None):
    rows = catalog.countries_for(service_id)  # по популярности
    if query:
        q = query.lower().strip()
        rows = [r for r in rows if q in r[0].name.lower() or q in r[0].key.lower()]
    return rows


async def show_countries(target, service_id: int, page: int, query: str | None = None,
                         notice: str | None = None):
    svc = catalog.SERVICES.get(service_id)
    rows = country_rows(service_id, query)
    page = min(page, max(0, (len(rows) - 1) // PAGE))  # список мог сократиться
    b = InlineKeyboardBuilder()
    for country, q in rows[page * PAGE:(page + 1) * PAGE]:
        b.button(text=f"{country.title} — от {rub(q.price)} ({qty(q.count)})",
                 callback_data=CountryCb(s=service_id, c=country.id))
    b.adjust(1)
    pager(b, page, len(rows), lambda p: CountriesCb(s=service_id, page=p))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=ServiceCb(id=service_id).pack()))
    if not rows:
        text = (f"По запросу «{escape(query)}» стран не найдено." if query
                else "Для этого сервиса сейчас нет доступных активаций.")
    else:
        text = (f"<b>{escape(svc.name if svc else '')}</b> — Выберите страну\n\n"
                "Можно написать название страны для поиска 🔍\n\n"
                "Сортировка по популярности:")
    if notice:
        text = f"{notice}\n\n{text}"
    await edit_or_send(target, text, b.as_markup())


@router.callback_query(SortCb.filter())
async def sort_legacy(cb: CallbackQuery, callback_data: SortCb, state: FSMContext):
    """Кнопки «Дороже / Дешевле» из старых сообщений — просто список стран."""
    await state.set_state(Flow.countries)
    await state.update_data(service_id=callback_data.s)
    await show_countries(cb, callback_data.s, 0)


@router.callback_query(CountriesCb.filter())
async def countries_list(cb: CallbackQuery, callback_data: CountriesCb, state: FSMContext):
    await state.set_state(Flow.countries)
    await state.update_data(service_id=callback_data.s)
    await show_countries(cb, callback_data.s, callback_data.page)


@router.callback_query(ServiceCb.filter())
async def service_chosen(cb: CallbackQuery, callback_data: ServiceCb, state: FSMContext):
    await show_auto(cb, state, callback_data.id)


async def show_auto(target, state: FSMContext, sid: int, notice: str | None = None) -> None:
    """Карточка сервиса в режиме «Авто»: человек выбирает бюджет, страну — бот (catalog.auto_choices)."""
    await state.set_state(Flow.countries)  # название страны в чат — сразу к ней
    await state.update_data(service_id=sid)
    svc = catalog.SERVICES.get(sid)
    levels = catalog.auto_choices(sid) if svc else []
    b = InlineKeyboardBuilder()
    for lv in levels:
        b.button(text=rub(lv.price), callback_data=AutoBuyCb(s=sid, price=int(lv.price)))
    guide = instruction_url(svc.instruction) if svc else None
    if levels:
        b.button(text="🌍 Выбрать страну", callback_data=CountriesCb(s=sid))
        if guide:  # «Инструкция» — в одном ряду с «Выбрать страну»
            b.button(text="📖 Инструкция", url=guide)
    b.button(text="⬅️ Назад", callback_data=BACK_TO_SERVICES)
    b.adjust(*[2] * (len(levels) // 2), *[1] * (len(levels) % 2), *([2 if guide else 1] if levels else []), 1)
    if not levels:
        text = "Для этого сервиса сейчас нет доступных активаций."
    else:
        balance = await wallet.get_balance(target.from_user.id)
        text = (f"<b>{escape(svc.name)}</b> · 🤖 Авто\n\n"
                "Выберите бюджет — бот сам подберёт самую надёжную активацию в этой ценовой категории "
                "среди всех стран.\n\n"
                "<b>Чем больше бюджет, тем выше шанс, что SMS придёт.</b>\n\n"
                + "\n".join(service_note(svc.note)) + ("\n" if svc.note else "")
                + f"Ваш баланс: {rub(balance)}\n\n"
                "Активация действует около 20 минут. Если SMS не придёт — деньги вернутся на баланс.")
    if notice:
        text = f"{notice}\n\n{text}"
    await edit_or_send(target, text, b.as_markup())


async def confirm_view(user_id: int, sid: int, cid: int, notice: str | None = None):
    """Экран активации: кнопки уровней цены -> (text, markup) или None, если номеров нет."""
    q = catalog.quote(sid, cid)
    svc, country = catalog.SERVICES.get(sid), catalog.COUNTRIES.get(cid)
    if not q or not svc or not country:
        return None
    balance = await wallet.get_balance(user_id)
    b = InlineKeyboardBuilder()
    for lv in q.levels:
        b.button(text=f"{rub(lv.price)} · {qty(lv.count)}",
                 callback_data=BuyCb(s=sid, c=cid, price=int(lv.price)))
    b.button(text="⬅️ Другая страна",
             callback_data=CountriesCb(s=sid, page=country_page(sid, cid)))
    b.adjust(*[2] * (len(q.levels) // 2), *[1] * (len(q.levels) % 2), 1)
    lines = [f"<b>{escape(svc.name)}</b> · {country.title}", "",
             "Выберите цену. Как правило, чем дороже активация, тем выше шанс, что SMS придёт с первого раза. "
             "Дешёвые гео работают хуже.", "",
             *service_note(svc.note),
             f"Ваш баланс: {rub(balance)}.", "",
             "Активация действует около 20 минут. Если SMS не придёт — деньги вернутся на баланс."]
    text = (f"{notice}\n\n" if notice else "") + "\n".join(lines)
    return text, b.as_markup()


async def show_confirm(cb: CallbackQuery, state: FSMContext, sid: int, cid: int, page: int,
                       notice: str) -> None:
    """Экран активации с пояснением, а если номеров больше нет — список стран."""
    view = await confirm_view(cb.from_user.id, sid, cid, notice)
    if view:
        await edit_or_send(cb, *view)
        return
    await state.set_state(Flow.countries)
    await state.update_data(service_id=sid)
    await show_countries(cb, sid, page, notice=f"{notice}\nВыберите другую страну:")


@router.callback_query(CountryCb.filter())
async def country_chosen(cb: CallbackQuery, callback_data: CountryCb):
    await catalog.refresh_pair(callback_data.s, callback_data.c)
    view = await confirm_view(cb.from_user.id, callback_data.s, callback_data.c)
    if not view:
        await cb.answer("Для этой страны сейчас нет доступных активаций.", show_alert=True)
        return
    await edit_or_send(cb, *view)


async def start_activation(cb: CallbackQuery, state: FSMContext, purchase, fail, back) -> None:
    """purchase — корутина orders.buy; fail(notice) — вернуть к выбору цены, если не вышло;
    back — кнопка «назад» на экране ошибки."""
    await state.clear()
    await cb.answer("Запускаем активацию…")
    uid = cb.from_user.id
    try:
        order = await purchase
    except orders.PurchaseInProgress:
        return  # первое нажатие уже обрабатывается и само обновит сообщение
    except orders.PriceChanged:
        notice = "ℹ️ Цены обновились:"
    except orders.InsufficientFunds as e:
        need = (e.price - await wallet.get_balance(uid)).to_integral_value(rounding=ROUND_CEILING)
        notice = f"⚠️ Для активации за {rub(e.price)} не хватает {rub(max(need, 1))}."
    except orders.ProviderUnavailable as e:
        notice = f"⚠️ {escape(str(e))}\nПопробуйте ещё раз через минуту или выберите другую цену."
    except orders.NoNumbersAvailable as e:
        notice = f"⚠️ {escape(str(e))}\nВыберите другую цену:"
    except orders.OrderError as e:
        b = InlineKeyboardBuilder()
        b.button(text="📋 Мои заказы", callback_data=OrdersCb())
        b.row(back)
        b.adjust(1)
        await edit_or_send(cb, f"⚠️ {escape(str(e))}", b.as_markup())
        return
    else:
        # активация уже куплена — карточку человек должен увидеть, даже если старое сообщение
        # нельзя изменить (Telegram не даёт править сообщения старше 48 часов и т.п.)
        card, screen = None, cb.message
        if screen.photo:  # экран с картинкой меню: карточку номера — отдельным сообщением,
            try:          # а сам экран убираем, чтобы не оставались устаревшие кнопки
                await screen.delete()
            except TelegramBadRequest:
                pass
            screen = None
        if screen:
            try:
                card = await screen.edit_text(order_text(order), reply_markup=order_kb(order))
            except TelegramBadRequest:
                pass
        if not isinstance(card, Message):
            card = await cb.message.answer(order_text(order), reply_markup=order_kb(order))
        await orders.set_message(order.id, card.chat.id, card.message_id)
        return
    await fail(notice)


@router.callback_query(BuyCb.filter())
async def buy(cb: CallbackQuery, callback_data: BuyCb, state: FSMContext):
    uid, sid, cid = cb.from_user.id, callback_data.s, callback_data.c
    page = country_page(sid, cid)  # до покупки: после отказа страна может исчезнуть

    async def fail(notice: str) -> None:
        await show_confirm(cb, state, sid, cid, page, notice)

    await start_activation(cb, state, orders.buy(uid, sid, cid, Decimal(callback_data.price)), fail,
                           InlineKeyboardButton(text="⬅️ К списку стран",
                                                callback_data=CountriesCb(s=sid, page=page).pack()))


@router.callback_query(AutoBuyCb.filter())
async def auto_buy(cb: CallbackQuery, callback_data: AutoBuyCb, state: FSMContext):
    sid = callback_data.s

    async def fail(notice: str) -> None:
        await show_auto(cb, state, sid, notice)

    await start_activation(cb, state, orders.buy(cb.from_user.id, sid, None, Decimal(callback_data.price)),
                           fail, InlineKeyboardButton(text="⬅️ Назад", callback_data=ServiceCb(id=sid).pack()))


# ------------------------------------------------------------------ заказы


@router.callback_query(OrderCb.filter())
async def order_action(cb: CallbackQuery, callback_data: OrderCb):
    oid, uid = callback_data.id, cb.from_user.id
    action = callback_data.action
    note, new = None, None
    owned = await orders.load(oid)
    if not owned or owned.user_id != uid:
        await cb.answer("Активация не найдена.", show_alert=True)
        return
    if action == "cancel":
        note = await orders.cancel(oid, uid)
    elif action == "replace":
        note, new = await orders.replace(oid, uid)
    elif action == "finish":
        note = await orders.finish(oid, uid)
    elif action == "another":
        note = await orders.request_another(oid, uid)
    elif action == "refresh":
        try:
            await orders.refresh(oid)
        except Exception:
            pass
    await show_card(cb, oid, action, note, new)


async def show_card(cb: CallbackQuery, oid: int, action: str, note: str | None, new=None) -> None:
    """Карточка активации после действия; после замены — уже с новым номером."""
    uid = cb.from_user.id
    order = new or await orders.load(oid)
    if not order or order.user_id != uid:
        await cb.answer("Активация не найдена.", show_alert=True)
        return
    try:
        if action == "view":
            msg = await cb.message.answer(order_text(order), reply_markup=order_kb(order))
            await orders.set_message(order.id, msg.chat.id, msg.message_id)
        else:
            await cb.message.edit_text(order_text(order), reply_markup=order_kb(order))
            await orders.set_message(order.id, cb.message.chat.id, cb.message.message_id)
    except TelegramBadRequest:
        pass
    try:
        await cb.answer(note or "", show_alert=bool(note and action in ("cancel", "replace", "another")))
    except TelegramBadRequest:
        pass  # замена может занять дольше, чем Telegram ждёт ответа на нажатие


@router.message(F.text == BTN_ORDERS)  # кнопка прежнего нижнего меню
async def my_orders(message: Message, state: FSMContext):
    await state.clear()
    await show_orders(message, message.from_user.id, 0)


@router.callback_query(OrdersCb.filter())
async def orders_page(cb: CallbackQuery, callback_data: OrdersCb):
    await show_orders(cb, cb.from_user.id, callback_data.page)


# «Мои заказы»: активные и успешные (код получен). Отменённые, заменённые и истёкшие
# пользователю не нужны — деньги за них уже вернулись на баланс.
SHOWN_STATUSES = ("waiting", "received", "completed")


async def show_orders(target, user_id: int, page: int):
    mine = (Order.user_id == user_id, Order.status.in_(SHOWN_STATUSES))
    async with Session() as s:
        total = await s.scalar(select(func.count()).select_from(Order).where(*mine))
        rows = (await s.scalars(
            select(Order).where(*mine)
            .order_by(Order.status.in_(("waiting", "received")).desc(), Order.id.desc())
            .offset(page * PAGE).limit(PAGE))).all()
    b = InlineKeyboardBuilder()
    if not rows:
        back_to_profile(b)
        await edit_or_send(target, f"У вас пока нет активаций. Нажмите «{BTN_BUY}».", b.as_markup(), image="orders")
        return
    for o in rows:
        icon = STATUS_TITLES[o.status].split()[0]
        b.button(text=f"{icon} {o.service.name} · {phone_fmt(o.phone)}",
                 callback_data=OrderCb(action="view", id=o.id))
    b.adjust(1)
    pager(b, page, total, lambda p: OrdersCb(page=p))
    back_to_profile(b)
    await edit_or_send(target, "<b>Мои заказы</b>\n\nАктивные — сверху. Нажмите, чтобы открыть.",
                       b.as_markup(), image="orders")


# ------------------------------------------------------------------ баланс


TOPUP_SBP = "topup_sbp"  # кнопка «СБП» из сообщений до подключения оплаты
BALANCE = "balance"
METHOD_HINTS = {payments.SBP: "в приложении своего банка по СБП", payments.CRYPTO: "криптовалютой"}


class Topup(StatesGroup):
    amount = State()  # ждём свою сумму пополнения
    stars = State()  # ждём своё количество звёзд


def method_title(method: int) -> str:
    return payments.METHODS[method][0]


@router.message(F.text == BTN_BALANCE)  # кнопка прежнего нижнего меню
async def balance(message: Message, state: FSMContext):
    await state.clear()
    await show_balance(message)


@router.callback_query(F.data == BALANCE)
async def balance_back(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await show_balance(cb)


async def show_balance(target) -> None:
    b = InlineKeyboardBuilder()
    text = f"💰 Ваш баланс: <b>{rub(await wallet.get_balance(target.from_user.id))}</b>"
    if payments.enabled():
        for m in payments.METHODS:
            b.button(text=method_title(m), callback_data=TopupCb(m=m))
    if stars.enabled():
        b.button(text="⭐ Telegram Stars", callback_data=StarsCb())
    if b.buttons:
        b.adjust(2)
        text += "\n\nВыберите способ пополнения:"
    else:
        text += "\n\nПополнение временно недоступно."
    back_to_profile(b)
    await edit_or_send(target, text, b.as_markup(), image="balance")


@router.callback_query(F.data == TOPUP_SBP)
async def topup_sbp_legacy(cb: CallbackQuery, state: FSMContext):
    await topup_method(cb, TopupCb(m=payments.SBP), state)


@router.callback_query(TopupCb.filter(F.amount == 0))
async def topup_method(cb: CallbackQuery, callback_data: TopupCb, state: FSMContext):
    """Способ оплаты выбран — суммы кнопками или своя."""
    await state.clear()
    m = callback_data.m
    if not payments.enabled() or m not in payments.METHODS:
        await cb.answer("Пополнение временно недоступно.", show_alert=True)
        return
    b = InlineKeyboardBuilder()
    lo, hi = payments.limits(m)
    for amount in payments.amounts(m):
        b.button(text=rub(amount), callback_data=TopupCb(m=m, amount=amount))
    b.adjust(3)
    b.row(InlineKeyboardButton(text="✏️ Другая сумма", callback_data=TopupCb(m=m, amount=-1).pack()))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=BALANCE))
    await edit_or_send(cb, f"<b>Пополнение · {method_title(m)}</b>\n\nВыберите сумму пополнения.\n"
                           f"От {rub(lo)} до {rub(hi)}.",
                       b.as_markup(), image="balance")


@router.callback_query(TopupCb.filter(F.amount == -1))
async def topup_custom(cb: CallbackQuery, callback_data: TopupCb, state: FSMContext):
    await state.set_state(Topup.amount)
    await state.update_data(method=callback_data.m)
    b = InlineKeyboardBuilder()
    lo, hi = payments.limits(callback_data.m)
    b.button(text="⬅️ Назад", callback_data=TopupCb(m=callback_data.m))
    await edit_or_send(cb, f"<b>Пополнение · {method_title(callback_data.m)}</b>\n\n"
                           f"Напишите сумму в рублях — от {rub(lo)} до {rub(hi)}, например <code>750</code>.",
                       b.as_markup(), image="balance")


@router.callback_query(TopupCb.filter(F.amount > 0))
async def topup_amount(cb: CallbackQuery, callback_data: TopupCb, state: FSMContext):
    await state.clear()
    await cb.answer("Создаём счёт…")
    await create_invoice(cb, callback_data.m, callback_data.amount)


async def create_invoice(target, method: int, amount: int) -> None:
    """Счёт на оплату: на месте экрана выбора суммы (нажатие кнопки) или новым сообщением."""
    user = target.from_user
    me = await target.bot.me()
    try:
        p = await payments.create(user.id, user.username, amount, method, f"https://t.me/{me.username}")
    except payments.PaymentError as e:
        b = InlineKeyboardBuilder()
        b.button(text="⬅️ Назад", callback_data=TopupCb(m=method))
        await edit_or_send(target, f"⚠️ {escape(str(e))}", b.as_markup(), image="balance")
        return
    text, markup = invoice_view(p)
    # счёт — текстом без картинки: потом его меняет уведомление об оплате
    msg = target.message if isinstance(target, CallbackQuery) else None
    if msg and not msg.photo:
        try:
            sent = await msg.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            sent = await msg.answer(text, reply_markup=markup)
    else:
        if msg:
            try:
                await msg.delete()
            except TelegramBadRequest:
                pass
        sent = await (msg or target).answer(text, reply_markup=markup)
    await payments.set_message(p.id, sent.chat.id, sent.message_id)


def invoice_view(p) -> tuple[str, object]:
    """Сообщение со счётом: текст и кнопки по статусу платежа."""
    b = InlineKeyboardBuilder()
    head = f"🧾 <b>Счёт №{p.id}</b> · {rub(p.amount)} · {method_title(p.method)}"
    if p.status == "pending":
        b.button(text="💳 Перейти к оплате", url=p.url)
        b.button(text="🔄 Проверить оплату", callback_data=PayCheckCb(id=p.id))
        b.adjust(1)
        text = (f"{head}\n\nНажмите «Перейти к оплате» и оплатите {METHOD_HINTS.get(p.method, 'счёт')}. "
                "Ссылка действует 15 минут.\n\nДеньги зачислятся на баланс автоматически сразу после оплаты.")
    elif p.status == "confirmed":
        text = f"{head}\n\n✅ Оплачено, {rub(p.amount)} зачислены на баланс."
    elif p.status == "chargebacked":
        text = f"{head}\n\n↩️ Платёж возвращён, сумма списана с баланса."
    else:
        b.button(text="🔁 Новый счёт", callback_data=TopupCb(m=p.method or payments.SBP))
        text = f"{head}\n\n❌ Счёт не оплачен и закрыт. Если хотите пополнить баланс — создайте новый."
    return text, b.as_markup() if b.buttons else None


@router.callback_query(PayCheckCb.filter())
async def pay_check(cb: CallbackQuery, callback_data: PayCheckCb):
    p = await payments.get(callback_data.id)
    if not p or p.user_id != cb.from_user.id:
        await cb.answer("Счёт не найден.", show_alert=True)
        return
    p = await payments.refresh(p.id)
    if p.status == "pending":
        await cb.answer("Оплата пока не поступила. Если вы уже оплатили — подождите минуту, "
                        "деньги зачислятся автоматически.", show_alert=True)
        return
    text, markup = invoice_view(p)
    try:
        await cb.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        pass
    await cb.answer()


# ------------------------------------------------------------------ пополнение звёздами Telegram (app/stars.py)


@router.callback_query(StarsCb.filter(F.n == 0))
async def stars_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if not stars.enabled():
        await cb.answer("Пополнение звёздами временно недоступно.", show_alert=True)
        return
    lo, hi = stars.limits()
    b = InlineKeyboardBuilder()
    for n in stars.packs():
        b.button(text=f"⭐{n}", callback_data=StarsCb(n=n))
    b.adjust(3)
    b.row(InlineKeyboardButton(text="✏️ Другое количество", callback_data=StarsCb(n=-1).pack()))
    b.row(InlineKeyboardButton(text="⬅️ Назад", callback_data=BALANCE))
    await edit_or_send(cb, "<b>Пополнение · ⭐ Telegram Stars</b>\n\n"
                           f"Курс: ⭐1 = {rub(stars.rate().quantize(Decimal('0.01'), rounding=ROUND_DOWN))} "
                           "на баланс.\n"
                           f"Выберите количество звёзд или напишите своё — от ⭐{lo} до ⭐{hi}.\n\n"
                           "Оплата — прямо в Telegram, деньги зачислятся сразу после оплаты.",
                       b.as_markup(), image="balance")


@router.callback_query(StarsCb.filter(F.n == -1))
async def stars_custom(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Topup.stars)
    lo, hi = stars.limits()
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ Назад", callback_data=StarsCb())
    await edit_or_send(cb, "<b>Пополнение · ⭐ Telegram Stars</b>\n\n"
                           f"Напишите количество звёзд — от {lo} до {hi}, например <code>700</code>.",
                       b.as_markup(), image="balance")


@router.callback_query(StarsCb.filter(F.n > 0))
async def stars_pack(cb: CallbackQuery, callback_data: StarsCb, state: FSMContext):
    await state.clear()
    await cb.answer()
    await send_stars_invoice(cb.message, cb.from_user.id, callback_data.n)


@router.message(Topup.stars, F.text)
async def stars_typed(message: Message, state: FSMContext):
    n = stars.parse(message.text)
    error = "Напишите количество звёзд целым числом, например 700." if n is None else stars.check(n)
    if error:
        await message.answer(f"⚠️ {error}")
        return  # ждём количество дальше
    await state.clear()
    await send_stars_invoice(message, message.from_user.id, n)


async def send_stars_invoice(message: Message, user_id: int, n: int) -> None:
    """Счёт Telegram в звёздах: рубли зафиксированы при создании, оплатить можно в течение часа."""
    try:
        p = await stars.create(user_id, n, message.chat.id)
    except stars.StarsError as e:
        b = InlineKeyboardBuilder()
        b.button(text="⬅️ Назад", callback_data=StarsCb())
        await message.answer(f"⚠️ {escape(str(e))}", reply_markup=b.as_markup())
        return
    await message.answer_invoice(
        title=f"Пополнение на {rub(p.amount)}"[:32],
        description=f"⭐{n} → {rub(p.amount)} на баланс Shifr SMS. Счёт №{p.id}, оплатить можно в течение часа.",
        payload=f"{stars.PAYLOAD}{p.id}", currency="XTR",
        prices=[LabeledPrice(label=f"{rub(p.amount)} на баланс", amount=n)])


@router.pre_checkout_query()
async def stars_precheck(q: PreCheckoutQuery):
    """Telegram спрашивает перед оплатой (ответ — за 10 с): тот ли счёт, не устарел ли."""
    try:
        error = await stars.precheck(q.invoice_payload, q.currency, q.total_amount, q.from_user.id)
    except Exception:
        log.exception("pre_checkout %s", q.invoice_payload)
        error = "Не получилось проверить счёт — попробуйте ещё раз через минуту."
    await q.answer(ok=error is None, error_message=error)


@router.message(F.successful_payment)
async def stars_paid(message: Message):
    sp = message.successful_payment
    await stars.paid(sp.invoice_payload, sp.currency, sp.total_amount, sp.telegram_payment_charge_id,
                     message.from_user.id)


@router.message(F.refunded_payment)
async def stars_refunded(message: Message):
    await stars.refunded(message.refunded_payment.telegram_payment_charge_id)


@router.message(Command("paysupport"))
async def paysupport(message: Message, state: FSMContext):
    """Обязательная для оплаты звёздами команда Telegram: вопросы по платежам — в нашу поддержку."""
    await state.clear()
    b = InlineKeyboardBuilder()
    if st.raw("support_chat").strip():
        b.button(text="💬 Поддержка", callback_data=SUPPORT)
    await message.answer("💳 <b>Вопросы по оплате</b>\n\nЕсли деньги или звёзды списались, а баланс не пополнился, "
                         "или нужна помощь с платежом — напишите в поддержку: укажите номер счёта, сумму и время оплаты. "
                         "Ответим здесь, в боте.", reply_markup=b.as_markup() if b.buttons else None)


@router.message(Topup.amount, F.text)
async def topup_typed(message: Message, state: FSMContext):
    method = (await state.get_data()).get("method", payments.SBP)
    amount = payments.parse_amount(message.text)
    error = "Напишите сумму целым числом в рублях, например 750." if amount is None else payments.check_amount(amount, method)
    if error:
        await message.answer(f"⚠️ {error}")
        return  # ждём сумму дальше
    await state.clear()
    await create_invoice(message, method, amount)


# ------------------------------------------------------------------ поиск текстом


@router.message(Flow.countries, F.text)
async def country_search(message: Message, state: FSMContext):
    data = await state.get_data()
    sid = data.get("service_id")
    if sid and country_rows(sid, message.text):
        await show_countries(message, sid, 0, message.text)
        return
    await service_search(message, state)  # не страна — ищем сервис


def search_view(query: str):
    """Результаты поиска сервиса -> (text, markup, найдено_ли)."""
    found = catalog.search_services(query)
    if not found:
        return (f"Сервис «{escape(query[:50])}» не найден. "
                "Попробуйте другое написание или откройте «📚 Все сервисы».",
                services_kb([], "all", 0, True).as_markup(), False)
    b = InlineKeyboardBuilder()
    for svc in found[:20]:
        b.button(text=svc.name, callback_data=ServiceCb(id=svc.id), style=service_style(svc))
    b.adjust(2)
    b.row(InlineKeyboardButton(text="📚 Все сервисы", callback_data=ServicesCb(mode="all").pack()))
    text = (f"🔍 «{escape(query[:50])}» — найдено: {len(found)}"
            + (" (показаны первые 20)" if len(found) > 20 else ""))
    return text, b.as_markup(), True


@router.message(F.text)
async def service_search(message: Message, state: FSMContext):
    await state.clear()
    query = message.text.strip()
    text, markup, found = search_view(query)
    if found:
        _services_origin[message.from_user.id] = ("search", query)
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data == BACK_TO_SERVICES)
async def back_to_services(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    mode, arg = _services_origin.get(cb.from_user.id, ("popular", 0))
    if mode == "search":
        text, markup, _ = search_view(str(arg))
        await edit_or_send(cb, text, markup)
    else:
        await show_services(cb, mode, int(arg))


@router.callback_query()
async def stale_button(cb: CallbackQuery):
    """Кнопка из старого сообщения, которой больше нет (например, прежнее тестовое пополнение)."""
    await cb.answer("Эта кнопка устарела — откройте раздел заново из меню.", show_alert=True)

"""Тексты и клавиатуры бота."""

from datetime import timedelta
from decimal import Decimal
from html import escape

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.db import utcnow
from app.textfmt import sms_text
from app.models import Order

MSK = timedelta(hours=3)
PAGE = 10  # строк на странице списка стран и «Моих заказов»
SERVICES_PAGE = 8  # кнопок на странице списка сервисов (и разбивка во вкладке панели «Популярные»)

BTN_BUY = "📱 Получить СМС"
BTN_BALANCE = "💰 Баланс"
BTN_ORDERS = "📋 Мои заказы"
BTN_HELP = "ℹ️ Информация"
BTN_INVITE = "👥 Пригласить друга"
BTN_WEB = "🌐 Веб-версия"
BTN_PROFILE = "👤 Профиль"  # баланс, заказы и приглашения — кнопками внутри
PARTNER_BUTTON = "📢 Партнёрка для владельцев каналов"  # ссылка на лендинг партнёрской программы
# Прежние надписи: у пользователей закреплена старая клавиатура, пока они не нажмут /start
SUPPORT = "support"  # «💬 Поддержка» / «✍️ Ответить» — чат поддержки (app/bot/support.py)
LEGACY_BUTTONS = {"📱 Купить номер": BTN_BUY, "📋 Мои номера": BTN_ORDERS, "ℹ️ Помощь": BTN_HELP}

MAIN_KB = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_BUY), KeyboardButton(text=BTN_WEB)],
              [KeyboardButton(text=BTN_HELP), KeyboardButton(text=BTN_PROFILE)]],
    resize_keyboard=True, is_persistent=True,
)
# меняется MAIN_KB — увеличить: каждому, кто ещё не получал новое меню, бот пришлёт его один раз (handlers.ensure_user)
MENU_VERSION = 3
MENU_NEWS = f"🔄 Меню обновлено: баланс, заказы и приглашения — в «{BTN_PROFILE}»."  # что изменилось в MENU_VERSION


class ServicesCb(CallbackData, prefix="sl"):
    mode: str  # popular | all
    page: int = 0


class SortCb(CallbackData, prefix="srt"):
    """Сортировка списка стран сервиса s: m — desc | asc."""
    s: int
    m: str


class ServiceCb(CallbackData, prefix="s"):
    """Карточка сервиса в режиме «Авто» (page — от старых кнопок, не используется)."""
    id: int
    page: int = 0


class CountriesCb(CallbackData, prefix="cl"):
    """Список стран сервиса s — «Выбрать страну» самому."""
    s: int
    page: int = 0


class AutoBuyCb(CallbackData, prefix="ab"):
    """Режим «Авто»: самая надёжная активация сервиса s любой страны за price ₽."""
    s: int
    price: int


class CountryCb(CallbackData, prefix="c"):
    s: int
    c: int


class BuyCb(CallbackData, prefix="b"):
    s: int
    c: int
    price: int  # уровень цены, ₽


class OrderCb(CallbackData, prefix="o"):
    action: str  # view (новой карточкой) | cancel | replace | finish | another | refresh
    id: int


class OrdersCb(CallbackData, prefix="ol"):
    page: int = 0


class TopupCb(CallbackData, prefix="tp"):
    """Пополнение: m — способ оплаты (payments.METHODS), amount — сумма ₽ (0 — ещё не выбрана, -1 — своя)."""
    m: int
    amount: int = 0


class StarsCb(CallbackData, prefix="st"):
    """Пополнение звёздами: n — сколько звёзд (0 — экран выбора, -1 — своё количество)."""
    n: int = 0


class PayCheckCb(CallbackData, prefix="pc"):
    """«Проверить оплату» счёта id."""
    id: int


def rub(x: Decimal | int) -> str:
    x = Decimal(x)
    s = f"{x:,.2f}".replace(",", " ").replace(".00", "")
    return f"{s} ₽"


def qty(n: int) -> str:
    if n > 10_000:
        return "10 000+ шт"
    return f"{n:,} шт".replace(",", " ")


def phone_fmt(phone: str) -> str:
    return "+" + phone.lstrip("+")


def msk(dt) -> str:
    return (dt + MSK).strftime("%H:%M")


def pager(builder: InlineKeyboardBuilder, page: int, total: int, make, size: int = PAGE) -> None:
    pages = (total + size - 1) // size
    if pages <= 1:
        return
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="◀️", callback_data=make(page - 1).pack()))
    row.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        row.append(InlineKeyboardButton(text="▶️", callback_data=make(page + 1).pack()))
    builder.row(*row)


STATUS_TITLES = {
    "waiting": "⏳ Ожидаем SMS",
    "received": "✅ SMS получена",
    "completed": "☑️ Завершён",
    "cancelled": "❌ Отменён",
    "replaced": "🔁 Заменён",
    "expired": "⌛ Истёк",
}


def service_note(note: str | None) -> list[str]:
    """Примечание сервиса («Прим. 1» / «Прим. 2» в панели) — абзацем внутри сообщения: [текст, пустая строка] или []."""
    note = (note or "").strip()
    return [escape(note), ""] if note else []


def instruction_url(raw: str | None) -> str | None:
    """Ссылка «Инструкция» из панели -> адрес для кнопки (t.me/… и @канал тоже подходят)."""
    url = (raw or "").strip()
    if url.startswith(("https://", "http://")):
        return url
    if url.startswith(("t.me/", "telegra.ph/")):
        return "https://" + url
    if url.startswith("@") and len(url) > 1:
        return "https://t.me/" + url[1:]
    return None


def order_text(order: Order) -> str:
    """Карточка активации. Примечания сервиса — только пока активация идёт: у закрытой (из «Мои заказы»
    и после завершения) их нет, SMS туда больше не придут."""
    lines = [
        f"<b>{escape(order.service.name)}</b> · {order.country.flag} {escape(order.country.name)}",
        f"Номер для активации: <code>{phone_fmt(order.phone)}</code>",
        f"Цена: {rub(order.price)}",
        "",
        *(service_note(order.service.note) if order.is_active else []),
    ]
    for i, m in enumerate(order.messages, 1):
        label = f"SMS-код {i}" if len(order.messages) > 1 else "SMS-код"
        if m.code:
            lines.append(f"🔑 {label}: <code>{escape(m.code)}</code>")
        lines.append(f"<i>{escape(sms_text(m.text)[:500])}</i>")
        lines.append("")
    if order.messages and order.is_active:  # «Прим. 2» сервиса — сразу под пришедшим кодом
        lines += service_note(order.service.note_sms)

    if order.status == "waiting":
        left = max(0, int((order.expires_at - utcnow()).total_seconds() // 60))
        lines.append(f"⏳ Ожидаем SMS… Активация действует до {msk(order.expires_at)} МСК "
                     f"(≈{left} мин).")
        lines.append("SMS обычно приходит за 1–5 минут. Если сервис не принимает номер или SMS "
                     "долго нет — нажмите «🔁 Другой номер», платить заново не нужно.")
        lines.append("Если SMS не придёт — деньги автоматически вернутся на баланс.")
    elif order.status == "received":
        lines.append(f"Активация действует до {msk(order.expires_at)} МСК. "
                     "Когда закончите — нажмите «Завершить».")
    elif order.status == "cancelled":
        lines.append(f"❌ Активация отменена, {rub(order.price)} возвращены на баланс.")
    elif order.status == "replaced":
        lines.append(f"🔁 Активация заменена, {rub(order.price)} возвращены на баланс.")
    elif order.status == "expired":
        lines.append(f"⌛ SMS не пришла, {rub(order.price)} возвращены на баланс.")
    elif order.status == "completed":
        lines.append("☑️ Активация завершена.")
    return "\n".join(lines)


def order_kb(order: Order) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if order.status == "waiting":
        b.button(text="🔁 Другой номер", callback_data=OrderCb(action="replace", id=order.id))
        b.button(text="❌ Отменить", callback_data=OrderCb(action="cancel", id=order.id))
        b.button(text="🔄 Обновить", callback_data=OrderCb(action="refresh", id=order.id))
    elif order.status == "received":
        if order.can_get_another or order.provider == "smspool":
            b.button(text="🔁 Ещё SMS", callback_data=OrderCb(action="another", id=order.id))
        b.button(text="✅ Завершить", callback_data=OrderCb(action="finish", id=order.id))
    else:
        b.button(text="🔄 Ещё одна такая активация",
                 callback_data=ServiceCb(id=order.service_id) if order.auto
                 else CountryCb(s=order.service_id, c=order.country_id))
    b.adjust(2)
    return b.as_markup()

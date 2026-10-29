"""Настройки бота, редактируемые из веб-панели. Хранятся в таблице settings, кэшируются в памяти."""

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from app.db import Session
from app.models import Setting


@dataclass(frozen=True)
class SettingDef:
    key: str
    label: str
    type: str  # decimal | int | bool | choice | str | chat (группа бота — выбор из списка) | users (@username через запятую)
    default: str
    help: str = ""
    choices: tuple[tuple[str, str], ...] = ()
    show_if: tuple[str, str] | None = None  # (ключ, значение): показывать в панели только при нём
    secure: bool = False  # настройка денег: меняется только на «Сверке» с кодом аутентификатора


DEFS: list[SettingDef] = [
    SettingDef("blocked_countries", "Запрещённые страны (ISO-коды)", "str", "RU,UA",
               "Через запятую, например RU,UA. Номера этих стран бот не показывает и не продаёт — "
               "включая виртуальные варианты и страны, которые площадки добавят позже"),
    SettingDef("markup_type", "Тип наценки", "choice", "percent",
               choices=(("percent", "Процент"), ("fixed", "Фиксированная сумма, ₽"))),
    SettingDef("markup_percent", "Наценка, %", "decimal", "50", show_if=("markup_type", "percent")),
    SettingDef("markup_min", "Минимальная наценка, ₽", "decimal", "3",
               "Наценка не меньше этой суммы. Закупка 1 ₽ при 50% и минимуме 3 ₽ → 4 ₽; "
               "закупка 20 ₽ → 30 ₽", show_if=("markup_type", "percent")),
    SettingDef("markup_max", "Максимальная наценка, ₽", "decimal", "70",
               "Наценка не больше этой суммы — на дорогих номерах процент не раздувает цену. "
               "Закупка 300 ₽ при 50% и максимуме 70 ₽ → 370 ₽ вместо 450 ₽. 0 — без ограничения",
               show_if=("markup_type", "percent")),
    SettingDef("markup_fixed", "Наценка, ₽", "decimal", "10", show_if=("markup_type", "fixed")),
    SettingDef("usd_rate_auto", "Курс USD автоматически (ЦБ РФ)", "bool", "1"),
    SettingDef("usd_rate", "Курс USD, ₽", "decimal", "95",
               "Если авто-курс включён — обновляется сам раз в час"),
    SettingDef("usd_rate_extra", "Надбавка к курсу, %", "decimal", "3",
               "Страховка от колебаний курса и комиссий при пополнении площадок"),
    SettingDef("quarantine_minutes", "Карантин после «нет номеров», мин", "int", "2",
               "Если площадка отказала («нет номеров»), хотя в прайсе номера были, или ответила ошибкой, "
               "этот поставщик для этой пары сервис+страна скрывается на указанное время"),
    SettingDef("quality_days", "Период для выбора поставщика, дней", "int", "14",
               "За какой период бот считает успешность поставщиков, чтобы пробовать лучших первыми. "
               "На странице «Качество» период выбирается отдельно"),
    SettingDef("quality_min_orders", "Минимум активаций для оценки", "int", "5",
               "Пока активаций меньше, на странице «Качество» оценка помечена «?» — она ещё близка к "
               "среднему и может заметно поменяться"),
    SettingDef("quality_quick_cancel_minutes", "Быстрая отмена, мин", "int", "3",
               "Отмена раньше этого срока считается «передумал» и не портит статистику. "
               "Отмена позже и замена номера — провал, как и истечение срока без SMS"),
    SettingDef("grizzly_enabled", "Grizzly SMS включён", "bool", "1"),
    SettingDef("smspool_enabled", "SMSPool включён", "bool", "1"),
    SettingDef("fivesim_enabled", "5SIM включён", "bool", "0"),
    SettingDef("onlinesim_enabled", "Onlinesim включён", "bool", "0"),
    SettingDef("max_active_orders", "Макс. активных активаций на пользователя", "int", "5"),
    SettingDef("catalog_sync_minutes", "Обновление прайса, мин", "int", "10"),
    SettingDef("poll_seconds", "Проверка статусов заказов, сек", "int", "30",
               "Резервный опрос площадок. Коды в основном приходят мгновенно через вебхуки"),
    SettingDef("privacy_url", "Политика конфиденциальности (ссылка)", "str",
               "https://telegra.ph/Politika-konfidencialnosti-09-29-131",
               "Кнопка в разделе «Информация». Пусто — кнопки нет"),
    SettingDef("terms_url", "Пользовательское соглашение (ссылка)", "str",
               "https://telegra.ph/Polzovatelskoe-soglashenie-09-29-45",
               "Кнопка в разделе «Информация». Пусто — кнопки нет"),
    SettingDef("support_chat", "Группа поддержки", "chat", "",
               "Группа с темами, где бот — администратор с правом «Управление темами». Кнопка «💬 Поддержка» в "
               "разделе «Информация»: каждое обращение — новая тема, ответы из темы бот пересылает пользователю. "
               "Не выбрана — кнопки нет"),
    SettingDef("topup_amounts", "Суммы пополнения по СБП, ₽", "str", "100,300,500,1000,2000,5000",
               "Кнопки с суммами в боте, через запятую. Свою сумму пользователь тоже может ввести"),
    SettingDef("topup_min", "Минимальное пополнение по СБП, ₽", "int", "100"),
    SettingDef("topup_amounts_crypto", "Суммы пополнения криптовалютой, ₽", "str", "500,1000,2000,3000,5000,10000",
               "Перевод криптовалюты стоит около $1 комиссии сети, поэтому мелкие суммы невыгодны — "
               "кнопки начинаются с ≈ $5"),
    SettingDef("topup_min_crypto", "Минимальное пополнение криптовалютой, ₽", "int", "500"),
    SettingDef("topup_max", "Максимальное пополнение, ₽", "int", "50000"),
    SettingDef("stars_enabled", "Пополнение звёздами Telegram", "bool", "1"),
    SettingDef("stars_usd", "Сколько Telegram платит за 1 звезду при выводе, $", "decimal", "0.013",
               "Курс вывода на Fragment (≈ $0,013, меняется вместе с TON). От него — курс зачисления и «Сверка»"),
    SettingDef("stars_margin", "Запас курса звёзд, %", "decimal", "10",
               "Зачисляем: звёзды × курс вывода × курс ЦБ × (1 − запас). Запас покрывает колебания курса"),
    SettingDef("stars_packs", "Пакеты звёзд", "str", "150,250,500,1000,2500,5000",
               "Кнопки в боте, через запятую. Своё количество пользователь тоже может ввести"),
    SettingDef("stars_min", "Минимум звёзд за раз", "int", "150"),
    SettingDef("stars_max", "Максимум звёзд за раз", "int", "5000"),
    SettingDef("channel_url", "Наш канал", "str", "https://t.me/shifrovalnya",
               "@канал или ссылка — кнопка «📢 Наш канал» в разделе «Информация». Пусто — кнопки нет"),
    SettingDef("trx_alert", "Предупреждать, когда TRX в кошельке меньше", "decimal", "100",
               "TRX сжигается на комиссию каждого перевода USDT (≈ 7–14 TRX). Уведомление владельцам в Telegram"),
    SettingDef("tg_check_enabled", "Проверять номера Telegram перед выдачей", "bool", "1",
               "Номер, на котором уже есть аккаунт Telegram, пользователю не выдаётся — бот берёт другой, а этот "
               "отменяет у площадки. Аккаунты для проверки — «Ещё» → «Проверка Telegram»"),
    SettingDef("tg_check_daily", "Проверок в сутки на один аккаунт", "int", "0",
               "0 — без лимита. Достиг лимита — проверяют остальные аккаунты"),
    SettingDef("tg_check_pause", "Пауза между проверками одного аккаунта, сек", "int", "0",
               "0 — без паузы. Аккаунты и так проверяют по очереди"),
    SettingDef("user_ref_enabled", "Приглашения пользователей", "bool", "1",
               "Кнопка «👥 Пригласить друга»: ссылка — после первого пополнения, пригласившему — доля с успешных "
               "активаций друга на баланс бота. Выключить — новые не привяжутся и начисления остановятся"),
    SettingDef("user_ref_revenue_percent", "Доля пригласившего от цены активации, %", "decimal", "15"),
    SettingDef("user_ref_profit_percent", "Но не больше, % нашей прибыли с активации", "decimal", "50"),
    SettingDef("partner_min_subscribers", "Минимум подписчиков канала", "int", "500",
               "Канал с меньшим числом подписчиков не подключится к партнёрской программе сам через страницу "
               "партнёрской программы. Вручную в «Каналах» — можно любой"),
    SettingDef("notify_users", "Кому присылать системные уведомления", "users", "",
               "@username через запятую — можно несколько. Выводы, поступления в кошелёк системы, пополнения площадок, "
               "мало денег на площадках или TRX. Каждый должен сначала написать боту /start. Пусто — никому"),
    SettingDef("provider_alert", "Предупреждать, когда на площадке меньше, $", "decimal", "5",
               "Уведомление владельцам в Telegram, чтобы вовремя пополнить. 0 — не предупреждать"),
    # --- деньги: только «Сверка» + код аутентификатора (app/web/money_pages.py)
    SettingDef("profit_since", "Учёт прибыли владельцев с", "str", "", secure=True),
    SettingDef("working_capital", "Оборотные средства, $", "decimal", "0", secure=True),
]
DEFS_BY_KEY = {d.key: d for d in DEFS}

# Группы на странице «Настройки» — в этом порядке. Не попавшие в группы — в «Прочее».
GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Цены", ("markup_type", "markup_percent", "markup_min", "markup_max", "markup_fixed")),
    ("Курс доллара", ("usd_rate_auto", "usd_rate", "usd_rate_extra")),
    ("Пополнение", ("topup_amounts", "topup_min", "topup_amounts_crypto", "topup_min_crypto", "topup_max")),
    ("Пополнение звёздами", ("stars_enabled", "stars_usd", "stars_margin", "stars_packs", "stars_min", "stars_max")),
    ("Площадки", ("grizzly_enabled", "smspool_enabled", "fivesim_enabled", "onlinesim_enabled",
                  "catalog_sync_minutes", "poll_seconds",
                  "quarantine_minutes")),
    ("Качество поставщиков", ("quality_days", "quality_min_orders", "quality_quick_cancel_minutes")),
    ("Ограничения", ("blocked_countries", "max_active_orders")),
    ("Документы и поддержка", ("privacy_url", "terms_url", "support_chat", "channel_url")),
    ("Кошелёк и площадки", ("notify_users", "trx_alert", "provider_alert")),
    ("Проверка номеров Telegram", ("tg_check_enabled", "tg_check_daily", "tg_check_pause")),
    ("Партнёрская программа", ("partner_min_subscribers",)),
    ("Приглашения пользователей", ("user_ref_enabled", "user_ref_revenue_percent", "user_ref_profit_percent")),
)


def grouped() -> list[tuple[str, list[SettingDef]]]:
    groups = [(title, [DEFS_BY_KEY[k] for k in keys]) for title, keys in GROUPS]
    placed = {k for _, keys in GROUPS for k in keys}
    rest = [d for d in DEFS if d.key not in placed and not d.secure]
    return groups + ([("Прочее", rest)] if rest else [])

_cache: dict[str, str] = {}


async def load() -> None:
    async with Session() as s:
        rows = (await s.scalars(select(Setting))).all()
    _cache.clear()
    _cache.update({d.key: d.default for d in DEFS})
    _cache.update({r.key: r.value for r in rows})


async def save(values: dict[str, str]) -> None:
    async with Session() as s:
        for key, value in values.items():
            if key not in DEFS_BY_KEY:
                continue
            row = await s.get(Setting, key)
            if row:
                row.value = value
            else:
                s.add(Setting(key=key, value=value))
        await s.commit()
    await load()


def raw(key: str) -> str:
    return _cache.get(key, DEFS_BY_KEY[key].default)


def dec(key: str) -> Decimal:
    try:
        value = Decimal(raw(key).replace(",", "."))
        if value.is_finite():
            return value
    except Exception:
        pass
    return Decimal(DEFS_BY_KEY[key].default)


def int_(key: str) -> int:
    try:
        return int(raw(key))
    except ValueError:
        return int(DEFS_BY_KEY[key].default)


def bool_(key: str) -> bool:
    return raw(key) in ("1", "true", "on", "yes")

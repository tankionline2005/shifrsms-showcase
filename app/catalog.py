"""Единый каталог: сопоставление сервисов/стран разных площадок и актуальные предложения.

Справочники (сервисы, страны, связи с кодами площадок) хранятся в БД и редактируются в панели.
Цены обновляются фоновой задачей и живут в памяти: {(service_id, country_id): [Offer, ...]}.
"""

import asyncio
import gettext
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import pycountry
from sqlalchemy import delete, func, select

from app import pricing, quality
from app import settings_store as st
from app.db import Session, utcnow
from app.models import (
    NOT_POPULAR_SORT, Country, CountryLink, Order, Quarantine, Service, ServiceCountry, ServiceLink,
)
from app.providers import PROVIDERS, enabled
from app.providers.base import Provider, Tier

log = logging.getLogger(__name__)


@dataclass
class Offer:
    """Предложение конкретного поставщика площадки для пары сервис+страна."""
    provider: str
    service_code: str
    country_code: str
    tiers: list[Tier]  # (цена USD, остаток) по возрастанию цены
    supplier_id: str | None = None  # для покупки у этого поставщика; None — не раскрыт
    supplier: str | None = None  # название для статистики и панели

    @property
    def cost_usd(self) -> Decimal:
        return self.tiers[0][0]

    @property
    def qkey(self) -> tuple[str, str, str, str]:
        return (self.provider, self.service_code, self.country_code, self.supplier_id or "")


@dataclass
class Candidate:
    offer: Offer
    max_price: Decimal  # предел цены для запроса к площадке
    rate: float = 0.0  # надёжность поставщика в стране 0..1 (quality.reliability)


@dataclass
class Level:
    """Уровень цены: за price ₽ покупаем у любого поставщика с закупкой не дороже cap."""
    price: Decimal  # цена продажи, ₽
    cap: Decimal  # максимум закупки, USD
    count: int  # номеров по этой цене и дешевле
    candidates: list[Candidate]  # в порядке попытки покупки


@dataclass
class AutoLevel:
    """Уровень цены режима «Авто»: за price ₽ — самый надёжный поставщик любой страны
    с закупкой не дороже cap."""
    price: Decimal
    cap: Decimal
    candidates: list[tuple[int, Candidate]]  # (страна, кандидат) в порядке попытки покупки


@dataclass
class Quote:
    """Что реально можно купить прямо сейчас для пары сервис+страна."""
    levels: list[Level]  # по возрастанию цены, не пустой

    @property
    def price(self) -> Decimal:
        return self.levels[0].price

    @property
    def cost_usd(self) -> Decimal:
        return min(c.offer.cost_usd for c in self.levels[0].candidates)

    @property
    def count(self) -> int:
        return self.levels[-1].count


@dataclass
class ServiceMeta:
    id: int
    key: str
    name: str
    is_popular: bool
    is_hidden: bool
    sort: int
    note: str | None = None
    highlight: bool = False
    instruction: str | None = None


@dataclass
class CountryMeta:
    id: int
    key: str
    name: str
    flag: str
    is_hidden: bool

    @property
    def title(self) -> str:
        return f"{self.flag} {self.name}"


SERVICES: dict[int, ServiceMeta] = {}
# service_id -> разрешённые страны (только для сервисов с включённым ограничением)
ALLOWED: dict[int, set[int]] = {}
COUNTRIES: dict[int, CountryMeta] = {}
# provider -> {(service_id, country_id): [Offer]}
_provider_offers: dict[str, dict[tuple[int, int], list[Offer]]] = {}
# provider -> {service_id: {country_id}} — индекс для быстрого списка стран
_index: dict[str, dict[int, set[int]]] = {}
last_sync: dict[str, tuple[str, datetime]] = {}  # provider -> ("OK" / "Ошибка: …", когда, UTC)
# Наш баланс на площадках (USD). Нет записи — баланс неизвестен, не ограничиваем.
balances: dict[str, Decimal] = {}
_visible_cache: set[int] | None = None  # сервисы, которые сейчас можно купить
# Поставщики, которые ответили «нет номеров», хотя в прайсе номера были. Прайс площадки
# такие «фантомы» возвращает снова, поэтому держим их скрытыми какое-то время.
# Копия таблицы quarantine в памяти:
# (provider, service_code, country_code, supplier_id) -> (up_to, until)
_quarantine: dict[tuple[str, str, str, str], tuple[Decimal, datetime]] = {}
# provider -> {(service_id, country_id): (service_code, country_code)} — коды пары у площадки
_pair_codes: dict[str, dict[tuple[int, int], tuple[str, str]]] = {}

# ---------------------------------------------------------------- сопоставление

POPULAR = [
    "telegram", "whatsapp", "google", "vk", "openai", "discord", "instagram", "tiktok",
    "facebook", "twitter", "microsoft", "apple", "amazon", "yandex", "wildberries", "avito",
    "ozon", "maxmessenger", "tinder", "steam", "claude", "snapchat", "viber", "uber",
]
SERVICE_ALIASES = {
    "claudeai": "claude",
    "anthropic": "claude",
    "max": "maxmessenger",
    "vkontakte": "vk",
    "chatgpt": "openai",
}
COUNTRY_ALIASES = {
    "salvador": "SV", "sintmaarten": "SX", "swaziland": "SZ", "nambia": "NA", "reunion": "RE",
    "saintvincent": "VC", "drcongo": "CD", "macedonia": "MK", "capeverde": "CV",
    "palestine": "PS", "kosovo": "XK", "ivorycoast": "CI", "argentinas": "AR", "turkey": "TR",
    "czech": "CZ", "papuanewgvineya": "PG", "russia": "RU", "usa": "US", "uk": "GB",
    # Onlinesim пишет названия слитно
    "northmacedonia": "MK", "ivory": "CI", "sierraleone": "SL", "hongkong": "HK", "bosnia": "BA",
    "belorussia": "BY", "saudiarabia": "SA", "burkinafaso": "BF", "guineabissau": "GW",
}
EXTRA_RU = {"XK": "Косово"}
# Короткие привычные названия вместо официальных
SHORT_RU = {
    "SY": "Сирия", "SX": "Синт-Мартен", "US": "США", "US_V": "США (виртуальные)",
    "IR": "Иран", "KR": "Южная Корея", "KP": "КНДР", "MD": "Молдова", "TZ": "Танзания",
    "BO": "Боливия", "VE": "Венесуэла", "LA": "Лаос", "TW": "Тайвань", "VN": "Вьетнам",
    "RU": "Россия", "GB": "Великобритания", "CF": "ЦАР", "DO": "Доминиканская Республика",
    "PS": "Палестина", "MK": "Северная Македония", "CZ": "Чехия", "CD": "Конго (ДРК)",
    "CG": "Конго", "CI": "Кот-д’Ивуар", "BN": "Бруней", "FM": "Микронезия", "SS": "Южный Судан",
}
# русские варианты написания для поиска в боте
RU_SEARCH = {
    "telegram": "телеграм телеграмм телега тг", "whatsapp": "ватсап вотсап ватсапп вацап",
    "google": "гугл гмейл джимейл ютуб", "vk": "вк вконтакте контакт", "openai": "чатгпт чат гпт опенаи",
    "discord": "дискорд", "instagram": "инстаграм инста", "tiktok": "тикток тик ток",
    "facebook": "фейсбук", "twitter": "твиттер икс", "microsoft": "майкрософт аутлук",
    "apple": "эпл апл айклауд", "amazon": "амазон", "yandex": "яндекс", "wildberries": "вайлдберриз вб",
    "avito": "авито", "ozon": "озон", "maxmessenger": "макс", "tinder": "тиндер", "steam": "стим",
    "claude": "клод антропик", "snapchat": "снапчат", "viber": "вайбер", "uber": "убер",
    "netflix": "нетфликс", "linkedin": "линкедин", "airbnb": "эйрбнб", "paypal": "пейпал",
    "binance": "бинанс", "bybit": "байбит", "aliexpress": "алиэкспресс", "alibaba": "алибаба",
    "sber": "сбер сбербанк", "mamba": "мамба", "line": "лайн", "wechat": "вичат", "kakaotalk": "какао",
}
PRETTY_NAMES = {
    "google": "Google / Gmail / YouTube", "whatsapp": "WhatsApp", "openai": "ChatGPT (OpenAI)",
    "claude": "Claude (Anthropic)", "instagram": "Instagram", "microsoft": "Microsoft / Outlook",
    "maxmessenger": "MAX", "twitter": "X (Twitter)", "vk": "ВКонтакте", "yandex": "Яндекс",
    "amazon": "Amazon", "facebook": "Facebook", "tiktok": "TikTok", "uber": "Uber",
}
_ru = gettext.translation("iso3166-1", pycountry.LOCALES_DIR, languages=["ru"], fallback=True)


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def service_key(name: str) -> str:
    first = re.split(r"[/,+|]", name)[0]
    key = _norm(first) or _norm(name) or name.lower()
    return SERVICE_ALIASES.get(key, key)[:128]


def flag(iso2: str) -> str:
    iso2 = iso2[:2].upper()
    if len(iso2) != 2 or not iso2.isalpha():
        return "🏳"
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso2)


def country_key(name_en: str, iso: str | None,
                name_ru: str | None = None) -> tuple[str | None, str]:
    """-> (ключ страны, суффикс варианта для названия)."""
    m = re.search(r"\(([^)]*)\)", name_en)
    suffix = m.group(1).strip() if m else ""
    base = re.sub(r"\s*\(.*\)", "", name_en).strip()
    if iso and re.fullmatch(r"[A-Z]{2}(_\w+)?", iso):
        return iso, suffix
    code = COUNTRY_ALIASES.get(_norm(base))
    if not code:
        try:
            code = pycountry.countries.lookup(base).alpha_2
        except LookupError:
            return None, suffix
    if suffix:
        # Grizzly называет виртуальные США "USA (2)", но в русском названии пишет «виртуальные»
        virtual = "virtual" in suffix.lower() or "виртуал" in (name_ru or "").lower()
        variant = "V" if virtual else _norm(suffix).upper()[:6]
        return f"{code}_{variant}", suffix
    return code, ""


def country_name_ru(key: str, provider_ru: str | None, suffix: str) -> str:
    if key in SHORT_RU:
        return SHORT_RU[key]
    if provider_ru and re.search("[а-яА-Я]", provider_ru):
        return provider_ru
    iso = key[:2]
    c = pycountry.countries.get(alpha_2=iso)
    name = EXTRA_RU.get(iso) or (_ru.gettext(c.name) if c else iso)
    if suffix:
        name += " (виртуальные)" if key.endswith("_V") else f" ({suffix})"
    return name


# ---------------------------------------------------------------- метаданные


async def reload_meta() -> None:
    async with Session() as s:
        services = (await s.scalars(select(Service))).all()
        countries = (await s.scalars(select(Country))).all()
    invalidate()
    SERVICES.clear()
    SERVICES.update({x.id: ServiceMeta(x.id, x.key, x.name, x.is_popular, x.is_hidden, x.sort,
                                             (x.note or "").strip() or None, bool(x.highlight),
                                             (x.instruction or "").strip() or None)
                     for x in services})
    COUNTRIES.clear()
    blocked = blocked_countries()
    COUNTRIES.update({x.id: CountryMeta(x.id, x.key, x.name, x.flag,
                                        x.is_hidden or x.key[:2] in blocked)
                      for x in countries})
    async with Session() as s:
        pairs = (await s.execute(select(ServiceCountry.service_id, ServiceCountry.country_id))).all()
    allowed: dict[int, set[int]] = {x.id: set() for x in services if x.country_filter}
    for sid, cid in pairs:
        if sid in allowed:
            allowed[sid].add(cid)
    ALLOWED.clear()
    ALLOWED.update(allowed)


# ---------------------------------------------------------------- синхронизация


async def _link_services(p: Provider, codes: set[str]) -> dict[str, int]:
    infos = {i.code: i for i in await p.get_services()}
    async with Session() as s:
        links = {l.code: l.service_id for l in
                 (await s.scalars(select(ServiceLink).where(ServiceLink.provider == p.name))).all()}
        by_key = {x.key: x for x in (await s.scalars(select(Service))).all()}
        for code in codes - links.keys():
            name = (infos[code].name if code in infos else code).strip() or code
            key = service_key(name)
            svc = by_key.get(key)
            if not svc:
                pretty = PRETTY_NAMES.get(key) or name[:1].upper() + name[1:]
                # Новый сервис площадки появляется скрытым: среди них бывают сервисы РФ и
                # Украины, которые нельзя продавать, — админ открывает нужные во вкладке «Сервисы»
                svc = Service(key=key, name=pretty[:128], is_hidden=True,
                              is_popular=key in POPULAR,
                              sort=POPULAR.index(key) if key in POPULAR else NOT_POPULAR_SORT)
                s.add(svc)
                await s.flush()
                by_key[key] = svc
            s.add(ServiceLink(provider=p.name, code=code, provider_name=name[:255],
                              service_id=svc.id))
            links[code] = svc.id
        await s.commit()
    return links


async def _link_countries(p: Provider, codes: set[str]) -> dict[str, int]:
    infos = {i.code: i for i in await p.get_countries()}
    async with Session() as s:
        links = {l.code: l.country_id for l in
                 (await s.scalars(select(CountryLink).where(CountryLink.provider == p.name))).all()}
        by_key = {x.key: x for x in (await s.scalars(select(Country))).all()}
        for code in codes - links.keys():
            info = infos.get(code)
            if not info:
                continue
            key, suffix = country_key(info.name_en, info.iso, info.name_ru)
            country_id = None
            if key:
                c = by_key.get(key)
                if not c:
                    c = Country(key=key, name=country_name_ru(key, info.name_ru, suffix),
                                flag=flag(key))
                    s.add(c)
                    await s.flush()
                    by_key[key] = c
                country_id = c.id
            else:
                log.warning("%s: не удалось определить страну %r", p.name, info.name_en)
            s.add(CountryLink(provider=p.name, code=code, provider_name=info.name_en[:255],
                              country_id=country_id))
            links[code] = country_id
        await s.commit()
    return {k: v for k, v in links.items() if v}


def _make_offers(p_name: str, items, service_links: dict, country_links: dict,
                 into: dict[tuple[int, int], list[Offer]]) -> None:
    """PriceItem площадки -> предложения каталога (с учётом карантина поставщиков)."""
    now = utcnow()
    for x in items:
        sid, cid = service_links.get(x.service_code), country_links.get(x.country_code)
        if not (sid and cid):
            continue
        _pair_codes.setdefault(p_name, {})[(sid, cid)] = (x.service_code, x.country_code)
        tiers = x.tiers
        q = _quarantine.get((p_name, x.service_code, x.country_code, x.supplier_id or ""))
        if q and q[1] > now:
            tiers = [t for t in tiers if t[0] > q[0]]
        if tiers:
            into.setdefault((sid, cid), []).append(
                Offer(p_name, x.service_code, x.country_code, tiers, x.supplier_id, x.supplier))


async def sync_provider(p: Provider) -> None:
    prices = await p.get_prices()
    service_links = await _link_services(p, {x.service_code for x in prices})
    country_links = await _link_countries(p, {x.country_code for x in prices})
    await _purge_quarantine()
    offers: dict[tuple[int, int], list[Offer]] = {}
    _pair_codes[p.name] = {}
    _make_offers(p.name, prices, service_links, country_links, offers)
    _provider_offers[p.name] = offers
    _rebuild_index(p.name)
    log.info("%s: %d предложений в наличии", p.name, sum(len(v) for v in offers.values()))


def _rebuild_index(provider: str) -> None:
    index: dict[int, set[int]] = {}
    for (sid, cid), items in _provider_offers.get(provider, {}).items():
        if items:
            index.setdefault(sid, set()).add(cid)
    _index[provider] = index
    invalidate()


_sync_lock = asyncio.Lock()


async def sync_all() -> None:
    """Обновление прайса. Кнопка в панели и фоновая задача не должны идти параллельно:
    обе создают связи с кодами площадок, и вторая упадёт на дублях."""
    if _sync_lock.locked():
        return  # уже обновляется — результат будет тот же
    async with _sync_lock:
        await _sync_all()


async def _sync_all() -> None:
    await refresh_balances()
    for name, p in PROVIDERS.items():
        try:
            await sync_provider(p)
            last_sync[name] = ("OK", utcnow())
        except Exception as e:
            log.exception("Ошибка синхронизации %s", name)
            last_sync[name] = (f"Ошибка: {e}"[:300], utcnow())
    await reload_meta()


async def relink_service(provider: str, code: str, service_id: int) -> None:
    """Ручное переназначение кода площадки на другой сервис (объединение)."""
    async with Session() as s:
        link = (await s.scalars(select(ServiceLink).where(
            ServiceLink.provider == provider, ServiceLink.code == code))).first()
        if link:
            link.service_id = service_id
            link.manual = True
            await s.commit()
    # переносим уже загруженные предложения без ожидания следующей синхронизации
    offers = _provider_offers.get(provider, {})
    codes = _pair_codes.get(provider, {})
    for (sid, cid) in list(offers):
        moved = [o for o in offers[(sid, cid)] if o.service_code == code]
        if moved and sid != service_id:
            offers[(sid, cid)] = [o for o in offers[(sid, cid)] if o.service_code != code]
            offers.setdefault((service_id, cid), []).extend(moved)
            codes[(service_id, cid)] = codes.pop((sid, cid), (code, moved[0].country_code))
    _rebuild_index(provider)


# ---------------------------------------------------------------- балансы площадок


def invalidate() -> None:
    global _visible_cache
    _visible_cache = None


def _set_balance(name: str, value: Decimal) -> None:
    if balances.get(name) != value:
        balances[name] = value
        invalidate()


async def refresh_balances() -> None:
    for name, p in PROVIDERS.items():
        try:
            _set_balance(name, await p.get_balance())
        except Exception as e:
            log.warning("баланс %s: %s", name, e)


def spend(provider: str, amount: Decimal) -> None:
    """Учесть покупку до следующего обновления баланса."""
    if provider in balances:
        _set_balance(provider, max(Decimal("0"), balances[provider] - amount))


def mark_no_balance(provider: str) -> None:
    _set_balance(provider, Decimal("0"))


# ---------------------------------------------------------------- карантин поставщиков


async def load_quarantine() -> None:
    async with Session() as s:
        rows = (await s.scalars(select(Quarantine).where(Quarantine.until > utcnow()))).all()
    _quarantine.clear()
    _quarantine.update({(r.provider, r.service_code, r.country_code, r.supplier_id or ""):
                        (r.up_to, r.until) for r in rows})


async def _purge_quarantine() -> None:
    now = utcnow()
    for k in [k for k, (_, until) in _quarantine.items() if until <= now]:
        del _quarantine[k]
    async with Session() as s:
        await s.execute(delete(Quarantine).where(Quarantine.until <= now))
        await s.commit()


async def remove_quarantine(quarantine_id: int) -> None:
    async with Session() as s:
        row = await s.get(Quarantine, quarantine_id)
        if row:
            _quarantine.pop((row.provider, row.service_code, row.country_code,
                             row.supplier_id or ""), None)
            await s.delete(row)
            await s.commit()


async def drop_tiers(offer: Offer, up_to: Decimal, reason: str = "", minutes: int | None = None) -> None:
    """Поставщик сказал «нет номеров» (или ответил ошибкой) до такой цены — прячем эти уровни
    ИМЕННО этого поставщика этой площадки для этой пары сервис+страна, даже если следующий прайс
    снова их вернёт, на quarantine_minutes (или minutes). Другие поставщики и уровни дороже up_to
    не затрагиваются."""
    key = offer.qkey
    now = utcnow()
    until = now + timedelta(minutes=minutes if minutes is not None else st.int_("quarantine_minutes"))
    async with Session() as s:
        row = (await s.scalars(select(Quarantine).where(
            Quarantine.provider == key[0], Quarantine.service_code == key[1],
            Quarantine.country_code == key[2], Quarantine.supplier_id == key[3]))).first()
        if row and row.until > now:  # действующий карантин только расширяем и продлеваем
            up_to, until = max(up_to, row.up_to), max(until, row.until)
        if row:
            row.up_to, row.until, row.reason = up_to, until, reason[:255]
        else:
            s.add(Quarantine(provider=key[0], service_code=key[1], country_code=key[2],
                             supplier_id=key[3], supplier=offer.supplier, up_to=up_to,
                             until=until, reason=reason[:255]))
        await s.commit()
    _quarantine[key] = (up_to, until)
    offer.tiers = [t for t in offer.tiers if t[0] > up_to]
    if not offer.tiers:
        for offers in _provider_offers.get(offer.provider, {}).values():
            if offer in offers:
                offers.remove(offer)
                break
    invalidate()


SERVICE_BLOCK_MINUTES = 60
NEVER = Decimal("999999")  # «все цены» для карантина


async def block_service(provider: str, service_code: str, reason: str) -> None:
    """Площадка закрыла нам сервис (providers.base.ServiceBlocked) — прячем все его предложения
    у этой площадки во всех странах на SERVICE_BLOCK_MINUTES (видно на странице «Карантин»)."""
    log.error("%s закрыла нам сервис %s: %s", provider, service_code, reason)
    offers = [o for items in _provider_offers.get(provider, {}).values() for o in items
              if o.service_code == service_code]
    for o in offers:
        await drop_tiers(o, NEVER, f"площадка закрыла сервис: {reason}", SERVICE_BLOCK_MINUTES)


# ---------------------------------------------------------------- свежие данные пары

LIVE_TIMEOUT = 5  # сек: если площадка отвечает дольше — показываем цену из общего прайса


async def refresh_pair(service_id: int, country_id: int) -> None:
    """Свежие цены и остатки поставщиков выбранной пары у всех площадок.
    Общий прайс обновляется раз в несколько минут, а у площадок (особенно SMSPool) цены
    и остатки меняются чаще — без этого пользователь видит цену, по которой купить уже нельзя."""

    async def one(name: str, codes: tuple[str, str]) -> None:
        provider = PROVIDERS[name]
        try:
            items = await asyncio.wait_for(provider.get_pair(*codes), LIVE_TIMEOUT)
        except Exception as e:
            log.info("свежая цена %s %s/%s: %s", name, *codes, e)
            return
        fresh: dict[tuple[int, int], list[Offer]] = {}
        _make_offers(name, items, {codes[0]: service_id}, {codes[1]: country_id}, fresh)
        _provider_offers.setdefault(name, {})[(service_id, country_id)] = \
            fresh.get((service_id, country_id), [])

    jobs = [one(name, codes) for name in enabled()
            if (codes := _pair_codes.get(name, {}).get((service_id, country_id)))]
    if jobs:
        await asyncio.gather(*jobs)
        for name in enabled():
            _rebuild_index(name)


def _limit(provider: str) -> Decimal:
    return balances.get(provider, Decimal("Infinity"))


# ---------------------------------------------------------------- котировка


MAX_LEVELS = 6  # кнопок цены на экране активации
LEVEL_STEP = Decimal("1.2")  # соседние уровни отличаются минимум на 20%


def _rate(offer: Offer, service_id: int, country_id: int) -> float:
    """Надёжность поставщика — только по нашей статистике (оценкам площадок не доверяем)."""
    return quality.reliability(offer.provider, offer.supplier, service_id, country_id)


def blocked_countries() -> set[str]:
    """Запрещённые страны из настроек (ISO2), например {"RU", "UA"}."""
    return {x.strip().upper()[:2] for x in st.raw("blocked_countries").split(",") if x.strip()}


def country_allowed(service_id: int, country_id: int) -> bool:
    """Можно ли продавать пару: сервис и страна не скрыты (в т.ч. запрещённые страны),
    и страна разрешена для сервиса (у некоторых сервисов регистрация только в своих странах).
    Проверяется и при показе, и при покупке — подделанная кнопка не обойдёт запрет."""
    svc, country = SERVICES.get(service_id), COUNTRIES.get(country_id)
    if not svc or svc.is_hidden or not country or country.is_hidden:
        return False
    allowed = ALLOWED.get(service_id)
    return allowed is None or country_id in allowed


def _offers(service_id: int, country_id: int) -> list[Offer]:
    """Предложения включённых площадок, на которые хватает нашего баланса."""
    if not country_allowed(service_id, country_id):
        return []
    result = []
    for name in enabled():
        limit = _limit(name)
        for o in _provider_offers.get(name, {}).get((service_id, country_id), []):
            if o.tiers and o.cost_usd <= limit:
                result.append(o)
    return sorted(result, key=lambda o: o.cost_usd)


def _level_prices(offers: list[Offer]) -> list[tuple[Decimal, Decimal]]:
    """[(цена ₽, предел закупки USD)]: не больше MAX_LEVELS, соседние отличаются хотя бы
    в LEVEL_STEP раз. Пропущенные цены не теряются — их поставщики входят в уровень дороже."""
    caps: dict[Decimal, Decimal] = {}  # цена ₽ -> самая дорогая закупка с такой ценой
    for cost in sorted({p for o in offers for p, _ in o.tiers if p <= _limit(o.provider)}):
        caps[pricing.sale_price(cost)] = cost
    prices = list(caps)
    chosen: list[Decimal] = []
    for i, price in enumerate(prices):
        if not chosen or price >= chosen[-1] * LEVEL_STEP or i == len(prices) - 1:
            chosen.append(price)
        if len(chosen) == MAX_LEVELS:
            break
    return [(p, caps[p]) for p in chosen]


def quote(service_id: int, country_id: int) -> Quote | None:
    offers = _offers(service_id, country_id)
    rates = {id(o): _rate(o, service_id, country_id) for o in offers}
    levels = []
    for price, cap in _level_prices(offers):
        cands, count = [], 0
        for o in offers:
            limit = min(cap, _limit(o.provider))
            n = sum(c for p, c in o.tiers if p <= limit)
            if n:
                count += n
                cands.append(Candidate(o, limit, rates[id(o)]))
        # сначала надёжные поставщики, среди равных — дороже: пользователь выбрал эту цену
        # ради надёжности, а самые дешёвые строки прайса чаще всего «пустые»
        cands.sort(key=lambda c: (-c.rate, -c.offer.cost_usd))
        levels.append(Level(price, cap, count, cands))
    return Quote(levels) if levels else None


def _spread(prices: list[Decimal]) -> list[Decimal]:
    """До MAX_LEVELS цен от первой до последней примерно равными шагами (не меньше LEVEL_STEP)."""
    if len(prices) <= 1:
        return prices
    lo, hi = prices[0], prices[-1]
    step = max(LEVEL_STEP, Decimal(str(float(hi / lo) ** (1 / (MAX_LEVELS - 1)))))
    chosen = [lo]
    for p in prices[1:-1]:
        if len(chosen) == MAX_LEVELS - 1:
            break
        if p >= chosen[-1] * step and p * LEVEL_STEP <= hi:
            chosen.append(p)
    return chosen + [hi]


AUTO_MIN_CANDIDATES = 5


def auto_levels(service_id: int) -> list[AutoLevel]:
    """Режим «Авто»: ценовые категории по всем странам сервиса. На каждой кнопке бот берёт самого
    надёжного поставщика любой страны не дороже этой цены, если у него пусто — следующего.
    Категории — от самой дешёвой активации до цены, где уже есть самый надёжный поставщик
    (но не ниже типичной цены, иначе без статистики осталась бы одна дешёвая кнопка). Категория,
    где бот выбрал бы того же поставщика, что и в более дешёвой, не показывается."""
    offers, rates, caps = _auto_market(service_id)
    if not offers:
        return []
    best = max(rates.values())
    top = min(pricing.sale_price(o.cost_usd) for _, o in offers if rates[id(o)] >= best)
    prices = sorted(pricing.sale_price(o.cost_usd) for _, o in offers)
    typical = prices[len(prices) // 2]
    # в самой дешёвой категории — хотя бы AUTO_MIN_CANDIDATES поставщиков: если первый не выдаст
    # номер (пусто, ошибка площадки), бот сразу возьмёт следующего
    floor = prices[min(AUTO_MIN_CANDIDATES, len(prices)) - 1]
    levels: list[AutoLevel] = []
    for price in _spread(sorted(p for p in caps if floor <= p <= max(top, typical, floor))):
        lv = _auto_level(offers, rates, price, caps[price])
        if levels and lv.candidates[0][1].offer is levels[-1].candidates[0][1].offer:
            continue  # тот же выбор, что дешевле, — платить больше незачем
        levels.append(lv)
    return levels


def auto_choices(service_id: int) -> list[AutoLevel]:
    """Категории «Авто», которые видит пользователь (бот, сайт, API): без самой дешёвой — она самая ненадёжная,
    но остаётся ориентиром в auto_levels (нижняя граница категорий). Категория одна — показывается она."""
    levels = auto_levels(service_id)
    return levels[1:] if len(levels) > 1 else levels


def auto_level_at(service_id: int, price: Decimal) -> AutoLevel | None:
    """Покупка в «Авто» за price ₽: все поставщики любой страны, чья цена не выше (категории на
    карточке к этому времени могли сдвинуться — прайс обновляется каждые несколько минут). Так же
    берётся замена — такая же по цене."""
    offers, rates, caps = _auto_market(service_id)
    fitting = [cost for p, cost in caps.items() if p <= price]
    return _auto_level(offers, rates, price, max(fitting)) if fitting else None


def _auto_market(service_id: int):
    """-> ([(страна, предложение)], {id(предложения): надёжность}, {цена ₽: самая дорогая закупка})."""
    cids: set[int] = set()
    for name in enabled():
        cids |= _index.get(name, {}).get(service_id, set())
    offers = [(cid, o) for cid in cids for o in _offers(service_id, cid)]
    rates = {id(o): _rate(o, service_id, cid) for cid, o in offers}
    caps: dict[Decimal, Decimal] = {}
    for cost in sorted({p for _, o in offers for p, _ in o.tiers if p <= _limit(o.provider)}):
        caps[pricing.sale_price(cost)] = cost
    return offers, rates, caps


def _auto_level(offers, rates, price: Decimal, cap: Decimal) -> AutoLevel:
    cands = [(cid, Candidate(o, min(cap, _limit(o.provider)), rates[id(o)]))
             for cid, o in offers if o.cost_usd <= min(cap, _limit(o.provider))]
    # как в quote: сначала надёжные, среди равных — дороже
    cands.sort(key=lambda x: (-x[1].rate, -x[1].offer.cost_usd))
    return AutoLevel(price, cap, cands)


def _visible_service_ids() -> set[int]:
    global _visible_cache
    if _visible_cache is not None:
        return _visible_cache
    ids: set[int] = set()
    for name in enabled():
        limit = _limit(name)
        offers = _provider_offers.get(name, {})
        for sid, cids in _index.get(name, {}).items():
            if sid in ids or sid not in SERVICES or SERVICES[sid].is_hidden:
                continue
            for cid in cids:
                if cid in COUNTRIES and not COUNTRIES[cid].is_hidden \
                        and country_allowed(sid, cid) and any(
                        o.tiers and o.cost_usd <= limit for o in offers.get((sid, cid), [])):
                    ids.add(sid)
                    break
    _visible_cache = ids
    return ids


def popular_services() -> list[ServiceMeta]:
    ids = _visible_service_ids()
    return sorted((SERVICES[i] for i in ids if SERVICES[i].is_popular),
                  key=lambda x: (x.sort, x.name.lower()))


# ---------------------------------------------------------------- популярность

POPULARITY_DAYS = 30
# service_id -> активаций за POPULARITY_DAYS дней (включая отменённые: это спрос, а не качество)
popularity: dict[int, int] = {}
# (service_id, country_id) -> активаций за те же дни: порядок стран в боте
country_popularity: dict[tuple[int, int], int] = {}


async def refresh_popularity() -> None:
    since = utcnow() - timedelta(days=POPULARITY_DAYS)
    async with Session() as s:
        rows = (await s.execute(select(Order.service_id, Order.country_id, func.count())
                                .where(Order.created_at >= since)
                                .group_by(Order.service_id, Order.country_id))).all()
    by_service: dict[int, int] = {}
    for sid, _cid, n in rows:
        by_service[sid] = by_service.get(sid, 0) + n
    popularity.clear()
    popularity.update(by_service)
    country_popularity.clear()
    country_popularity.update({(sid, cid): n for sid, cid, n in rows})


def all_services() -> list[ServiceMeta]:
    """Все сервисы с номерами: сначала популярные (больше активаций), дальше по алфавиту."""
    return sorted((SERVICES[i] for i in _visible_service_ids()),
                  key=lambda x: (-popularity.get(x.id, 0), x.name.lower()))


def other_services() -> list[ServiceMeta]:
    """Экран «Все сервисы»: без популярных — они уже в главном списке."""
    return [x for x in all_services() if not x.is_popular]


def search_services(q: str) -> list[ServiceMeta]:
    q = q.strip().lower()
    nq = _norm(q)
    found = [x for x in all_services()
             if q in x.name.lower() or (nq and nq in _norm(x.name))
             or any(w.startswith(q) for w in RU_SEARCH.get(x.key, "").split())]
    return sorted(found, key=lambda x: (not x.name.lower().startswith(q), x.name.lower()))


def countries_for(service_id: int) -> list[tuple[CountryMeta, Quote]]:
    """[(страна, котировка)] по популярности: сколько активаций сервиса брали в этой стране за
    POPULARITY_DAYS дней; при равной (чаще всего — нет данных) дороже выше: дорогие обычно надёжнее.
    Только то, что реально можно купить."""
    cids: set[int] = set()
    for name in enabled():
        cids |= _index.get(name, {}).get(service_id, set())
    rows = []
    for cid in cids:
        if cid not in COUNTRIES or COUNTRIES[cid].is_hidden:
            continue
        q = quote(service_id, cid)
        if q and q.count > 0:
            rows.append((COUNTRIES[cid], q))
    return sorted(rows, key=lambda r: (-country_popularity.get((service_id, r[0].id), 0),
                                       -r[1].price, -r[1].cost_usd, r[0].name))


def stats() -> dict[str, int]:
    return {name: sum(len(v) for v in offers.values())
            for name, offers in _provider_offers.items()}

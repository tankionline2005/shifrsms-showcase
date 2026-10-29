"""Время в панели — в часовом поясе того, кто её открыл.

В базе всё хранится в UTC (naive datetime, `utcnow()`). Браузер кладёт свой пояс в cookie `tz`
(`Intl.DateTimeFormat().resolvedOptions().timeZone`, скрипт в base.html), middleware кладёт его в contextvar
на время запроса, а шаблоны (фильтр `msk`, исторически так названный) и расчёты «сегодня / по дням» берут его
отсюда. Нет cookie или пояс неизвестен — московское время. Бот для пользователей пишет время по-прежнему в МСК."""

from contextvars import ContextVar
from datetime import date, datetime, time, timedelta, timezone
from http.cookies import SimpleCookie
from urllib.parse import unquote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT = ZoneInfo("Europe/Moscow")
COOKIE = "tz"
_zone: ContextVar[ZoneInfo] = ContextVar("panel_tz", default=DEFAULT)


def parse(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name) if name and len(name) < 64 else DEFAULT
    except (ZoneInfoNotFoundError, ValueError):
        return DEFAULT


def zone() -> ZoneInfo:
    return _zone.get()


def local(dt: datetime) -> datetime:
    """UTC из базы -> время в поясе смотрящего (без tzinfo — для сравнения и форматирования)."""
    return dt.replace(tzinfo=timezone.utc).astimezone(zone()).replace(tzinfo=None)


def fmt(dt: datetime | None, pattern: str = "%d.%m.%Y %H:%M") -> str:
    return local(dt).strftime(pattern) if dt else ""


def today() -> date:
    return local(datetime.now(timezone.utc).replace(tzinfo=None)).date()


def day_start_utc(day: date) -> datetime:
    """Полночь дня day в поясе смотрящего -> UTC (naive, как в базе)."""
    return datetime.combine(day, time.min, tzinfo=zone()).astimezone(timezone.utc).replace(tzinfo=None)


def label() -> str:
    """Подпись пояса в заголовках таблиц: «МСК» или «UTC+7»."""
    z = zone()
    if z.key == "Europe/Moscow":
        return "МСК"
    off = datetime.now(z).utcoffset() or timedelta(0)
    minutes = int(off.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "−"
    h, m = divmod(abs(minutes), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "") if minutes else "UTC"


class Middleware:
    """ASGI: пояс из cookie — в contextvar на время запроса."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        raw = next((v.decode("latin-1") for k, v in scope.get("headers", []) if k == b"cookie"), "")
        name = None
        if raw:
            jar = SimpleCookie()
            try:
                jar.load(raw)
            except Exception:
                jar = {}
            if COOKIE in jar:
                name = unquote(jar[COOKIE].value)
        token = _zone.set(parse(name))
        try:
            await self.app(scope, receive, send)
        finally:
            _zone.reset(token)

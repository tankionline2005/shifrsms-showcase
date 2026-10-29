"""Лимит неудачных попыток входа (кабинет партнёра, панель): по адресу посетителя и по логину отдельно.
В памяти: после перезапуска счётчики обнуляются — это не страшно, пауза короткая."""

import time

from fastapi import Request

FAILS, WINDOW = 10, 15 * 60  # не больше 10 неудачных попыток за 15 минут
_fails: dict[str, list[float]] = {}


def ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _recent(key: str) -> list[float]:
    now = time.monotonic()
    hits = [t for t in _fails.get(key, []) if now - t < WINDOW]
    if hits:
        _fails[key] = hits
    else:
        _fails.pop(key, None)
    return hits


def blocked(*keys: str) -> bool:
    """Слишком много неудачных попыток по любому из ключей (адрес, логин)?"""
    return any(len(_recent(k)) >= FAILS for k in keys)


def failed(*keys: str) -> None:
    now = time.monotonic()
    for k in keys:
        _fails.setdefault(k, []).append(now)
    if len(_fails) > 50000:
        _fails.clear()


MESSAGE = "Слишком много неудачных попыток входа — попробуйте через 15 минут."


_hits: dict[str, list[float]] = {}


def allow(key: str, n: int, window: float) -> bool:
    """Не больше n событий за window секунд по ключу (регистрации с адреса, запросы API и т. п.)."""
    now = time.monotonic()
    hits = [t for t in _hits.get(key, []) if now - t < window]
    if len(hits) >= n:
        _hits[key] = hits
        return False
    hits.append(now)
    _hits[key] = hits
    if len(_hits) > 100000:
        _hits.clear()
    return True

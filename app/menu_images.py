"""Картинки-оформление пунктов главного меню бота: картинка приходит вместе с текстом раздела
(подписью к ней). Загружаются в «Настройках» панели, в памяти — копия для быстрой отправки."""

from dataclasses import dataclass

from sqlalchemy import delete, select, update

from app import thumbs
from app.db import Session, utcnow
from app.models import MenuImage

# ключ -> название пункта меню (тексты кнопок — app/bot/ui.py)
ITEMS = {"buy": "📱 Получить СМС", "web": "🌐 Веб-версия", "info": "ℹ️ Информация", "profile": "👤 Профиль",
         "balance": "💰 Баланс", "orders": "📋 Мои заказы", "invite": "👥 Пригласить друга"}


@dataclass
class Image:
    photo: bytes
    photo_type: str
    file_id: str | None
    thumb: bytes | None = None  # превью для панели


_cache: dict[str, Image] = {}
_unique: dict[str, str] = {}  # ключ -> file_unique_id в Telegram: какая картинка сейчас на экране


async def load() -> None:
    async with Session() as s:
        rows = (await s.scalars(select(MenuImage))).all()
    _cache.clear()
    _cache.update({r.key: Image(r.photo, r.photo_type, r.file_id, r.thumb) for r in rows})


def get(key: str) -> Image | None:
    return _cache.get(key)


def unique_id(key: str) -> str | None:
    return _unique.get(key)


async def put(key: str, photo: bytes, photo_type: str, thumb: bytes | None) -> None:
    async with Session() as s:
        row = await s.get(MenuImage, key)
        if row is None:
            row = MenuImage(key=key)
            s.add(row)
        row.photo, row.photo_type, row.file_id, row.thumb, row.updated_at = photo, photo_type, None, thumb, utcnow()
        await s.commit()
    _cache[key] = Image(photo, photo_type, None, thumb)
    _unique.pop(key, None)


async def thumb(key: str) -> bytes | None:
    """Превью картинки; у загруженных до появления превью — делается сейчас и сохраняется."""
    img = _cache.get(key)
    if not img:
        return None
    if img.thumb is None:
        try:
            img.thumb = await thumbs.make(img.photo)
        except thumbs.BadImage:
            return None
        async with Session() as s:
            await s.execute(update(MenuImage).where(MenuImage.key == key).values(thumb=img.thumb))
            await s.commit()
    return img.thumb


async def remove(key: str) -> None:
    async with Session() as s:
        await s.execute(delete(MenuImage).where(MenuImage.key == key))
        await s.commit()
    _cache.pop(key, None)
    _unique.pop(key, None)


async def remember_file_id(key: str, file_id: str, unique: str | None = None) -> None:
    """Картинка загружена в Telegram — дальше отправляем по file_id, без загрузки файла."""
    img = _cache.get(key)
    if img and unique:
        _unique[key] = unique
    if not img or img.file_id == file_id:
        return
    img.file_id = file_id
    async with Session() as s:
        await s.execute(update(MenuImage).where(MenuImage.key == key).values(file_id=file_id))
        await s.commit()

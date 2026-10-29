"""Превью загруженных картинок для панели: в интерфейсе они маленькие, так что вместо исходника
(до 10 МБ) страница грузит лёгкое WEBP. Исходник открывается по нажатию — на весь экран."""

import asyncio
import hashlib
import io

from PIL import Image, ImageOps

SIDE = 640  # px по длинной стороне — с запасом для экранов с двойной плотностью
QUALITY = 72


class BadImage(ValueError):
    pass


def _make(data: bytes) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as im:
            im = ImageOps.exif_transpose(im)  # фото с телефона — с учётом поворота
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA" if "transparency" in im.info or im.mode in ("LA", "PA") else "RGB")
            im.thumbnail((SIDE, SIDE))
            out = io.BytesIO()
            im.save(out, "WEBP", quality=QUALITY, method=4)
            return out.getvalue()
    except Exception as e:  # битый файл или не картинка
        raise BadImage("Не получилось прочитать картинку — возможно, файл повреждён.") from e


async def make(data: bytes) -> bytes:
    """-> превью WEBP. Сжатие — в отдельном потоке, чтобы не тормозить бота."""
    return await asyncio.to_thread(_make, data)


def etag(data: bytes) -> str:
    return '"' + hashlib.md5(data).hexdigest() + '"'


def version(data: bytes | None) -> str:
    """Короткая метка содержимого — в адрес картинки, чтобы после замены браузер не показал старую."""
    return hashlib.md5(data).hexdigest()[:10] if data else ""

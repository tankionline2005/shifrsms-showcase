"""Страницы «Каналы» — общие для админки и кабинета партнёра (/partner-dashboard).

register() вешает их на роутер; scope(request) — какие каналы видит текущий вход: None — все
(админ), {chat_id} — только своя связка (реферал). Чужую связку по прямой ссылке реферал не откроет."""

import secrets
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import func, select, update

from app import referrals, textfmt, thumbs
from app.bot import channels
from app.bot.ui import instruction_url
from app.db import Session
from app.models import BotChat, ChannelMessage, Referral
from app.web import tables

CHAT_TYPES = {"channel": "Канал", "group": "Группа", "supergroup": "Группа"}
STATUS_TITLES = {"creator": "владелец", "administrator": "администратор", "member": "участник",
                 "restricted": "ограничен", "left": "не состоит", "kicked": "удалён"}
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_PHOTO = 10 * 1024 * 1024  # лимит Telegram для фото

DRAFT_TTL, MAX_DRAFTS = 24 * 3600, 30  # фото, загруженное заранее, ждёт «Добавить» / «Отправить» (в памяти)
DRAFT_LOST = "Фото не сохранилось (сервер перезапускался или прошло много времени) — прикрепите его заново."

FIRST_OTHERS, MORE_STEP = 10, 10  # прочих связок: сразу / за одну подгрузку при прокрутке

Scope = Callable[[Request], set[int] | None]
Render = Callable[..., Response]


def _pair(channel: BotChat, group: BotChat | None, count: int, bot_name: str) -> dict:
    """Связка канал + группа обсуждения и что мешает комментариям."""
    hints = []
    if not channel.linked_id:
        hints.append("У канала нет группы обсуждения: в настройках канала включите «Обсуждение» "
                     "и привяжите группу — комментарии пишутся в неё.")
    elif not group or group.status not in channels.ADMIN:
        where = f"«{group.title}»" if group else "обсуждения"
        what = "сделайте его администратором" if group and group.status in ("member", "restricted") \
            else f"добавьте {bot_name} администратором"
        hints.append(f"Бот не видит новые посты: в группе {where} {what} (достаточно права «Отправка сообщений»).")
    if channel.comments_on and not count:
        hints.append("Комментарии включены, но нет ни одного сообщения.")
    return {"channel": channel, "group": group, "hints": hints, "count": count,
            "ready": bool(channel.comments_on and count and not hints)}


def add_bot_url(username: str | None) -> str | None:
    """Ссылка «добавить бота в группу»: Telegram сам предложит выбрать группу и сразу сделает бота
    администратором с минимальными правами (manage_chat — есть у любого администратора)."""
    return f"https://t.me/{username}?startgroup=&admin=manage_chat" if username else None


def steps(p: dict, name: str, username: str | None) -> list[dict]:
    """Шаги настройки канала для кабинета партнёра: что сделано и что осталось, чтобы бот комментировал посты.
    title — что сделать, sub — подсказка, url / url_title — кнопка-ссылка, page — кнопка на страницу панели."""
    ch, g = p["channel"], p["group"]
    bot_in = bool(g and g.status in channels.ADMIN)
    if g and g.status in ("member", "restricted"):
        bot_title = f"Сделайте {name} администратором в группе «{g.title}»"
    else:
        bot_title = f"Добавьте {name} администратором в группу обсуждения"
    return [
        {"done": True, "title": "Канал подключён к партнёрской программе"},
        {"done": bool(ch.linked_id), "title": "Включите в канале «Обсуждение» и привяжите группу",
         "sub": "Настройки канала → Обсуждение. Комментарии бота пишутся в эту группу."},
        {"done": bot_in, "title": bot_title,
         "sub": "Кнопка откроет Telegram: выберите группу обсуждения канала и подтвердите права администратора.",
         "url": None if bot_in else add_bot_url(username), "url_title": "Добавить бота в группу"},
        {"done": p["count"] > 0, "title": "Добавьте хотя бы одно сообщение для комментариев",
         "sub": "Бот оставляет под каждым новым постом одно из ваших сообщений с кнопкой, ведущей в бота.",
         "page": "messages", "page_title": "Добавить сообщение"},
        {"done": bool(ch.comments_on), "title": "Включите «Комментировать новые посты»",
         "sub": "Переключатель — в блоке «Комментарии под постами» ниже."},
    ]


async def bot_name() -> str:
    return f"@{(await channels.bot.me()).username}" if channels.bot else "бота"


async def pairs(only: set[int] | None) -> tuple[list[dict], list[BotChat]]:
    """-> (связки канал + обсуждение, прочие чаты бота). only — показать только эти каналы."""
    async with Session() as s:
        chats = {c.id: c for c in (await s.scalars(select(BotChat).order_by(BotChat.title))).all()}
        counts = dict((await s.execute(select(ChannelMessage.chat_id, func.count())
                                       .group_by(ChannelMessage.chat_id))).all())
        refs = {r.chat_id: r for r in (await s.scalars(select(Referral))).all()}
    name = await bot_name()
    username = name.lstrip("@") if name.startswith("@") else None
    result, used = [], set()
    for c in chats.values():
        if c.type != "channel" or (only is not None and c.id not in only):
            continue
        p = _pair(c, chats.get(c.linked_id) if c.linked_id else None, counts.get(c.id, 0), name)
        p["ref"] = refs.get(c.id)
        p["ref_info"] = await referrals.summary(p["ref"].id) if p["ref"] else None
        # реферальная ссылка — та же, что в кнопке под постами канала
        p["ref_link"] = (f"https://t.me/{username}?start={referrals.START_PREFIX}{p['ref'].code}"
                         if p["ref"] and username else None)
        # порядок групп: комментирует, но не реферал — рефералы — все остальные
        p["group_n"] = 1 if p["ref"] else 0 if c.comments_on else 2
        p["steps"] = steps(p, name, username)
        p["left"] = sum(not s["done"] for s in p["steps"])
        result.append(p)
        used |= {c.id, c.linked_id}
    result.sort(key=lambda p: (p["group_n"], p["channel"].title.lower()))
    others = [c for c in chats.values() if c.id not in used] if only is None else []
    return result, others


async def _read_photo(upload: UploadFile | None) -> tuple[bytes | None, str | None, str]:
    """-> (байты, тип, ошибка). Без файла — (None, None, "")."""
    if upload is None or not upload.filename:
        return None, None, ""
    data = await upload.read(MAX_PHOTO + 1)
    if len(data) > MAX_PHOTO:
        return None, None, "Фото больше 10 МБ — Telegram такое не примет."
    if upload.content_type not in PHOTO_TYPES:
        return None, None, "Подойдёт фото в формате JPG, PNG или WEBP."
    return data, upload.content_type, ""


async def read_image(upload: UploadFile | None) -> tuple[bytes | None, str | None, bytes | None, str]:
    """-> (байты, тип, превью, ошибка): как _read_photo, плюс превью — заодно проверка, что это картинка."""
    data, kind, error = await _read_photo(upload)
    if error or data is None:
        return None, None, None, error or "Выберите картинку."
    try:
        return data, kind, await thumbs.make(data), ""
    except thumbs.BadImage as e:
        return None, None, None, str(e)


def image_response(request: Request, data: bytes, media_type: str) -> Response:
    """Картинку можно заменить, а адрес у неё один: браузер каждый раз сверяет отпечаток (ETag) и
    скачивает заново, только если картинка поменялась — иначе после замены показывал бы старую."""
    etag = thumbs.etag(data)
    headers = {"Cache-Control": "private, no-cache", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(data, media_type=media_type, headers=headers)


def photo_urls(base: str, thumb: bytes) -> dict:
    """Адреса превью и исходника для страницы; метка версии — чтобы браузер сразу показал новую картинку."""
    v = thumbs.version(thumb)
    return {"thumb": f"{base}?size=thumb&v={v}", "full": f"{base}?v={v}"}


@dataclass
class Draft:
    chat_id: int
    photo: bytes
    photo_type: str
    thumb: bytes
    at: float


_drafts: dict[str, Draft] = {}


def _put_draft(d: Draft) -> str:
    now = time.monotonic()
    for k in [k for k, v in _drafts.items() if now - v.at > DRAFT_TTL]:
        del _drafts[k]
    while len(_drafts) >= MAX_DRAFTS:
        del _drafts[next(iter(_drafts))]
    token = secrets.token_urlsafe(12)
    _drafts[token] = d
    return token


def _button(raw: str) -> str | None:
    """Текст кнопки под сообщением: одна строка, без лишних пробелов. Пусто — стандартный (channels.BUTTON)."""
    text = " ".join((raw or "").split())[:channels.MAX_BUTTON]
    return None if not text or text == channels.BUTTON else text


def _url(raw: str) -> tuple[str | None, str]:
    """Ссылка кнопки из формы -> (адрес или None — по умолчанию, ошибка). Подходят https://…, t.me/…, @username."""
    raw = (raw or "").strip()
    if not raw:
        return None, ""
    url = instruction_url(raw)
    if not url or " " in url or len(url) > 512:
        return None, "Ссылка кнопки — адрес вида https://…, t.me/… или @username."
    return url, ""


def _check(text: str, has_photo: bool) -> str:
    if not text and not has_photo:
        return "Сообщение пустое — добавьте текст или фото."
    limit, n = (channels.MAX_CAPTION if has_photo else channels.MAX_TEXT), textfmt.visible_len(text)
    if n > limit:  # Telegram считает видимый текст, без тегов оформления
        return (f"Подпись к фото — не длиннее {limit} символов (сейчас {n})." if has_photo
                else f"Текст — не длиннее {limit} символов (сейчас {n}).")
    return ""


def register(router: APIRouter, prefix: str, guard, scope: Scope, render: Render,
             extra: Callable[[Request], Awaitable[dict]] | None = None) -> None:
    """Страницы каналов на router. prefix — начало адресов (для редиректов), guard — зависимость
    «вошёл ли», extra(request) — дополнительный контекст страницы списка (например, разовый пароль)."""
    deps = [Depends(guard)]

    def allowed(request: Request, chat_id: int) -> bool:
        only = scope(request)
        return only is None or chat_id in only

    def to_list(error: str = "") -> RedirectResponse:
        return RedirectResponse(f"{prefix}/channels" + (f"?error={quote_plus(error)}" if error else ""),
                                status_code=303)

    def to_messages(chat_id: int, error: str = "", saved: str = "") -> RedirectResponse:
        q = "&".join(f"{k}={quote_plus(v)}" for k, v in (("error", error), ("saved", saved)) if v)
        return RedirectResponse(f"{prefix}/channels/{chat_id}/messages" + (f"?{q}" if q else ""), status_code=303)

    def card_ctx() -> dict:
        return {"types": CHAT_TYPES, "statuses": STATUS_TITLES, "admin": channels.ADMIN,
                "max_messages": channels.MAX_MESSAGES, "button": channels.BUTTON}

    @router.get("/channels", dependencies=deps)
    async def channels_page(request: Request, error: str = ""):
        items, others = await pairs(scope(request))
        head = [p for p in items if p["group_n"] < 2]  # комментирующие и рефералы — всегда целиком
        rest = [p for p in items if p["group_n"] == 2]
        ctx = await extra(request) if extra else {}
        return render(request, "channels.html", pairs=head + rest[:FIRST_OTHERS], others=others,
                      bot_name=await bot_name(), error=error, more_total=len(rest), shown=FIRST_OTHERS,
                      step=MORE_STEP, **card_ctx(), **ctx)

    @router.get("/channels/more", dependencies=deps)
    async def channels_more(request: Request, offset: int = 0):
        """Следующие прочие связки — подгружаются, когда долистали до конца страницы."""
        items, _ = await pairs(scope(request))
        rest = [p for p in items if p["group_n"] == 2]
        return render(request, "_channel_cards.html", pairs=rest[max(0, offset):max(0, offset) + MORE_STEP],
                      **card_ctx())

    @router.post("/channels/{chat_id}", dependencies=deps)
    async def channel_toggle(request: Request, chat_id: int, comments_on: str | None = Form(None)):
        if allowed(request, chat_id):
            async with Session() as s:
                row = await s.get(BotChat, chat_id)
                if row and row.type == "channel":
                    row.comments_on = bool(comments_on)
                    await s.commit()
        return to_list()

    def template_ok(request: Request, chat_id: int) -> bool:
        """Шаблон сообщения — только в админке (у кабинета реферала scope ограничен его каналами)."""
        return chat_id == channels.TEMPLATE and scope(request) is None

    @router.get("/channels/{chat_id}/messages", dependencies=deps)
    async def channel_messages(request: Request, chat_id: int, error: str = "", saved: str = ""):
        is_template = template_ok(request, chat_id)
        if is_template:
            channel = BotChat(id=channels.TEMPLATE, type="channel", title="Шаблон сообщения")
        else:
            async with Session() as s:
                channel = await s.get(BotChat, chat_id)
            if not channel or channel.type != "channel" or not allowed(request, chat_id):
                return to_list()
        me = await channels.bot.me() if channels.bot else None
        return render(request, "channel_messages.html", channel=channel, items=await channels.messages_of(chat_id),
                      is_referral=not is_template and await referrals.by_chat(chat_id) is not None,
                      is_template=is_template,
                      bot_url=f"https://t.me/{me.username}" if me else "https://t.me/…", error=error, saved=saved,
                      max_messages=1 if is_template else channels.MAX_MESSAGES, button=channels.BUTTON,
                      max_button=channels.MAX_BUTTON,
                      max_text=channels.MAX_TEXT, max_caption=channels.MAX_CAPTION)

    @router.post("/channels/{chat_id}/messages", dependencies=deps)
    async def channel_message_add(request: Request, chat_id: int, text: str = Form(""),
                                  photo: UploadFile | None = File(None), photo_token: str = Form(""),
                                  button_text: str = Form(""), button_url: str = Form("")):
        if not allowed(request, chat_id):
            return to_list()
        text = textfmt.clean(text)
        data, kind, error = await _read_photo(photo)
        thumb = None
        draft = _drafts.get(photo_token)
        if data is None and draft and draft.chat_id == chat_id:  # фото уже загружено заранее
            data, kind, thumb = draft.photo, draft.photo_type, draft.thumb
        elif data is None and photo_token:
            error = DRAFT_LOST
        error = error or _check(text, data is not None)
        url, url_error = _url(button_url)
        if await referrals.by_chat(chat_id):
            url = None  # у канала реферала кнопка всегда ведёт на его реферальную ссылку
        else:
            error = error or url_error
        is_template = template_ok(request, chat_id)
        async with Session() as s:
            channel = await s.get(BotChat, chat_id)
            count = await s.scalar(select(func.count()).select_from(ChannelMessage)
                                   .where(ChannelMessage.chat_id == chat_id))
            if not channel and not is_template:
                return to_list()
            if is_template and count:
                error = "Шаблон — одно сообщение: отредактируйте его или удалите и добавьте новое."
            elif count >= channels.MAX_MESSAGES:
                error = f"У канала уже {channels.MAX_MESSAGES} сообщений — удалите одно, чтобы добавить новое."
            if error:
                return to_messages(chat_id, error=error)
            if data is not None and thumb is None:
                try:
                    thumb = await thumbs.make(data)
                except thumbs.BadImage as e:
                    return to_messages(chat_id, error=str(e))
            s.add(ChannelMessage(chat_id=chat_id, text=text, photo=data, photo_type=kind, thumb=thumb,
                                 button_text=_button(button_text), button_url=url))
            await s.commit()
        _drafts.pop(photo_token, None)
        return to_messages(chat_id, saved="Сообщение добавлено.")

    async def own_message(request: Request, message_id: int) -> ChannelMessage | None:
        async with Session() as s:
            row = await s.get(ChannelMessage, message_id)
        return row if row and allowed(request, row.chat_id) else None

    @router.post("/channels/messages/{message_id}", dependencies=deps)
    async def channel_message_save(request: Request, message_id: int, text: str = Form(""),
                                   photo: UploadFile | None = File(None), remove_photo: str | None = Form(None),
                                   button_text: str = Form(""), button_url: str = Form("")):
        if not await own_message(request, message_id):
            return to_list()
        text = textfmt.clean(text)
        data, kind, error = await _read_photo(photo)
        async with Session() as s:
            row = await s.get(ChannelMessage, message_id)
            has_photo = data is not None or (bool(row.photo) and not remove_photo)
            error = error or _check(text, has_photo)
            if error:
                return to_messages(row.chat_id, error=error)
            is_ref = await referrals.by_chat(row.chat_id) is not None
            url, url_error = _url(button_url)
            if url_error and not is_ref:
                return to_messages(row.chat_id, error=url_error)
            row.text = text
            row.button_text = _button(button_text)
            if not is_ref:  # у канала реферала ссылку не меняем — всегда реферальная
                row.button_url = url
            if data is not None:  # новое фото — старый file_id больше не подходит
                row.photo, row.photo_type, row.photo_file_id, row.thumb = data, kind, None, None
            elif remove_photo:
                row.photo = row.photo_type = row.photo_file_id = row.thumb = None
            await s.commit()
            return to_messages(row.chat_id, saved="Сохранено.")

    @router.post("/channels/messages/{message_id}/delete", dependencies=deps)
    async def channel_message_delete(request: Request, message_id: int):
        row = await own_message(request, message_id)
        if not row:
            return to_list()
        async with Session() as s:
            await s.delete(await s.get(ChannelMessage, message_id))
            await s.commit()
        return to_messages(row.chat_id, saved="Сообщение удалено.")

    @router.get("/channels/messages/{message_id}/photo", dependencies=deps)
    async def channel_message_photo(request: Request, message_id: int, size: str = ""):
        """Фото сообщения: size=thumb — лёгкое превью для страницы, без него — исходник."""
        if size == "thumb":
            async with Session() as s:
                found = (await s.execute(select(ChannelMessage.chat_id, ChannelMessage.thumb)
                                         .where(ChannelMessage.id == message_id))).first()
            if found and found.thumb and allowed(request, found.chat_id):
                return image_response(request, found.thumb, "image/webp")
        row = await own_message(request, message_id)
        if not row or not row.photo:
            return Response(status_code=404)
        if size != "thumb":
            return image_response(request, row.photo, row.photo_type or "image/jpeg")
        try:  # загружено до появления превью — делаем сейчас
            thumb = await thumbs.make(row.photo)
        except thumbs.BadImage:
            return image_response(request, row.photo, row.photo_type or "image/jpeg")
        async with Session() as s:
            await s.execute(update(ChannelMessage).where(ChannelMessage.id == message_id).values(thumb=thumb))
            await s.commit()
        return image_response(request, thumb, "image/webp")

    @router.post("/channels/messages/{message_id}/photo", dependencies=deps)
    async def channel_message_photo_upload(request: Request, message_id: int, photo: UploadFile | None = File(None)):
        """Фото выбрали — сразу сохраняем (без кнопки «Сохранить»); ответ — JSON для страницы."""
        if not await own_message(request, message_id):
            return JSONResponse({"error": "Сообщение не найдено — обновите страницу."})
        data, kind, thumb, error = await read_image(photo)
        if error:
            return JSONResponse({"error": error})
        async with Session() as s:
            row = await s.get(ChannelMessage, message_id)
            if error := _check(row.text, True):
                return JSONResponse({"error": error + " Сократите текст и сохраните, потом добавьте фото."})
            row.photo, row.photo_type, row.photo_file_id, row.thumb = data, kind, None, thumb
            await s.commit()
        return JSONResponse(photo_urls(f"{prefix}/channels/messages/{message_id}/photo", thumb))

    @router.post("/channels/messages/{message_id}/photo/delete", dependencies=deps)
    async def channel_message_photo_delete(request: Request, message_id: int):
        if not await own_message(request, message_id):
            return JSONResponse({"error": "Сообщение не найдено — обновите страницу."})
        async with Session() as s:
            row = await s.get(ChannelMessage, message_id)
            if not row.text:
                return JSONResponse({"error": "Без фото сообщение останется пустым — сначала добавьте текст и сохраните."})
            row.photo = row.photo_type = row.photo_file_id = row.thumb = None
            await s.commit()
        return JSONResponse({"ok": True})

    @router.post("/channels/{chat_id}/photo-draft", dependencies=deps)
    async def channel_photo_draft(request: Request, chat_id: int, photo: UploadFile | None = File(None)):
        """Фото для нового сообщения: загружается сразу, а сообщение создаётся кнопкой «Добавить»."""
        if not allowed(request, chat_id):
            return JSONResponse({"error": "Канал не найден — обновите страницу."})
        data, kind, thumb, error = await read_image(photo)
        if error:
            return JSONResponse({"error": error})
        token = _put_draft(Draft(chat_id, data, kind, thumb, time.monotonic()))
        return JSONResponse({"token": token, **photo_urls(f"{prefix}/channels/photo-draft/{token}", thumb)})

    @router.get("/channels/photo-draft/{token}", dependencies=deps)
    async def channel_photo_draft_get(request: Request, token: str, size: str = ""):
        d = _drafts.get(token)
        if not d or not allowed(request, d.chat_id):
            return Response(status_code=404)
        return image_response(request, d.thumb, "image/webp") if size == "thumb" else \
            image_response(request, d.photo, d.photo_type)

    @router.get("/channels/{chat_id}/referral", dependencies=deps)
    async def channel_referral(request: Request, chat_id: int, error: str = "", saved: str = ""):
        ref = await referrals.by_chat(chat_id)
        async with Session() as s:
            channel = await s.get(BotChat, chat_id)
        if not ref or not channel or not allowed(request, chat_id):
            return to_list()
        return render(request, "channel_referral.html", channel=channel, ref=ref, kinds=referrals.KIND_TITLES,
                      info=await referrals.summary(ref.id), error=error, saved=saved,
                      t=await tables.ref_history_table(ref.id, referrals.KIND_TITLES))

    @router.get("/channels/{chat_id}/referral/history", dependencies=deps)
    async def channel_referral_history(request: Request, chat_id: int, q: str = "", page: int = 1):
        """Страница истории баланса реферала — для поиска и переключения страниц без перезагрузки."""
        ref = await referrals.by_chat(chat_id)
        if not ref or not allowed(request, chat_id):
            return Response(status_code=404)
        return render(request, "_tbl_ref_history.html", kinds=referrals.KIND_TITLES,
                      t=await tables.ref_history_table(ref.id, referrals.KIND_TITLES, q, page))


def money_usd(value: Decimal | None) -> str:
    return "$0.00" if not value else f"${value:,.2f}".replace(",", " ")


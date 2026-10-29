from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base, utcnow

Money = Numeric(12, 2)
UsdCost = Numeric(12, 4)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user id
    username: Mapped[str | None] = mapped_column(String(64))
    first_name: Mapped[str | None] = mapped_column(String(128))
    balance: Mapped[Decimal] = mapped_column(Money, default=Decimal("0"))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    country_sort: Mapped[str | None] = mapped_column(String(8))  # устарело: страны теперь только по популярности
    # реферал, по чьей ссылке человек впервые открыл бота — навсегда (app/referrals.py)
    referral_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # пользователь заблокировал бота (рассылка не дошла) — до следующего его сообщения боту
    bot_blocked: Mapped[bool | None] = mapped_column(Boolean, default=False)
    # UTM-метка ссылки, по которой человек впервые открыл бота (?start=utm_…, app/utm.py) — навсегда
    utm: Mapped[str | None] = mapped_column(String(64))
    # пригласивший пользователь (ссылка /r/<код> или ?start=u_<код>, app/user_refs.py) — навсегда
    inviter_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    # второй пригласивший — только после склейки двух аккаунтов, приглашённых разными людьми (app/shop_accounts.merge):
    # пригласивших не больше двух (каналы — referral_id/referral2_id, пользователи — inviter_id/inviter2_id), доля
    # с активации делится между ними поровну (app/user_refs.attributions)
    referral2_id: Mapped[int | None] = mapped_column(Integer, index=True)
    inviter2_id: Mapped[int | None] = mapped_column(BigInteger, index=True)
    ref_code: Mapped[str | None] = mapped_column(String(16), index=True)  # основной код (показывается); все — RefCode
    menu_ver: Mapped[int | None] = mapped_column(Integer)  # версия нижнего меню, которую ему уже прислали
    # сайт /shop (app/shop_accounts.py): вход логином и паролем. Пользователь без Telegram — id от WEB_ID_BASE
    login: Mapped[str | None] = mapped_column(String(32), unique=True, index=True)  # в нижнем регистре
    password_hash: Mapped[str | None] = mapped_column(String(255))
    lang: Mapped[str | None] = mapped_column(String(2))  # язык сайта: ru | en
    session_ver: Mapped[int | None] = mapped_column(Integer)  # +1 при смене пароля — все входы на сайт закрываются
    api_key_hash: Mapped[str | None] = mapped_column(String(64), unique=True, index=True)  # sha256 ключа API
    api_key_hint: Mapped[str | None] = mapped_column(String(8))  # последние символы ключа — показать в профиле
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Transaction(Base):
    """Журнал всех движений по балансу пользователя."""

    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Money)  # + пополнение/возврат, − покупка
    kind: Mapped[str] = mapped_column(String(32))  # purchase | refund | admin (topup_test — старые тестовые пополнения)
    order_id: Mapped[int | None] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


NOT_POPULAR_SORT = 999  # «Сорт.» у непопулярных сервисов: порядок важен только популярным


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), unique=True)  # нормализованное имя
    name: Mapped[str] = mapped_column(String(128))
    is_popular: Mapped[bool] = mapped_column(Boolean, default=False)
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)
    sort: Mapped[int] = mapped_column(Integer, default=NOT_POPULAR_SORT)
    # кнопка сервиса в боте — синяя (стиль кнопок Telegram «primary»)
    highlight: Mapped[bool | None] = mapped_column(Boolean, default=False)
    # True — в боте показываются только страны из service_countries
    country_filter: Mapped[bool | None] = mapped_column(Boolean, default=False)
    # «Прим. 1» — абзацем в карточке «Авто», на экране цены страны и в карточке активации
    note: Mapped[str | None] = mapped_column(Text)
    # «Прим. 2» — абзацем в карточке активации, когда пришла SMS (например, совет защитить аккаунт)
    note_sms: Mapped[str | None] = mapped_column(Text)
    # ссылка на инструкцию — кнопка «Инструкция» рядом с «Выбрать страну»
    instruction: Mapped[str | None] = mapped_column(String(512))


class ServiceCountry(Base):
    """Страны, разрешённые для сервиса (действует, если у сервиса включён country_filter)."""

    __tablename__ = "service_countries"

    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), primary_key=True)
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id"), primary_key=True)


class Country(Base):
    __tablename__ = "countries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(16), unique=True)  # ISO2 или ISO2_V (виртуальные)
    name: Mapped[str] = mapped_column(String(128))
    flag: Mapped[str] = mapped_column(String(16), default="🏳")
    is_hidden: Mapped[bool] = mapped_column(Boolean, default=False)


class ServiceLink(Base):
    """Код сервиса у конкретной площадки -> наш сервис."""

    __tablename__ = "service_links"
    __table_args__ = (UniqueConstraint("provider", "code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    code: Mapped[str] = mapped_column(String(64))
    provider_name: Mapped[str] = mapped_column(String(255))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"), index=True)
    manual: Mapped[bool] = mapped_column(Boolean, default=False)  # задано вручную в панели


class CountryLink(Base):
    __tablename__ = "country_links"
    __table_args__ = (UniqueConstraint("provider", "code"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    code: Mapped[str] = mapped_column(String(32))
    provider_name: Mapped[str] = mapped_column(String(255))
    country_id: Mapped[int | None] = mapped_column(ForeignKey("countries.id"))


class Order(Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    country_id: Mapped[int] = mapped_column(ForeignKey("countries.id"))
    provider: Mapped[str] = mapped_column(String(16))
    provider_order_id: Mapped[str] = mapped_column(String(64), index=True)
    supplier: Mapped[str | None] = mapped_column(String(32))  # поставщик внутри площадки (пул SMSPool)
    phone: Mapped[str] = mapped_column(String(32))
    cost_usd: Mapped[Decimal] = mapped_column(UsdCost)  # закупка
    usd_rate: Mapped[Decimal] = mapped_column(Numeric(12, 4))  # курс на момент покупки
    price: Mapped[Decimal] = mapped_column(Money)  # продажа, ₽
    # waiting -> received -> completed | cancelled | replaced | expired
    status: Mapped[str] = mapped_column(String(16), default="waiting", index=True)
    can_get_another: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    cancel_available_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    # куплено в режиме «Авто»: страну выбрал бот — замена тоже ищет по всем странам
    auto: Mapped[bool | None] = mapped_column(Boolean)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(BigInteger)

    service: Mapped[Service] = relationship(lazy="joined")
    country: Mapped[Country] = relationship(lazy="joined")
    messages: Mapped[list["OrderSms"]] = relationship(
        lazy="selectin", order_by="OrderSms.id", back_populates="order"
    )

    @property
    def is_active(self) -> bool:
        return self.status in ("waiting", "received")


class OrderSms(Base):
    __tablename__ = "order_sms"
    __table_args__ = (UniqueConstraint("order_id", "text"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    code: Mapped[str | None] = mapped_column(String(64))
    text: Mapped[str] = mapped_column(Text)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    order: Mapped[Order] = relationship(back_populates="messages")


class DiscardedOrder(Base):
    """Купленный у площадки номер, который пользователю не отдали (площадка продала его дороже
    лимита). Его нужно отменить у площадки — иногда это можно сделать только через пару минут."""

    __tablename__ = "discarded_orders"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    provider_order_id: Mapped[str] = mapped_column(String(64))
    phone: Mapped[str] = mapped_column(String(32))
    cancel_after: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    done: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    cost_usd: Mapped[Decimal | None] = mapped_column(UsdCost)  # закупка — её теряем, если отмена не прошла
    # итог: False — отменён, деньги на площадке вернулись; True — отменить не удалось; None — ещё пробуем
    lost: Mapped[bool | None] = mapped_column(Boolean)


class BuyAttempt(Base):
    """Попытка купить номер у поставщика — выдал ли он номер или только числился в прайсе."""

    __tablename__ = "buy_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    provider: Mapped[str] = mapped_column(String(16))
    supplier: Mapped[str | None] = mapped_column(String(64))
    service_id: Mapped[int] = mapped_column(Integer)
    country_id: Mapped[int] = mapped_column(Integer)
    price: Mapped[Decimal] = mapped_column(Money)  # уровень цены, ₽
    # ok | no_numbers | error
    result: Mapped[str] = mapped_column(String(16))


class BotChat(Base):
    """Канал или группа, где состоит бот (app/bot/channels.py). Канал и его группа обсуждения
    ссылаются друг на друга через linked_id — в панели это одна связка."""

    __tablename__ = "bot_chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)  # id чата в Telegram
    type: Mapped[str] = mapped_column(String(16))  # channel | group | supergroup
    title: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    # права бота: creator | administrator | member | restricted | left | kicked
    status: Mapped[str] = mapped_column(String(16))
    linked_id: Mapped[int | None] = mapped_column(BigInteger)  # группа обсуждения канала / канал группы
    # только у канала: комментировать новые посты одним из его сообщений (ChannelMessage)
    comments_on: Mapped[bool | None] = mapped_column(Boolean, default=False)
    last_comment_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(255))  # почему не удалось прокомментировать
    is_forum: Mapped[bool | None] = mapped_column(Boolean)  # группа с темами (для поддержки)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChannelMessage(Base):
    """Рекламный комментарий канала: текст и/или фото. Бот выбирает случайный из сообщений канала."""

    __tablename__ = "channel_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)  # канал (BotChat.id)
    text: Mapped[str] = mapped_column(Text, default="")  # HTML-разметка Telegram; с фото — подпись
    photo: Mapped[bytes | None] = mapped_column(LargeBinary)
    photo_type: Mapped[str | None] = mapped_column(String(32))  # image/jpeg | image/png | image/webp
    # фото, уже загруженное в Telegram, — чтобы не отправлять файл каждый раз
    photo_file_id: Mapped[str | None] = mapped_column(String(255))
    thumb: Mapped[bytes | None] = mapped_column(LargeBinary)  # лёгкое превью для панели (app/thumbs.py)
    button_text: Mapped[str | None] = mapped_column(String(64))  # текст кнопки под сообщением; пусто — BUTTON
    # ссылка кнопки (только у канала не реферала; например с UTM-меткой); пусто — просто бот.
    # У канала реферала кнопка всегда ведёт на его реферальную ссылку
    button_url: Mapped[str | None] = mapped_column(String(512))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class TwoFactor:
    """Аутентификатор (TOTP) и автовывод — общее для владельца и реферала (app/accounts.py)."""

    totp_secret: Mapped[str | None] = mapped_column(String(64))  # base32; пусто — аутентификатор не привязан
    totp_step: Mapped[int | None] = mapped_column(BigInteger)  # последний принятый шаг — код не примут дважды
    recovery: Mapped[str | None] = mapped_column(Text)  # JSON: хеши неиспользованных резервных кодов
    code_fails: Mapped[int | None] = mapped_column(Integer)  # неверные коды подряд
    locked_until: Mapped[datetime | None] = mapped_column(DateTime)  # после 5 неверных — пауза 10 минут
    auto_threshold: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))  # автовывод при балансе от, $
    auto_address: Mapped[str | None] = mapped_column(String(64))
    # версия входа: «Выйти» и смена пароля её увеличивают — все прежние входы (в т. ч. скопированные cookie) закрыты
    session_ver: Mapped[int | None] = mapped_column(Integer)


class Referral(TwoFactor, Base):
    """Реферал — друг со своим каналом (связка в «Каналах»). Люди, впервые открывшие бота по кнопке
    под его постами, закреплены за ним навсегда; с их успешных активаций ему идёт revenue_percent % цены
    активации, но не больше percent % нашей прибыли (app/referrals.py::share). Свою связку он редактирует
    в кабинете /partner-dashboard — там видна только доля от выручки."""

    __tablename__ = "referrals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)  # канал (BotChat.id)
    code: Mapped[str] = mapped_column(String(16), unique=True)  # t.me/бот?start=ref_<code>
    login: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    percent: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("50"))  # максимум от прибыли, %
    revenue_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), default=Decimal("15"))  # доля от выручки, %
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RefCode(Base):
    """Код ссылки-приглашения пользователя (app/user_refs.py). Кодов у человека может быть несколько: при склейке
    аккаунтов коды обоих остаются действующими — старые ссылки (на сайт и в бота) продолжают работать."""

    __tablename__ = "ref_codes"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UserRefReward(Base):
    """Начисление пользователю за приглашённого (app/user_refs.py): одно на заказ и пригласившего, ₽ на баланс."""

    __tablename__ = "user_ref_rewards"

    order_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    inviter_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False, index=True)
    invitee_id: Mapped[int] = mapped_column(BigInteger)
    amount: Mapped[Decimal] = mapped_column(Money)  # ₽
    price: Mapped[Decimal] = mapped_column(Money)
    profit: Mapped[Decimal] = mapped_column(Money)
    revenue_percent: Mapped[Decimal] = mapped_column(Numeric(5, 2))
    percent: Mapped[Decimal] = mapped_column(Numeric(5, 2))  # потолок от прибыли
    capped: Mapped[bool] = mapped_column(Boolean, default=False)
    split: Mapped[int | None] = mapped_column(Integer)  # на скольких пригласивших делилась доля (пусто — 1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class PartnerBan(Base):
    """Канал, у которого мы сами удалили реферала: сам через страницу партнёрской программы он больше не
    подключится (app/partner_signup.py). Вернуть — кнопкой «Сделать рефералом» в «Каналах»."""

    __tablename__ = "partner_bans"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RefTransaction(Base):
    """Движение по балансу реферала (в долларах): начисление с активации или выплата."""

    __tablename__ = "ref_transactions"
    __table_args__ = (Index("ux_ref_tx_order", "order_id", "referral_id", unique=True),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    referral_id: Mapped[int] = mapped_column(Integer, index=True)
    # accrual | payout (выплачено вручную) | withdrawal (вывод на кошелёк) | withdrawal_back (вывод не прошёл)
    kind: Mapped[str] = mapped_column(String(16))
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(12, 4))  # + начисление, − выплата
    payout_id: Mapped[int | None] = mapped_column(Integer, index=True)
    # начисление: заказ, наша прибыль с него, процент и курс ЦБ на момент начисления
    order_id: Mapped[int | None] = mapped_column(Integer)  # одно начисление на заказ и реферала — ux_ref_tx_order
    profit_rub: Mapped[Decimal | None] = mapped_column(Money)
    percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))  # максимум от прибыли на момент начисления
    usd_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    # с 17.10.2026: доля от выручки, цена активации и сколько ₽ начислено (capped — упёрлись в долю прибыли)
    revenue_percent: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    price_rub: Mapped[Decimal | None] = mapped_column(Money)
    share_rub: Mapped[Decimal | None] = mapped_column(Money)
    capped: Mapped[bool | None] = mapped_column(Boolean)
    split: Mapped[int | None] = mapped_column(Integer)  # с 20.10.2026: на скольких пригласивших делилась доля (пусто — 1)
    comment: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Broadcast(Base):
    """Рассылка пользователям бота (app/broadcasts.py)."""

    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    text: Mapped[str] = mapped_column(Text, default="")  # HTML-разметка Telegram; с фото — подпись
    photo: Mapped[bytes | None] = mapped_column(LargeBinary)
    photo_type: Mapped[str | None] = mapped_column(String(32))
    photo_file_id: Mapped[str | None] = mapped_column(String(255))
    thumb: Mapped[bytes | None] = mapped_column(LargeBinary)  # лёгкое превью для панели (app/thumbs.py)
    segment: Mapped[str] = mapped_column(String(32))  # группа получателей (broadcasts.SEGMENTS)
    # sending — отправляется | done — отправлена | cancelling — удаляем отправленное | cancelled
    status: Mapped[str] = mapped_column(String(16), default="sending")
    total: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime)


class BroadcastRecipient(Base):
    """Получатель рассылки: чтобы продолжить после перезапуска и удалить отправленное при отмене."""

    __tablename__ = "broadcast_recipients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    broadcast_id: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    # pending | sent | failed | blocked (заблокировал бота) | deleted (удалено при отмене)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)


class MenuImage(Base):
    """Картинка-оформление пункта главного меню бота (app/menu_images.py)."""

    __tablename__ = "menu_images"

    key: Mapped[str] = mapped_column(String(16), primary_key=True)  # buy | balance | orders | info
    photo: Mapped[bytes] = mapped_column(LargeBinary)
    photo_type: Mapped[str] = mapped_column(String(32))
    file_id: Mapped[str | None] = mapped_column(String(255))  # уже загружена в Telegram
    thumb: Mapped[bytes | None] = mapped_column(LargeBinary)  # лёгкое превью для панели (app/thumbs.py)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Payment(Base):
    """Пополнение баланса через платёжную систему (app/payments.py)."""

    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    provider: Mapped[str] = mapped_column(String(16), default="platega")  # platega | stars
    # ID транзакции у платёжки; у звёзд — telegram_payment_charge_id
    external_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    amount: Mapped[Decimal] = mapped_column(Money)  # ₽ на баланс
    method: Mapped[int | None] = mapped_column(Integer)  # способ оплаты платёжки (2 — СБП)
    # pending -> confirmed | canceled | expired; confirmed -> chargebacked (возврат денег плательщику)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    url: Mapped[str | None] = mapped_column(String(512))  # страница оплаты
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)  # сообщение со ссылкой на оплату
    message_id: Mapped[int | None] = mapped_column(BigInteger)
    stars: Mapped[int | None] = mapped_column(Integer)  # оплата звёздами Telegram (provider="stars"): сколько звёзд


class Setting(Base):
    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class Quarantine(Base):
    """Предложение площадки, на которое она ответила «нет номеров», хотя в прайсе оно было.
    Уровни цены не выше up_to не показываются до until."""

    # v2: карантин на уровне поставщика площадки (старая таблица quarantine больше не используется)
    __tablename__ = "quarantine_v2"
    __table_args__ = (UniqueConstraint("provider", "service_code", "country_code", "supplier_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    service_code: Mapped[str] = mapped_column(String(64))
    country_code: Mapped[str] = mapped_column(String(32))
    supplier_id: Mapped[str] = mapped_column(String(32), default="")  # "" — поставщик неизвестен
    supplier: Mapped[str | None] = mapped_column(String(64))
    up_to: Mapped[Decimal] = mapped_column(UsdCost)
    until: Mapped[datetime] = mapped_column(DateTime, index=True)
    reason: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class WebhookLog(Base):
    __tablename__ = "webhook_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider: Mapped[str] = mapped_column(String(16))
    payload: Mapped[str] = mapped_column(Text)
    result: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# ------------------------------------------------------------------ деньги: владельцы, выводы, кошелёк


class Owner(TwoFactor, Base):
    """Владелец: вход в панель со всеми правами и своя доля прибыли (после доли рефералов) в долларах."""

    __tablename__ = "owners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    login: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255), default="")  # пусто — пароль ещё не задан
    share: Mapped[Decimal] = mapped_column(Numeric(5, 2), default=Decimal("50"))  # % прибыли
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger)  # уведомления о деньгах в Telegram
    tg_username: Mapped[str | None] = mapped_column(String(64))  # как владелец его указал (@username)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class OwnerTransaction(Base):
    """Движение по балансу владельца, $: доля прибыли с активации, расход (бонус пользователю),
    распределение разницы «Сверки», вывод и его возврат."""

    __tablename__ = "owner_transactions"
    __table_args__ = (UniqueConstraint("owner_id", "order_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_id: Mapped[int] = mapped_column(Integer, index=True)
    # profit | bonus | adjust | withdrawal | withdrawal_back
    kind: Mapped[str] = mapped_column(String(16))
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    order_id: Mapped[int | None] = mapped_column(Integer)
    payout_id: Mapped[int | None] = mapped_column(Integer, index=True)
    profit_rub: Mapped[Decimal | None] = mapped_column(Money)  # прибыль с активации до долей
    usd_rate: Mapped[Decimal | None] = mapped_column(Numeric(12, 4))
    comment: Mapped[str | None] = mapped_column(String(255))
    actor: Mapped[str | None] = mapped_column(String(64))  # кто сделал (для ручных операций)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class Payout(Base):
    """Исходящий перевод USDT TRC20 из кошелька системы (app/payouts.py).

    queued — ждёт отправки (в т.ч. денег в кошельке) → signed — подписан и сохранён ДО отправки в сеть
    → done (подтверждён) | failed (сеть отклонила — деньги вернулись на баланс) | canceled.
    Подписанный перевод создаётся заново, только когда старый точно истёк и его нет в сети — двойной
    отправки не бывает."""

    __tablename__ = "payouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    purpose: Mapped[str] = mapped_column(String(16))  # withdrawal | provider | trx (оплата счёта обменника)
    account: Mapped[str | None] = mapped_column(String(8))  # owner | ref — чей вывод
    account_id: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str | None] = mapped_column(String(16))  # пополнение площадки
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))  # списано с баланса (вывод) / отправлено
    fee_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6), default=Decimal(0))  # комиссия сети за счёт выводящего
    send_usd: Mapped[Decimal] = mapped_column(Numeric(14, 6))  # уходит на адрес
    address: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    auto: Mapped[bool | None] = mapped_column(Boolean, default=False)
    actor: Mapped[str | None] = mapped_column(String(64))
    txid: Mapped[str | None] = mapped_column(String(80))
    tx_json: Mapped[str | None] = mapped_column(Text)
    expires_ms: Mapped[int | None] = mapped_column(BigInteger)  # подписанный перевод недействителен после
    attempts: Mapped[int | None] = mapped_column(Integer, default=0)
    fee_trx: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))  # сколько TRX реально сожжено
    note: Mapped[str | None] = mapped_column(String(255))  # почему ждёт / ошибка
    balance_before: Mapped[Decimal | None] = mapped_column(Numeric(14, 4))  # баланс площадки при отправке
    credited_at: Mapped[datetime | None] = mapped_column(DateTime)  # площадка зачислила
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    done_at: Mapped[datetime | None] = mapped_column(DateTime)


class SavedWallet(Base):
    """Сохранённый адрес для вывода — чтобы не вводить вручную."""

    __tablename__ = "saved_wallets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account: Mapped[str] = mapped_column(String(8))  # owner | ref
    account_id: Mapped[int] = mapped_column(Integer, index=True)
    name: Mapped[str] = mapped_column(String(64))
    address: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ChainTransfer(Base):
    """Перевод USDT в кошелёк системы или из него, как его видит сеть (для «Сверки»)."""

    __tablename__ = "chain_transfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    txid: Mapped[str] = mapped_column(String(80), unique=True)
    incoming: Mapped[bool] = mapped_column(Boolean)
    counterparty: Mapped[str] = mapped_column(String(64))
    amount_usd: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    at: Mapped[datetime] = mapped_column(DateTime, index=True)


class AuditLog(Base):
    """Кто из владельцев (или автоматика) что сделал с деньгами и настройками денег."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class SupportTicket(Base):
    """Обращение в поддержку (app/bot/support.py): тема в группе поддержки, пока открыто — сообщения
    пользователя копируются в тему, ответы из темы бот отправляет пользователю от своего имени."""

    __tablename__ = "support_tickets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)  # номер обращения
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)  # группа поддержки
    topic_id: Mapped[int | None] = mapped_column(Integer, index=True)  # message_thread_id темы
    card_message_id: Mapped[int | None] = mapped_column(Integer)  # карточка пользователя в теме
    status: Mapped[str] = mapped_column(String(8), default="open", index=True)  # open | closed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime)
    closed_by: Mapped[str | None] = mapped_column(String(64))


class SupportMessage(Base):
    """Сообщение обращения — для истории на сайте (/shop/support). Пишется и для обращений из бота."""

    __tablename__ = "support_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(Integer, index=True)
    from_staff: Mapped[bool] = mapped_column(Boolean, default=False)
    text: Mapped[str] = mapped_column(Text)
    via: Mapped[str | None] = mapped_column(String(8))  # tg | web — откуда написал пользователь
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class UtmStart(Base):
    """Каждый /start по ссылке с UTM-меткой (app/utm.py) — и новые пользователи, и уже знакомые."""

    __tablename__ = "utm_starts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    label: Mapped[str] = mapped_column(String(64), index=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    new: Mapped[bool] = mapped_column(Boolean, default=False)  # открыл бота впервые
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)


class TgChecker(Base):
    """Аккаунт Telegram, которым сервер проверяет номера Telegram перед выдачей (app/tgcheck.py)."""

    __tablename__ = "tg_checkers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    phone: Mapped[str] = mapped_column(String(32), unique=True)  # только цифры
    session: Mapped[str | None] = mapped_column(Text)  # StringSession Telethon, зашифрована
    status: Mapped[str] = mapped_column(String(12), default="active")  # active | paused | dead
    tg_user_id: Mapped[int | None] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(String(64))
    name: Mapped[str | None] = mapped_column(String(128))
    rest_until: Mapped[datetime | None] = mapped_column(DateTime)  # Telegram попросил паузу (FLOOD_WAIT)
    checks_day: Mapped[str | None] = mapped_column(String(10))  # YYYY-MM-DD (UTC) для счётчика за сутки
    checks_today: Mapped[int | None] = mapped_column(Integer, default=0)
    checks_total: Mapped[int | None] = mapped_column(Integer, default=0)
    busy_total: Mapped[int | None] = mapped_column(Integer, default=0)  # сколько номеров оказались заняты
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

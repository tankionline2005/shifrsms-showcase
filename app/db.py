from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import config

engine = create_async_engine(config.database_url, pool_pre_ping=True)
Session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    """Naive UTC — одинаково работает в SQLite и PostgreSQL."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def init_db() -> None:
    from app import models  # noqa: F401  — регистрирует таблицы

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
        await conn.run_sync(_migrate)


def _add_missing_columns(conn) -> None:
    """create_all не добавляет колонки в уже существующие таблицы — добавляем новые
    (только nullable) сами, чтобы не терять данные при обновлениях."""
    from sqlalchemy import inspect, text

    inspector = inspect(conn)
    for table in Base.metadata.sorted_tables:
        existing = {c["name"] for c in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name not in existing and column.nullable:
                col_type = column.type.compile(dialect=conn.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN {column.name} {col_type}'))
        for index in table.indexes:  # индексы новых колонок (в т. ч. уникальные) — create_all их тоже не добавит
            index.create(conn, checkfirst=True)


def _migrate(conn) -> None:
    """Разовые изменения, которых не сделают create_all и _add_missing_columns; каждое можно повторять."""
    from sqlalchemy import text

    if conn.dialect.name == "postgresql":  # SQLite — только для проверок, таблицы там создаются заново
        # 20.10.2026: у склеенного аккаунта до двух пригласивших — начисление одно на заказ И реферала/пригласившего
        conn.execute(text("ALTER TABLE ref_transactions DROP CONSTRAINT IF EXISTS ref_transactions_order_id_key"))
        pk = set(conn.execute(text(
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'user_ref_rewards'::regclass AND i.indisprimary")).scalars())
        if pk == {"order_id"}:
            conn.execute(text("ALTER TABLE user_ref_rewards DROP CONSTRAINT user_ref_rewards_pkey, "
                              "ADD PRIMARY KEY (order_id, inviter_id)"))
    # коды приглашений, созданные до таблицы ref_codes (у пользователя был один код — users.ref_code)
    conn.execute(text("INSERT INTO ref_codes (code, user_id, created_at) SELECT u.ref_code, u.id, u.created_at FROM users u "
                      "WHERE u.ref_code IS NOT NULL AND NOT EXISTS (SELECT 1 FROM ref_codes c WHERE c.code = u.ref_code)"))

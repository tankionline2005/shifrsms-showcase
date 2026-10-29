"""Таблицы денег по страницам с поиском (шаблон _paged.html): переводы из кошелька, движения кошелька
в сети, журнал действий, история баланса владельца, история выводов. По TABLE_PAGE строк, новые сверху."""

from sqlalchemy import String, and_, cast, false, func, or_, select

from app import payouts
from app.db import Session
from app.models import AuditLog, ChainTransfer, Owner, OwnerTransaction, Payout, Referral, RefTransaction

TABLE_PAGE = 10


def _like(q: str) -> str:
    return "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _titles(q: str, titles: dict) -> list[str]:
    """Ключи, у которых название содержит запрос: «в очереди» → queued."""
    return [k for k, t in titles.items() if q.lower() in t.lower()]


def _number(q: str) -> int | None:
    q = q.lstrip("#№ ")
    return int(q) if q.isdigit() and len(q) < 10 else None


async def paged(stmt, page: int, q: str) -> dict:
    async with Session() as s:
        total = await s.scalar(select(func.count()).select_from(stmt.order_by(None).subquery()))
        pages = max(1, -(-total // TABLE_PAGE))
        page = min(max(1, page), pages)
        rows = list((await s.scalars(stmt.offset((page - 1) * TABLE_PAGE).limit(TABLE_PAGE))).all())
    return {"rows": rows, "page": page, "pages": pages, "total": total, "q": q}


async def payouts_table(q: str = "", page: int = 1, account: tuple[str, int] | None = None) -> dict:
    """Переводы из кошелька; account — только выводы этого счёта (вкладка «Вывести»)."""
    stmt = select(Payout)
    if account:
        stmt = stmt.where(Payout.account == account[0], Payout.account_id == account[1])
    q = q.strip()
    if q:
        p = _like(q)
        conds = [Payout.address.ilike(p, escape="\\"), Payout.txid.ilike(p, escape="\\"),
                 Payout.actor.ilike(p, escape="\\"), Payout.note.ilike(p, escape="\\"),
                 cast(Payout.amount_usd, String).ilike(p, escape="\\"), cast(Payout.send_usd, String).ilike(p, escape="\\")]
        if (n := _number(q)) is not None:
            conds.append(Payout.id == n)
        for column, titles in ((Payout.status, payouts.STATUS_TITLES), (Payout.purpose, payouts.PURPOSE_TITLES),
                               (Payout.provider, payouts.PROVIDER_TITLES)):
            if keys := _titles(q, titles):
                conds.append(column.in_(keys))
        if not account:  # по логину владельца или реферала
            async with Session() as s:
                owner_ids = list((await s.scalars(select(Owner.id).where(Owner.login.ilike(p, escape="\\")))).all())
                ref_ids = list((await s.scalars(select(Referral.id).where(Referral.login.ilike(p, escape="\\")))).all())
            if owner_ids:
                conds.append(and_(Payout.account == "owner", Payout.account_id.in_(owner_ids)))
            if ref_ids:
                conds.append(and_(Payout.account == "ref", Payout.account_id.in_(ref_ids)))
        stmt = stmt.where(or_(*conds))
    return await paged(stmt.order_by(Payout.id.desc()), page, q)


async def transfers_table(q: str = "", page: int = 1) -> dict:
    stmt = select(ChainTransfer)
    q = q.strip()
    if q:
        p = _like(q)
        conds = [ChainTransfer.counterparty.ilike(p, escape="\\"), ChainTransfer.txid.ilike(p, escape="\\"),
                 cast(ChainTransfer.amount_usd, String).ilike(p, escape="\\")]
        low = q.lower()
        if "поступ" in low or low in "поступление":
            conds.append(ChainTransfer.incoming.is_(True))
        if "списан" in low or low in "списание":
            conds.append(ChainTransfer.incoming.is_(False))
        stmt = stmt.where(or_(*conds))
    return await paged(stmt.order_by(ChainTransfer.at.desc(), ChainTransfer.id.desc()), page, q)


async def audit_table(q: str = "", page: int = 1) -> dict:
    stmt = select(AuditLog)
    q = q.strip()
    if q:
        p = _like(q)
        stmt = stmt.where(or_(AuditLog.actor.ilike(p, escape="\\"), AuditLog.action.ilike(p, escape="\\")))
    return await paged(stmt.order_by(AuditLog.id.desc()), page, q)


async def owner_history_table(owner_id: int, titles: dict, q: str = "", page: int = 1) -> dict:
    stmt = select(OwnerTransaction).where(OwnerTransaction.owner_id == owner_id)
    q = q.strip()
    if q:
        p = _like(q)
        conds = [OwnerTransaction.comment.ilike(p, escape="\\"), OwnerTransaction.actor.ilike(p, escape="\\"),
                 cast(OwnerTransaction.amount_usd, String).ilike(p, escape="\\")]
        if (n := _number(q)) is not None:
            conds += [OwnerTransaction.order_id == n, OwnerTransaction.payout_id == n]
        conds.append(OwnerTransaction.kind.in_(_titles(q, titles)) if _titles(q, titles) else false())
        stmt = stmt.where(or_(*conds))
    return await paged(stmt.order_by(OwnerTransaction.id.desc()), page, q)


async def ref_history_table(referral_id: int, titles: dict, q: str = "", page: int = 1) -> dict:
    stmt = select(RefTransaction).where(RefTransaction.referral_id == referral_id)
    q = q.strip()
    if q:
        p = _like(q)
        conds = [RefTransaction.comment.ilike(p, escape="\\"), cast(RefTransaction.amount_usd, String).ilike(p, escape="\\")]
        if (n := _number(q)) is not None:
            conds += [RefTransaction.order_id == n, RefTransaction.payout_id == n]
        if keys := _titles(q, titles):
            conds.append(RefTransaction.kind.in_(keys))
        stmt = stmt.where(or_(*conds))
    return await paged(stmt.order_by(RefTransaction.id.desc()), page, q)

from decimal import Decimal

from sqlalchemy import update

from app.db import Session
from app.models import Transaction, User


async def get_or_create_user(user_id: int, username: str | None, first_name: str | None) -> tuple[User, bool]:
    """-> (пользователь, создан ли только что)."""
    async with Session() as s:
        user = await s.get(User, user_id)
        created = user is None
        if created:
            user = User(id=user_id, username=username, first_name=first_name)
            s.add(user)
        else:
            user.username, user.first_name = username, first_name
            user.bot_blocked = False  # написал боту — значит, не заблокировал его
        await s.commit()
        return user, created


async def get_balance(user_id: int) -> Decimal:
    async with Session() as s:
        user = await s.get(User, user_id)
        return user.balance if user else Decimal("0")


async def credit(user_id: int, amount: Decimal, kind: str, order_id: int | None = None,
                 comment: str | None = None) -> None:
    async with Session() as s:
        await s.execute(update(User).where(User.id == user_id)
                        .values(balance=User.balance + amount))
        s.add(Transaction(user_id=user_id, amount=amount, kind=kind, order_id=order_id,
                          comment=comment))
        await s.commit()


async def debit(user_id: int, amount: Decimal, kind: str, comment: str | None = None) -> int | None:
    """Атомарно списывает сумму, если хватает средств. Возвращает id транзакции."""
    async with Session() as s:
        res = await s.execute(update(User)
                              .where(User.id == user_id, User.balance >= amount)
                              .values(balance=User.balance - amount))
        if res.rowcount != 1:
            await s.rollback()
            return None
        tx = Transaction(user_id=user_id, amount=-amount, kind=kind, comment=comment)
        s.add(tx)
        await s.commit()
        return tx.id

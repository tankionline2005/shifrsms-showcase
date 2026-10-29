"""Денежные счета: владелец (owner) и реферал (ref) — одинаковые для вывода денег.

Баланс счёта — сумма его движений (OwnerTransaction / RefTransaction), а не число, которое можно
перезаписать. Аутентификатор (TOTP) обязателен для всех операций с деньгами; после 5 неверных кодов
подряд — пауза 10 минут (фиксированная)."""

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import delete, func, select

from app import totp
from app.db import Session, utcnow
from app.models import AuditLog, Owner, OwnerTransaction, Referral, RefTransaction, SavedWallet

KINDS = ("owner", "ref")
MODELS = {"owner": Owner, "ref": Referral}
MAX_FAILS, LOCK = 5, timedelta(minutes=10)
MIN_WITHDRAW = Decimal("10")
MIN_AUTO = Decimal("50")
MAX_SAVED = 20


@dataclass(frozen=True)
class Acc:
    kind: str  # owner | ref
    id: int

    @property
    def model(self):
        return MODELS[self.kind]


async def get(acc: Acc):
    async with Session() as s:
        return await s.get(acc.model, acc.id)


async def name(acc: Acc) -> str:
    row = await get(acc)
    return row.login if row else f"{acc.kind}:{acc.id}"


async def balance(acc: Acc) -> Decimal:
    tx = OwnerTransaction if acc.kind == "owner" else RefTransaction
    key = tx.owner_id if acc.kind == "owner" else tx.referral_id
    async with Session() as s:
        total = await s.scalar(select(func.coalesce(func.sum(tx.amount_usd), 0)).where(key == acc.id))
    return Decimal(total)


async def audit(actor: str, action: str) -> None:
    async with Session() as s:
        s.add(AuditLog(actor=actor[:64], action=action[:255]))
        await s.commit()


# ---------------------------------------------------------------- аутентификатор


def bound(row) -> bool:
    return bool(row and row.totp_secret)


async def bind(acc: Acc, secret: str, code: str) -> list[str] | None:
    """Привязать аутентификатор: код из приложения должен подойти к новому секрету.
    -> резервные коды (показать один раз) или None, если код не подошёл."""
    step = totp.match(secret, code, None)
    if step is None:
        return None
    codes, hashes = totp.new_recovery()
    async with Session() as s:
        row = await s.get(acc.model, acc.id)
        if row.totp_secret:  # уже привязан (вторая вкладка) — второй раз не перепривязываем
            return None
        row.totp_secret, row.totp_step, row.recovery, row.code_fails, row.locked_until = secret, step, hashes, 0, None
        await s.commit()
    return codes


async def check(acc: Acc, code: str) -> str | None:
    """Проверить код аутентификатора (или резервный код). -> текст ошибки или None, если подошёл."""
    code = (code or "").strip()
    async with Session() as s:
        row = await s.get(acc.model, acc.id)
        if not bound(row):
            return "Сначала привяжите аутентификатор."
        now = utcnow()
        if row.locked_until and row.locked_until > now:
            minutes = max(1, int((row.locked_until - now).total_seconds() // 60) + 1)
            return f"Слишком много неверных кодов — попробуйте через {minutes} мин."
        step = totp.match(row.totp_secret, code, row.totp_step)
        rest = None if step is not None or not code else totp.use_recovery(row.recovery, code)
        if step is None and rest is None:
            row.code_fails = (row.code_fails or 0) + 1
            if row.code_fails >= MAX_FAILS:
                row.code_fails, row.locked_until = 0, now + LOCK
            await s.commit()
            return "Неверный код аутентификатора."
        if step is not None:
            row.totp_step = step
        else:
            row.recovery = rest
        row.code_fails, row.locked_until = 0, None
        await s.commit()
    return None


async def reset_2fa(acc: Acc) -> None:
    """Сбросить привязку (потерян телефон) — только с сервера: python -m app.cli reset-2fa <логин>."""
    async with Session() as s:
        row = await s.get(acc.model, acc.id)
        row.totp_secret = row.totp_step = row.recovery = row.locked_until = None
        row.code_fails = 0
        row.auto_threshold = row.auto_address = None  # автовывод — заново, с новым кодом
        await s.commit()


def recovery_left(row) -> int:
    return totp.recovery_left(row.recovery) if row else 0


# ---------------------------------------------------------------- сохранённые адреса и автовывод


async def wallets(acc: Acc) -> list[SavedWallet]:
    async with Session() as s:
        return list((await s.scalars(select(SavedWallet).where(
            SavedWallet.account == acc.kind, SavedWallet.account_id == acc.id).order_by(SavedWallet.id))).all())


async def add_wallet(acc: Acc, name_: str, address: str) -> str | None:
    if len(await wallets(acc)) >= MAX_SAVED:
        return f"Можно сохранить не больше {MAX_SAVED} адресов."
    async with Session() as s:
        s.add(SavedWallet(account=acc.kind, account_id=acc.id, name=name_.strip()[:64] or address[:8] + "…",
                          address=address))
        await s.commit()
    return None


async def remove_wallet(acc: Acc, wallet_id: int) -> None:
    async with Session() as s:
        await s.execute(delete(SavedWallet).where(SavedWallet.id == wallet_id, SavedWallet.account == acc.kind,
                                                  SavedWallet.account_id == acc.id))
        await s.commit()


async def set_auto(acc: Acc, threshold: Decimal | None, address: str | None) -> None:
    async with Session() as s:
        row = await s.get(acc.model, acc.id)
        row.auto_threshold, row.auto_address = threshold, address
        await s.commit()


async def with_auto() -> list[tuple[Acc, object]]:
    """Счета с включённым автовыводом."""
    out = []
    async with Session() as s:
        for kind, model in MODELS.items():
            rows = (await s.scalars(select(model).where(model.auto_threshold.is_not(None),
                                                         model.auto_address.is_not(None)))).all()
            out += [(Acc(kind, r.id), r) for r in rows if r.totp_secret]
    return out

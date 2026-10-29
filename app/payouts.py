"""Исходящие переводы USDT из кошелька системы: выводы владельцев и рефералов, пополнение площадок,
оплата счёта обменника за TRX (TRX сжигается на комиссии переводов).

Как не потерять деньги:
- вывод списывается с баланса в той же записи базы, что и создаётся (под общим замком) — дважды
  потратить баланс нельзя; если сеть перевод отклонила — деньги возвращаются на баланс;
- перевод подписывается и СОХРАНЯЕТСЯ в базе до отправки в сеть. После перезапуска система сначала
  спрашивает сеть об этом переводе; новый подписывается, только когда старый истёк и в сети его нет
  (истёкший перевод сеть уже никогда не примет) — двойной отправки не бывает;
- комиссию сети оплачивает выводящий: на адрес уходит сумма минус комиссия.
Очередь обрабатывается по одному переводу (process), раз в 15 секунд и сразу после создания."""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal
from typing import Awaitable, Callable

from sqlalchemy import func, select

from app import accounts, catalog, tron
from app import settings_store as st
from app.accounts import Acc
from app.db import Session, utcnow
from app.models import ChainTransfer, OwnerTransaction, Payout, RefTransaction

log = logging.getLogger(__name__)

PROVIDER_TITLES = {"grizzly": "Grizzly", "smspool": "SMSPool", "fivesim": "5SIM", "onlinesim": "Onlinesim"}
ACTIVE = ("queued", "signed")
STATUS_TITLES = {"queued": "в очереди", "signed": "отправлен, ждёт подтверждения", "done": "выполнен",
                 "failed": "не прошёл", "canceled": "отменён"}
PURPOSE_TITLES = {"withdrawal": "Вывод", "provider": "Пополнение площадки", "trx": "Покупка TRX"}
CENT = Decimal("0.01")
RESEND_AFTER_MS = 5 * 60_000  # перевод истёк + 5 минут, а сеть его не знает — подписываем новый
CREDIT_WAIT = timedelta(hours=1)  # площадка должна зачислить пополнение за это время

lock = asyncio.Lock()  # списания с балансов и создание переводов — строго по одному
_queue_lock = asyncio.Lock()

Notifier = Callable[[str], Awaitable[None]]
_notifier: Notifier | None = None
_said: dict[str, float] = {}  # уведомление -> когда отправлено (не повторять слишком часто)


class PayoutError(Exception):
    pass


def set_notifier(fn: Notifier) -> None:
    global _notifier
    _notifier = fn


async def notify(text: str, key: str | None = None, every: int = 0) -> None:
    """Уведомить владельцев в Telegram. key+every — не чаще раза в every секунд."""
    if key:
        if time.monotonic() - _said.get(key, -1e9) < every:
            return
        _said[key] = time.monotonic()
    if _notifier:
        try:
            await _notifier(text)
        except Exception:
            log.exception("уведомление владельцам")


def money(v: Decimal) -> str:
    return f"${v:,.2f}".replace(",", " ")


def short(addr: str) -> str:
    return f"{addr[:6]}…{addr[-4:]}" if addr and len(addr) > 12 else addr


def check_address(address: str) -> str:
    address = (address or "").strip()
    if not tron.valid(address):
        return "Неверный адрес: нужен адрес USDT в сети TRON (TRC20) — 34 символа, начинается с T."
    if address in (tron.address(), tron.net()["usdt"]):
        return "Это адрес самой системы — на него выводить нельзя."
    return ""


async def pending_out() -> Decimal:
    """USDT, которые ещё уйдут из кошелька (в очереди и отправленные, но не подтверждённые)."""
    async with Session() as s:
        total = await s.scalar(select(func.coalesce(func.sum(Payout.send_usd), 0))
                               .where(Payout.status.in_(ACTIVE)))
    return Decimal(total)


def _ledger(acc: Acc, amount: Decimal, kind: str, payout_id: int, comment: str):
    if acc.kind == "owner":
        return OwnerTransaction(owner_id=acc.id, kind=kind, amount_usd=amount, payout_id=payout_id, comment=comment)
    return RefTransaction(referral_id=acc.id, kind=kind, amount_usd=amount, payout_id=payout_id, comment=comment)


# ---------------------------------------------------------------- создание


async def quote_withdrawal(amount: Decimal, address: str) -> dict:
    """Предпросмотр вывода: комиссия и сколько придёт. -> {fee, send} или PayoutError."""
    if not tron.enabled():
        raise PayoutError("Кошелёк системы ещё не подключён.")
    if error := check_address(address):
        raise PayoutError(error)
    amount = amount.quantize(CENT, rounding=ROUND_DOWN)
    if amount < accounts.MIN_WITHDRAW:
        raise PayoutError(f"Минимальная сумма вывода — {money(accounts.MIN_WITHDRAW)}.")
    try:
        q = await tron.quote(address.strip())
    except tron.ChainError as e:
        raise PayoutError(f"Не удалось рассчитать комиссию сети: {e}") from e
    send = (amount - q.fee_usd).quantize(CENT, rounding=ROUND_DOWN)
    if send < 1:
        raise PayoutError(f"Комиссия сети {money(q.fee_usd)} — почти вся сумма. Увеличьте сумму вывода.")
    return {"amount": amount, "fee": q.fee_usd, "send": send, "energy": q.energy, "fee_trx": q.fee_trx}


async def create_withdrawal(acc: Acc, amount: Decimal, address: str, actor: str, auto: bool = False) -> Payout:
    """Вывод с баланса счёта. Код аутентификатора проверяет страница (у автовывода — при включении)."""
    from app import reconcile  # «Сверка» знает, есть ли недостача

    address = (address or "").strip()
    q = await quote_withdrawal(amount, address)
    async with lock:
        if q["amount"] > await accounts.balance(acc):
            raise PayoutError("Сумма больше баланса.")
        if acc.kind == "owner" and (gap := reconcile.shortage()) is not None:
            raise PayoutError(f"На «Сверке» недостача {money(gap)} — сначала распределите разницу, "
                              "потом выводите.")
        async with Session() as s:
            p = Payout(purpose="withdrawal", account=acc.kind, account_id=acc.id, amount_usd=q["amount"],
                       fee_usd=q["fee"], send_usd=q["send"], address=address, auto=auto, actor=actor)
            s.add(p)
            await s.flush()
            s.add(_ledger(acc, -q["amount"], "withdrawal", p.id, f"на {short(address)}"))
            await s.commit()
    who = await accounts.name(acc)
    await accounts.audit(actor, f"вывод #{p.id} {money(q['amount'])} ({who}) на {address}"
                         + (" — автовывод" if auto else ""))
    await notify(f"💸 Вывод #{p.id}: {who} — {money(q['amount'])}, придёт {money(q['send'])} "
                 f"(комиссия сети {money(q['fee'])}) на {short(address)}" + (" · автовывод" if auto else ""))
    asyncio.create_task(process())
    return p


async def create_transfer(purpose: str, address: str, amount: Decimal, actor: str,
                          provider: str | None = None) -> Payout:
    """Перевод из кошелька системы не с баланса счёта: пополнение площадки или оплата счёта обменника за TRX.
    Деньги остаются нашими (переходят на площадку или в TRX кошелька) — балансы владельцев не меняются."""
    if not tron.enabled():
        raise PayoutError("Кошелёк системы ещё не подключён.")
    address = (address or "").strip()
    if error := check_address(address):
        raise PayoutError(error)
    amount = amount.quantize(CENT, rounding=ROUND_DOWN)
    if amount < 1:
        raise PayoutError("Сумма — от $1.")
    async with lock:
        try:
            free = await tron.usdt_balance() - await pending_out()
        except tron.ChainError as e:
            raise PayoutError(f"Сеть TRON недоступна: {e}") from e
        if amount > free:
            raise PayoutError(f"В кошельке системы свободно {money(max(free, Decimal(0)))} — не хватает.")
        async with Session() as s:
            p = Payout(purpose=purpose, provider=provider, amount_usd=amount, send_usd=amount, address=address,
                       actor=actor)
            s.add(p)
            await s.commit()
    title = PROVIDER_TITLES.get(provider, "") if provider else ""
    what = f"пополнение {title}" if purpose == "provider" else PURPOSE_TITLES.get(purpose, purpose).lower()
    await accounts.audit(actor, f"перевод #{p.id}: {what} {money(amount)} на {address}")
    await notify(f"🏦 Перевод #{p.id}: {what} — {money(amount)} на {short(address)}")
    asyncio.create_task(process())
    return p


async def cancel(payout_id: int, actor: str) -> str | None:
    """Отменить перевод, который ещё не подписан (ждёт денег в кошельке). Вывод вернётся на баланс."""
    async with lock, Session() as s:
        p = await s.get(Payout, payout_id)
        if not p or p.status != "queued" or p.txid:
            return "Этот перевод уже отправляется — отменить нельзя."
        p.status, p.note, p.done_at = "canceled", f"отменил {actor}", utcnow()
        if p.purpose == "withdrawal":
            s.add(_ledger(Acc(p.account, p.account_id), p.amount_usd, "withdrawal_back", p.id, "вывод отменён"))
        await s.commit()
    await accounts.audit(actor, f"отменил перевод #{payout_id}")
    return None


# ---------------------------------------------------------------- очередь


async def _save(p: Payout, **fields) -> None:
    async with Session() as s:
        row = await s.get(Payout, p.id)
        for k, v in fields.items():
            setattr(row, k, v)
            setattr(p, k, v)
        await s.commit()


async def _wait(p: Payout, note: str) -> None:
    if p.note != note:
        await _save(p, note=note)
        await notify(f"⏳ Перевод #{p.id} ({money(p.send_usd)}) ждёт: {note}")


async def _check_signed(p: Payout) -> None:
    status, fee = await tron.tx_status(p.txid)
    if status == "success":
        await _save(p, status="done", fee_trx=fee, done_at=utcnow(), note=None)
        log.info("перевод %s выполнен: %s", p.id, p.txid)
        if p.purpose == "withdrawal":
            await notify(f"✅ Вывод #{p.id} выполнен: {money(p.send_usd)} на {short(p.address)}")
    elif status.startswith("failed"):
        reason = status.split(":", 1)[1] or "ошибка сети"
        async with lock, Session() as s:
            row = await s.get(Payout, p.id)
            if row.status != "signed":
                return
            row.status, row.fee_trx, row.done_at, row.note = "failed", fee, utcnow(), f"сеть отклонила: {reason}"
            if row.purpose == "withdrawal":  # USDT не ушли — возвращаем на баланс
                s.add(_ledger(Acc(row.account, row.account_id), row.amount_usd, "withdrawal_back", row.id,
                              f"сеть отклонила перевод ({reason})"))
            await s.commit()
        await notify(f"⚠️ Перевод #{p.id} не прошёл ({reason})"
                     + (" — деньги вернулись на баланс." if p.purpose == "withdrawal" else "."))
    elif status == "missing":
        if int(time.time() * 1000) > (p.expires_ms or 0) + RESEND_AFTER_MS:
            # истёк, и сети он неизвестен — уже никогда не пройдёт: подписываем новый
            await _save(p, status="queued", txid=None, tx_json=None, expires_ms=None,
                        attempts=(p.attempts or 0) + 1, note="перевод не попал в сеть — отправляем заново")
            if (p.attempts or 0) >= 3:
                await notify(f"⚠️ Перевод #{p.id} уже {p.attempts} раза не попал в сеть — проверьте кошелёк.")
        else:
            await tron.broadcast(p.tx_json)  # тот же подписанный перевод — повтор безопасен


async def _send(p: Payout) -> None:
    async with Session() as s:
        signed = await s.scalar(select(func.coalesce(func.sum(Payout.send_usd), 0))
                                .where(Payout.status == "signed"))
    if await tron.usdt_balance() - Decimal(signed) < p.send_usd:
        await _wait(p, "в кошельке системы не хватает USDT")
        return
    q = await tron.quote(p.address)
    if await tron.trx_balance() < q.fee_trx * Decimal("1.1"):
        await _wait(p, f"в кошельке мало TRX на комиссию (нужно ≈ {q.fee_trx:.1f} TRX)")
        return
    fields = {}
    if p.purpose == "provider" and p.provider in catalog.balances:
        fields["balance_before"] = catalog.balances[p.provider]
    txid, tx_json, expires_ms = await tron.sign_transfer(p.address, p.send_usd, tron.fee_limit_for(q.fee_trx))
    # сначала сохраняем подписанный перевод — после перезапуска его найдём и не отправим второй
    await _save(p, status="signed", txid=txid, tx_json=tx_json, expires_ms=expires_ms, sent_at=utcnow(),
                note=None, **fields)
    ok, message = await tron.broadcast(tx_json)
    log.info("перевод %s %s: %s", p.id, txid, message)
    if not ok:
        await _save(p, note=f"сеть пока не приняла ({message}) — повторим")


async def process() -> None:
    """Обработать очередь: подтвердить отправленные, отправить ждущие (по одному, по порядку)."""
    if not tron.enabled():
        return
    async with _queue_lock:
        async with Session() as s:
            rows = list((await s.scalars(select(Payout).where(Payout.status.in_(ACTIVE))
                                         .order_by(Payout.id))).all())
        for p in rows:
            try:
                if p.status == "signed":
                    await _check_signed(p)
                else:
                    await _send(p)
            except tron.ChainError as e:
                log.warning("перевод %s: %s", p.id, e)
                return  # сеть недоступна — попробуем в следующий раз


# ---------------------------------------------------------------- автоматика


def _floor(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_DOWN)


async def auto_withdrawals() -> None:
    """Автовывод: баланс дорос до порога — выводим весь баланс на выбранный адрес."""
    if not tron.enabled():
        return
    for acc, row in await accounts.with_auto():
        async with Session() as s:
            busy = await s.scalar(select(func.count()).select_from(Payout).where(
                Payout.account == acc.kind, Payout.account_id == acc.id, Payout.status.in_(ACTIVE)))
        balance = _floor(await accounts.balance(acc))
        if busy or balance < row.auto_threshold:
            continue
        try:
            await create_withdrawal(acc, balance, row.auto_address, actor="автовывод", auto=True)
        except PayoutError as e:
            await notify(f"⚠️ Автовывод {row.login}: {e}", key=f"auto:{acc}", every=6 * 3600)


async def check_credited() -> None:
    """Пополнение площадки подтверждено в сети — ждём, когда баланс площадки вырастет."""
    async with Session() as s:
        rows = list((await s.scalars(select(Payout).where(
            Payout.purpose == "provider", Payout.status == "done", Payout.credited_at.is_(None)))).all())
    for p in rows:
        balance = catalog.balances.get(p.provider)
        if balance is not None and p.balance_before is not None and balance >= p.balance_before + p.send_usd / 2:
            await _save(p, credited_at=utcnow())
        elif p.done_at and utcnow() - p.done_at > CREDIT_WAIT:
            await notify(f"⚠️ {PROVIDER_TITLES.get(p.provider, p.provider)} не зачислила пополнение #{p.id} "
                         f"({money(p.send_usd)}) больше часа. Проверьте в личном кабинете площадки и отметьте "
                         "«Зачислено» на «Сверке».", key=f"credit:{p.id}", every=6 * 3600)


async def mark_credited(payout_id: int, actor: str) -> None:
    async with Session() as s:
        p = await s.get(Payout, payout_id)
        if p and p.purpose == "provider" and p.credited_at is None:
            p.credited_at = utcnow()
            await s.commit()
    await accounts.audit(actor, f"отметил пополнение #{payout_id} зачисленным")


async def alerts() -> None:
    """Мало TRX на комиссии, мало денег на площадках."""
    if tron.enabled():
        trx = await tron.trx_balance()
        if trx < st.dec("trx_alert"):
            await notify(f"⚠️ В кошельке системы {trx:.1f} TRX — пополните TRX: он сжигается на комиссию "
                         f"каждого перевода (≈ 7–14 TRX). Адрес: {tron.address()}", key="trx", every=6 * 3600)
    limit = st.dec("provider_alert")
    for p, balance in list(catalog.balances.items()):  # копия: балансы обновляются параллельно
        if limit > 0 and balance < limit and st.bool_(f"{p}_enabled"):
            await notify(f"⚠️ На {PROVIDER_TITLES.get(p, p)} осталось {money(balance)} — пополните "
                         "(«Сверка» → «Пополнить»).", key=f"low:{p}", every=6 * 3600)


async def sync_chain() -> None:
    """Переводы в кошелёк и из него — в историю «Сверки»; о поступлениях — уведомление."""
    if not tron.enabled():
        return
    async with Session() as s:
        last = await s.scalar(select(func.max(ChainTransfer.at)))
    since_ms = int((last - timedelta(minutes=10)).replace(tzinfo=timezone.utc).timestamp() * 1000) if last else 0
    new = []
    for t in await tron.transfers(since_ms):
        async with Session() as s:
            if await s.scalar(select(func.count()).select_from(ChainTransfer).where(ChainTransfer.txid == t["txid"])):
                continue
            row = ChainTransfer(txid=t["txid"], incoming=t["incoming"], counterparty=t["counterparty"] or "",
                                amount_usd=t["amount"], at=_utc(t["at_ms"]))
            s.add(row)
            await s.commit()
            new.append(row)
    for row in new:
        if row.incoming:
            await notify(f"📥 В кошелёк системы пришло {money(row.amount_usd)} USDT от {short(row.counterparty)}")


def _utc(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)

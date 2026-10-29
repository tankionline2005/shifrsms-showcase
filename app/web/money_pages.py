"""Вкладка «Вывести» — одинаковая у владельцев (панель) и рефералов (кабинет /partner-dashboard).

Первым делом — привязка аутентификатора по QR-коду. Дальше код из него нужен для каждого вывода,
сохранения адреса и включения автовывода. register() вешает страницы на роутер; acc_of(request) —
чей счёт, actor_of(request) — кто действует (для журнала)."""

from decimal import Decimal, InvalidOperation
from typing import Callable
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from sqlalchemy import select

from app import accounts, payouts, totp, tron
from app.accounts import Acc
from app.db import Session
from app.models import Payout
from app.web import tables

Render = Callable[..., Response]


def parse_usd(raw: str) -> Decimal | None:
    try:
        value = Decimal((raw or "").replace(",", ".").replace(" ", "").replace("$", "").strip())
    except InvalidOperation:
        return None
    return value if value.is_finite() and Decimal(0) < value < Decimal(10_000_000) else None


def setup_ctx(request: Request, login: str) -> dict:
    """Новый секрет аутентификатора живёт в сессии, пока код из приложения его не подтвердит."""
    secret = request.session.get("totp_new") or totp.new_secret()
    request.session["totp_new"] = secret
    return {"secret": secret, "qr": totp.qr_svg(totp.uri(secret, login)),
            "secret_spaced": " ".join(secret[i:i + 4] for i in range(0, len(secret), 4))}


def register(router: APIRouter, prefix: str, guard, acc_of: Callable[[Request], Acc],
             actor_of: Callable[[Request], str], render: Render) -> None:
    deps = [Depends(guard)]

    def back(error: str = "", saved: str = "") -> RedirectResponse:
        q = "&".join(f"{k}={quote_plus(v)}" for k, v in (("error", error), ("saved", saved)) if v)
        return RedirectResponse(f"{prefix}/withdraw" + (f"?{q}" if q else ""), status_code=303)

    @router.get("/withdraw", dependencies=deps)
    async def withdraw_page(request: Request, error: str = "", saved: str = ""):
        acc = acc_of(request)
        row = await accounts.get(acc)
        ctx = {} if accounts.bound(row) else setup_ctx(request, row.login)
        codes = request.session.pop("recovery_codes", None)  # показать один раз сразу после привязки
        return render(request, "withdraw.html", row=row, bound=accounts.bound(row), codes=codes,
                      balance=await accounts.balance(acc), wallets=await accounts.wallets(acc),
                      t=await tables.payouts_table(account=(acc.kind, acc.id)), statuses=payouts.STATUS_TITLES,
                      error=error, saved=saved,
                      enabled=tron.enabled(), min_withdraw=accounts.MIN_WITHDRAW, min_auto=accounts.MIN_AUTO,
                      recovery_left=accounts.recovery_left(row), tx_url=tron.tx_url, **ctx)

    @router.get("/withdraw/history", dependencies=deps)
    async def withdraw_history(request: Request, q: str = "", page: int = 1):
        acc = acc_of(request)
        return render(request, "_tbl_withdrawals.html", statuses=payouts.STATUS_TITLES, tx_url=tron.tx_url,
                      t=await tables.payouts_table(q, page, account=(acc.kind, acc.id)))

    @router.post("/withdraw/2fa", dependencies=deps)
    async def withdraw_bind(request: Request, code: str = Form("")):
        secret = request.session.get("totp_new")
        if not secret:
            return back("Обновите страницу и отсканируйте QR-код ещё раз.")
        codes = await accounts.bind(acc_of(request), secret, code)
        if codes is None:
            return back("Код не подошёл. Проверьте время на телефоне и введите свежий код.")
        request.session.pop("totp_new", None)
        request.session["recovery_codes"] = codes
        await accounts.audit(actor_of(request), "привязал аутентификатор")
        return back(saved="Аутентификатор привязан.")

    async def find_address(request: Request, wallet_id: str, address: str) -> str:
        if wallet_id.isdigit():
            for w in await accounts.wallets(acc_of(request)):
                if w.id == int(wallet_id):
                    return w.address
        return address.strip()

    @router.post("/withdraw/quote", dependencies=deps)
    async def withdraw_quote(request: Request, amount: str = Form(""), wallet_id: str = Form(""),
                             address: str = Form("")):
        """Предпросмотр: комиссия сети и сколько придёт — до ввода кода."""
        value = parse_usd(amount)
        if value is None:
            return JSONResponse({"error": "Укажите сумму в долларах, например 25.00"})
        try:
            q = await payouts.quote_withdrawal(value, await find_address(request, wallet_id, address))
        except payouts.PayoutError as e:
            return JSONResponse({"error": str(e)})
        return JSONResponse({"amount": f"{q['amount']:.2f}", "fee": f"{q['fee']:.2f}", "send": f"{q['send']:.2f}",
                             "fee_trx": f"{q['fee_trx']:.1f}"})

    @router.post("/withdraw", dependencies=deps)
    async def withdraw_create(request: Request, amount: str = Form(""), wallet_id: str = Form(""),
                              address: str = Form(""), code: str = Form("")):
        acc = acc_of(request)
        value = parse_usd(amount)
        if value is None:
            return back("Укажите сумму в долларах, например 25.00")
        if error := await accounts.check(acc, code):
            return back(error)
        try:
            p = await payouts.create_withdrawal(acc, value, await find_address(request, wallet_id, address),
                                                actor_of(request))
        except payouts.PayoutError as e:
            return back(str(e))
        return back(saved=f"Вывод #{p.id} создан: придёт {payouts.money(p.send_usd)} "
                          f"(комиссия сети {payouts.money(p.fee_usd)}). Обычно это занимает 1–3 минуты.")

    @router.post("/withdraw/{payout_id}/cancel", dependencies=deps)
    async def withdraw_cancel(request: Request, payout_id: int):
        acc = acc_of(request)
        async with Session() as s:
            p = await s.get(Payout, payout_id)
        if not p or p.account != acc.kind or p.account_id != acc.id:
            return back()
        error = await payouts.cancel(payout_id, actor_of(request))
        return back(error or "", "" if error else "Вывод отменён, деньги вернулись на баланс.")

    @router.post("/withdraw/wallets", dependencies=deps)
    async def wallet_add(request: Request, name: str = Form(""), address: str = Form(""), code: str = Form("")):
        acc = acc_of(request)
        if error := payouts.check_address(address):
            return back(error)
        if error := await accounts.check(acc, code):
            return back(error)
        if error := await accounts.add_wallet(acc, name, address.strip()):
            return back(error)
        await accounts.audit(actor_of(request), f"сохранил адрес «{name.strip()}» {address.strip()}")
        return back(saved="Адрес сохранён.")

    @router.post("/withdraw/wallets/{wallet_id}/delete", dependencies=deps)
    async def wallet_delete(request: Request, wallet_id: int):
        await accounts.remove_wallet(acc_of(request), wallet_id)
        return back(saved="Адрес удалён из сохранённых.")

    @router.post("/withdraw/auto", dependencies=deps)
    async def auto_set(request: Request, enabled: str = Form(""), threshold: str = Form(""), wallet_id: str = Form(""),
                       address: str = Form(""), code: str = Form("")):
        acc = acc_of(request)
        if error := await accounts.check(acc, code):
            return back(error)
        if not enabled:
            await accounts.set_auto(acc, None, None)
            await accounts.audit(actor_of(request), "выключил автовывод")
            return back(saved="Автовывод выключен.")
        value = parse_usd(threshold)
        if value is None or value < accounts.MIN_AUTO:
            return back(f"Порог автовывода — от {payouts.money(accounts.MIN_AUTO)}.")
        target = await find_address(request, wallet_id, address)
        if error := payouts.check_address(target):
            return back(error)
        await accounts.set_auto(acc, value.quantize(Decimal("0.01")), target)
        await accounts.audit(actor_of(request), f"включил автовывод от {payouts.money(value)} на {target}")
        return back(saved=f"Автовывод включён: когда на балансе наберётся {payouts.money(value)}, "
                          f"весь баланс уйдёт на {payouts.short(target)}.")

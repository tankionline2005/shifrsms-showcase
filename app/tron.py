"""Сеть TRON: кошелёк системы, USDT TRC20.

Перевод собирается и подписывается ЛОКАЛЬНО: номер транзакции (txid) считаем сами из её содержимого
(tronpy offline), узел сети сообщает только номер последнего блока и принимает уже подписанное. Подменить
получателя или сумму узел не может — подпись покрывает ровно то, что собрали мы.

Ключ — WALLET_PRIVATE_KEY в .env. Узел — TronGrid (с ключом TRONGRID_API_KEY лимиты выше), при отказе —
запасной публичный узел."""

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal

import httpx
from tronpy import AsyncTron, keys

from app.config import config

log = logging.getLogger(__name__)

NETWORKS = {
    "mainnet": {"nodes": ("https://api.trongrid.io", "https://tron-rpc.publicnode.com"),
                "usdt": "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t", "scan": "https://tronscan.org/#"},
    "nile": {"nodes": ("https://nile.trongrid.io", "https://api.nileex.io"),
             "usdt": "TXYZopYRdj2D9XRtbG411XZZ3kM5VkAeBf", "scan": "https://nile.tronscan.org/#"},
}
UNIT = Decimal(10) ** 6  # USDT и TRX — 6 знаков после запятой
CENT = Decimal("0.01")
# энергия перевода USDT, если оценить у сети не удалось: получателю, у которого USDT уже есть, и новому
ENERGY_HOLDER, ENERGY_NEW = 65_000, 131_000
BANDWIDTH = 350  # байт на перевод USDT
FEE_BUFFER = Decimal("1.10")  # запас 10% к комиссии: курс TRX и цена энергии меняются
FEE_LIMIT_MAX = 60 * 1_000_000  # больше 60 TRX за один перевод не сжигаем ни при каких условиях


class ChainError(Exception):
    pass


def net() -> dict:
    return NETWORKS.get(config.tron_network, NETWORKS["mainnet"])


def enabled() -> bool:
    return bool(config.wallet_private_key.strip())


_key_cache: list = []


def _key() -> keys.PrivateKey:
    if not _key_cache:
        _key_cache.append(keys.PrivateKey(bytes.fromhex(config.wallet_private_key.strip().removeprefix("0x"))))
    return _key_cache[0]


def address() -> str:
    return _key().public_key.to_base58check_address() if enabled() else ""


def valid(addr: str) -> bool:
    addr = (addr or "").strip()
    return len(addr) == 34 and addr.startswith("T") and keys.is_base58check_address(addr)


def tx_url(txid: str) -> str:
    return f"{net()['scan']}/transaction/{txid}"


def address_url(addr: str) -> str:
    return f"{net()['scan']}/address/{addr}"


# ---------------------------------------------------------------- запросы к узлу


def _headers(node: str) -> dict:
    return {"TRON-PRO-API-KEY": config.trongrid_api_key} if config.trongrid_api_key and "trongrid" in node else {}


async def _call(path: str, payload: dict | None = None, *, get: bool = False, trongrid_only: bool = False) -> dict:
    nodes = net()["nodes"][:1] if trongrid_only else net()["nodes"]
    last = None
    for node in nodes:
        try:
            async with httpx.AsyncClient(timeout=20) as http:
                r = await (http.get(f"{node}/{path}", params=payload, headers=_headers(node)) if get else
                           http.post(f"{node}/{path}", json=payload or {}, headers=_headers(node)))
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code} {r.text[:200]}"
        except (httpx.HTTPError, ValueError) as e:
            last = repr(e)
        log.warning("tron %s %s: %s", node, path, last)
    raise ChainError(f"сеть TRON недоступна ({path}): {last}")


def _abi_address(addr: str) -> str:
    return keys.to_hex_address(addr)[2:].rjust(64, "0")


async def _const(function: str, parameter: str, owner: str | None = None) -> dict:
    data = await _call("wallet/triggerconstantcontract", {
        "owner_address": owner or address() or net()["usdt"], "contract_address": net()["usdt"],
        "function_selector": function, "parameter": parameter, "visible": True})
    if not data.get("result", {}).get("result"):
        raise ChainError(f"{function}: {data.get('result')}")
    return data


async def usdt_balance(addr: str | None = None) -> Decimal:
    data = await _const("balanceOf(address)", _abi_address(addr or address()))
    return Decimal(int(data["constant_result"][0] or "0", 16)) / UNIT


async def trx_balance(addr: str | None = None) -> Decimal:
    data = await _call("wallet/getaccount", {"address": addr or address(), "visible": True})
    return Decimal(data.get("balance", 0)) / UNIT


_params: dict = {}


async def chain_fees() -> tuple[int, int]:
    """-> (цена энергии, цена байта пропускной способности) в sun. Параметры сети меняются голосованием."""
    if not _params or time.monotonic() - _params["at"] > 600:
        data = await _call("wallet/getchainparameters")
        values = {p["key"]: p.get("value", 0) for p in data.get("chainParameter", [])}
        _params.update(at=time.monotonic(), energy=int(values.get("getEnergyFee") or 100),
                       bandwidth=int(values.get("getTransactionFee") or 1000))
    return _params["energy"], _params["bandwidth"]


async def energy_for(to: str) -> int:
    """Энергия на перевод USDT получателю, с запасом. Не меньше нормы: получателю с USDT ≈ 65 тыс., новому
    адресу ≈ 131 тыс. Пробный прогон перевода у сети учитывается, только если он прошёл без ошибки
    (с пустого кошелька перевод «падает» в самом начале и показывает в разы меньше энергии)."""
    floor = ENERGY_HOLDER if await usdt_balance(to) > 0 else ENERGY_NEW
    try:
        data = await _const("transfer(address,uint256)", _abi_address(to) + "1".rjust(64, "0"))
        failed = data.get("result", {}).get("message") or any(
            r.get("ret") == "FAILED" for r in (data.get("transaction") or {}).get("ret", []))
        if not failed:
            return max(floor, int(int(data.get("energy_used") or 0) * 1.05))
    except ChainError:
        pass
    return floor


_price: dict = {}
PRICE_SOURCES = (
    ("https://api.binance.com/api/v3/ticker/price", {"symbol": "TRXUSDT"}, lambda d: d["price"]),
    ("https://www.okx.com/api/v5/market/ticker", {"instId": "TRX-USDT"}, lambda d: d["data"][0]["last"]),
    ("https://api.coingecko.com/api/v3/simple/price", {"ids": "tron", "vs_currencies": "usd"},
     lambda d: d["tron"]["usd"]),
)


async def trx_usd() -> Decimal:
    """Курс TRX в долларах — с бирж по очереди, кэш 5 минут."""
    if _price and time.monotonic() - _price["at"] < 300:
        return _price["value"]
    async with httpx.AsyncClient(timeout=10) as http:
        for url, params, pick in PRICE_SOURCES:
            try:
                value = Decimal(str(pick((await http.get(url, params=params)).json())))
                if value > 0:
                    _price.update(at=time.monotonic(), value=value)
                    return value
            except Exception as e:  # биржа недоступна или ответила не то — следующая
                log.warning("курс TRX %s: %r", url, e)
    if _price:
        return _price["value"]
    raise ChainError("не удалось узнать курс TRX")


@dataclass
class Quote:
    energy: int
    fee_trx: Decimal  # сколько TRX сожжёт перевод
    fee_usd: Decimal  # комиссия для выводящего, с запасом
    trx_usd: Decimal


async def quote(to: str) -> Quote:
    energy = await energy_for(to)
    energy_fee, bw_fee = await chain_fees()
    fee_trx = Decimal(energy * energy_fee + BANDWIDTH * bw_fee) / UNIT
    price = await trx_usd()
    fee_usd = max(CENT, (fee_trx * price * FEE_BUFFER).quantize(CENT, rounding=ROUND_CEILING))
    return Quote(energy, fee_trx, fee_usd, price)


def fee_limit_for(fee_trx: Decimal) -> int:
    """Потолок сжигания TRX для перевода: оценка с запасом 50%, не больше FEE_LIMIT_MAX."""
    return min(FEE_LIMIT_MAX, int(fee_trx * Decimal("1.5") * UNIT) + 2_000_000)


# ---------------------------------------------------------------- перевод


async def sign_transfer(to: str, amount: Decimal, fee_limit: int) -> tuple[str, str, int]:
    """Собрать и подписать перевод USDT. Ничего не отправляет. -> (txid, транзакция JSON, истекает_ms)."""
    if not valid(to):
        raise ChainError(f"неверный адрес {to}")
    units = int((amount * UNIT).to_integral_value(rounding=ROUND_DOWN))
    if units <= 0:
        raise ChainError("сумма перевода должна быть больше нуля")
    block = await _call("walletsolidity/getnowblock")
    ref_block_id = block["blockID"]
    data = "a9059cbb" + _abi_address(to) + hex(units)[2:].rjust(64, "0")
    builder = AsyncTron(network=config.tron_network if config.tron_network in NETWORKS else "mainnet") \
        .trx._build_transaction("TriggerSmartContract", {
            "owner_address": keys.to_hex_address(address()), "contract_address": keys.to_hex_address(net()["usdt"]),
            "data": data, "call_value": 0})
    builder.fee_limit(fee_limit)
    builder.expiration(10 * 60_000)  # 10 минут на попадание в блок; после — перевод недействителен навсегда
    txn = await builder.build(offline=True, ref_block_id=ref_block_id)
    txn.sign(_key())
    body = txn.to_json()
    body["visible"] = False  # адреса внутри — hex, как их собрал tronpy
    return txn.txid, json.dumps(body), int(body["raw_data"]["expiration"])


async def broadcast(tx_json: str) -> tuple[bool, str]:
    """Отправить подписанный перевод. Повторная отправка того же перевода безопасна (DUP)."""
    data = await _call("wallet/broadcasttransaction", json.loads(tx_json))
    if data.get("result"):
        return True, "ok"
    code = data.get("code", "")
    message = data.get("message", "")
    try:
        message = bytes.fromhex(message).decode(errors="replace")
    except ValueError:
        pass
    if code == "DUP_TRANSACTION_ERROR":
        return True, "уже отправлен"
    return False, f"{code} {message}".strip()[:200]


async def tx_status(txid: str) -> tuple[str, Decimal | None]:
    """-> (success | failed:<причина> | pending | missing, сожжено TRX).
    success/failed — по необратимо подтверждённым блокам; missing — сеть перевода не знает."""
    info = await _call("walletsolidity/gettransactioninfobyid", {"value": txid})
    if info:
        fee = Decimal(info.get("fee", 0)) / UNIT
        result = (info.get("receipt") or {}).get("result", "")
        if info.get("result") == "FAILED" or (result and result != "SUCCESS"):
            return f"failed:{result or info.get('resMessage', '')}", fee
        return "success", fee
    pending = await _call("wallet/gettransactionbyid", {"value": txid})
    return ("pending" if pending else "missing"), None


async def transfers(since_ms: int = 0) -> list[dict]:
    """Переводы USDT в кошелёк и из него (подтверждённые) -> [{txid, incoming, counterparty, amount, at_ms}]."""
    if not enabled():
        return []
    data = await _call(f"v1/accounts/{address()}/transactions/trc20", {
        "only_confirmed": "true", "limit": 200, "contract_address": net()["usdt"], "min_timestamp": since_ms,
        "order_by": "block_timestamp,asc"}, get=True, trongrid_only=True)
    me, out = address(), []
    for t in data.get("data", []):
        incoming = t.get("to") == me
        out.append({"txid": t["transaction_id"], "incoming": incoming,
                    "counterparty": t.get("from") if incoming else t.get("to"),
                    "amount": Decimal(t.get("value", "0")) / (Decimal(10) ** int(t.get("token_info", {}).get("decimals", 6))),
                    "at_ms": int(t.get("block_timestamp", 0))})
    return out


def new_private_key() -> str:
    return keys.PrivateKey.random().hex()


async def _selftest() -> None:  # python -m app.tron — проверка связи с сетью (только чтение)
    print("сеть:", config.tron_network, "| адрес:", address() or "кошелька нет")
    print("комиссии:", await chain_fees(), "| TRX:", await trx_usd(), "$")
    if enabled():
        print("USDT:", await usdt_balance(), "| TRX:", await trx_balance())


if __name__ == "__main__":
    asyncio.run(_selftest())

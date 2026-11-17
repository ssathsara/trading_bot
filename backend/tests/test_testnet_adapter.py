"""Testnet execution adapter against MOCKED testnet responses (no credentials used)."""
from decimal import Decimal as D

import httpx
import respx

from app.exchange.rest import TestnetClient
from app.execution.adapters import OrderRequest, TestnetExecutionAdapter

T = "https://testnet.binance.vision"
REQ = OrderRequest("tbt-abc", "BTCUSDT", "BUY", "MARKET", D("0.001"), base_asset="BTC", quote_asset="USDT")


def adapter():
    return TestnetExecutionAdapter(TestnetClient(T, "mock-key", "mock-secret"))


@respx.mock
async def test_submit_full_response_with_fills():
    respx.post(f"{T}/api/v3/order").mock(return_value=httpx.Response(200, json={
        "symbol": "BTCUSDT", "orderId": 42, "clientOrderId": "tbt-abc", "status": "FILLED", "executedQty": "0.001",
        "fills": [{"price": "60000.00", "qty": "0.0006", "commission": "0.0000006", "commissionAsset": "BTC", "tradeId": 1},
                  {"price": "60001.00", "qty": "0.0004", "commission": "0.024", "commissionAsset": "USDT", "tradeId": 2}]}))
    r = await adapter().submit(REQ)
    assert r.status == "FILLED" and r.exchange_order_id == "42" and r.filled_qty == D("0.001")
    assert r.fills[0].fee_asset == "BTC" and r.fills[1].fee == D("0.024")


@respx.mock
async def test_submit_timeout_is_unknown_not_failed():
    respx.post(f"{T}/api/v3/order").mock(side_effect=httpx.ReadTimeout("timeout"))
    r = await adapter().submit(REQ)
    assert r.status == "UNKNOWN" and "unknown" in r.error


@respx.mock
async def test_submit_rejection_and_unknown_codes():
    respx.post(f"{T}/api/v3/order").mock(return_value=httpx.Response(400, json={"code": -1013, "msg": "Filter failure: NOTIONAL"}))
    r = await adapter().submit(REQ)
    assert r.status == "REJECTED" and "NOTIONAL" in r.error
    respx.post(f"{T}/api/v3/order").mock(return_value=httpx.Response(400, json={"code": -1007, "msg": "Timeout waiting for response"}))
    assert (await adapter().submit(REQ)).status == "UNKNOWN"


@respx.mock
async def test_query_reconciles_fills_and_not_found():
    respx.get(f"{T}/api/v3/order").mock(return_value=httpx.Response(200, json={
        "orderId": 7, "status": "PARTIALLY_FILLED", "executedQty": "0.0005"}))
    respx.get(f"{T}/api/v3/myTrades").mock(return_value=httpx.Response(200, json=[
        {"id": 900, "price": "60000", "qty": "0.0005", "commission": "0.03", "commissionAsset": "USDT"}]))
    r = await adapter().query(REQ)
    assert r.status == "PARTIALLY_FILLED" and r.fills[0].fill_id == "900"
    respx.get(f"{T}/api/v3/order").mock(return_value=httpx.Response(400, json={"code": -2013, "msg": "Order does not exist."}))
    assert (await adapter().query(REQ)).status == "NOT_FOUND"


@respx.mock
async def test_free_balance():
    respx.get(f"{T}/api/v3/account").mock(return_value=httpx.Response(200, json={"balances": [
        {"asset": "USDT", "free": "1234.5", "locked": "0"}]}))
    assert await adapter().free_balance("USDT") == D("1234.5")

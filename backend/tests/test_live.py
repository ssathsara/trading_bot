"""LIVE (real-money) mode gates. All exchange traffic is MOCKED; no real credentials are used."""
from decimal import Decimal as D

import httpx
import pytest
import respx
from pydantic import ValidationError

from app.config import Settings, get_settings
from app.exchange.rest import BinanceError, LiveClient, TestnetClient
from app.execution.adapters import LiveExecutionAdapter, OrderRequest, TestnetExecutionAdapter
from app.schemas import LIVE_CONFIRMATION_PHRASE

L = "https://api.binance.com"
BASE = dict(jwt_secret="x" * 40, admin_password="long-enough-pw", app_env="test")
LIVE_ENV = dict(live_trading_enabled=True, binance_live_api_key="live-key", binance_live_api_secret="live-secret",
                live_max_capital_usdt=D("100"))


def test_live_config_requires_explicit_opt_in_keys_and_cap():
    with pytest.raises(ValidationError, match="requires LIVE_TRADING_ENABLED"):
        Settings(**BASE, execution_mode="LIVE")
    with pytest.raises(ValidationError, match="BINANCE_LIVE_API_KEY"):
        Settings(**BASE, live_trading_enabled=True, live_max_capital_usdt=D("100"))
    with pytest.raises(ValidationError, match="LIVE_MAX_CAPITAL_USDT"):
        Settings(**BASE, **(LIVE_ENV | {"live_max_capital_usdt": D("0")}))
    with pytest.raises(ValidationError, match="not an allowed endpoint"):
        Settings(**BASE, **LIVE_ENV, binance_live_rest_url="https://testnet.binance.vision")
    with pytest.raises(ValidationError, match="same as the testnet key"):
        Settings(**BASE, **LIVE_ENV, binance_testnet_api_key="live-key", binance_testnet_api_secret="s")
    s = Settings(**BASE, **LIVE_ENV, execution_mode="LIVE")
    assert s.live_configured and s.initial_balance_for("LIVE") == D("100")
    assert s.initial_balance_for("PAPER") == s.initial_balance_usdt


def test_signed_clients_are_bound_to_their_own_hosts():
    with pytest.raises(ValueError):
        LiveClient("https://testnet.binance.vision", "k", "s")
    with pytest.raises(ValueError):
        LiveClient("https://data-api.binance.vision", "k", "s")  # read-only data host
    with pytest.raises(ValueError):
        TestnetClient(L, "k", "s")
    assert "live-key" not in repr(LiveClient(L, "live-key", "live-secret"))


async def test_live_client_rechecks_host_before_signing():
    c = LiveClient(L, "k", "s")
    c.base_url = "https://evil.example.com"
    with pytest.raises(BinanceError, match="not allowed for live"):
        await c.account()
    await c.close()


def test_adapters_refuse_the_other_venues_client():
    with pytest.raises(TypeError):
        LiveExecutionAdapter(TestnetClient("https://testnet.binance.vision", "k", "s"))
    with pytest.raises(TypeError):
        TestnetExecutionAdapter(LiveClient(L, "k", "s"))


@respx.mock
async def test_live_adapter_submit_and_rejection():
    req = OrderRequest("tbl-abc", "BTCUSDT", "BUY", "MARKET", D("0.001"), base_asset="BTC", quote_asset="USDT")
    route = respx.post(f"{L}/api/v3/order").mock(return_value=httpx.Response(200, json={
        "orderId": 7, "status": "FILLED",
        "fills": [{"price": "60000", "qty": "0.001", "commission": "0.06", "commissionAsset": "USDT", "tradeId": 9}]}))
    r = await LiveExecutionAdapter(LiveClient(L, "k", "s")).submit(req)
    assert r.status == "FILLED" and r.filled_qty == D("0.001") and not r.simulated
    assert route.calls.last.request.headers["X-MBX-APIKEY"] == "k"
    route.mock(return_value=httpx.Response(400, json={"code": -2010, "msg": "insufficient balance"}))
    r = await LiveExecutionAdapter(LiveClient(L, "k", "s")).submit(req)
    assert r.status == "REJECTED" and r.error.startswith("live rejected order")


@pytest.fixture
def live_client(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.security import limiter

    for k, v in {"LIVE_TRADING_ENABLED": "true", "BINANCE_LIVE_API_KEY": "live-key",
                 "BINANCE_LIVE_API_SECRET": "live-secret", "LIVE_MAX_CAPITAL_USDT": "150"}.items():
        monkeypatch.setenv(k, v)
    get_settings.cache_clear()
    limiter.reset()
    with TestClient(create_app()) as c:
        r = c.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
        yield c, {"Authorization": f"Bearer {r.json()['access_token']}"}
    get_settings.cache_clear()


def test_switching_to_live_requires_confirmation_phrase(live_client):
    c, auth = live_client
    body = c.get("/api/settings", headers=auth).json()
    assert body["live_trading_available"] is True and body["live_max_capital_usdt"] == "150"
    r = c.put("/api/settings", headers=auth, json={"execution_mode": "LIVE"})
    assert r.status_code == 422 and "confirm_live" in r.json()["error"]
    r = c.put("/api/settings", headers=auth, json={"execution_mode": "LIVE", "confirm_live": "yes"})
    assert r.status_code == 422
    r = c.put("/api/settings", headers=auth, json={"execution_mode": "LIVE", "confirm_live": LIVE_CONFIRMATION_PHRASE})
    assert r.status_code == 200 and r.json()["execution_mode"] == "LIVE"
    st = c.get("/api/system/status", headers=auth).json()
    assert st["environment_label"] == "LIVE TRADING - REAL MONEY" and st["live_trading_enabled"] is True
    from app.db import session_scope
    from app.portfolio import service as pf

    with session_scope() as s:  # LIVE ledger starts at the capital cap, not INITIAL_BALANCE_USDT
        assert pf.get_account(s, "LIVE").initial_balance == D("150")


def test_live_ledger_never_persisted_with_zero_capital():
    """Selecting LIVE while it is not configured must not create a 0-balance ledger that sticks."""
    from app.db import session_scope
    from app.models import AccountState
    from app.portfolio import service as pf

    with session_scope() as s:
        acct = pf.get_account(s, "LIVE")  # LIVE_MAX_CAPITAL_USDT=0 in the test env
        assert acct.initial_balance == 0
    with session_scope() as s:
        assert s.query(AccountState).filter_by(execution_mode="LIVE").count() == 0


def test_default_ledger_balance_follows_the_mode(monkeypatch):
    """get_account without an explicit balance must use the mode's configured start, not a fixed 10000."""
    from app.db import session_scope
    from app.portfolio import service as pf

    monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("BINANCE_LIVE_API_KEY", "live-key")
    monkeypatch.setenv("BINANCE_LIVE_API_SECRET", "live-secret")
    monkeypatch.setenv("LIVE_MAX_CAPITAL_USDT", "250")
    get_settings.cache_clear()
    try:
        with session_scope() as s:
            assert pf.get_account(s, "LIVE").initial_balance == D("250")
        with session_scope() as s:
            assert pf.get_account(s, "LIVE").initial_balance == D("250")  # persisted
    finally:
        get_settings.cache_clear()


def test_oversized_ids_are_422_not_500_and_health_reports_mode():
    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.security import limiter

    limiter.reset()
    with TestClient(create_app()) as c:
        tok = c.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"}).json()["access_token"]
        h = {"Authorization": f"Bearer {tok}"}
        big = "99999999999999999999"
        assert c.get(f"/api/trading/orders/{big}", headers=h).status_code == 422
        assert c.get(f"/api/backtest/results/{big}", headers=h).status_code == 422
        assert c.post(f"/api/trading/close-position/{big}", headers=h).status_code == 422
        assert c.get("/api/trading/orders/0", headers=h).status_code == 422
        assert c.get("/api/trading/orders/12345", headers=h).status_code == 404
        assert c.get("/api/health").json()["live_trading"] is False

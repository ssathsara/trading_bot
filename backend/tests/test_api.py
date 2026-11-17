from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from app.db import session_scope
from app.models import Position, SystemSetting, Trade
from app.portfolio import service as pf
from app.security import limiter


@pytest.fixture
def client():
    limiter.reset()
    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def auth(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "test-password-123"})
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_authentication_required(client):
    for path in ("/api/dashboard/summary", "/api/settings", "/api/trading/positions", "/api/system/status"):
        assert client.get(path).status_code == 401
    assert client.get("/api/settings", headers={"Authorization": "Bearer garbage"}).status_code == 401
    assert client.post("/api/system/start").status_code == 401
    assert client.get("/api/health").status_code == 200


def test_login_rejects_bad_password_and_rate_limits(client):
    assert client.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
    codes = [client.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code for _ in range(12)]
    assert 429 in codes


def test_password_is_hashed_with_argon2(client):
    from sqlalchemy import select

    from app.models import User

    with session_scope() as s:
        u = s.scalar(select(User))
        assert u.hashed_password.startswith("$argon2") and "test-password-123" not in u.hashed_password


def test_settings_roundtrip_and_live_mode_rejected(client, auth):
    r = client.get("/api/settings", headers=auth)
    body = r.json()
    assert body["execution_mode"] == "PAPER" and body["live_trading_available"] is False
    assert body["risk"]["risk_per_trade_pct"] == "0.5"  # decimals serialised exactly as strings
    assert client.put("/api/settings", headers=auth, json={"execution_mode": "LIVE"}).status_code == 422
    r = client.put("/api/settings", headers=auth, json={"execution_mode": "TESTNET"})
    assert r.status_code == 422 and "credentials" in r.json()["error"]
    risk = body["risk"] | {"risk_per_trade_pct": "1.0", "max_open_positions": 5}
    r = client.put("/api/settings/risk", headers=auth, json=risk)
    assert r.status_code == 200 and r.json()["max_open_positions"] == 5
    bad = body["risk"] | {"max_position_alloc_pct": "90"}  # > total allocation
    assert client.put("/api/settings/risk", headers=auth, json=bad).status_code == 422


def test_system_control_and_hard_halt_requires_reset(client, auth):
    assert client.post("/api/system/start", headers=auth).json()["state"] == "RUNNING"
    assert client.post("/api/system/pause", headers=auth).json()["state"] == "PAUSED"
    with session_scope() as s:
        acct = pf.get_account(s, "PAPER")
        acct.halted, acct.halt_reason = True, "MAX_DRAWDOWN: test"
    r = client.post("/api/system/resume", headers=auth)
    assert r.status_code == 409 and "reset" in r.json()["error"]
    assert client.post("/api/system/risk/reset", headers=auth).json()["reset"] is True
    assert client.post("/api/system/resume", headers=auth).json()["state"] == "RUNNING"
    st = client.get("/api/system/status", headers=auth).json()
    assert st["live_trading_enabled"] is False and st["environment_label"].startswith("PAPER")
    logs = client.get("/api/system/logs", headers=auth).json()
    assert logs["total"] >= 4
    assert client.get("/api/risk/events", headers=auth).json()["items"][0]["event_type"] == "RISK_RESET"


def test_trading_endpoints_and_manual_close(client, auth):
    with session_scope() as s:
        pf.get_account(s, "PAPER", D("10000"))
        p = Position(symbol="BTCUSDT", execution_mode="PAPER", quantity=D("0.1"), average_entry_price=D("60000"),
                     cost_basis=D("6000"), entry_fees_open=D("6"), unrealised_pnl=D("0"), realised_pnl=D("0"), status="OPEN")
        s.add(p)
        s.flush()
        pid = p.id
    r = client.get("/api/trading/positions", headers=auth)
    assert r.status_code == 200 and r.json()[0]["quantity"] == "0.1" and r.json()[0]["invested_amount"] == "6000"
    assert client.get("/api/trading/orders", headers=auth).json()["total"] == 0
    assert client.get("/api/trading/history?page_size=500", headers=auth).status_code == 422  # pagination bounds
    r = client.post(f"/api/trading/close-position/{pid}", headers=auth)
    assert r.status_code == 202
    with session_scope() as s:
        from sqlalchemy import select

        row = s.scalar(select(SystemSetting).where(SystemSetting.setting_key == "commands.close_position"))
        assert row.setting_value[0]["position_id"] == pid
    assert client.post("/api/trading/close-position/99999", headers=auth).status_code == 404


def test_portfolio_endpoints(client, auth):
    with session_scope() as s:
        pf.get_account(s, "PAPER", D("10000"))
    r = client.get("/api/portfolio/summary", headers=auth)
    assert r.status_code == 200 and r.json()["total_equity"] == "10000"
    perf = client.get("/api/portfolio/performance", headers=auth).json()
    assert perf["daily_pnl"] == [] and perf["summary"]["net_profit"] == "0"
    dash = client.get("/api/dashboard/summary", headers=auth).json()
    assert dash["simulated"] is True and dash["execution_mode"] == "PAPER"


def test_input_validation(client, auth):
    assert client.get("/api/ai/signals/btc;drop", headers=auth).status_code == 422
    assert client.get("/api/market/candles/BTCUSDT?timeframe=7m", headers=auth).status_code == 422
    r = client.post("/api/backtest/run", headers=auth, json={"symbol": "BTCUSDT", "timeframe": "15m",
                                                            "start": "2025-01-02T00:00:00Z", "end": "2025-01-01T00:00:00Z"})
    assert r.status_code == 422
    assert client.get("/api/backtest/results/123", headers=auth).status_code == 404
    assert client.get("/api/ai/signals/BTCUSDT", headers=auth).status_code == 404


def test_websocket_requires_token(client, auth):
    from starlette.websockets import WebSocketDisconnect

    for url in ("/ws/dashboard", "/ws/dashboard?token=not-a-jwt"):
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect(url) as ws:
                ws.receive_json()
        assert exc.value.code == 4401  # the dashboard logs out on 4401 instead of reconnecting forever
    token = auth["Authorization"].split()[1]
    with client.websocket_connect(f"/ws/dashboard?token={token}") as ws:
        assert ws.receive_json()["type"] == "hello"


def test_config_fails_closed():
    from pydantic import ValidationError

    from app.config import Settings

    base = dict(jwt_secret="x" * 40, admin_password="long-enough-pw", app_env="test")
    with pytest.raises(ValidationError, match="LIVE|Unsupported"):
        Settings(**base, execution_mode="LIVE")
    with pytest.raises(ValidationError, match="not an allowed endpoint"):
        Settings(**base, binance_testnet_rest_url="https://api.binance.com")
    with pytest.raises(ValidationError, match="requires BINANCE_TESTNET"):
        Settings(**base, execution_mode="TESTNET")
    with pytest.raises(ValidationError, match="JWT_SECRET"):
        Settings(jwt_secret="short", admin_password="long-enough-pw")
    with pytest.raises(ValidationError, match="ADMIN_PASSWORD"):
        Settings(jwt_secret="y" * 40, admin_password="change-me-please", app_env="production")

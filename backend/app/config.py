"""Application configuration.

All configuration comes from environment variables (optionally a .env file).
Validation FAILS CLOSED: any invalid or unsafe setting raises at startup and the
application refuses to run.

Safety invariants enforced here:
* Only PAPER and TESTNET execution modes exist. Anything else (e.g. LIVE) is rejected.
* Signed (credentialed) requests may only target the Binance Spot Testnet host.
* Public market-data endpoints must be on Binance's read-only allowlist.
"""
from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class ExecutionMode(StrEnum):
    PAPER = "PAPER"
    TESTNET = "TESTNET"


# Hosts that only serve public market data (they cannot accept orders) or the
# standard public REST hosts used read-only without credentials.
MARKET_DATA_REST_HOSTS = {
    "data-api.binance.vision",
    "api.binance.com",
    "api1.binance.com",
    "api2.binance.com",
    "api3.binance.com",
    "api4.binance.com",
}
MARKET_DATA_WS_HOSTS = {"data-stream.binance.vision", "stream.binance.com"}

# The ONLY hosts credentials may ever be sent to.
TESTNET_REST_HOSTS = {"testnet.binance.vision"}
TESTNET_WS_HOSTS = {"stream.testnet.binance.vision", "testnet.binance.vision"}

PLACEHOLDER_PREFIX = "change-me"
REPO_ROOT = Path(__file__).resolve().parents[2]


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(str(REPO_ROOT / ".env"), ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    app_env: str = "development"
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:5173"

    jwt_secret: SecretStr
    jwt_expire_minutes: int = Field(default=720, ge=5, le=60 * 24 * 7)
    admin_username: str = "admin"
    admin_password: SecretStr
    admin_email: str = "admin@example.com"

    database_url: str = "sqlite:///./data/trading.db"
    redis_url: str = ""
    run_worker_in_api: bool = True

    execution_mode: ExecutionMode = ExecutionMode.PAPER
    initial_balance_usdt: Decimal = Field(default=Decimal("10000"), gt=0)

    market_data_rest_url: str = "https://data-api.binance.vision"
    market_data_ws_url: str = "wss://data-stream.binance.vision"

    binance_testnet_api_key: SecretStr = SecretStr("")
    binance_testnet_api_secret: SecretStr = SecretStr("")
    binance_testnet_rest_url: str = "https://testnet.binance.vision"
    binance_testnet_ws_url: str = "wss://stream.testnet.binance.vision"

    @field_validator("execution_mode", mode="before")
    @classmethod
    def _reject_live(cls, v: object) -> object:
        if isinstance(v, str) and v.strip().upper() not in {m.value for m in ExecutionMode}:
            raise ValueError(
                f"Unsupported EXECUTION_MODE '{v}'. Only PAPER and TESTNET are available; "
                "live trading is disabled in this release."
            )
        return v.strip().upper() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _validate(self) -> "Settings":
        is_test = self.app_env == "test"
        secret = self.jwt_secret.get_secret_value()
        if len(secret) < 32 or (secret.startswith(PLACEHOLDER_PREFIX) and not is_test):
            raise ValueError("JWT_SECRET must be a random value of at least 32 characters")
        pwd = self.admin_password.get_secret_value()
        if len(pwd) < 10 or (pwd.startswith(PLACEHOLDER_PREFIX) and not is_test):
            raise ValueError("ADMIN_PASSWORD must be set to a non-placeholder value (>= 10 chars)")

        for name, url, allowed, schemes in (
            ("MARKET_DATA_REST_URL", self.market_data_rest_url, MARKET_DATA_REST_HOSTS, {"https"}),
            ("MARKET_DATA_WS_URL", self.market_data_ws_url, MARKET_DATA_WS_HOSTS, {"wss"}),
            ("BINANCE_TESTNET_REST_URL", self.binance_testnet_rest_url, TESTNET_REST_HOSTS, {"https"}),
            ("BINANCE_TESTNET_WS_URL", self.binance_testnet_ws_url, TESTNET_WS_HOSTS, {"wss"}),
        ):
            parsed = urlparse(url)
            if parsed.scheme not in schemes or _host(url) not in allowed:
                raise ValueError(f"{name}={url!r} is not an allowed endpoint (allowed hosts: {sorted(allowed)})")

        if self.execution_mode == ExecutionMode.TESTNET:
            if not self.binance_testnet_api_key.get_secret_value() or not self.binance_testnet_api_secret.get_secret_value():
                raise ValueError("EXECUTION_MODE=TESTNET requires BINANCE_TESTNET_API_KEY and BINANCE_TESTNET_API_SECRET")
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @property
    def testnet_configured(self) -> bool:
        return bool(self.binance_testnet_api_key.get_secret_value() and self.binance_testnet_api_secret.get_secret_value())


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]

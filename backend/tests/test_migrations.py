import os
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, inspect

from app.models import Base

BACKEND = Path(__file__).resolve().parents[1]


def _alembic(url: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "DATABASE_URL": url}
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=BACKEND, env=env, capture_output=True, text=True)


def test_upgrade_matches_models_and_downgrade_works():
    db = Path(tempfile.mkdtemp()) / "mig.db"
    url = f"sqlite:///{db.as_posix()}"
    r = _alembic(url, "upgrade", "head")
    assert r.returncode == 0, r.stderr
    eng = create_engine(url)
    tables = set(inspect(eng).get_table_names())
    assert set(Base.metadata.tables) <= tables
    idx = {i["name"]: i for i in inspect(eng).get_indexes("positions")}
    assert idx["uq_position_open_symbol_mode"]["unique"]
    uqs = {u["name"] for u in inspect(eng).get_unique_constraints("market_candles")}
    assert "uq_candle_symbol_tf_open" in uqs
    eng.dispose()
    check = _alembic(url, "check")  # no drift between models and migrations
    assert check.returncode == 0, check.stdout + check.stderr
    assert _alembic(url, "downgrade", "base").returncode == 0
    eng = create_engine(url)
    assert set(inspect(eng).get_table_names()) <= {"alembic_version"}
    eng.dispose()

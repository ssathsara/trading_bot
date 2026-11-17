"""Optional ML signal filter (Gradient Boosting / Random Forest).

* Features are causal (computed from indicator columns at row i only).
* Labels use future returns and are therefore ONLY used for training; the last
  ``horizon`` rows (which would need unknown future data) are dropped.
* Validation is chronological walk-forward with a purge gap of ``horizon`` bars
  between train and test to prevent label leakage.
* A model is marked ``validated`` only if it beats the base rate out-of-sample.
  Even then it is used solely as an additional *filter* on strategy BUY signals.
"""
from __future__ import annotations

import json
import pickle
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingClassifier, RandomForestClassifier
from sklearn.metrics import accuracy_score, precision_score, roc_auc_score

MODEL_DIR = Path(__file__).resolve().parents[2] / "models_store"
FEATURES = ["rsi_n", "dist_ema20", "ema20_50", "ema50_200", "macd_hist_n", "atr_n", "adx_n", "vol_ratio",
            "bb_pos", "ret_1", "ret_3", "ret_6"]


def build_features(df: pd.DataFrame) -> pd.DataFrame:
    c = df["close"]
    f = pd.DataFrame(index=df.index)
    f["rsi_n"] = df["rsi"] / 100
    f["dist_ema20"] = c / df["ema_20"] - 1
    f["ema20_50"] = df["ema_20"] / df["ema_50"] - 1
    f["ema50_200"] = df["ema_50"] / df["ema_200"] - 1
    f["macd_hist_n"] = df["macd_hist"] / c
    f["atr_n"] = df["atr"] / c
    f["adx_n"] = df["adx"] / 100
    f["vol_ratio"] = df["volume"] / df["volume_ma"]
    width = (df["bb_upper"] - df["bb_lower"]).replace(0, np.nan)
    f["bb_pos"] = (c - df["bb_lower"]) / width
    for n in (1, 3, 6):
        f[f"ret_{n}"] = c.pct_change(n)
    return f.replace([np.inf, -np.inf], np.nan)


def build_labels(df: pd.DataFrame, horizon: int, threshold: float) -> pd.Series:
    fwd = df["close"].shift(-horizon) / df["close"] - 1
    y = (fwd > threshold).astype(float)
    y[fwd.isna()] = np.nan  # rows whose future is unknown are unlabeled
    return y


def _model(kind: str):
    if kind == "random_forest":
        return RandomForestClassifier(n_estimators=200, max_depth=5, min_samples_leaf=20, random_state=7, n_jobs=-1)
    return GradientBoostingClassifier(n_estimators=150, max_depth=3, learning_rate=0.05, subsample=0.8, random_state=7)


def _metrics(y_true: np.ndarray, proba: np.ndarray) -> dict:
    pred = (proba >= 0.5).astype(int)
    out = {"n": int(len(y_true)), "base_rate": float(y_true.mean()) if len(y_true) else 0.0,
           "accuracy": float(accuracy_score(y_true, pred)),
           "precision": float(precision_score(y_true, pred, zero_division=0)),
           "positives_predicted": int(pred.sum())}
    out["auc"] = float(roc_auc_score(y_true, proba)) if len(set(y_true)) > 1 else None
    return out


@dataclass
class TrainResult:
    symbol: str
    timeframe: str
    kind: str
    validated: bool
    walk_forward: list[dict]
    test: dict
    trained_at: str
    note: str


def train(df_ind: pd.DataFrame, symbol: str, timeframe: str, *, kind: str = "gradient_boosting",
          horizon: int = 12, threshold: float = 0.004, n_splits: int = 5, save: bool = True) -> TrainResult:
    X = build_features(df_ind)
    y = build_labels(df_ind, horizon, threshold)
    data = pd.concat([X, y.rename("y")], axis=1).dropna()
    if len(data) < 400:
        raise ValueError(f"not enough labeled rows to train ({len(data)} < 400)")
    Xv, yv = data[FEATURES].to_numpy(), data["y"].to_numpy().astype(int)

    # Chronological hold-out: last 20% is the untouched out-of-sample test set.
    test_start = int(len(data) * 0.8)
    dev_X, dev_y = Xv[: test_start - horizon], yv[: test_start - horizon]  # purge gap before test

    folds: list[dict] = []
    fold_size = len(dev_X) // (n_splits + 1)
    for k in range(1, n_splits + 1):
        tr_end = fold_size * k
        te_start, te_end = tr_end + horizon, min(tr_end + horizon + fold_size, len(dev_X))
        if te_end - te_start < 20 or len(set(dev_y[:tr_end])) < 2:
            continue
        m = _model(kind).fit(dev_X[:tr_end], dev_y[:tr_end])
        folds.append({"fold": k, "train_size": tr_end, **_metrics(dev_y[te_start:te_end], m.predict_proba(dev_X[te_start:te_end])[:, 1])})

    final = _model(kind).fit(dev_X, dev_y)
    test = _metrics(yv[test_start:], final.predict_proba(Xv[test_start:])[:, 1])
    validated = bool(test["auc"] is not None and test["auc"] > 0.52 and test["precision"] > test["base_rate"]
                     and test["positives_predicted"] >= 10)
    note = ("Out-of-sample metrics beat the base rate; usable as a filter only." if validated else
            "Model did NOT beat the base rate out-of-sample; it will not be used.")
    res = TrainResult(symbol, timeframe, kind, validated, folds, test, datetime.now(timezone.utc).isoformat(), note)
    if save:
        MODEL_DIR.mkdir(parents=True, exist_ok=True)
        with open(MODEL_DIR / f"{symbol}_{timeframe}.pkl", "wb") as fh:
            pickle.dump(final, fh)
        (MODEL_DIR / f"{symbol}_{timeframe}.json").write_text(json.dumps(res.__dict__, indent=2))
    return res


def load_meta(symbol: str, timeframe: str) -> dict | None:
    p = MODEL_DIR / f"{symbol}_{timeframe}.json"
    return json.loads(p.read_text()) if p.exists() else None


def predict_proba(df_ind: pd.DataFrame, symbol: str, timeframe: str) -> tuple[float | None, bool]:
    """Return (P(up), available). Only validated models are considered available."""
    meta = load_meta(symbol, timeframe)
    path = MODEL_DIR / f"{symbol}_{timeframe}.pkl"
    if not meta or not meta.get("validated") or not path.exists():
        return None, False
    with open(path, "rb") as fh:  # files are produced locally by train() only
        model = pickle.load(fh)
    row = build_features(df_ind).iloc[[-1]][FEATURES]
    if row.isna().any(axis=None):
        return None, True
    return float(model.predict_proba(row.to_numpy())[0, 1]), True

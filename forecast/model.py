"""The model itself: one gradient boosted tree model per series.

Why gradient boosting: demand, solar and wind all depend on weather and
time of day in non linear ways (air conditioning kicks in above ~70°F,
solar is zero at night whatever the weather). Boosted trees learn those
shapes without hand written formulas, handle missing values natively,
and train in seconds on a few years of hourly data.
"""

from __future__ import annotations

from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from .features import FEATURES

MODEL_DIR = Path(__file__).resolve().parent.parent / "models"

# Rows need these to be worth learning from (or forecasting with)
REQUIRED = {"demand": ["temp_f"], "solar": ["solar_wm2", "cap_proxy"],
            "wind": ["wind_ms", "fleet_cap"]}

# Solar is learned as a share of recent peak output at that hour instead
# of in MW. California adds solar every month, so next month's output is
# usually higher than anything in the training data, and trees can't
# predict above the range they were trained on. As a share, the model
# learns "how sunny relative to a clear day", and multiplying back by the
# recent peak carries the growth through. Wind gets the same treatment
# with its 60 day peak: summer 2026 wind ran well above any earlier summer,
# and a model trained in MW kept forecasting the older, lower level.
SCALE_BY = {"solar": "cap_proxy", "wind": "fleet_cap"}


def new_model() -> HistGradientBoostingRegressor:
    return HistGradientBoostingRegressor(
        max_iter=400,
        learning_rate=0.05,
        max_leaf_nodes=31,
        min_samples_leaf=40,
        l2_regularization=1.0,
        early_stopping=False,   # its validation split is random, which leaks across time
        random_state=0,
    )


def usable(df: pd.DataFrame, series: str, need_target: bool = True) -> pd.DataFrame:
    mask = df[REQUIRED[series]].notna().all(axis=1)
    if need_target:
        mask &= df[series].notna()
    return df[mask]


def predict(bundle: dict, df: pd.DataFrame, series: str) -> np.ndarray:
    pred = bundle["model"].predict(df[bundle["features"]])
    scale = SCALE_BY.get(series)
    if scale:
        pred = pred * df[scale].to_numpy()
    pred = np.clip(pred, 0, None)
    if series == "solar":
        pred = np.where(df["solar_wm2"].fillna(1) <= 0, 0.0, pred)   # dark = no solar
    return pred


def fit(df: pd.DataFrame, series: str) -> dict:
    # Skip a feature with no data at all (e.g. a newly added station not
    # backfilled yet); the saved bundle remembers which ones were used.
    features = [f for f in FEATURES[series] if df[f].notna().any()]
    target = df[series]
    scale = SCALE_BY.get(series)
    if scale:
        df = df[df[scale] > 0]           # nothing to scale (solar at night)
        target = df[series] / df[scale]
    model = new_model().fit(df[features], target)
    return {"model": model, "features": features, "series": series}


def save(bundle: dict, series: str) -> Path:
    MODEL_DIR.mkdir(exist_ok=True)
    path = MODEL_DIR / f"{series}.joblib"
    joblib.dump(bundle, path)
    return path


def load(series: str) -> dict:
    return joblib.load(MODEL_DIR / f"{series}.joblib")

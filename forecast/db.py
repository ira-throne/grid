"""Database helpers for the forecasting scripts."""

from __future__ import annotations

import os

import pandas as pd
import psycopg
from dotenv import load_dotenv

load_dotenv()


def connect() -> psycopg.Connection:
    return psycopg.connect(os.environ["DATABASE_URL"], autocommit=True)


def frame(conn, sql: str, params: dict | None = None) -> pd.DataFrame:
    """Run a SELECT and return a DataFrame with Decimals turned into floats."""
    cur = conn.execute(sql, params or {})
    df = pd.DataFrame(cur.fetchall(), columns=[c.name for c in cur.description])
    for col in df.columns:
        if df[col].dtype == object:
            values = df[col].dropna()   # a column can be all NULL
            if len(values) and type(values.iloc[0]).__name__ == "Decimal":
                df[col] = df[col].astype(float)
    return df

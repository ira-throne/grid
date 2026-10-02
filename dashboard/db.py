"""Database access for the dashboard.

One connection pool for the whole app. Every query goes through query(),
which returns a pandas DataFrame, so the rest of the code never touches
cursors or connections directly.
"""

from __future__ import annotations

import atexit
import os

import pandas as pd
from dotenv import load_dotenv
from psycopg_pool import ConnectionPool

load_dotenv()

# DASHBOARD_DATABASE_URL lets the web app use a read only login later
# (see sql/05_dashboard_role.sql). Until you set it, the app falls back
# to the same DATABASE_URL the ingest scripts use.
DSN = os.environ.get("DASHBOARD_DATABASE_URL") or os.environ["DATABASE_URL"]

# A pool keeps a few connections open and hands them out per request.
# Opening a fresh Postgres connection on every chart refresh would be
# slow, and with several browser tabs open it adds up fast.
pool = ConnectionPool(DSN, min_size=1, max_size=5, open=True,
                      kwargs={"autocommit": True})
atexit.register(pool.close)  # close connections cleanly when the app stops


def query(sql: str, params: dict | None = None) -> pd.DataFrame:
    """Run a SELECT and return the result as a DataFrame.

    Always pass values through params (%(name)s placeholders) and never
    with f-strings. psycopg then sends them separately from the SQL, which
    is what prevents SQL injection once the dropdowns are on a public site.
    """
    with pool.connection() as conn:
        cur = conn.execute(sql, params or {})
        cols = [c.name for c in cur.description]
        df = pd.DataFrame(cur.fetchall(), columns=cols)

    # Postgres NUMERIC arrives as Python Decimal, which plotly and pandas
    # math handle poorly. Convert those columns to float once, here.
    for col in df.columns:
        if df[col].dtype == object:
            values = df[col].dropna()   # a column can be all NULL
            if len(values) and type(values.iloc[0]).__name__ == "Decimal":
                df[col] = df[col].astype(float)
    return df

"""Shared CAISO OASIS client.

OASIS returns every response as a ZIP. Success = a CSV; failure = an XML
error file inside the ZIP. fetch() unzips, detects errors, retries with
backoff, and returns the CSV text (or None when OASIS says "no data").
"""

from __future__ import annotations

import csv
import io
import logging
import re
import time
import zipfile
from datetime import datetime, timezone

import requests

OASIS_URL = "https://oasis.caiso.com/oasisapi/SingleZip"
PAUSE_SECONDS = 5                # OASIS rate limits aggressive callers
MAX_RETRIES = 5
NO_DATA_CODE = "1000"            # OASIS: no data for this selection

log = logging.getLogger("oasis")


class OasisError(RuntimeError):
    def __init__(self, code: str, desc: str):
        super().__init__(f"OASIS error {code}: {desc}")
        self.code = code


def oasis_time(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H:%M-0000")


def fetch(session: requests.Session, queryname: str, version: str,
          start: datetime, end: datetime, **extra) -> tuple[dict, str, str] | None:
    """Return (params, file_name, csv_text) for one window, or None if no data."""
    params = {
        "queryname": queryname,
        "version": version,
        "resultformat": "6",
        "startdatetime": oasis_time(start),
        "enddatetime": oasis_time(end),
        **{k: v for k, v in extra.items() if v is not None},
    }
    err: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(OASIS_URL, params=params, timeout=120)
            resp.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
                name = zf.namelist()[0]
                body = zf.read(name).decode("utf-8", errors="replace")
            if name.lower().endswith(".xml"):
                code = re.search(r"<(?:\w+:)?ERR_CODE>(\d+)<", body)
                desc = re.search(r"<(?:\w+:)?ERR_DESC>([^<]*)<", body)
                raise OasisError(code.group(1) if code else "?",
                                 desc.group(1) if desc else body[:300])
            return params, name, body
        except OasisError as exc:
            if exc.code == NO_DATA_CODE:
                return None
            err = exc
        except (requests.RequestException, zipfile.BadZipFile) as exc:
            err = exc
        wait = PAUSE_SECONDS * 2 ** attempt
        log.warning("%s attempt %d failed (%s), retrying in %ds", queryname, attempt, err, wait)
        time.sleep(wait)
    raise RuntimeError(f"OASIS {queryname} failed after {MAX_RETRIES} attempts: {err}")


def rows(csv_text: str) -> list[dict]:
    return list(csv.DictReader(io.StringIO(csv_text)))


def interval_start(row: dict) -> datetime:
    return datetime.fromisoformat(row["INTERVALSTARTTIME_GMT"]).astimezone(timezone.utc)

"""
Talks to the SchoolPay API (https://www.schoolpay.co.ug/apidocumentation).

  Sync:     GET /AndroidRS/SyncSchoolTransactions/{code}/{date}/{hash}
            GET /AndroidRS/SchoolRangeTransactions/{code}/{from}/{to}/{hash}  (max 31 days)
            hash = MD5(schoolCode + date_or_fromDate + password), upper-case hex
  Webhook:  signature = SHA256 hex of (password + schoolpayReceiptNumber)
"""
import hashlib
import hmac
from datetime import date, timedelta

import requests
from django.conf import settings

BASE_URL = getattr(settings, "SCHOOLPAY_BASE_URL", "https://schoolpay.co.ug/paymentapi")
TIMEOUT = getattr(settings, "SCHOOLPAY_TIMEOUT_SECONDS", 30)
MAX_RANGE_DAYS = 31


class SchoolPayError(Exception):
    """SchoolPay could not be reached or refused the request."""


def request_hash(school_code: str, identifying_date: str, password: str) -> str:
    raw = f"{school_code}{identifying_date}{password}"
    return hashlib.md5(raw.encode()).hexdigest().upper()


def webhook_signature(password: str, receipt_number: str) -> str:
    return hashlib.sha256(f"{password}{receipt_number}".encode()).hexdigest()


def signature_is_valid(password: str, receipt_number: str, signature: str) -> bool:
    if not (password and receipt_number and signature):
        return False
    expected = webhook_signature(password, str(receipt_number))
    return hmac.compare_digest(expected.lower(), str(signature).strip().lower())


def _get(url):
    try:
        resp = requests.get(url, headers={"Accept": "application/json"}, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise SchoolPayError(f"Could not reach SchoolPay: {exc}") from exc
    if resp.status_code != 200:
        raise SchoolPayError(f"SchoolPay returned HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise SchoolPayError("SchoolPay returned an unreadable response") from exc
    if str(data.get("returnCode")) != "0":
        raise SchoolPayError(
            f"SchoolPay refused the request (code {data.get('returnCode')}): "
            f"{data.get('returnMessage') or 'no message'}"
        )
    return data


def _split(data):
    """-> list of (type, payment_dict)."""
    rows = [("SCHOOL_FEES", t) for t in (data.get("transactions") or [])]
    rows += [("OTHER_FEES", t) for t in (data.get("supplementaryFeePayments") or [])]
    return rows


def fetch_day(school_code, password, day: date):
    d = day.isoformat()
    url = f"{BASE_URL}/AndroidRS/SyncSchoolTransactions/{school_code}/{d}/{request_hash(school_code, d, password)}"
    return _split(_get(url))


def fetch_range(school_code, password, start: date, end: date):
    """Any length of range: split into SchoolPay's 31-day maximum windows."""
    if end < start:
        start, end = end, start
    rows = []
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=MAX_RANGE_DAYS - 1), end)
        f, t = cursor.isoformat(), window_end.isoformat()
        url = (f"{BASE_URL}/AndroidRS/SchoolRangeTransactions/{school_code}/{f}/{t}/"
               f"{request_hash(school_code, f, password)}")
        rows += _split(_get(url))
        cursor = window_end + timedelta(days=1)
    return rows

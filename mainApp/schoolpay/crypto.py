"""
Encrypts each school's SchoolPay API password before it is saved.

Key: settings.FIELD_ENCRYPTION_KEY (recommended - generate once with
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
and keep it in .env). If it is not set, a key is derived from SECRET_KEY;
in that case changing SECRET_KEY means re-entering the SchoolPay passwords.
"""
import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _fernet():
    key = getattr(settings, "FIELD_ENCRYPTION_KEY", "") or ""
    if not key:
        digest = hashlib.sha256(f"schoolpay:{settings.SECRET_KEY}".encode()).digest()
        key = base64.urlsafe_b64encode(digest).decode()
    try:
        return Fernet(key.encode() if isinstance(key, str) else key)
    except ValueError as exc:
        raise ImproperlyConfigured("FIELD_ENCRYPTION_KEY is not a valid Fernet key.") from exc


def encrypt(raw: str) -> str:
    return _fernet().encrypt(raw.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        # Key changed since the password was saved.
        return ""

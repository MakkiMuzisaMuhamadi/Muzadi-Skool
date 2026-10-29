# mainApp/utils2/attendance_tokens.py
"""
Signed tokens printed on student QR cards.

Set ATTENDANCE_QR_SIGNING_KEY in settings (read it from your .env) so the cards
keep working even if you later rotate SECRET_KEY. If it is not set, SECRET_KEY
is used. Changing whichever key is in use invalidates every printed card.
"""
from django.conf import settings
from django.core import signing

SIGN_SALT = "attendance-qr-v1"


def _key():
    return getattr(settings, "ATTENDANCE_QR_SIGNING_KEY", None) or settings.SECRET_KEY


def make_student_token(student_id: int) -> str:
    return signing.dumps({"sid": int(student_id)}, key=_key(), salt=SIGN_SALT)


def parse_student_token(token: str) -> int:
    """Raises signing.BadSignature, ValueError or KeyError if the token is invalid."""
    data = signing.loads(token, key=_key(), salt=SIGN_SALT, max_age=None)
    return int(data["sid"])
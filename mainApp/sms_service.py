import re

from django.conf import settings

import africastalking

UG_E164_RE = re.compile(r"^\+2567\d{8}$")

# Africa's Talking recipient status codes that mean "accepted by the network"
AT_ACCEPTED_CODES = {100, 101, 102}   # 100 Processed, 101 Sent, 102 Queued


def normalize_ug_number(raw) -> str:
    """
    Accepts: 0772123456, 772123456, 256772123456, +256 772 123 456, 0772-123-456, 772123456.0 (Excel)
    Returns: +256772123456 (or the cleaned input if it can't be normalised)
    """
    if not raw:
        return ""
    s = str(raw).strip()
    if re.fullmatch(r"\d+\.0+", s):      # Excel stores numbers as 772123456.0
        s = s.split(".")[0]
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)

    if digits.startswith("256") and len(digits) == 12:
        return "+" + digits
    if digits.startswith("0") and len(digits) == 10:
        return "+256" + digits[1:]
    if digits.startswith("7") and len(digits) == 9:
        return "+256" + digits
    return ("+" if has_plus else "") + digits


def is_valid_ug_number(e164: str) -> bool:
    return bool(UG_E164_RE.match(e164 or ""))


def pick_number(phone, alternate_phone=None, use_alternate_fallback=True) -> str:
    """First valid Ugandan mobile number from phone (then alternate_phone), or ""."""
    p1 = normalize_ug_number(phone)
    if is_valid_ug_number(p1):
        return p1
    if use_alternate_fallback:
        p2 = normalize_ug_number(alternate_phone)
        if is_valid_ug_number(p2):
            return p2
    return ""


def pick_parent_number(parent, use_alternate_fallback=True) -> str:
    return pick_number(
        getattr(parent, "phone", ""),
        getattr(parent, "alternate_phone", ""),
        use_alternate_fallback,
    )


def is_gsm_text(text: str) -> bool:
    """Rough check: plain ASCII stays in the cheap 160-char encoding."""
    return all(ord(ch) < 128 for ch in (text or ""))


def sms_units(text: str) -> int:
    """Number of SMS segments the text will be billed as."""
    n = len(text or "")
    single, multi = (160, 153) if is_gsm_text(text) else (70, 67)
    return 1 if n <= single else -(-n // multi)


class AfricaTalkingSMS:
    _initialized = False

    def __init__(self):
        username = settings.AFRICASTALKING_USERNAME
        if getattr(settings, "AFRICASTALKING_ENV", "prod").lower() == "sandbox":
            username = "sandbox"

        if not AfricaTalkingSMS._initialized:
            africastalking.initialize(username=username, api_key=settings.AFRICASTALKING_API_KEY)
            AfricaTalkingSMS._initialized = True

        self.sms = africastalking.SMS
        self.sender_id = getattr(settings, "AFRICASTALKING_SENDER_ID", "") or None

    def send_one(self, message: str, to_number: str, enqueue: bool = True) -> dict:
        recipients = [to_number]
        if self.sender_id:
            return self.sms.send(message, recipients, sender_id=self.sender_id, enqueue=enqueue)
        return self.sms.send(message, recipients, enqueue=enqueue)

    @staticmethod
    def parse_response(resp: dict) -> dict:
        """
        Turns the raw AT response into:
        {"ok": bool, "message_id": str, "status": str, "cost": str}
        AT returns HTTP success even when a number is rejected, so we must
        check the recipient statusCode, not just "no exception".
        """
        resp = resp or {}
        data = resp.get("SMSMessageData", {}) or {}
        recs = data.get("Recipients", []) or []
        rec = recs[0] if recs else {}
        try:
            code = int(rec.get("statusCode", 0))
        except (TypeError, ValueError):
            code = 0
        return {
            "ok": code in AT_ACCEPTED_CODES,
            "message_id": "" if rec.get("messageId") in (None, "None") else rec["messageId"],
            "status": rec.get("status", "") or data.get("Message", "") or "No recipient returned",
            "cost": rec.get("cost", "") or "",
        }

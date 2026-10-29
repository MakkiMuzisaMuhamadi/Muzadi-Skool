# mainApp/whatsapp_service.py
"""
WhatsApp Cloud API (Meta) helper.

settings.py:
    WHATSAPP_TOKEN               = config("WHATSAPP_TOKEN", default="")
    WHATSAPP_PHONE_NUMBER_ID     = config("WHATSAPP_PHONE_NUMBER_ID", default="")
    WHATSAPP_BUSINESS_ACCOUNT_ID = config("WHATSAPP_BUSINESS_ACCOUNT_ID", default="")
    WHATSAPP_API_VERSION         = config("WHATSAPP_API_VERSION", default="v21.0")
    WHATSAPP_VERIFY_TOKEN        = config("WHATSAPP_VERIFY_TOKEN", default="")
    WHATSAPP_APP_SECRET          = config("WHATSAPP_APP_SECRET", default="")   # optional, recommended

Rules to remember:
  * Messages the school starts MUST use a template that Meta has approved.
  * Template blanks are {{1}}, {{2}} ... (positional) or {{name}} (named).
  * WhatsApp numbers are sent WITHOUT "+": 256772123456
"""
import hashlib
import hmac
import re

import requests
from django.conf import settings

from mainApp.sms_service import normalize_ug_number, is_valid_ug_number

GRAPH_URL = "https://graph.facebook.com"
PARAM_RE = re.compile(r"\{\{\s*([A-Za-z0-9_]+)\s*\}\}")

# Meta error codes that mean "this person can't get WhatsApp on this number"
NOT_ON_WHATSAPP_CODES = {131026, 131030, 131049, 131050}


# ---------------------------------------------------------------------------
# Numbers
# ---------------------------------------------------------------------------
def to_wa_number(raw) -> str:
    """0772123456 / +256772123456 / 772123456.0  ->  256772123456  (or "")."""
    n = normalize_ug_number(raw)
    return n[1:] if is_valid_ug_number(n) else ""


def pick_whatsapp_number(whatsapp, phone=None, alternate=None, use_phone_fallback=True) -> str:
    """Parent's WhatsApp number first, then (optionally) their normal phone numbers."""
    n = to_wa_number(whatsapp)
    if n:
        return n
    if use_phone_fallback:
        return to_wa_number(phone) or to_wa_number(alternate)
    return ""


def pick_parent_whatsapp(parent, use_phone_fallback=True) -> str:
    return pick_whatsapp_number(
        getattr(parent, "whatsapp_number", ""),
        getattr(parent, "phone", ""),
        getattr(parent, "alternate_phone", ""),
        use_phone_fallback,
    )


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------
def template_param_names(body: str):
    """
    Returns (format, names) for a template body.
      "Dear {{1}}, {{2}} ..."          -> ("POSITIONAL", ["1", "2"])
      "Dear {{parent_name}} ..."       -> ("NAMED", ["parent_name"])
    """
    names = []
    for m in PARAM_RE.finditer(body or ""):
        if m.group(1) not in names:
            names.append(m.group(1))
    if names and all(n.isdigit() for n in names):
        return "POSITIONAL", sorted(names, key=int)
    return ("NAMED", names) if names else ("POSITIONAL", [])


def fill_template_body(body: str, param_names, values) -> str:
    """Puts the values into the template text (used for the log / preview)."""
    mapping = dict(zip([str(n) for n in param_names], values))
    return PARAM_RE.sub(lambda m: mapping.get(m.group(1), m.group(0)), body or "")


def clean_param(text) -> str:
    """
    Meta rejects parameters with new lines, tabs or more than 4 spaces in a row,
    and rejects empty parameters.
    """
    s = re.sub(r"[\r\n\t]+", " ", str(text or ""))
    s = re.sub(r" {2,}", " ", s).strip()
    return s[:1000] or "-"


# ---------------------------------------------------------------------------
# API client
# ---------------------------------------------------------------------------
class WhatsAppCloud:
    def __init__(self):
        self.token = getattr(settings, "WHATSAPP_TOKEN", "")
        self.phone_number_id = getattr(settings, "WHATSAPP_PHONE_NUMBER_ID", "")
        self.waba_id = getattr(settings, "WHATSAPP_BUSINESS_ACCOUNT_ID", "")
        self.version = getattr(settings, "WHATSAPP_API_VERSION", "v21.0") or "v21.0"

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.phone_number_id)

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}", "Content-Type": "application/json"}

    def _url(self, path):
        return f"{GRAPH_URL}/{self.version}/{path}"

    # ---- sending ----
    def send_template(self, to: str, template_name: str, language: str,
                      params=None, param_format="POSITIONAL", param_names=None) -> dict:
        if not self.is_configured:
            raise RuntimeError("WhatsApp is not configured (WHATSAPP_TOKEN / WHATSAPP_PHONE_NUMBER_ID).")

        template = {"name": template_name, "language": {"code": language or "en"}}
        params = params or []
        if params:
            parameters = []
            for i, value in enumerate(params):
                p = {"type": "text", "text": clean_param(value)}
                if param_format == "NAMED" and param_names and i < len(param_names):
                    p["parameter_name"] = param_names[i]
                parameters.append(p)
            template["components"] = [{"type": "body", "parameters": parameters}]

        payload = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": to,
            "type": "template",
            "template": template,
        }
        r = requests.post(self._url(f"{self.phone_number_id}/messages"),
                          json=payload, headers=self._headers(), timeout=20)
        try:
            return r.json()
        except ValueError:
            return {"error": {"message": f"HTTP {r.status_code}: {r.text[:200]}", "code": r.status_code}}

    @staticmethod
    def parse_response(resp: dict) -> dict:
        """
        Success: {"messages": [{"id": "wamid..."}], ...}
        Error:   {"error": {"message": "...", "code": 131030, ...}}
        """
        resp = resp or {}
        msgs = resp.get("messages") or []
        if msgs and msgs[0].get("id"):
            return {"ok": True, "message_id": msgs[0]["id"], "status": "Accepted by WhatsApp", "code": None}

        err = resp.get("error") or {}
        code = err.get("code")
        detail = (err.get("error_data") or {}).get("details") or ""
        message = err.get("message") or "Unknown WhatsApp error"
        return {
            "ok": False,
            "message_id": "",
            "status": f"WhatsApp error {code}: {message} {detail}".strip(),
            "code": code,
        }

    # ---- templates ----
    def fetch_templates(self):
        """All templates in the WhatsApp Business Account (follows paging)."""
        if not (self.token and self.waba_id):
            raise RuntimeError("Set WHATSAPP_TOKEN and WHATSAPP_BUSINESS_ACCOUNT_ID to sync templates.")

        url = self._url(f"{self.waba_id}/message_templates")
        params = {"fields": "name,language,status,category,components,parameter_format", "limit": 100}
        results = []
        for _ in range(50):                      # safety limit on pages
            r = requests.get(url, params=params, headers=self._headers(), timeout=20)
            data = r.json()
            if "error" in data:
                raise RuntimeError(data["error"].get("message", "Could not load templates"))
            results.extend(data.get("data", []))
            nxt = (data.get("paging") or {}).get("next")
            if not nxt:
                break
            url, params = nxt, None
        return results

    @staticmethod
    def describe_template(t: dict) -> dict:
        """
        Turns Meta's template JSON into what we store.
        Only templates whose variables are all in the BODY are supported
        (static headers / footers / quick-reply buttons are fine).
        """
        body = ""
        supported = True
        for comp in t.get("components", []):
            ctype = comp.get("type")
            if ctype == "BODY":
                body = comp.get("text", "")
            elif ctype == "HEADER":
                if comp.get("format", "TEXT") != "TEXT" or PARAM_RE.search(comp.get("text", "")):
                    supported = False            # media header or header variables
            elif ctype == "BUTTONS":
                for b in comp.get("buttons", []):
                    if b.get("type") == "URL" and PARAM_RE.search(b.get("url", "")):
                        supported = False        # dynamic link buttons

        fmt, names = template_param_names(body)
        if (t.get("parameter_format") or "").upper() == "NAMED":
            fmt = "NAMED"
        return {
            "name": t.get("name", ""),
            "language": t.get("language", "en"),
            "status": t.get("status", ""),
            "category": t.get("category", ""),
            "body": body,
            "param_format": fmt,
            "param_names": names,
            "is_supported": supported,
        }

    # ---- webhook security ----
    @staticmethod
    def verify_signature(raw_body: bytes, signature_header: str) -> bool:
        """Checks X-Hub-Signature-256 when WHATSAPP_APP_SECRET is set."""
        secret = getattr(settings, "WHATSAPP_APP_SECRET", "")
        if not secret:
            return True
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, signature_header.split("=", 1)[1])

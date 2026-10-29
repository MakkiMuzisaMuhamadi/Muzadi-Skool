import secrets

from django.contrib.auth.models import User
from django.db import models

from mainApp.models.auditModels import AuditMixin
from mainApp.models.fees_models import FeeCategory, StudentFeePayment
from mainApp.models.models import School
from mainApp.models.student import Student


def _new_webhook_token():
    """Secret part of a school's webhook address.

    Keep this name: migration 0078 refers to it (mainApp.models.schoolpay_models._new_webhook_token).
    """
    return secrets.token_urlsafe(32)


# Older name, kept so nothing that imported it breaks.
_new_token = _new_webhook_token


class SchoolPayConfig(AuditMixin, models.Model):
    """One per school: the SchoolPay school code, encrypted API password and switches."""
    school = models.OneToOneField(School, on_delete=models.CASCADE, related_name="schoolpay_config")
    school_code = models.CharField(max_length=20, blank=True)
    api_password_encrypted = models.TextField(blank=True, default="")
    is_enabled = models.BooleanField(default=False)
    strict_mode = models.BooleanField(
        default=False,
        help_text="Off: webhook payments post at once and the hourly check confirms them. "
                  "On: they wait until SchoolPay's own records confirm the amount.")
    sms_parent_on_payment = models.BooleanField(default=True)
    sync_days_back = models.PositiveSmallIntegerField(default=3)
    allowed_ips = models.TextField(blank=True, default="")

    webhook_token = models.CharField(max_length=64, unique=True, default=_new_webhook_token,
                                     help_text="Secret part of this school's webhook address.")
    last_webhook_at = models.DateTimeField(null=True, blank=True)
    last_sync_at = models.DateTimeField(null=True, blank=True)
    last_sync_ok = models.BooleanField(default=False)
    last_sync_message = models.CharField(max_length=255, blank=True)

    class Meta:
        verbose_name = "SchoolPay connection"

    def __str__(self):
        return f"SchoolPay {self.school_code or '-'} ({self.school})"

    # --- password -------------------------------------------------------
    @property
    def has_password(self):
        return bool(self.api_password_encrypted)

    def set_api_password(self, raw):
        from mainApp.schoolpay.crypto import encrypt
        self.api_password_encrypted = encrypt(raw) if raw else ""

    def get_api_password(self):
        if not self.api_password_encrypted:
            return ""
        from mainApp.schoolpay.crypto import decrypt
        return decrypt(self.api_password_encrypted)

    # --- webhook --------------------------------------------------------
    def regenerate_webhook_token(self):
        self.webhook_token = _new_webhook_token()

    @property
    def allowed_ip_list(self):
        return [x.strip() for x in (self.allowed_ips or "").splitlines() if x.strip()]


class SchoolPayFeeMapping(AuditMixin, models.Model):
    """SchoolPay 'supplementary fee' (e.g. Uniform) -> our FeeCategory, so the money pays that fee first."""
    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="schoolpay_fee_mappings")
    supplementary_fee_id = models.CharField(max_length=20)
    description = models.CharField(max_length=150, blank=True)
    fee_category = models.ForeignKey(FeeCategory, on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="schoolpay_mappings")

    class Meta:
        unique_together = ("school", "supplementary_fee_id")
        ordering = ["description"]

    def __str__(self):
        return f"{self.description or self.supplementary_fee_id} -> {self.fee_category or 'not mapped'}"


class SchoolPayTransaction(AuditMixin, models.Model):
    """Every payment SchoolPay tells us about, posted or not. The receipt number is unique per school."""
    TYPE_SCHOOL_FEES = "SCHOOL_FEES"
    TYPE_OTHER_FEES = "OTHER_FEES"
    TYPE_CHOICES = [(TYPE_SCHOOL_FEES, "School fees"), (TYPE_OTHER_FEES, "Other fees")]

    SOURCE_WEBHOOK = "WEBHOOK"
    SOURCE_SYNC = "SYNC"
    SOURCE_CHOICES = [(SOURCE_WEBHOOK, "Instant notification"), (SOURCE_SYNC, "Scheduled check")]

    STATUS_POSTED = "POSTED"
    STATUS_PENDING = "PENDING"
    STATUS_UNMATCHED = "UNMATCHED"
    STATUS_FAILED = "FAILED"
    STATUS_MISMATCH = "MISMATCH"
    STATUS_IGNORED = "IGNORED"
    STATUS_CHOICES = [
        (STATUS_POSTED, "Posted"),
        (STATUS_PENDING, "Waiting for verification"),
        (STATUS_UNMATCHED, "Student not found"),
        (STATUS_FAILED, "Could not post"),
        (STATUS_MISMATCH, "Amount mismatch"),
        (STATUS_IGNORED, "Ignored"),
    ]
    NEEDS_ATTENTION = [STATUS_UNMATCHED, STATUS_FAILED, STATUS_MISMATCH]

    school = models.ForeignKey(School, on_delete=models.CASCADE, related_name="schoolpay_transactions")
    receipt_number = models.CharField(max_length=40)
    transaction_type = models.CharField(max_length=20, choices=TYPE_CHOICES, default=TYPE_SCHOOL_FEES)
    source = models.CharField(max_length=10, choices=SOURCE_CHOICES)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=STATUS_FAILED, db_index=True)
    status_message = models.CharField(max_length=255, blank=True)

    amount = models.DecimalField(max_digits=12, decimal_places=2)
    synced_amount = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    verified_by_sync = models.BooleanField(default=False)
    paid_at = models.DateTimeField(null=True, blank=True)

    student_payment_code = models.CharField(max_length=40, blank=True)
    student_registration_number = models.CharField(max_length=60, blank=True)
    student_name = models.CharField(max_length=150, blank=True)
    student_class = models.CharField(max_length=60, blank=True)
    channel = models.CharField(max_length=60, blank=True)
    channel_transaction_id = models.CharField(max_length=80, blank=True)
    payer_detail = models.CharField(max_length=255, blank=True)
    settlement_bank = models.CharField(max_length=40, blank=True)
    supplementary_fee_id = models.CharField(max_length=20, blank=True)
    supplementary_fee_description = models.CharField(max_length=150, blank=True)
    completion_status = models.CharField(max_length=30, blank=True)
    raw_payload = models.JSONField(default=dict, blank=True)

    student = models.ForeignKey(Student, on_delete=models.PROTECT, null=True, blank=True,
                                related_name="schoolpay_transactions")
    fee_payment = models.ForeignKey(StudentFeePayment, on_delete=models.PROTECT, null=True, blank=True,
                                    related_name="schoolpay_transactions")
    resolved_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="resolved_schoolpay_transactions")
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-paid_at", "-id"]
        constraints = [
            models.UniqueConstraint(fields=["school", "receipt_number"], name="uniq_schoolpay_receipt_per_school"),
        ]
        indexes = [models.Index(fields=["school", "status"]), models.Index(fields=["school", "paid_at"])]

    def __str__(self):
        return f"SchoolPay {self.receipt_number} UGX {self.amount:,.0f}"

    @property
    def needs_attention(self):
        return self.status in self.NEEDS_ATTENTION
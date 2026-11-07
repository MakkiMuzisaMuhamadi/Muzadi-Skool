"""
Platform (Muzadi Technologies) models: things that belong to running the
system for many schools, not to one school's daily work.

  * SchoolSubscription   - plan, dates, modules switched on, suspension
  * SubscriptionPayment  - what a school paid you for its subscription
  * SchoolMessagingAccount - SMS / WhatsApp switches and the SMS credit wallet
  * SmsCreditEntry       - every top-up, SMS charge and refund (the wallet ledger)
  * ReleaseNote / ReleaseNoteRead - "What's new" after an upgrade
"""
from datetime import timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.db import models
from django.utils import timezone

__all__ = [
    "MODULE_CHOICES", "ALL_MODULES", "PLAN_CHOICES", "PLAN_MODULES",
    "SchoolSubscription", "SubscriptionPayment", "SchoolMessagingAccount", "SmsCreditEntry",
    "ReleaseNote", "ReleaseNoteRead",
]


# ---------------------------------------------------------------------------
# Modules a school can have switched on or off
# ---------------------------------------------------------------------------
# Core features (students, teachers, marks, report cards, fees, attendance,
# calendar, users) are always on. These are the optional ones.
MODULE_CHOICES = [
    ("timetable", "Timetable"),
    ("boarding", "Boarding / hostels"),
    ("transport", "Transport"),
    ("health", "Health / sick bay"),
    ("inventory", "Inventory & library"),
    ("online_exams", "Online exams"),
    ("messaging", "SMS & WhatsApp to parents"),
    ("schoolpay", "SchoolPay integration"),
]
ALL_MODULES = [code for code, _ in MODULE_CHOICES]

PLAN_CHOICES = [
    ("BASIC", "Basic"),
    ("STANDARD", "Standard"),
    ("PREMIUM", "Premium"),
    ("CUSTOM", "Custom"),
]
# Modules each plan switches on when you pick the plan. You can still tick or
# untick modules per school afterwards (the plan then shows as Custom).
PLAN_MODULES = {
    "BASIC": ["messaging", "schoolpay"],
    "STANDARD": ["messaging", "schoolpay", "timetable", "health", "online_exams"],
    "PREMIUM": list(ALL_MODULES),
}


def default_modules():
    return list(ALL_MODULES)


class SchoolSubscription(models.Model):
    # Stored status (what you set). The *effective* state also depends on dates - see state().
    STATUS_TRIAL = "TRIAL"
    STATUS_ACTIVE = "ACTIVE"
    STATUS_SUSPENDED = "SUSPENDED"
    STATUS_CHOICES = [
        (STATUS_TRIAL, "Free trial"),
        (STATUS_ACTIVE, "Active (paid)"),
        (STATUS_SUSPENDED, "Suspended by Muzadi"),
    ]

    # Effective states
    STATE_TRIAL = "TRIAL"
    STATE_ACTIVE = "ACTIVE"
    STATE_GRACE = "GRACE"          # expired, warning banner, still fully usable
    STATE_READ_ONLY = "READ_ONLY"  # grace over: can view and download, cannot change anything
    STATE_SUSPENDED = "SUSPENDED"  # switched off by you

    school = models.OneToOneField("mainApp.School", on_delete=models.CASCADE, related_name="subscription")
    plan = models.CharField(max_length=20, choices=PLAN_CHOICES, default="PREMIUM")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default=STATUS_ACTIVE)
    trial_ends_on = models.DateField(null=True, blank=True)
    # Last day that has been paid for. Empty = no end date (e.g. schools set up before subscriptions).
    paid_until = models.DateField(null=True, blank=True)
    grace_days = models.PositiveSmallIntegerField(default=14)
    student_limit = models.PositiveIntegerField(default=0, help_text="0 = no limit")
    price_per_term_ugx = models.DecimalField(max_digits=12, decimal_places=0, default=0)
    enabled_modules = models.JSONField(default=default_modules, blank=True)
    suspended_reason = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "School subscription"

    def __str__(self):
        return f"{self.school} - {self.get_plan_display()} ({self.state()})"

    # -- dates -------------------------------------------------------------
    def end_date(self):
        if self.status == self.STATUS_TRIAL:
            return self.trial_ends_on
        return self.paid_until

    def state(self, today=None):
        today = today or timezone.localdate()
        if self.status == self.STATUS_SUSPENDED:
            return self.STATE_SUSPENDED
        end = self.end_date()
        if end is None or today <= end:
            return self.STATE_TRIAL if self.status == self.STATUS_TRIAL else self.STATE_ACTIVE
        if today <= end + timedelta(days=self.grace_days):
            return self.STATE_GRACE
        return self.STATE_READ_ONLY

    def days_left(self, today=None):
        """Days until the end date (negative once passed), or None when there is no end date."""
        end = self.end_date()
        if end is None:
            return None
        return (end - (today or timezone.localdate())).days

    def grace_ends_on(self):
        end = self.end_date()
        return end + timedelta(days=self.grace_days) if end else None

    # -- modules -----------------------------------------------------------
    def has_module(self, code):
        return code in (self.enabled_modules or [])

    def apply_plan(self, plan):
        self.plan = plan
        if plan in PLAN_MODULES:
            self.enabled_modules = list(PLAN_MODULES[plan])


class SubscriptionPayment(models.Model):
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="subscription_payments")
    amount_ugx = models.DecimalField(max_digits=12, decimal_places=0)
    paid_on = models.DateField(default=timezone.localdate)
    covers_until = models.DateField(help_text="The subscription is paid up to this date.")
    reference = models.CharField(max_length=100, blank=True, default="", help_text="Receipt / mobile money ID")
    note = models.CharField(max_length=255, blank=True, default="")
    recorded_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-paid_on", "-id"]

    def __str__(self):
        return f"{self.school} UGX {self.amount_ugx:,.0f} on {self.paid_on}"


# ---------------------------------------------------------------------------
# Messaging: switches + SMS credit wallet
# ---------------------------------------------------------------------------
class SchoolMessagingAccount(models.Model):
    BILLING_PREPAID = "PREPAID"
    BILLING_POSTPAID = "POSTPAID"
    BILLING_CHOICES = [
        (BILLING_PREPAID, "Prepaid - SMS stop when the credit runs out"),
        (BILLING_POSTPAID, "Postpaid - send freely, invoice the usage monthly"),
    ]

    school = models.OneToOneField("mainApp.School", on_delete=models.CASCADE, related_name="messaging")
    sms_enabled = models.BooleanField(default=True)
    whatsapp_enabled = models.BooleanField(default=False)
    billing_mode = models.CharField(max_length=10, choices=BILLING_CHOICES, default=BILLING_PREPAID)
    # One SMS unit = one 160-character SMS (longer messages use several units).
    credit_units = models.IntegerField(default=0)
    monthly_cap_units = models.PositiveIntegerField(default=0, help_text="0 = no monthly limit")
    low_balance_alert_units = models.PositiveIntegerField(default=200)
    price_per_unit_ugx = models.DecimalField(max_digits=8, decimal_places=0, default=35,
                                             help_text="What you charge the school per SMS unit")
    sender_id = models.CharField(max_length=11, blank=True, default="",
                                 help_text="Approved Africa's Talking sender ID (max 11 characters). "
                                           "Empty = the platform's default sender ID.")
    # Optional: the school uses its own Africa's Talking account instead of yours.
    own_provider = models.BooleanField(default=False)
    at_username = models.CharField(max_length=100, blank=True, default="")
    at_api_key_encrypted = models.TextField(blank=True, default="")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "School messaging account"

    def __str__(self):
        return f"{self.school} messaging ({self.credit_units} units)"

    def set_api_key(self, raw):
        from mainApp.schoolpay.crypto import encrypt
        self.at_api_key_encrypted = encrypt(raw) if raw else ""

    def get_api_key(self):
        from mainApp.schoolpay.crypto import decrypt
        return decrypt(self.at_api_key_encrypted) if self.at_api_key_encrypted else ""

    @property
    def is_prepaid(self):
        return self.billing_mode == self.BILLING_PREPAID

    @property
    def is_low(self):
        return self.is_prepaid and self.credit_units <= self.low_balance_alert_units


class SmsCreditEntry(models.Model):
    KIND_TOPUP = "TOPUP"
    KIND_USAGE = "USAGE"
    KIND_REFUND = "REFUND"
    KIND_ADJUST = "ADJUST"
    KIND_CHOICES = [
        (KIND_TOPUP, "Top-up"),
        (KIND_USAGE, "SMS sent"),
        (KIND_REFUND, "Refund (SMS not delivered to network)"),
        (KIND_ADJUST, "Adjustment"),
    ]

    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="sms_credit_entries")
    kind = models.CharField(max_length=10, choices=KIND_CHOICES)
    units = models.IntegerField(help_text="Positive adds credit, negative uses credit")
    balance_after = models.IntegerField()
    amount_ugx = models.DecimalField(max_digits=12, decimal_places=0, default=0)
    note = models.CharField(max_length=255, blank=True, default="")
    refund_of = models.OneToOneField("self", null=True, blank=True, on_delete=models.SET_NULL,
                                     related_name="refund")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["school", "kind", "created_at"])]

    def __str__(self):
        return f"{self.school} {self.kind} {self.units:+d}"


# ---------------------------------------------------------------------------
# What's new
# ---------------------------------------------------------------------------
class ReleaseNote(models.Model):
    AUDIENCE_CHOICES = [
        ("ADMINS", "School admins"),
        ("STAFF", "All staff"),
        ("EVERYONE", "Everyone (including students & parents)"),
    ]
    version = models.CharField(max_length=30, blank=True, default="", help_text="e.g. 2.4")
    title = models.CharField(max_length=200)
    body = models.TextField(help_text="What changed and how to use it. Plain text; blank lines start new paragraphs.")
    audience = models.CharField(max_length=10, choices=AUDIENCE_CHOICES, default="STAFF")
    is_published = models.BooleanField(default=False)
    published_at = models.DateTimeField(null=True, blank=True)
    # Empty = every school. Otherwise only these schools see it (e.g. a feature on early access).
    schools = models.ManyToManyField("mainApp.School", blank=True, related_name="release_notes")
    created_by = models.ForeignKey(User, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-published_at", "-id"]

    def __str__(self):
        return f"{self.version} {self.title}".strip()

    def publish(self):
        self.is_published = True
        self.published_at = self.published_at or timezone.now()


class ReleaseNoteRead(models.Model):
    note = models.ForeignKey(ReleaseNote, on_delete=models.CASCADE, related_name="reads")
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="release_note_reads")
    read_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("note", "user")

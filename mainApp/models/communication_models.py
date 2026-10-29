# mainApp/models/communication_models.py
from django.db import models
from django.conf import settings
from django.utils import timezone

from mainApp.models.models import AuditMixin
from mainApp.models.student import Parent, Student


PLACEHOLDER_HELP = (
    "You can use placeholders: {student_name}, {first_name}, {class}, {stream}, "
    "{school_name}, {parent_name}"
)


class SmsTemplate(AuditMixin, models.Model):
    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="sms_templates")
    name = models.CharField(max_length=120)
    body = models.TextField(help_text=PLACEHOLDER_HELP)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ("school", "name")
        ordering = ["name"]

    def __str__(self):
        return self.name


class WhatsAppTemplate(AuditMixin, models.Model):
    """
    A WhatsApp message template approved by Meta.
    Filled automatically by "Sync from WhatsApp" on the WhatsApp templates page.
    """
    FORMAT_CHOICES = [("POSITIONAL", "Numbered {{1}}"), ("NAMED", "Named {{name}}")]

    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="whatsapp_templates")
    name = models.CharField(max_length=512, help_text="Template name exactly as in WhatsApp Manager")
    language = models.CharField(max_length=15, default="en")
    category = models.CharField(max_length=30, blank=True, default="")
    status = models.CharField(max_length=30, blank=True, default="APPROVED")
    body = models.TextField(blank=True, default="")
    param_format = models.CharField(max_length=12, choices=FORMAT_CHOICES, default="POSITIONAL")
    param_names = models.JSONField(default=list, blank=True)   # ["1","2"] or ["parent_name", ...]
    is_supported = models.BooleanField(default=True)           # False: media header / dynamic buttons
    is_active = models.BooleanField(default=True)
    last_synced_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        unique_together = ("school", "name", "language")
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.language})"

    @property
    def param_count(self):
        return len(self.param_names or [])

    @property
    def is_usable(self):
        return self.is_active and self.is_supported and self.status == "APPROVED"


class SmsCampaign(AuditMixin, models.Model):
    CHANNEL_CHOICES = [
        ("SMS", "SMS"),
        ("WHATSAPP", "WhatsApp"),
        ("WHATSAPP_SMS", "WhatsApp, SMS if not on WhatsApp"),
    ]
    AUDIENCE_CHOICES = [
        ("ALL", "All Parents"),
        ("LEVEL", "By Level"),
        ("CLASS", "By Class"),
        ("STREAM", "By Stream"),
        ("STUDENTS", "Selected Students"),
        ("PARENTS", "Selected Parents"),
    ]

    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="sms_campaigns")
    title = models.CharField(max_length=160)
    audience = models.CharField(max_length=20, choices=AUDIENCE_CHOICES)
    channel = models.CharField(max_length=20, choices=CHANNEL_CHOICES, default="SMS")
    template = models.ForeignKey(SmsTemplate, on_delete=models.SET_NULL, null=True, blank=True)

    # WhatsApp: the approved template and what was typed in each blank (may contain {student_name} etc.)
    wa_template = models.ForeignKey(WhatsAppTemplate, on_delete=models.SET_NULL, null=True, blank=True)
    wa_params = models.JSONField(default=list, blank=True)

    # SMS text (before placeholders are filled). Empty for WhatsApp-only campaigns.
    message_body = models.TextField(blank=True, default="")

    # filters used (store IDs/params so campaign is reproducible)
    filters = models.JSONField(default=dict, blank=True)

    sender_id_used = models.CharField(max_length=40, blank=True, default="")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    queued_at = models.DateTimeField(default=timezone.now)

    class Meta:
        ordering = ["-queued_at"]

    def __str__(self):
        return f"{self.title} ({self.queued_at.date()})"


class SmsMessage(AuditMixin, models.Model):
    CHANNEL_CHOICES = [("SMS", "SMS"), ("WHATSAPP", "WhatsApp")]

    STATUS_CHOICES = [
        ("PENDING", "Pending"),
        ("SENDING", "Sending"),          # NEW: row claimed by a batch, prevents double sending
        ("SENT", "Sent"),
        ("FAILED", "Failed"),            # rejected before/at Africa's Talking
        ("DELIVERED", "Delivered"),
        ("READ", "Read"),                # WhatsApp only: the parent opened the message
        ("UNDELIVERED", "Undelivered"),  # accepted, but the network could not deliver
        ("UNKNOWN", "Unknown"),
    ]

    school = models.ForeignKey("mainApp.School", on_delete=models.CASCADE, related_name="sms_messages")
    campaign = models.ForeignKey(SmsCampaign, on_delete=models.CASCADE, related_name="messages")

    # optional links for traceability
    student = models.ForeignKey(Student, on_delete=models.SET_NULL, null=True, blank=True)
    parent = models.ForeignKey(Parent, on_delete=models.SET_NULL, null=True, blank=True)

    channel = models.CharField(max_length=10, choices=CHANNEL_CHOICES, default="SMS")
    to_number = models.CharField(max_length=20)
    text = models.TextField()

    # WhatsApp: the filled-in blanks for this recipient (kept so a retry sends the same thing)
    wa_params = models.JSONField(default=list, blank=True)
    # "WhatsApp, SMS if not on WhatsApp": what to send by SMS if WhatsApp fails
    fallback_text = models.TextField(blank=True, default="")
    fallback_number = models.CharField(max_length=20, blank=True, default="")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="PENDING")
    status_detail = models.CharField(max_length=255, blank=True, default="")

    provider = models.CharField(max_length=40, default="AFRICASTALKING")
    provider_message_id = models.CharField(max_length=120, blank=True, default="")

    cost = models.CharField(max_length=40, blank=True, default="")  # AT returns "UGX x.xx"
    units = models.PositiveIntegerField(default=1)

    sent_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    read_at = models.DateTimeField(null=True, blank=True)

    raw_response = models.JSONField(default=dict, blank=True)
    raw_dlr = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ["-id"]
        indexes = [
            models.Index(fields=["school", "status"]),
            models.Index(fields=["school", "provider_message_id"]),
            models.Index(fields=["campaign", "status"]),   # NEW: batch sending filters by campaign+status
        ]

    def __str__(self):
        return f"{self.to_number} - {self.status}"

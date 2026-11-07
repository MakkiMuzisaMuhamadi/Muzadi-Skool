"""
Schools that existed before subscriptions keep working exactly as before:
  * subscription ACTIVE with no end date and every module switched on
  * messaging POSTPAID (no SMS credit needed), SMS and WhatsApp on
  * an empty school code is filled in (the code must be unique)
"""
import uuid

from django.db import migrations


def forwards(apps, schema_editor):
    School = apps.get_model("mainApp", "School")
    SchoolSubscription = apps.get_model("mainApp", "SchoolSubscription")
    SchoolMessagingAccount = apps.get_model("mainApp", "SchoolMessagingAccount")
    all_modules = ["timetable", "boarding", "transport", "health", "inventory",
                   "online_exams", "messaging", "schoolpay"]
    for school in School._base_manager.all():
        if not school.school_id:
            School._base_manager.filter(pk=school.pk).update(school_id=f"SCH-{uuid.uuid4().hex[:8].upper()}")
        SchoolSubscription.objects.get_or_create(
            school=school,
            defaults={"plan": "PREMIUM", "status": "ACTIVE", "paid_until": None,
                      "enabled_modules": all_modules},
        )
        SchoolMessagingAccount.objects.get_or_create(
            school=school,
            defaults={"billing_mode": "POSTPAID", "sms_enabled": True, "whatsapp_enabled": True},
        )


class Migration(migrations.Migration):
    dependencies = [("mainApp", "0097_platform_multi_school")]
    operations = [migrations.RunPython(forwards, migrations.RunPython.noop)]

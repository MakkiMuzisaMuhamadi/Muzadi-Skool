"""Every new school automatically gets a subscription (free trial) and a messaging account."""
from datetime import timedelta

from django.conf import settings
from django.db.models.signals import post_save, pre_save
from django.dispatch import receiver
from django.utils import timezone

from mainApp.models.models import School


@receiver(pre_save, sender=School, dispatch_uid="school_remember_old_domains")
def _remember_old_domains(sender, instance, **kwargs):
    if instance.pk:
        old = School._base_manager.filter(pk=instance.pk).values("subdomain", "custom_domain").first()
        if old:
            instance._old_subdomain = old["subdomain"]
            instance._old_custom_domain = old["custom_domain"]
    if instance.subdomain:
        instance.subdomain = instance.subdomain.strip().lower()
    else:
        instance.subdomain = None
    if instance.custom_domain:
        instance.custom_domain = instance.custom_domain.strip().lower()
    else:
        instance.custom_domain = None


def ensure_platform_rows(school_id):
    """Subscription (free trial) + messaging account for a school that has none yet."""
    from mainApp.models.platform_models import SchoolMessagingAccount, SchoolSubscription

    if not School._base_manager.filter(pk=school_id).exists():
        return
    trial_days = int(getattr(settings, "NEW_SCHOOL_TRIAL_DAYS", 30))
    SchoolSubscription.objects.get_or_create(
        school_id=school_id,
        defaults={
            "status": SchoolSubscription.STATUS_TRIAL,
            "trial_ends_on": timezone.localdate() + timedelta(days=trial_days),
        },
    )
    SchoolMessagingAccount.objects.get_or_create(
        school_id=school_id,
        defaults={"credit_units": int(getattr(settings, "NEW_SCHOOL_FREE_SMS_UNITS", 0))},
    )


@receiver(post_save, sender=School, dispatch_uid="school_platform_setup")
def _school_platform_setup(sender, instance, created, raw=False, **kwargs):
    from django.db import transaction

    from mainApp import tenancy

    tenancy.forget_host_cache(instance)
    if not created or raw:
        return
    # After the transaction: a school restored from the recycle bin brings its own
    # subscription back, so nothing is created twice.
    school_id = instance.pk
    transaction.on_commit(lambda: ensure_platform_rows(school_id))

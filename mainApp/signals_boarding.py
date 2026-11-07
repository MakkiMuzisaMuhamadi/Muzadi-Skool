"""
Frees a student's bed automatically when they stop boarding, leave the school or become alumni.

Register it once in mainApp/apps.py:

    class MainappConfig(AppConfig):
        ...
        def ready(self):
            import mainApp.signals_boarding  # noqa: F401
"""
from django.db.models.signals import post_save
from django.dispatch import receiver

from mainApp.models.student import Student
from mainApp.models.hostel_models import HostelAssignment


@receiver(post_save, sender=Student, dispatch_uid="boarding_release_bed_on_student_change")
def release_bed_when_student_stops_boarding(sender, instance, created, **kwargs):
    if created or kwargs.get("raw"):
        return

    if not instance.is_active or getattr(instance, "is_alumni", False):
        reason = "LEFT_SCHOOL"
    elif instance.residence != "BOARDING":
        reason = "LEFT_BOARDING"
    else:
        return

    for a in HostelAssignment.objects.filter(student=instance, is_active=True).select_related("hostel", "room", "bed"):
        a.end(reason=reason)

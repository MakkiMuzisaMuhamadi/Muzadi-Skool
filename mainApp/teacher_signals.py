"""
Keep a teacher's allowed levels in sync whenever their allocations
or class-teacher roles change. Connected in apps.py -> ready().
"""
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from mainApp.models.teacher_models import (
    ClassTeacherAssignment,
    Teacher,
    TeacherTeachingAllocation,
)


def _sync(instance):
    from mainApp.services.teacher_service import sync_teacher_levels

    try:
        teacher = Teacher.objects.select_related("user__profile").get(pk=instance.teacher_id)
    except Teacher.DoesNotExist:
        return  # teacher itself is being deleted
    sync_teacher_levels(teacher)


@receiver(post_save, sender=TeacherTeachingAllocation)
@receiver(post_delete, sender=TeacherTeachingAllocation)
@receiver(post_save, sender=ClassTeacherAssignment)
@receiver(post_delete, sender=ClassTeacherAssignment)
def teacher_scope_changed(sender, instance, **kwargs):
    _sync(instance)

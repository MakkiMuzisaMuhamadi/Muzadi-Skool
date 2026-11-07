"""
Background job that creates login accounts for students or parents.

* Every account starts with the group's shared default password and must
  choose its own password at the first sign-in.
* Students: username = admission number (or first initial + surname).
* Parents:  username = Parent ID; they can also sign in with their phone number.
* Each account is created in its own savepoint, so one bad row can never undo
  or block the rest of the batch.
"""
import logging

from celery import shared_task
from django.contrib.auth.hashers import make_password
from django.contrib.auth.models import User
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.utils import timezone

from mainApp import accounts
from mainApp.models.models import Profile
from mainApp.models.student import Parent, Student
from mainApp.models.user_jobs import StudentAccountGenerationJob

logger = logging.getLogger(__name__)

BATCH_SIZE = 25


def _make_user(*, username, hashed_password, first_name, last_name, email, school, role):
    user = User.objects.create(
        username=username,
        password=hashed_password,
        first_name=(first_name or "")[:150],
        last_name=(last_name or "")[:150],
        email=email or "",
        is_active=True,
    )
    Profile.objects.create(
        user=user,
        school=school,
        role=role,
        email_verified=True,          # portal users do not verify email at sign-in
        must_change_password=True,
    )
    return user


def create_student_user(student, username_format="ADMISSION", hashed_password=None, reserved=None):
    first = (student.first_name or "").strip()
    last = (student.last_name or "").strip()
    if not first and student.name:
        parts = student.name.split()
        first, last = (parts[0], " ".join(parts[1:])) if parts else ("", "")
    username = accounts.unique_username(accounts.student_username_base(student, username_format), reserved)
    user = _make_user(
        username=username,
        hashed_password=hashed_password or make_password(accounts.default_password_for_role("STUDENT")),
        first_name=first, last_name=last,
        email=getattr(student, "email", "") or "",
        school=student.school, role="STUDENT",
    )
    linked = Student.objects.filter(pk=student.pk, user__isnull=True).update(user=user)
    if not linked:
        raise IntegrityError("Student already has an account.")
    return user


def create_parent_user(parent, hashed_password=None, reserved=None):
    username = parent.parent_id
    if User.objects.filter(username__iexact=username).exists():
        username = accounts.unique_username(parent.parent_id, reserved)
    with transaction.atomic():
        user = _make_user(
            username=username,
            hashed_password=hashed_password or make_password(accounts.default_password_for_role("PARENT")),
            first_name=parent.first_name, last_name=parent.last_name,
            email=parent.email or "",
            school=parent.school, role="PARENT",
        )
        linked = Parent.objects.filter(pk=parent.pk, user__isnull=True).update(user=user)
        if not linked:
            raise IntegrityError("Parent already has an account.")
    return user


def _target_queryset(job):
    requester = job.requested_by
    role = getattr(requester.profile, "role", "")
    school = getattr(requester.profile, "school", None)
    search = job.search_value

    if job.account_type == "PARENT":
        qs = Parent.objects.filter(user__isnull=True, is_active=True).select_related("school")
        if role != "SUPER_ADMIN":
            qs = qs.filter(school=school) if school else qs.none()
        if job.selection_mode == "filtered":
            if search:
                qs = qs.filter(Q(full_name__icontains=search) | Q(parent_id__icontains=search)
                               | Q(phone__icontains=search))
        else:
            qs = qs.filter(id__in=job.selected_ids or [])
        return qs.order_by("id")

    qs = Student.objects.filter(user__isnull=True).select_related("school", "level", "current_class", "stream")
    if role != "SUPER_ADMIN":
        qs = qs.filter(school=school) if school else qs.none()
    if job.selection_mode == "filtered":
        class_id = (job.filters or {}).get("class_id")
        if class_id:
            qs = qs.filter(current_class_id=class_id)
        if search:
            qs = qs.filter(
                Q(name__icontains=search) | Q(first_name__icontains=search)
                | Q(middle_name__icontains=search) | Q(last_name__icontains=search)
                | Q(admission_number__icontains=search)
            )
    else:
        qs = qs.filter(id__in=job.selected_ids or [])
    return qs.order_by("current_class__name", "id")


@shared_task(bind=True)
def generate_student_accounts_task(self, job_id):
    """Creates student OR parent accounts, depending on job.account_type."""
    logger.info("JOB %s: task entered", job_id)
    job = None

    try:
        job = StudentAccountGenerationJob.objects.select_related("requested_by__profile").get(pk=job_id)
        job.status = "STARTED"
        job.started_at = timezone.now()
        job.celery_task_id = getattr(self.request, "id", None) or job.celery_task_id or ""
        job.message = "Task started."
        job.processed_count = job.created_count = job.skipped_count = 0
        job.created_user_ids = []
        job.save(update_fields=["status", "started_at", "celery_task_id", "message",
                                "processed_count", "created_count", "skipped_count", "created_user_ids"])

        targets = _target_queryset(job)
        total_target = targets.count()
        job.total_target = total_target
        job.message = f"Found {total_target} record(s) to process."
        job.save(update_fields=["total_target", "message"])

        if total_target == 0:
            job.status = "SUCCESS"
            job.finished_at = timezone.now()
            job.message = "Nobody matched the selection (they may already have accounts)."
            job.save(update_fields=["status", "finished_at", "message"])
            return

        hashed = make_password(accounts.default_password_for_role(job.account_type))
        processed = created = skipped = 0
        created_ids = []
        reserved = set()   # usernames given out in this run

        def flush(text):
            StudentAccountGenerationJob.objects.filter(pk=job.pk).update(
                processed_count=processed, created_count=created, skipped_count=skipped,
                created_user_ids=created_ids, message=text,
            )

        for obj in targets.iterator(chunk_size=BATCH_SIZE):
            try:
                with transaction.atomic():   # one savepoint per account
                    if job.account_type == "PARENT":
                        user = create_parent_user(obj, hashed_password=hashed, reserved=reserved)
                    else:
                        user = create_student_user(obj, job.username_format, hashed_password=hashed,
                                                   reserved=reserved)
                reserved.add(user.username.lower())
                created_ids.append(user.pk)
                created += 1
            except Exception:
                logger.exception("JOB %s: could not create account for %s %s", job_id, job.account_type, obj.pk)
                skipped += 1
            processed += 1
            if processed % BATCH_SIZE == 0:
                flush(f"Processed {processed} of {total_target}.")

        job.refresh_from_db()
        job.status = "SUCCESS"
        job.finished_at = timezone.now()
        job.processed_count, job.created_count, job.skipped_count = processed, created, skipped
        job.created_user_ids = created_ids
        job.message = f"Completed. Created {created}, skipped {skipped}."
        job.save(update_fields=["status", "finished_at", "message", "processed_count",
                                "created_count", "skipped_count", "created_user_ids"])
        logger.info("JOB %s: finished successfully", job_id)

    except Exception as e:
        logger.exception("JOB %s: task failed", job_id)
        if job is not None:
            StudentAccountGenerationJob.objects.filter(pk=job.pk).update(
                status="FAILED", finished_at=timezone.now(), message=str(e)[:1000],
            )
        raise

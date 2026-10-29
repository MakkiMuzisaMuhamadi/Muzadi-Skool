import logging
from celery import shared_task
from django.utils import timezone
from django.db import transaction
from django.db.models import Q
from django.contrib.auth.models import User
from django.contrib.auth.hashers import make_password
from django.utils.text import slugify

from mainApp.models.student import Student
from mainApp.models.models import Profile
from mainApp.models.user_jobs import StudentAccountGenerationJob
from mainApp.utils2.user_creds import DEFAULT_STUDENT_PASSWORD, generate_unique_student_username

logger = logging.getLogger(__name__)


@shared_task(bind=True)
def generate_student_accounts_task(self, job_id):
    logger.info("JOB %s: task entered", job_id)

    job = None

    try:
        job = StudentAccountGenerationJob.objects.select_related("requested_by").get(pk=job_id)
        logger.info("JOB %s: job loaded", job_id)

        job.status = "STARTED"
        job.started_at = timezone.now()
        job.celery_task_id = self.request.id or job.celery_task_id or ""
        job.message = "Task started."
        job.processed_count = 0
        job.created_count = 0
        job.skipped_count = 0
        job.save(update_fields=[
            "status",
            "started_at",
            "celery_task_id",
            "message",
            "processed_count",
            "created_count",
            "skipped_count",
        ])
        logger.info("JOB %s: marked STARTED", job_id)

        requester = job.requested_by
        requester_school = getattr(requester.profile, "school", None)

        students_qs = (
            Student.objects
            .filter(user__isnull=True)
            .select_related("school", "level", "current_class", "stream")
            .order_by("id")
        )

        if requester_school and requester.profile.role != "SUPER_ADMIN":
            students_qs = students_qs.filter(school=requester_school)

        if job.selection_mode == "filtered":
            if job.search_value:
                students_qs = students_qs.filter(
                    Q(name__icontains=job.search_value) |
                    Q(first_name__icontains=job.search_value) |
                    Q(middle_name__icontains=job.search_value) |
                    Q(last_name__icontains=job.search_value) |
                    Q(admission_number__icontains=job.search_value)
                )
        else:
            selected_ids = job.selected_ids or []
            students_qs = students_qs.filter(id__in=selected_ids)

        total_target = students_qs.count()
        job.total_target = total_target
        job.message = f"Found {total_target} student(s) to process."
        job.save(update_fields=["total_target", "message"])
        logger.info("JOB %s: total_target=%s", job_id, total_target)

        if total_target == 0:
            job.status = "SUCCESS"
            job.finished_at = timezone.now()
            job.message = "No students matched the selected criteria."
            job.save(update_fields=["status", "finished_at", "message"])
            return

        hashed_default_password = make_password(DEFAULT_STUDENT_PASSWORD)

        batch_size = 25
        processed_count = 0
        created_count = 0
        skipped_count = 0
        batch = []

        def save_progress(message_text):
            job.refresh_from_db()
            job.processed_count = processed_count
            job.created_count = created_count
            job.skipped_count = skipped_count
            job.message = message_text
            job.save(update_fields=[
                "processed_count",
                "created_count",
                "skipped_count",
                "message",
            ])
            logger.info(
                "JOB %s: progress saved processed=%s created=%s skipped=%s",
                job_id, processed_count, created_count, skipped_count
            )

        def process_batch(student_batch):
            nonlocal processed_count, created_count, skipped_count

            with transaction.atomic():
                for student in student_batch:
                    try:
                        if student.user_id:
                            skipped_count += 1
                            processed_count += 1
                            continue

                        first_name = (getattr(student, "first_name", "") or "").strip()
                        last_name = (getattr(student, "last_name", "") or "").strip()

                        if not first_name and getattr(student, "name", None):
                            full_name = (student.name or "").strip().split()
                            if full_name:
                                first_name = full_name[0]
                                last_name = " ".join(full_name[1:]) if len(full_name) > 1 else ""

                        username = generate_unique_student_username(first_name, last_name)

                        if not username:
                            base_username = getattr(student, "admission_number", None) or f"student{student.pk}"
                            username = slugify(str(base_username)).replace("-", "").lower() or f"student{student.pk}"

                            original_username = username
                            counter = 1
                            while User.objects.filter(username=username).exists():
                                username = f"{original_username}{counter}"
                                counter += 1

                        email = getattr(student, "email", "") or ""

                        user = User.objects.create(
                            username=username,
                            password=hashed_default_password,
                            first_name=first_name,
                            last_name=last_name,
                            email=email,
                            is_active=True,
                        )

                        profile = Profile.objects.create(
                            user=user,
                            school=getattr(student, "school", None),
                            role="STUDENT",
                            email_verified=True,
                            verification_code=None,
                            verification_expiry=None,
                        )

                        if hasattr(profile, "allowed_levels"):
                            profile.allowed_levels.clear()

                        Student.objects.filter(pk=student.pk, user__isnull=True).update(user=user)

                        created_count += 1

                    except Exception:
                        logger.exception("JOB %s: failed processing student %s", job_id, getattr(student, "pk", None))
                        skipped_count += 1
                    finally:
                        processed_count += 1

            save_progress(f"Processed {processed_count} of {total_target} students.")

        for student in students_qs.iterator(chunk_size=batch_size):
            batch.append(student)
            if len(batch) >= batch_size:
                process_batch(batch)
                batch = []

        if batch:
            process_batch(batch)

        job.refresh_from_db()
        job.status = "SUCCESS"
        job.finished_at = timezone.now()
        job.message = f"Completed. Created {created_count}, skipped {skipped_count}."
        job.processed_count = processed_count
        job.created_count = created_count
        job.skipped_count = skipped_count
        job.save(update_fields=[
            "status",
            "finished_at",
            "message",
            "processed_count",
            "created_count",
            "skipped_count",
        ])
        logger.info("JOB %s: finished successfully", job_id)

    except Exception as e:
        logger.exception("JOB %s: task failed", job_id)

        if job is not None:
            job.refresh_from_db()
            job.status = "FAILED"
            job.finished_at = timezone.now()
            job.message = str(e)
            job.save(update_fields=["status", "finished_at", "message"])

        raise
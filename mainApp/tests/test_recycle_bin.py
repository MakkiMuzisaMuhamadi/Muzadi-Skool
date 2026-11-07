"""
Recycle bin tests: delete -> bin -> restore, cascades, conflicts, settings, views.

Run:  python manage.py test mainApp.tests.test_recycle_bin
"""
from datetime import timedelta

from django.contrib.auth.models import User
from django.db.models import ProtectedError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from mainApp.models.auditModels import AuditLog
from mainApp.models.models import Class, School
from mainApp.models.recycleBin import RecycleBin, RecycleBinSettings
from mainApp.models.student import Parent, Student, StudentGuardian
from mainApp.models.subject_models import Paper, Subject
from mainApp.recycle_bin import (
    RestoreError,
    check_restore,
    move_to_recycle_bin,
    purge_expired,
    restore_entry,
)
from mainApp.tests.factories import make_charge, make_school, make_student, make_user


def make_subject(ctx, name="Mathematics", code="MTC"):
    subject = Subject.objects.create(level=ctx["level"], name=name, code=code)
    Paper.objects.create(subject=subject, paper_code=f"{code}1", paper_name="Paper 1", max_marks=100, order=1)
    Paper.objects.create(subject=subject, paper_code=f"{code}2", paper_name="Paper 2", max_marks=100, order=2)
    return subject


class DeleteAndRestoreTests(TestCase):
    def setUp(self):
        self.ctx = make_school()
        self.admin = make_user("boss", role="SUPER_ADMIN", school=self.ctx["school"], email="b@x.com")

    def test_bin_works_without_a_settings_row(self):
        RecycleBinSettings.objects.all().delete()
        student = make_student(self.ctx)
        student.delete()
        self.assertEqual(RecycleBin.objects.count(), 1)
        self.assertEqual(RecycleBinSettings.objects.count(), 1)

    def test_student_is_binned_and_restored_with_same_id_and_links(self):
        student = make_student(self.ctx, first="Amina")
        parent = Parent.objects.create(school=self.ctx["school"], first_name="Sarah",
                                       last_name="Nambi", phone="0772000111")
        link = StudentGuardian.objects.create(student=student, parent=parent, relationship="MOTHER")
        pk, cand, link_pk = student.pk, student.candidate_id, link.pk

        student.delete(deleted_by=self.admin, deletion_reason="Left school")
        self.assertFalse(Student.objects.filter(pk=pk).exists())
        self.assertFalse(StudentGuardian.objects.filter(pk=link_pk).exists())

        entry = RecycleBin.objects.get()
        self.assertEqual(entry.object_type, "Student")
        self.assertEqual(entry.object_id, pk)
        self.assertEqual(entry.deleted_by, self.admin)
        self.assertEqual(entry.deletion_reason, "Left school")
        self.assertGreaterEqual(entry.total_rows, 2)
        self.assertIn("Amina", entry.display_name)

        result = restore_entry(entry, user=self.admin)
        restored = Student.objects.get(pk=pk)
        self.assertEqual(restored.candidate_id, cand)          # ID not regenerated
        self.assertEqual(result.root, restored)
        self.assertTrue(StudentGuardian.objects.filter(pk=link_pk, student=restored, parent=parent).exists())
        self.assertFalse(RecycleBin.objects.exists())
        self.assertTrue(AuditLog.objects.filter(action="RESTORE", model_name="Student").exists())

    def test_protected_records_still_block_the_delete(self):
        student = make_student(self.ctx)
        make_charge(self.ctx, student, 100000)
        with self.assertRaises(ProtectedError):
            student.delete()
        self.assertFalse(RecycleBin.objects.exists())
        self.assertTrue(Student.objects.filter(pk=student.pk).exists())

    def test_subject_delete_no_longer_crashes_and_papers_come_back(self):
        subject = make_subject(self.ctx)
        paper_ids = sorted(subject.papers.values_list("pk", flat=True))
        subject_pk = subject.pk
        subject.delete()
        self.assertFalse(Paper.objects.filter(pk__in=paper_ids).exists())
        restore_entry(RecycleBin.objects.get())
        self.assertEqual(sorted(Paper.objects.filter(subject_id=subject_pk).values_list("pk", flat=True)),
                         paper_ids)

    def test_school_cascade_and_many_to_many_links_are_restored(self):
        school = self.ctx["school"]
        school_pk, class_pk = school.pk, self.ctx["class"].pk
        s1, s2 = make_student(self.ctx), make_student(self.ctx)
        level_ids = list(school.level.values_list("pk", flat=True))
        self.admin.profile.school = None
        self.admin.profile.save()

        school.delete()
        self.assertFalse(Student.objects.filter(pk__in=[s1.pk, s2.pk]).exists())
        entry = RecycleBin.objects.get()
        self.assertEqual(entry.object_type, "School")

        restore_entry(entry)
        school = School.objects.get(pk=school_pk)
        self.assertEqual(list(school.level.values_list("pk", flat=True)), level_ids)
        self.assertEqual(Student.objects.filter(school=school).count(), 2)
        self.assertTrue(Class.objects.filter(pk=class_pk).exists())

    def test_links_set_to_null_are_relinked(self):
        student = make_student(self.ctx)
        klass = self.ctx["class"]
        klass_pk = klass.pk
        klass.delete()
        student.refresh_from_db()
        self.assertIsNone(student.current_class_id)

        restore_entry(RecycleBin.objects.get(object_type="Class"))
        student.refresh_from_db()
        self.assertEqual(student.current_class_id, klass_pk)

    def test_restore_blocked_when_parent_is_gone(self):
        student = make_student(self.ctx)
        student.delete()
        entry = RecycleBin.objects.get()
        self.admin.profile.school = None
        self.admin.profile.save()
        self.ctx["school"].hard_delete()

        check = check_restore(entry)
        self.assertFalse(check.ok)
        self.assertTrue(any("no longer exists" in p for p in check.problems))
        with self.assertRaises(RestoreError):
            restore_entry(entry)
        self.assertTrue(RecycleBin.objects.filter(pk=entry.pk).exists())

    def test_restore_blocked_when_id_is_taken(self):
        subject = make_subject(self.ctx)
        subject.delete()
        entry = RecycleBin.objects.get()
        restore_entry(entry)
        # pretend the same snapshot is restored twice
        entry.pk = None
        entry.save()
        with self.assertRaises(RestoreError) as cm:
            restore_entry(entry)
        self.assertIn("already exist", str(cm.exception))

    def test_restore_unique_clash_gives_friendly_message(self):
        subject = make_subject(self.ctx, name="Science", code="SCI")
        subject_pk = subject.pk
        subject.delete()
        entry = RecycleBin.objects.get()
        # Someone re-created the subject with the same unique values meanwhile
        Subject.objects.create(level=self.ctx["level"], name="Science", code="SCI")
        try:
            restore_entry(entry)
        except RestoreError as exc:
            self.assertNotIn("Traceback", str(exc))
            self.assertTrue(RecycleBin.objects.filter(pk=entry.pk).exists())
        else:
            # No unique constraint on these fields - restore is allowed.
            self.assertTrue(Subject.objects.filter(pk=subject_pk).exists())

    def test_disabled_bin_deletes_permanently(self):
        s = RecycleBinSettings.get_solo()
        s.enabled = False
        s.save()
        make_student(self.ctx).delete()
        self.assertFalse(RecycleBin.objects.exists())

    def test_queryset_delete_makes_one_entry_and_hard_delete_skips_bin(self):
        subject = make_subject(self.ctx)
        Paper.objects.filter(subject=subject).delete()
        entry = RecycleBin.objects.get()
        self.assertEqual(entry.total_rows, 2)
        self.assertIn("2 ", entry.object_repr)

        subject2 = make_subject(self.ctx, name="English", code="ENG")
        Paper.objects.filter(subject=subject2).hard_delete()
        self.assertEqual(RecycleBin.objects.count(), 1)

    def test_deleting_the_user_keeps_their_bin_entries(self):
        make_student(self.ctx).delete(deleted_by=self.admin)
        entry = RecycleBin.objects.get(object_type="Student")
        User.objects.filter(pk=self.admin.pk).delete()
        entry.refresh_from_db()
        self.assertIsNone(entry.deleted_by)
        self.assertEqual(entry.deleted_by_display, "boss")

    def test_student_and_login_account_restore_together(self):
        student = make_student(self.ctx)
        login = User.objects.create_user(username="stud1", password="x")
        student.user = login
        student.save()
        student_pk, login_pk = student.pk, login.pk
        move_to_recycle_bin([student, login])
        self.assertFalse(User.objects.filter(pk=login_pk).exists())
        entry = RecycleBin.objects.get()
        self.assertEqual(entry.object_type, "Student")
        restore_entry(entry)
        self.assertEqual(Student.objects.get(pk=student_pk).user_id, login_pk)

    def test_max_items_per_type(self):
        s = RecycleBinSettings.get_solo()
        s.max_items_per_type = 2
        s.save()
        for _ in range(4):
            make_student(self.ctx).delete()
        self.assertEqual(RecycleBin.objects.filter(object_type="Student").count(), 2)

    def test_purge_expired(self):
        make_student(self.ctx).delete()
        RecycleBin.objects.update(expires_at=timezone.now() - timedelta(days=1))
        self.assertEqual(purge_expired(), 1)
        self.assertIsNotNone(RecycleBinSettings.get_solo().last_cleanup)

    def test_legacy_entry_can_be_restored(self):
        subject = make_subject(self.ctx, name="History", code="HIS")
        papers = [
            {"id": p.pk, "paper_code": p.paper_code, "paper_name": p.paper_name,
             "max_marks": p.max_marks, "order": p.order, "compulsory": True}
            for p in subject.papers.all()
        ]
        data = {"name": subject.name, "code": subject.code, "level_id": self.ctx["level"].pk}
        pk = subject.pk
        subject.hard_delete()
        entry = RecycleBin.objects.create(
            object_type="Subject", object_id=pk, original_data=data,
            related_objects={"papers": papers}, expires_at=timezone.now() + timedelta(days=5),
        )
        restore_entry(entry)
        self.assertTrue(Subject.objects.filter(pk=pk, name="History").exists())
        self.assertEqual(Paper.objects.filter(subject_id=pk).count(), 2)


class RecycleBinViewTests(TestCase):
    def setUp(self):
        self.ctx = make_school()
        self.admin = make_user("boss", role="SUPER_ADMIN", school=self.ctx["school"], email="b@x.com")
        self.client.force_login(self.admin)
        self.student = make_student(self.ctx, first="Brian")
        self.student_pk = self.student.pk
        self.student.delete(deleted_by=self.admin)
        self.entry = RecycleBin.objects.get()

    def test_dashboard_lists_and_filters(self):
        r = self.client.get(reverse("recycle_bin_dashboard"))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Brian")
        r = self.client.get(reverse("recycle_bin_dashboard"), {"type": "Subject"})
        self.assertNotContains(r, "Brian")
        r = self.client.get(reverse("recycle_bin_dashboard"), {"q": "brian"})
        self.assertContains(r, "Brian")

    def test_details_page(self):
        r = self.client.get(reverse("view_item_details", args=[self.entry.pk]))
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Brian")

    def test_actions_require_post(self):
        r = self.client.get(reverse("restore_item", args=[self.entry.pk]))
        self.assertEqual(r.status_code, 405)
        r = self.client.get(reverse("permanent_delete", args=[self.entry.pk]))
        self.assertEqual(r.status_code, 405)
        self.assertTrue(RecycleBin.objects.filter(pk=self.entry.pk).exists())

    def test_restore_view(self):
        r = self.client.post(reverse("restore_item", args=[self.entry.pk]))
        self.assertRedirects(r, reverse("recycle_bin_dashboard"), fetch_redirect_response=False)
        self.assertTrue(Student.objects.filter(pk=self.student_pk).exists())

    def test_bulk_actions(self):
        other = make_student(self.ctx)
        other_pk = other.pk
        other.delete()
        ids = list(RecycleBin.objects.values_list("pk", flat=True))
        r = self.client.post(reverse("bulk_restore"), {"item_ids": ids, "action": "restore"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(Student.objects.filter(pk__in=[self.student_pk, other_pk]).count(), 2)

        Student.objects.filter(pk__in=[self.student_pk, other_pk]).delete()
        ids = list(RecycleBin.objects.values_list("pk", flat=True))
        self.client.post(reverse("bulk_permanent_delete"), {"item_ids": ids})
        self.assertFalse(RecycleBin.objects.exists())

    def test_stats_endpoint(self):
        r = self.client.get(reverse("recycle_bin_stats"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["total_count"], 1)

    def test_settings_validation(self):
        r = self.client.post(reverse("update_recycle_bin_settings"),
                             {"retention_days": "abc", "max_items_per_type": "50"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(RecycleBinSettings.get_solo().retention_days, 30)
        self.client.post(reverse("update_recycle_bin_settings"),
                         {"retention_days": "60", "max_items_per_type": "50", "enabled": "on"})
        s = RecycleBinSettings.get_solo()
        self.assertEqual((s.retention_days, s.max_items_per_type, s.enabled, s.auto_cleanup),
                         (60, 50, True, False))

    def test_non_super_admin_is_blocked(self):
        user = make_user("teacher1", role="TEACHER", school=self.ctx["school"], email="t@x.com")
        self.client.force_login(user)
        r = self.client.get(reverse("recycle_bin_dashboard"))
        self.assertEqual(r.status_code, 403)


class StudentDeleteViewTests(TestCase):
    def setUp(self):
        self.ctx = make_school()
        self.admin = make_user("head", role="ADMIN", school=self.ctx["school"], email="h@x.com")
        self.client.force_login(self.admin)

    def test_student_and_login_go_to_bin_and_come_back(self):
        student = make_student(self.ctx, first="Kato")
        login = User.objects.create_user(username="kato.student", password="x")
        student.user = login
        student.save()
        spk, upk = student.pk, login.pk

        r = self.client.post(reverse("student_delete", args=[spk]))
        self.assertEqual(r.status_code, 302)
        self.assertFalse(Student.objects.filter(pk=spk).exists())
        self.assertFalse(User.objects.filter(pk=upk).exists())
        entry = RecycleBin.objects.get()
        self.assertEqual(entry.deleted_by, self.admin)

        restore_entry(entry)
        self.assertEqual(Student.objects.get(pk=spk).user_id, upk)

    def test_student_with_fees_shows_clear_message(self):
        student = make_student(self.ctx)
        make_charge(self.ctx, student, 5000)
        r = self.client.post(reverse("student_delete", args=[student.pk]), follow=True)
        self.assertTrue(Student.objects.filter(pk=student.pk).exists())
        self.assertContains(r, "cannot be deleted")

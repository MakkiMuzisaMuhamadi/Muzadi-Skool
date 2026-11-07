"""
User accounts: parent login, first-login password change, reset to default,
forgot password (SMS / email), user management rules and account generation.

Run:  python manage.py test mainApp.tests.test_user_accounts
"""
from unittest import mock

from django.contrib.auth.models import User
from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from mainApp import accounts
from mainApp.models.models import EducationLevel, PasswordResetCode, Profile
from mainApp.models.student import Parent, StudentGuardian
from mainApp.models.user_jobs import StudentAccountGenerationJob
from mainApp.security import mask_email
from mainApp.tasks.user_creation_tasks import create_parent_user, generate_student_accounts_task
from mainApp.tests.factories import make_school, make_student, make_user

STRONG = "Str0ng-Pass!"
NEW_PW = "Mango-Tree-42"

SETTINGS = dict(
    DEFAULT_STUDENT_PASSWORD="Student@123",
    DEFAULT_PARENT_PASSWORD="Parent@123",
    DEFAULT_STAFF_PASSWORD="Staff@123",
    FIRST_LOGIN_IDENTITY_CHECK=True,
    PASSWORD_RESET_SMS_ENABLED=True,
    AFRICASTALKING_USERNAME="sandbox",
    AFRICASTALKING_API_KEY="key",
    PASSWORD_RESET_RESEND_COOLDOWN_SECONDS=60,
    PASSWORD_RESET_SMS_MAX_PER_USER_PER_DAY=3,
    PASSWORD_RESET_SMS_MAX_PER_DAY=300,
    PASSWORD_RESET_LOOKUPS_PER_IP_PER_HOUR=20,
)


def make_parent(ctx, phone="0772123456", first="Grace", last="Achieng", **kw):
    return Parent.objects.create(school=ctx["school"], first_name=first, last_name=last, phone=phone, **kw)


@override_settings(**SETTINGS)
class Base(TestCase):
    def setUp(self):
        cache.clear()
        self.ctx = make_school()
        self.admin = make_user("headteacher", role="ADMIN", school=self.ctx["school"], email="ht@example.com")

    def login_as(self, user):
        self.client.force_login(user)


class ParentLoginTests(Base):
    def setUp(self):
        super().setUp()
        self.parent = make_parent(self.ctx)
        self.child = make_student(self.ctx)
        self.child.admission_number = "ADM-2026-001"
        self.child.save(update_fields=["admission_number"])
        StudentGuardian.objects.create(student=self.child, parent=self.parent, relationship="MOTHER")
        self.user = create_parent_user(self.parent)

    def post_login(self, ident, password, tab="parent"):
        return self.client.post(reverse("login"), {"username": ident, "password": password, "login_type": tab})

    def test_username_is_parent_id(self):
        self.assertEqual(self.user.username, self.parent.parent_id)
        self.assertEqual(self.user.profile.role, "PARENT")
        self.assertTrue(self.user.profile.must_change_password)

    def test_login_with_phone_in_any_format(self):
        for phone in ("0772123456", "+256 772 123 456", "772123456"):
            self.client.logout()
            cache.clear()
            resp = self.post_login(phone, "Parent@123")
            self.assertRedirects(resp, reverse("set_new_password"), fetch_redirect_response=False)

    def test_login_with_parent_id(self):
        resp = self.post_login(self.parent.parent_id.lower(), "Parent@123")
        self.assertRedirects(resp, reverse("set_new_password"), fetch_redirect_response=False)

    def test_wrong_password(self):
        resp = self.post_login("0772123456", "nope")
        self.assertContains(resp, "Invalid phone number / Parent ID or password")
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_shared_phone_with_same_password_asks_for_parent_id(self):
        other = make_parent(self.ctx, first="John", last="Okello")
        create_parent_user(other)
        resp = self.post_login("0772123456", "Parent@123")
        self.assertContains(resp, "Please sign in with your Parent ID")
        self.assertNotIn("_auth_user_id", self.client.session)
        # Parent ID still works
        resp = self.post_login(other.parent_id, "Parent@123")
        self.assertEqual(resp.status_code, 302)

    def test_shared_phone_with_different_passwords_picks_the_right_one(self):
        other = make_parent(self.ctx, first="John", last="Okello")
        other_user = create_parent_user(other)
        other_user.set_password(NEW_PW)
        other_user.save()
        self.post_login("0772123456", NEW_PW)
        self.assertEqual(int(self.client.session["_auth_user_id"]), other_user.pk)

    def test_first_login_requires_childs_admission_number(self):
        self.post_login("0772123456", "Parent@123")
        resp = self.client.post(reverse("set_new_password"), {
            "identity_answer": "WRONG", "new_password1": NEW_PW, "new_password2": NEW_PW})
        self.assertContains(resp, "is not correct")
        resp = self.client.post(reverse("set_new_password"), {
            "identity_answer": "adm 2026 001", "new_password1": NEW_PW, "new_password2": NEW_PW})
        self.assertRedirects(resp, reverse("parent_portal"), fetch_redirect_response=False)
        self.user.refresh_from_db()
        self.assertFalse(self.user.profile.must_change_password)
        self.assertTrue(self.user.check_password(NEW_PW))

    def test_cannot_keep_default_password(self):
        self.post_login("0772123456", "Parent@123")
        resp = self.client.post(reverse("set_new_password"), {
            "identity_answer": "ADM-2026-001", "new_password1": "Parent@123", "new_password2": "Parent@123"})
        self.assertContains(resp, "not the one given to you by the school")

    def test_parent_cannot_open_staff_pages(self):
        self.user.profile.must_change_password = False
        self.user.profile.save()
        self.login_as(self.user)
        self.assertEqual(self.client.get(reverse("class_list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("report_command_center")).status_code, 403)
        # "Dashboard" takes a parent to their own home page
        self.assertRedirects(self.client.get(reverse("dashboard")), reverse("parent_portal"),
                             fetch_redirect_response=False)
        resp = self.client.get(reverse("ajax_classes_by_level"), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(resp.status_code, 403)

    def test_portal_shows_only_own_children(self):
        self.user.profile.must_change_password = False
        self.user.profile.save()
        stranger = make_student(self.ctx, first="Stranger")
        self.login_as(self.user)
        resp = self.client.get(reverse("parent_portal"))
        self.assertContains(resp, self.child.first_name)
        self.assertNotContains(resp, "Stranger")
        resp = self.client.get(reverse("parent_child_reports", args=[stranger.pk]))
        self.assertEqual(resp.status_code, 404)


class StudentPortalTests(Base):
    def test_student_cannot_open_staff_only_pages(self):
        st = make_student(self.ctx)
        user = User.objects.create_user("stud9", password=STRONG)
        Profile.objects.create(user=user, role="STUDENT", school=self.ctx["school"], email_verified=True)
        st.user = user
        st.save(update_fields=["user"])
        self.login_as(user)
        # these views only had @login_required before
        self.assertEqual(self.client.get(reverse("class_list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("sms_campaign_list")).status_code, 403)
        self.assertEqual(self.client.get(reverse("student_my_reports")).status_code, 200)


class ForcedPasswordChangeTests(Base):
    def test_staff_signing_in_with_default_password_must_change_it(self):
        teacher = make_user("teach", role="TEACHER", school=self.ctx["school"], password="Staff@123", email="")
        resp = self.client.post(reverse("login"), {"username": "teach", "password": "Staff@123"})
        self.assertRedirects(resp, reverse("set_new_password"), fetch_redirect_response=False)
        teacher.refresh_from_db()
        self.assertTrue(teacher.profile.must_change_password)
        # Every other page sends them back until they change it
        resp = self.client.get(reverse("dashboard"))
        self.assertRedirects(resp, reverse("set_new_password"), fetch_redirect_response=False)

    def test_staff_without_email_can_sign_in(self):
        make_user("noemail", role="TEACHER", school=self.ctx["school"], email="", verified=False)
        resp = self.client.post(reverse("login"), {"username": "noemail", "password": STRONG})
        self.assertRedirects(resp, reverse("dashboard"), fetch_redirect_response=False)

    def test_student_first_login_checks_admission_number(self):
        st = make_student(self.ctx)
        st.admission_number = "S123"
        st.save(update_fields=["admission_number"])
        user = User.objects.create_user("stud1", password="Student@123")
        Profile.objects.create(user=user, role="STUDENT", school=self.ctx["school"], email_verified=True,
                               must_change_password=True)
        st.user = user
        st.save(update_fields=["user"])
        self.login_as(user)
        resp = self.client.get(reverse("set_new_password"))
        self.assertContains(resp, "Your admission number")
        resp = self.client.post(reverse("set_new_password"), {
            "identity_answer": "s123", "new_password1": NEW_PW, "new_password2": NEW_PW})
        self.assertRedirects(resp, reverse("dashboard"), fetch_redirect_response=False)

    def test_admin_reset_to_default(self):
        teacher = make_user("teach2", role="TEACHER", school=self.ctx["school"])
        self.login_as(self.admin)
        resp = self.client.post(reverse("reset_user_password", args=[teacher.pk]))
        self.assertEqual(resp.status_code, 302)
        teacher.refresh_from_db()
        self.assertTrue(teacher.check_password("Staff@123"))
        self.assertTrue(teacher.profile.must_change_password)


@override_settings(**SETTINGS)
class ForgotPasswordTests(TestCase):
    def setUp(self):
        cache.clear()
        self.ctx = make_school()
        self.teacher = make_user("teacher9", role="TEACHER", school=self.ctx["school"], email="t9@example.com")
        self.teacher.profile.phone = "0701234567"
        self.teacher.profile.save()

    def start(self, ident):
        return self.client.post(reverse("password_reset_start"), {"identifier": ident})

    def test_options_show_last_three_digits_and_email(self):
        self.start("teacher9")
        resp = self.client.get(reverse("password_reset_choose"))
        self.assertContains(resp, "Number ending in <strong>567</strong>", html=False)
        self.assertContains(resp, mask_email("t9@example.com"))
        self.assertNotContains(resp, "0701234567")

    @mock.patch("mainApp.views.password_reset_views._send_sms")
    def test_sms_code_resets_password(self, send_sms):
        captured = {}

        def fake(user, phone, code):
            captured["code"], captured["phone"] = code, phone
            return True, {"message_id": "ATX1", "status": "Success", "cost": "UGX 25.0000"}
        send_sms.side_effect = fake

        self.start("teacher9")
        resp = self.client.post(reverse("password_reset_choose"), {"channel": "SMS"})
        self.assertRedirects(resp, reverse("password_reset_verify"), fetch_redirect_response=False)
        self.assertEqual(captured["phone"], "+256701234567")
        record = PasswordResetCode.objects.get(user=self.teacher)
        self.assertEqual(record.cost, "UGX 25.0000")
        self.assertNotEqual(record.code_hash, captured["code"])   # stored hashed

        resp = self.client.post(reverse("password_reset_verify"), {
            "code": captured["code"], "new_password1": NEW_PW, "new_password2": NEW_PW})
        self.assertEqual(resp.status_code, 302)
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.check_password(NEW_PW))
        # The code cannot be used twice
        self.assertIsNotNone(PasswordResetCode.objects.get(pk=record.pk).used_at)

    def test_email_code(self):
        self.start("teacher9")
        self.client.post(reverse("password_reset_choose"), {"channel": "EMAIL"})
        self.assertEqual(len(mail.outbox), 1)
        body = mail.outbox[0].body
        code = next(tok for tok in body.split() if tok.isdigit() and len(tok) == 6)
        self.client.post(reverse("password_reset_verify"), {
            "code": code, "new_password1": NEW_PW, "new_password2": NEW_PW})
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.check_password(NEW_PW))

    @override_settings(PASSWORD_RESET_MAX_ATTEMPTS=3)
    def test_wrong_codes_cancel_the_code(self):
        self.start("teacher9")
        self.client.post(reverse("password_reset_choose"), {"channel": "EMAIL"})
        for _ in range(3):
            resp = self.client.post(reverse("password_reset_verify"), {
                "code": "000000", "new_password1": NEW_PW, "new_password2": NEW_PW})
        self.assertRedirects(resp, reverse("password_reset_choose"), fetch_redirect_response=False)
        self.assertTrue(PasswordResetCode.objects.get(user=self.teacher).cancelled)

    def test_resend_cooldown(self):
        self.start("teacher9")
        self.client.post(reverse("password_reset_choose"), {"channel": "EMAIL"})
        resp = self.client.post(reverse("password_reset_choose"), {"channel": "EMAIL"})
        self.assertContains(resp, "Please wait")
        self.assertEqual(len(mail.outbox), 1)

    def test_student_is_told_to_ask_the_school(self):
        user = User.objects.create_user("kid", password="x", email="kid@example.com")
        Profile.objects.create(user=user, role="STUDENT", school=self.ctx["school"])
        resp = self.start("kid")
        self.assertContains(resp, "Student passwords are reset by the school")

    def test_no_phone_no_email_contact_admin(self):
        make_user("bare", role="TEACHER", school=self.ctx["school"], email="")
        self.start("bare")
        resp = self.client.get(reverse("password_reset_choose"))
        self.assertContains(resp, "contact the school administrator")

    @override_settings(PASSWORD_RESET_SMS_ENABLED=False)
    def test_sms_switched_off_offers_email_only(self):
        self.start("teacher9")
        resp = self.client.get(reverse("password_reset_choose"))
        self.assertNotContains(resp, "SMS to my phone")
        self.assertContains(resp, mask_email("t9@example.com"))

    @mock.patch("mainApp.views.password_reset_views._send_sms", return_value=(True, {}))
    @override_settings(PASSWORD_RESET_SMS_MAX_PER_USER_PER_DAY=1, PASSWORD_RESET_RESEND_COOLDOWN_SECONDS=0)
    def test_daily_sms_cap(self, _send):
        self.start("teacher9")
        self.client.post(reverse("password_reset_choose"), {"channel": "SMS"})
        resp = self.client.post(reverse("password_reset_choose"), {"channel": "SMS"})
        self.assertContains(resp, "too many SMS codes today")

    def test_parent_by_phone(self):
        parent = make_parent(self.ctx, phone="0752000111")
        create_parent_user(parent)
        self.start("0752 000 111")
        resp = self.client.get(reverse("password_reset_choose"))
        self.assertContains(resp, "Number ending in <strong>111</strong>", html=False)


class UserManagementTests(Base):
    def setUp(self):
        super().setUp()
        self.other_ctx = make_school()
        self.other_teacher = make_user("faraway", role="TEACHER", school=self.other_ctx["school"])
        self.teacher = make_user("localteacher", role="TEACHER", school=self.ctx["school"])
        self.super = make_user("root", role="SUPER_ADMIN", school=None, email="root@example.com")

    def data(self, **params):
        base = {"draw": 1, "start": 0, "length": 50}
        base.update(params)
        return self.client.get(reverse("user_management_data"), base).json()

    def test_school_admin_sees_only_own_school(self):
        self.login_as(self.admin)
        names = " ".join(r["username"] for r in self.data()["data"])
        self.assertIn("localteacher", names)
        self.assertNotIn("faraway", names)
        self.assertNotIn("root", names)

    def test_school_admin_cannot_edit_other_school_user(self):
        self.login_as(self.admin)
        self.client.post(reverse("edit_user", args=[self.other_teacher.pk]),
                         {"username": "hacked", "role": "TEACHER", "is_active": "on"})
        self.other_teacher.refresh_from_db()
        self.assertEqual(self.other_teacher.username, "faraway")

    def test_school_admin_cannot_create_super_admin(self):
        self.login_as(self.admin)
        self.client.post(reverse("add_user"), {"username": "evil", "role": "SUPER_ADMIN"})
        self.assertFalse(User.objects.filter(username="evil").exists())

    def test_teacher_cannot_open_user_management(self):
        self.login_as(self.teacher)
        self.assertEqual(self.client.get(reverse("user_management")).status_code, 403)

    def test_output_is_escaped(self):
        self.teacher.first_name = "<script>alert(1)</script>"
        self.teacher.save()
        self.login_as(self.admin)
        payload = str(self.data())
        self.assertNotIn("<script>", payload)
        self.assertIn("&lt;script&gt;", payload)

    def test_edit_keeps_levels_and_only_school_levels(self):
        ple = EducationLevel.objects.get(code="PLE")
        uce = EducationLevel.objects.create(code="UCE", name="UCE", required_subjects=8)
        self.login_as(self.admin)
        self.client.post(reverse("edit_user", args=[self.teacher.pk]), {
            "username": "localteacher", "role": "TEACHER", "is_active": "on",
            "allowed_levels": [ple.pk, uce.pk],          # UCE is not offered by the school
        })
        self.assertEqual(list(self.teacher.profile.allowed_levels.all()), [ple])

    def test_cannot_deactivate_self_or_change_own_role(self):
        self.login_as(self.admin)
        self.client.post(reverse("edit_user", args=[self.admin.pk]), {"username": "headteacher", "role": "TEACHER"})
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)
        self.assertEqual(self.admin.profile.role, "ADMIN")

    def test_super_admins_cannot_lock_everyone_out(self):
        other = make_user("root2", role="SUPER_ADMIN", school=None, email="r2@example.com")
        self.assertFalse(accounts.is_last_active_super_admin(other))
        self.login_as(other)
        self.client.post(reverse("toggle_user_active", args=[self.super.pk]))   # two active: allowed
        self.super.refresh_from_db()
        self.assertFalse(self.super.is_active)
        self.assertTrue(accounts.is_last_active_super_admin(other))
        # ...and the last one cannot deactivate or demote themselves
        self.client.post(reverse("toggle_user_active", args=[other.pk]))
        self.client.post(reverse("edit_user", args=[other.pk]), {"username": "root2", "role": "ADMIN"})
        other.refresh_from_db()
        self.assertTrue(other.is_active)
        self.assertEqual(other.profile.role, "SUPER_ADMIN")

    def test_staff_cannot_become_student(self):
        self.login_as(self.admin)
        self.client.post(reverse("edit_user", args=[self.teacher.pk]),
                         {"username": "localteacher", "role": "STUDENT", "is_active": "on"})
        self.teacher.refresh_from_db()
        self.assertEqual(self.teacher.profile.role, "TEACHER")

    def test_bulk_reset(self):
        self.login_as(self.admin)
        self.client.post(reverse("bulk_user_action"), {"action": "reset_password", "user_ids": [self.teacher.pk]})
        self.teacher.refresh_from_db()
        self.assertTrue(self.teacher.profile.must_change_password)

    def test_role_name_search(self):
        make_user("nurse1", role="NURSE", school=self.ctx["school"], email="n@example.com")
        self.login_as(self.admin)
        rows = self.data(**{"search[value]": "nurse"})["data"]
        self.assertTrue(any("nurse1" in r["username"] for r in rows))

    def test_profile_password_change_redirects_to_profile(self):
        self.login_as(self.teacher)
        resp = self.client.post(reverse("change_password"), {
            "current_password": STRONG, "new_password": NEW_PW, "confirm_password": NEW_PW})
        self.assertRedirects(resp, reverse("user_profile"), fetch_redirect_response=False)


class AccountGenerationTests(Base):
    def run_job(self, **kw):
        job = StudentAccountGenerationJob.objects.create(requested_by=self.admin, **kw)
        generate_student_accounts_task.apply(args=[str(job.id)])
        job.refresh_from_db()
        return job

    def test_students_by_admission_number_with_slips(self):
        a = make_student(self.ctx, first="John", last="Musoke")
        b = make_student(self.ctx, first="Jane", last="Musoke")
        a.admission_number, b.admission_number = "ADM/2026/001", "ADM/2026/002"
        a.save(); b.save()
        job = self.run_job(selection_mode="filtered", username_format="ADMISSION")
        self.assertEqual(job.status, "SUCCESS")
        self.assertEqual(job.created_count, 2)
        a.refresh_from_db()
        self.assertEqual(a.user.username, "adm2026001")
        self.assertTrue(a.user.profile.must_change_password)

        self.login_as(self.admin)
        resp = self.client.get(reverse("account_login_slips", args=[job.id]))
        self.assertContains(resp, "adm2026001")
        csv_resp = self.client.get(reverse("account_login_slips", args=[job.id]), {"format": "csv"})
        self.assertIn("adm2026001", csv_resp.content.decode())

    def test_same_name_students_get_different_usernames(self):
        make_student(self.ctx, first="John", last="Musoke")
        make_student(self.ctx, first="Joan", last="Musoke")
        self.run_job(selection_mode="filtered", username_format="NAME")
        names = sorted(User.objects.filter(username__startswith="jmusoke").values_list("username", flat=True))
        self.assertEqual(names, ["jmusoke", "jmusoke1"])

    def test_one_bad_row_does_not_stop_the_rest(self):
        make_student(self.ctx, first="Ok", last="One")
        make_student(self.ctx, first="Ok", last="Two")
        real = __import__("mainApp.tasks.user_creation_tasks", fromlist=["x"]).create_student_user
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                # A real database error inside the savepoint (duplicate username)
                User.objects.create(username=self.admin.username)
            return real(*a, **k)
        with mock.patch("mainApp.tasks.user_creation_tasks.create_student_user", side_effect=flaky):
            job = self.run_job(selection_mode="filtered", username_format="NAME")
        self.assertEqual((job.created_count, job.skipped_count, job.processed_count), (1, 1, 2))

    def test_parent_accounts(self):
        p = make_parent(self.ctx)
        job = self.run_job(account_type="PARENT", selection_mode="selected", selected_ids=[str(p.pk)])
        p.refresh_from_db()
        self.assertEqual(job.created_count, 1)
        self.assertEqual(p.user.username, p.parent_id)

    def test_other_school_students_are_not_touched(self):
        other = make_school()
        far = make_student(other)
        self.run_job(selection_mode="filtered", username_format="NAME")
        far.refresh_from_db()
        self.assertIsNone(far.user_id)


class PagesRenderTests(Base):
    def test_pages_render(self):
        anon_pages = [reverse("login"), reverse("login") + "?tab=parent",
                      reverse("password_reset_start"), reverse("password_reset_start") + "?tab=parent"]
        for url in anon_pages:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        resp = self.client.get(reverse("login") + "?tab=parent")
        self.assertContains(resp, "Phone number or Parent ID")

        self.login_as(self.admin)
        for url in [reverse("user_management"), reverse("account_candidates"),
                    reverse("account_candidates") + "?type=parent", reverse("user_profile")]:
            self.assertEqual(self.client.get(url).status_code, 200, url)
        data = self.client.get(reverse("account_candidates_data"), {"type": "parent", "draw": 1}).json()
        self.assertIn("data", data)

        parent = make_parent(self.ctx)
        resp = self.client.get(reverse("parent_detail", args=[parent.pk]))
        self.assertContains(resp, "Create login")
        self.client.post(reverse("create_parent_account", args=[parent.pk]))
        parent.refresh_from_db()
        self.assertIsNotNone(parent.user_id)
        resp = self.client.get(reverse("parent_detail", args=[parent.pk]))
        self.assertContains(resp, "Reset password to default")

    def test_bursar_lands_on_fee_dashboard_not_403(self):
        make_user("bursar1", role="BURSAR", school=self.ctx["school"], email="")
        resp = self.client.post(reverse("login"), {"username": "bursar1", "password": STRONG})
        self.assertRedirects(resp, reverse("dashboard"), fetch_redirect_response=False)
        resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Fee Management")       # bursar sees the fees menu
        self.assertContains(resp, "bursarChartData")      # and the fee dashboard
        self.assertNotContains(resp, "Fee Structures")    # but not the admin-only fee setup

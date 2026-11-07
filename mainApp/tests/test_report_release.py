"""
Report cards: access control, publish step, fees hold, parent links,
attendance/fees/conduct extras, setup screens.

Run:  python manage.py test mainApp.tests.test_report_release
"""
import shutil
import tempfile
from datetime import date
from decimal import Decimal
from unittest import mock

from django.core.files.base import ContentFile
from django.test import TestCase, override_settings
from django.urls import reverse

from mainApp.models.attendance_models import AttendanceRecord, AttendanceSession
from mainApp.models.calendar import Term
from mainApp.models.competency_models import ReportComment
from mainApp.models.fees_models import FeeCollectionSetting, StudentFeePayment
from mainApp.models.report_template_models import ReportCardTemplate
from mainApp.models.report_terms_models import (
    ReportGradeDescriptor,
    ReportKeywordDefinition,
    ReportKeywordScoreBand,
)
from mainApp.models.reporting_task_celery_models import (
    ReportCardSnapshot,
    ReportReleaseOverride,
    ReportRun,
)
from mainApp.report_configs.report_access import (
    bulk_fee_holds,
    make_parent_link_token,
    report_access_for,
)
from mainApp.report_configs.report_extras import build_report_extras
from mainApp.report_configs.report_template_service import (
    ensure_default_template_sections,
    serialize_template_snapshot,
)
from mainApp.tests.factories import make_charge, make_school, make_student, make_user

MEDIA = tempfile.mkdtemp(prefix="rc-media-")


def tearDownModule():
    shutil.rmtree(MEDIA, ignore_errors=True)


@override_settings(MEDIA_ROOT=MEDIA)
class ReleaseBase(TestCase):
    def setUp(self):
        self.ctx = make_school()
        self.school, self.term = self.ctx["school"], self.ctx["term"]
        self.admin = make_user(username="adm", role="ADMIN", school=self.school, email="adm@example.com")
        self.teacher = make_user(username="tch", role="TEACHER", school=self.school, email="t@example.com")
        self.student = make_student(self.ctx, first="Amina")
        self.student_user = make_user(username="amina", role="STUDENT", school=self.school, email="s@example.com")
        self.student.user = self.student_user
        self.student.save()
        self.run = ReportRun.objects.create(
            status=ReportRun.Status.SUCCESS, scope=ReportRun.Scope.CLASS, school=self.school,
            academic_year=self.ctx["year"], term=self.term, class_ref=self.ctx["class"], created_by=self.admin,
        )
        self.snap = self._snapshot(self.student)

    def _snapshot(self, student, published=False):
        snap = ReportCardSnapshot.objects.create(
            run=self.run, student=student, academic_year=self.ctx["year"], term=self.term,
            class_ref=self.ctx["class"], computed_json={"meta": {"level_code": "PLE"}},
            is_latest=True, is_published=published,
        )
        snap.pdf.save(f"rc_{student.pk}.pdf", ContentFile(b"%PDF-1.4 test"), save=True)
        return snap

    def hold_on(self, basis="TERM", allowance=0):
        cfg = FeeCollectionSetting.for_school(self.school)
        cfg.withhold_reports_for_defaulters = True
        cfg.report_hold_basis = basis
        cfg.report_hold_allowance = Decimal(allowance)
        cfg.save()
        return cfg

    def login(self, user):
        self.client.force_login(user)


class AccessControlTests(ReleaseBase):
    def test_only_admins_reach_report_admin_pages(self):
        urls = [
            reverse("report_command_center"),
            reverse("report_run_list"),
            reverse("report_run_detail", args=[self.run.id]),
            reverse("view_report_pdf_admin", args=[self.snap.id]),
            reverse("report_snapshot_preview_html", args=[self.snap.id]),
            reverse("report_release_settings"),
            reverse("report_auto_comment_list"),
            reverse("report_grade_desc_list"),
        ]
        for user in (self.teacher, self.student_user):
            self.login(user)
            for url in urls:
                self.assertEqual(self.client.get(url).status_code, 403, f"{user.username} {url}")

    def test_admin_from_another_school_cannot_open_a_report(self):
        other = make_school()
        other_admin = make_user(username="adm2", role="ADMIN", school=other["school"], email="o@example.com")
        self.login(other_admin)
        self.assertEqual(self.client.get(reverse("view_report_pdf_admin", args=[self.snap.id])).status_code, 404)
        self.assertEqual(self.client.get(reverse("report_snapshot_preview_html", args=[self.snap.id])).status_code, 404)

    def test_admin_pages_render(self):
        self.login(self.admin)
        for url in [
            reverse("report_command_center"),
            reverse("report_run_detail", args=[self.run.id]),
            reverse("report_release_settings"),
            reverse("report_release_overrides") + f"?term_id={self.term.id}&class_ref_id={self.ctx['class'].id}",
            reverse("report_template_list"),
        ]:
            self.assertEqual(self.client.get(url).status_code, 200, url)


class PublishAndHoldTests(ReleaseBase):
    def test_draft_is_hidden_from_student(self):
        self.login(self.student_user)
        resp = self.client.get(reverse("student_my_reports"))
        self.assertNotContains(resp, "Report Card 2026")
        resp = self.client.get(reverse("view_report_pdf_student", args=[self.snap.id]))
        self.assertRedirects(resp, reverse("student_my_reports"), fetch_redirect_response=False)

    def test_release_all_then_student_sees_it(self):
        self.login(self.admin)
        self.client.post(reverse("report_run_publish", args=[self.run.id]), {"action": "publish"})
        self.snap.refresh_from_db()
        self.assertTrue(self.snap.is_published)
        self.assertEqual(self.snap.published_by, self.admin)

        self.login(self.student_user)
        self.assertEqual(self.client.get(reverse("view_report_pdf_student", args=[self.snap.id])).status_code, 200)

    def test_toggle_one_card(self):
        self.login(self.admin)
        self.client.post(reverse("report_snapshot_publish_toggle", args=[self.snap.id]))
        self.snap.refresh_from_db()
        self.assertTrue(self.snap.is_published)
        self.client.post(reverse("report_snapshot_publish_toggle", args=[self.snap.id]))
        self.snap.refresh_from_db()
        self.assertFalse(self.snap.is_published)

    def test_fees_hold_blocks_student_until_paid(self):
        self.snap.is_published = True
        self.snap.save()
        charge = make_charge(self.ctx, self.student, 500000)
        self.hold_on()

        access = report_access_for(self.snap, self.student_user)
        self.assertFalse(access.allowed)
        self.assertEqual(access.code, "FEES_HOLD")
        self.assertEqual(access.balance, Decimal("500000"))

        self.login(self.student_user)
        resp = self.client.get(reverse("student_my_reports"))
        self.assertContains(resp, "Withheld")
        resp = self.client.get(reverse("view_report_pdf_student", args=[self.snap.id]))
        self.assertRedirects(resp, reverse("student_my_reports"), fetch_redirect_response=False)

        # parent pays -> unlocks immediately, nothing regenerated
        charge.amount_paid = charge.amount
        charge.save()
        self.assertTrue(report_access_for(self.snap, self.student_user).allowed)

    def test_staff_always_allowed(self):
        make_charge(self.ctx, self.student, 500000)
        self.hold_on()
        self.assertTrue(report_access_for(self.snap, self.admin).allowed)

    def test_allowance_ignores_small_balance(self):
        self.snap.is_published = True
        self.snap.save()
        make_charge(self.ctx, self.student, 5000)
        self.hold_on(allowance=10000)
        self.assertTrue(report_access_for(self.snap, self.student_user).allowed)

    def test_override_lets_learner_through(self):
        self.snap.is_published = True
        self.snap.save()
        make_charge(self.ctx, self.student, 500000)
        self.hold_on()
        self.login(self.admin)
        self.client.post(
            reverse("report_release_overrides"),
            {"action": "allow", "student_id": self.student.id, "term_id": self.term.id, "reason": "Payment plan"},
        )
        self.assertTrue(ReportReleaseOverride.objects.filter(student=self.student, is_active=True).exists())
        self.assertTrue(report_access_for(self.snap, self.student_user).allowed)
        hold = bulk_fee_holds(self.school, self.term, [self.student.id])[self.student.id]
        self.assertTrue(hold.overridden)

    def test_full_balance_basis_counts_arrears(self):
        self.snap.is_published = True
        self.snap.save()
        t2 = Term.objects.create(academic_year=self.ctx["year"], name="Term 2", order=2,
                                 start_date=date(2026, 6, 1), end_date=date(2026, 8, 1))
        make_charge(self.ctx, self.student, 200000)          # Term 1 arrears
        self.run.term = t2
        self.run.save()
        self.snap.term = t2
        self.snap.save()
        self.hold_on(basis="TERM")
        self.assertTrue(report_access_for(self.snap, self.student_user).allowed)   # T2 has no charges
        self.hold_on(basis="ALL")
        self.assertFalse(report_access_for(self.snap, self.student_user).allowed)

    def test_cleared_only_zip_leaves_out_held(self):
        other = make_student(self.ctx, first="Brian")
        self._snapshot(other)
        make_charge(self.ctx, self.student, 300000)
        self.hold_on()
        self.login(self.admin)
        import io
        import zipfile
        resp = self.client.get(reverse("report_run_download_zip", args=[self.run.id]) + "?cleared=1")
        data = b"".join(resp.streaming_content)
        names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        self.assertEqual(len(names), 1)
        self.assertIn("Brian", names[0])


class ParentLinkTests(ReleaseBase):
    def test_bad_token(self):
        self.assertEqual(self.client.get(reverse("report_parent_link", args=["nonsense"])).status_code, 404)

    def test_link_honours_release_and_fees(self):
        token = make_parent_link_token(self.snap)
        url = reverse("report_parent_link", args=[token])
        self.assertEqual(self.client.get(url).status_code, 403)          # draft

        self.snap.is_published = True
        self.snap.save()
        charge = make_charge(self.ctx, self.student, 450000)
        self.hold_on()
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "450,000", status_code=403)

        charge.amount_paid = charge.amount
        charge.save()
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/pdf")

    def test_link_follows_to_newest_version(self):
        self.snap.is_published = True
        self.snap.save()
        token = make_parent_link_token(self.snap)
        self.snap.is_latest = False
        self.snap.save()
        newer = self._snapshot(self.student, published=True)
        resp = self.client.get(reverse("report_parent_link", args=[token]))
        self.assertEqual(resp.status_code, 200)
        self.assertIn(f"rc_{self.student.pk}", resp["Content-Disposition"])
        self.assertTrue(newer.is_latest)

    def test_send_links_queues_sms_for_released_only(self):
        from mainApp.models.communication_models import SmsMessage
        from mainApp.models.student import Parent, StudentGuardian

        parent = Parent.objects.create(school=self.school, first_name="Sarah", last_name="N", phone="0772123456")
        StudentGuardian.objects.create(student=self.student, parent=parent, receives_sms=True)

        self.login(self.admin)
        with mock.patch("mainApp.tasks.fee_tasks.send_sms_campaign.delay"):
            self.client.post(reverse("report_run_send_to_parents", args=[self.run.id]))
            self.assertEqual(SmsMessage.objects.count(), 0)              # draft -> nothing sent

            self.snap.is_published = True
            self.snap.save()
            with self.captureOnCommitCallbacks(execute=True):
                self.client.post(reverse("report_run_send_to_parents", args=[self.run.id]))
        msg = SmsMessage.objects.get()
        self.assertEqual(msg.to_number, "+256772123456")
        self.assertIn("/r/", msg.text)

        # held learner -> reminder without the link
        SmsMessage.objects.all().delete()
        make_charge(self.ctx, self.student, 300000)
        self.hold_on()
        with mock.patch("mainApp.tasks.fee_tasks.send_sms_campaign.delay"):
            self.client.post(reverse("report_run_send_to_parents", args=[self.run.id]), {"remind_held": "1"})
        msg = SmsMessage.objects.get()
        self.assertNotIn("/r/", msg.text)
        self.assertIn("300,000", msg.text)


class ExtrasTests(ReleaseBase):
    def test_attendance_fees_conduct(self):
        for i, status in enumerate(["P", "P", "L", "A"]):
            sess = AttendanceSession.objects.create(
                school=self.school, term=self.term, class_ref=self.ctx["class"], date=date(2026, 3, 2 + i),
            )
            AttendanceRecord.objects.create(session=sess, student=self.student, status=status)
        charge = make_charge(self.ctx, self.student, 400000)
        charge.amount_paid = Decimal("150000")
        charge.save()
        ReportComment.objects.create(
            school=self.school, level=self.ctx["level"], academic_year=self.ctx["year"], term=self.term,
            class_ref=self.ctx["class"], student=self.student, conduct="Very good", co_curricular="Football",
        )

        extras = build_report_extras(student=self.student, term=self.term)
        self.assertEqual(extras["attendance"]["days_recorded"], 4)
        self.assertEqual(extras["attendance"]["days_present"], 3)
        self.assertEqual(extras["attendance"]["days_absent"], 1)
        self.assertEqual(extras["attendance"]["percent"], 75)
        self.assertEqual(extras["fees"]["term_balance"], Decimal("250000"))
        self.assertEqual(extras["fees"]["total_balance"], Decimal("250000"))
        self.assertEqual(extras["conduct"]["conduct"], "Very good")

    def test_no_data_means_no_sections(self):
        self.assertEqual(build_report_extras(student=self.student, term=self.term), {})

    def test_sections_render_in_pdf_html(self):
        from django.template.loader import render_to_string
        from mainApp.tasks.report_tasks import _build_template_context, _normalize_payload, make_json_safe

        tpl = ReportCardTemplate.objects.create(school=self.school, level="PLE", name="Main")
        ensure_default_template_sections(tpl)
        self.snap.computed_json = _normalize_payload({
            "meta": {"level_code": "PLE", "student_name": "Amina"},
            "summary_rows": [],
            "total_points": None,
            "extras": {
                "attendance": {"days_recorded": 60, "days_present": 57, "days_absent": 3, "days_late": 2, "percent": 95},
                "fees": {"term_billed": Decimal("900000"), "term_paid": Decimal("400000"), "term_balance": Decimal("500000"),
                         "total_balance": Decimal("500000"), "credit_amount": 0, "next_term_name": "Term 2",
                         "next_term_fees": Decimal("950000"), "schoolpay_code": "1002003", "as_at": "31 Oct 2026"},
                "conduct": {"conduct": "Excellent", "co_curricular": "Debate club"},
            },
        })
        self.snap.template_snapshot = make_json_safe(serialize_template_snapshot(tpl))
        ctx = _build_template_context(self.snap, tpl)
        for name in ["report_card.html", "report_card_ple.html", "report_card_uace.html"]:
            html = render_to_string(f"dashboard/reports/pdf/{name}", ctx)
            self.assertIn("57", html, name)
            self.assertIn("500,000", html, name)
            self.assertIn("1002003", html, name)
            self.assertIn("Debate club", html, name)


class SetupScreenTests(ReleaseBase):
    def test_sections_order_and_flags(self):
        tpl = ReportCardTemplate.objects.create(school=self.school, level="PLE", name="Main")
        ensure_default_template_sections(tpl)
        sections = list(tpl.sections.order_by("sort_order", "id"))
        self.login(self.admin)
        post = {"section_order": [str(s.id) for s in reversed(sections)], "action": "save"}
        for s in sections:
            if s.section_key != "signatures":
                post[f"section_{s.id}_is_visible"] = "on"
        self.client.post(reverse("report_template_sections_edit", args=[tpl.pk]), post)
        tpl.refresh_from_db()
        first = tpl.sections.order_by("sort_order").first()
        self.assertEqual(first.id, sections[-1].id)
        self.assertFalse(tpl.show_signatures)                # follows the Sections page

        self.client.post(reverse("report_template_sections_edit", args=[tpl.pk]), {"action": "reset_order"})
        self.assertEqual(tpl.sections.order_by("sort_order").first().section_key, "student_info")

    def test_design_form_does_not_reset_section_switches(self):
        tpl = ReportCardTemplate.objects.create(school=self.school, level="PLE", name="Main", show_signatures=False)
        self.login(self.admin)
        resp = self.client.get(reverse("report_template_edit", args=[tpl.pk]))
        self.assertNotContains(resp, 'name="show_signatures"')

    def test_activate_needs_post(self):
        tpl = ReportCardTemplate.objects.create(school=self.school, level="PLE", name="B", is_active=False)
        self.login(self.admin)
        self.assertEqual(self.client.get(reverse("report_template_activate", args=[tpl.pk])).status_code, 405)
        self.client.post(reverse("report_template_activate", args=[tpl.pk]))
        tpl.refresh_from_db()
        self.assertTrue(tpl.is_active)

    def test_preview_without_any_report_shows_help(self):
        tpl = ReportCardTemplate.objects.create(school=self.school, level="UCE", name="U")
        self.login(self.admin)
        resp = self.client.get(reverse("report_template_preview", args=[tpl.pk]))
        self.assertContains(resp, "Nothing to preview with yet")

    def test_load_standard_values_is_safe_to_repeat(self):
        from mainApp.models.models import EducationLevel
        uce, _ = EducationLevel.objects.get_or_create(code="UCE", defaults={"name": "UCE", "required_subjects": 8})
        self.login(self.admin)
        url = reverse("report_load_standard_terms")
        self.client.post(url, {"level": "UCE"})
        self.client.post(url, {"level": "UCE"})
        self.assertEqual(ReportGradeDescriptor.objects.filter(school=self.school, level=uce).count(), 5)
        self.assertEqual(ReportKeywordScoreBand.objects.filter(school=self.school, level=uce).count(), 3)
        self.assertEqual(ReportKeywordDefinition.objects.filter(school=self.school, level=uce).count(), 3)

    def test_learner_comments_save_conduct(self):
        self.login(self.admin)
        url = reverse("report_comment_entry") + f"?term={self.term.id}&class_ref={self.ctx['class'].id}"
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.client.post(url, {f"cd_{self.student.id}": "Very good", f"cc_{self.student.id}": "Choir"})
        rc = ReportComment.objects.get(student=self.student, term=self.term)
        self.assertEqual((rc.conduct, rc.co_curricular), ("Very good", "Choir"))


class ReadinessTests(ReleaseBase):
    def test_missing_next_term_warns_with_fix_link(self):
        from mainApp.report_configs.report_precheck import (
            ReadinessIssue,
            _attach_fix_links,
            _check_next_term_date,
        )
        issues = []
        _check_next_term_date(academic_year=self.ctx["year"], term=self.term, issues=issues)
        self.assertEqual([i.code for i in issues], ["NO_NEXT_TERM"])
        more = [ReadinessIssue(level="ERROR", code="MISSING_TEMPLATE", message="x")]
        _attach_fix_links(issues + more)
        self.assertEqual(more[0].fix_url, reverse("report_template_list"))
        self.assertTrue(issues[0].fix_url)

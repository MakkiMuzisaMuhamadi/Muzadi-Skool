"""
SchoolPay integration tests. Payloads are SchoolPay's own published samples
(https://www.schoolpay.co.ug/apidocumentation). No real network calls are made.

Run:  python manage.py test mainApp.tests.test_schoolpay
"""
import hashlib
import io
import json
from datetime import date
from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings
from django.urls import reverse

from mainApp.models.auditModels import AuditLog
from mainApp.models.fees_models import StudentFeePayment
from mainApp.models.schoolpay_models import SchoolPayConfig, SchoolPayFeeMapping, SchoolPayTransaction as Txn
from mainApp.schoolpay import client, service
from mainApp.schoolpay.tasks import sync_all_schools
from mainApp.tests.factories import make_charge, make_school, make_student, make_user

D = Decimal
PASSWORD = "sp-secret-123"
SCHOOL_CODE = "809"


def school_fees_payment(receipt="18847257", amount="50000", code="1006480152", reg="", name="Jeremy Kyagulanyi Soran",
                        status="Completed", when="2026-03-10 20:01:52"):
    return {
        "amount": amount, "paymentDateAndTime": when, "schoolpayReceiptNumber": receipt,
        "settlementBankCode": "TROPICAL", "sourceChannelTransDetail": "John Doe",
        "sourceChannelTransactionId": f"TXN_{receipt}", "sourcePaymentChannel": "MTN MobileMoney",
        "studentName": name, "studentPaymentCode": code, "studentRegistrationNumber": reg,
        "transactionCompletionStatus": status,
    }


def other_fees_payment(receipt="18843597", amount="150000.00", code="1006480152", fee_id="20"):
    p = school_fees_payment(receipt=receipt, amount=amount, code=code)
    p.update({"supplementaryFeeDescription": "UNIFORM FEES", "supplementaryFeeId": fee_id, "studentClass": "P.7"})
    return p


def signed(payment, kind="SCHOOL_FEES", password=PASSWORD):
    sig = hashlib.sha256(f"{password}{payment['schoolpayReceiptNumber']}".encode()).hexdigest()
    return json.dumps({"signature": sig, "type": kind, "payment": payment})


class SchoolPayTestBase(TestCase):
    def setUp(self):
        self.ctx = make_school()
        self.school = self.ctx["school"]
        self.school.current_term = self.ctx["term"]
        self.school.academic_year = self.ctx["year"]
        self.school.save()
        self.student = make_student(self.ctx, first="Jeremy", last="Soran")
        self.student.schoolpay_code = "1006480152"
        self.student.save()
        self.tuition = make_charge(self.ctx, self.student, 300_000, "Tuition", priority=1)
        self.config = SchoolPayConfig(school=self.school, school_code=SCHOOL_CODE, is_enabled=True)
        self.config.set_api_password(PASSWORD)
        self.config.save()
        self.url = reverse("schoolpay_webhook", args=[self.config.webhook_token])

    def post_webhook(self, body, **extra):
        return self.client.post(self.url, data=body, content_type="application/json", **extra)

    def sync_returns(self, *rows):
        """Pretend SchoolPay's sync API returns these (type, payment) rows."""
        return mock.patch.object(client, "fetch_range", return_value=list(rows)), \
            mock.patch.object(client, "fetch_day", return_value=list(rows))


# ------------------------------------------------------------- the client --

class ClientTests(SchoolPayTestBase):
    def test_hash_matches_schoolpay_formula(self):
        expected = hashlib.md5(b"8092024-01-15secret").hexdigest().upper()
        self.assertEqual(client.request_hash("809", "2024-01-15", "secret"), expected)

    def test_signature_check(self):
        sig = hashlib.sha256(b"secret18847257").hexdigest()
        self.assertTrue(client.signature_is_valid("secret", "18847257", sig))
        self.assertTrue(client.signature_is_valid("secret", "18847257", sig.upper()))
        self.assertFalse(client.signature_is_valid("secret", "18847258", sig))
        self.assertFalse(client.signature_is_valid("", "18847257", sig))

    def test_range_is_split_into_31_day_windows(self):
        ok = mock.Mock(status_code=200, json=lambda: {"returnCode": 0, "transactions": [school_fees_payment()],
                                                         "supplementaryFeePayments": [other_fees_payment()]})
        with mock.patch("mainApp.schoolpay.client.requests.get", return_value=ok) as get:
            rows = client.fetch_range("809", "pw", date(2026, 1, 1), date(2026, 3, 11))  # 70 days
        urls = [c.args[0] for c in get.call_args_list]
        self.assertEqual(len(urls), 3)
        self.assertIn("/SchoolRangeTransactions/809/2026-01-01/2026-01-31/" + client.request_hash("809", "2026-01-01", "pw"), urls[0])
        self.assertIn("/2026-02-01/2026-03-03/", urls[1])
        self.assertIn("/2026-03-04/2026-03-11/", urls[2])
        self.assertEqual(len(rows), 6)
        self.assertEqual({t for t, _ in rows}, {"SCHOOL_FEES", "OTHER_FEES"})

    def test_refusal_and_network_errors_raise(self):
        bad = mock.Mock(status_code=200, json=lambda: {"returnCode": 1, "returnMessage": "Invalid hash"})
        with mock.patch("mainApp.schoolpay.client.requests.get", return_value=bad):
            with self.assertRaisesMessage(client.SchoolPayError, "Invalid hash"):
                client.fetch_day("809", "pw", date(2026, 3, 1))
        import requests
        with mock.patch("mainApp.schoolpay.client.requests.get", side_effect=requests.ConnectionError("down")):
            with self.assertRaises(client.SchoolPayError):
                client.fetch_day("809", "pw", date(2026, 3, 1))


# ---------------------------------------------------------------- webhook --

class WebhookTests(SchoolPayTestBase):
    def test_valid_payment_is_posted_to_the_student(self):
        resp = self.post_webhook(signed(school_fees_payment()))
        self.assertEqual(resp.status_code, 200)
        txn = Txn.objects.get()
        self.assertEqual(txn.status, Txn.STATUS_POSTED)
        payment = txn.fee_payment
        self.assertEqual((payment.student, payment.amount, payment.payment_method), (self.student, D("50000"), "SCHOOLPAY"))
        self.assertEqual(payment.reference, "SP-18847257")
        self.assertEqual(payment.received_by.username, "schoolpay")
        self.assertEqual(payment.payment_date, date(2026, 3, 10))
        self.tuition.refresh_from_db()
        self.assertEqual(self.tuition.amount_paid, D("50000"))

    def test_same_webhook_twice_posts_once(self):
        self.post_webhook(signed(school_fees_payment()))
        self.post_webhook(signed(school_fees_payment()))
        self.assertEqual(StudentFeePayment.objects.count(), 1)

    def test_bad_signature_rejected_and_audited(self):
        resp = self.post_webhook(signed(school_fees_payment(), password="wrong"))
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Txn.objects.exists())
        self.assertTrue(AuditLog.objects.filter(object_repr__startswith="REJECTED webhook").exists())

    def test_unknown_or_disabled_endpoint(self):
        resp = self.client.post(reverse("schoolpay_webhook", args=["nope"]), data="{}", content_type="application/json")
        self.assertEqual(resp.status_code, 404)
        self.config.is_enabled = False
        self.config.save()
        self.assertEqual(self.post_webhook(signed(school_fees_payment())).status_code, 404)

    def test_get_and_garbage_rejected(self):
        self.assertEqual(self.client.get(self.url).status_code, 405)
        self.assertEqual(self.post_webhook("not json").status_code, 400)
        self.assertEqual(self.post_webhook(json.dumps({"payment": {}})).status_code, 400)

    def test_ip_allow_list(self):
        self.config.allowed_ips = "41.210.1.1"
        self.config.save()
        self.assertEqual(self.post_webhook(signed(school_fees_payment()), REMOTE_ADDR="8.8.8.8").status_code, 403)
        self.assertEqual(self.post_webhook(signed(school_fees_payment()), REMOTE_ADDR="41.210.1.1").status_code, 200)

    @override_settings(SCHOOLPAY_TRUST_X_FORWARDED_FOR=True)
    def test_ip_allow_list_cannot_be_faked_with_forwarded_header(self):
        self.config.allowed_ips = "41.210.1.1"
        self.config.save()
        # Attacker puts the allowed IP first; nginx appends the attacker's real IP last.
        resp = self.post_webhook(signed(school_fees_payment()), HTTP_X_FORWARDED_FOR="41.210.1.1, 8.8.8.8")
        self.assertEqual(resp.status_code, 403)

    def test_unknown_student_goes_to_inbox_not_lost(self):
        resp = self.post_webhook(signed(school_fees_payment(code="999", name="New Learner")))
        self.assertEqual(resp.status_code, 200)
        txn = Txn.objects.get()
        self.assertEqual(txn.status, Txn.STATUS_UNMATCHED)
        self.assertFalse(StudentFeePayment.objects.exists())

    def test_matches_by_registration_number_and_learns_code(self):
        other = make_student(self.ctx, first="Amina")
        self.post_webhook(signed(school_fees_payment(receipt="5", code="1777", reg=other.admission_number)))
        other.refresh_from_db()
        self.assertEqual(Txn.objects.get().student, other)
        self.assertEqual(other.schoolpay_code, "1777")

    def test_incomplete_payment_ignored(self):
        self.post_webhook(signed(school_fees_payment(status="Pending")))
        self.assertEqual(Txn.objects.get().status, Txn.STATUS_IGNORED)
        self.assertFalse(StudentFeePayment.objects.exists())

    def test_no_current_term_fails_then_sync_retries(self):
        self.school.current_term = None
        self.school.save()
        self.post_webhook(signed(school_fees_payment()))
        self.assertEqual(Txn.objects.get().status, Txn.STATUS_FAILED)

        self.school.current_term = self.ctx["term"]
        self.school.save()
        self.config.refresh_from_db()
        p1, p2 = self.sync_returns(("SCHOOL_FEES", school_fees_payment()))
        with p1, p2:
            service.sync_school(self.config)
        self.assertEqual(Txn.objects.get().status, Txn.STATUS_POSTED)

    def test_other_fees_pay_the_mapped_category_first(self):
        uniform = make_charge(self.ctx, self.student, 150_000, "Uniform", priority=5)
        SchoolPayFeeMapping.objects.create(school=self.school, supplementary_fee_id="20", fee_category=uniform.fee_category)
        self.post_webhook(signed(other_fees_payment(), kind="OTHER_FEES"))
        uniform.refresh_from_db(); self.tuition.refresh_from_db()
        self.assertEqual(uniform.status, "PAID")
        self.assertEqual(self.tuition.amount_paid, D("0"))

    def test_unmapped_other_fee_is_listed_for_mapping(self):
        self.post_webhook(signed(other_fees_payment(), kind="OTHER_FEES"))
        mapping = SchoolPayFeeMapping.objects.get()
        self.assertEqual((mapping.supplementary_fee_id, mapping.description, mapping.fee_category), ("20", "UNIFORM FEES", None))

    def test_parent_sms_with_balance(self):
        from mainApp.models.student import Parent
        parent = Parent.objects.create(school=self.school, first_name="Grace", last_name="Soran",
                                       relationship="MOTHER", phone="0772123456", address="Kampala")
        self.student.parents.add(parent)
        self.config.sms_parent_on_payment = True
        self.config.save()
        with mock.patch("mainApp.sms_service.AfricaTalkingSMS") as sms_cls:
            with self.captureOnCommitCallbacks(execute=True):
                self.post_webhook(signed(school_fees_payment()))
        message, number = sms_cls.return_value.send_one.call_args.args
        self.assertEqual(number, "+256772123456")
        self.assertIn("UGX 50,000", message)
        self.assertIn("Balance: UGX 250,000", message)


# ------------------------------------------------------------------- sync --

class SyncTests(SchoolPayTestBase):
    def test_sync_posts_payments_the_webhook_missed(self):
        p1, p2 = self.sync_returns(("SCHOOL_FEES", school_fees_payment(receipt="1")),
                                   ("SCHOOL_FEES", school_fees_payment(receipt="2", amount="20000")))
        with p1, p2:
            summary = service.sync_school(self.config)
        self.assertEqual((summary["fetched"], summary["new"], summary["posted"]), (2, 2, 2))
        self.assertEqual(StudentFeePayment.objects.count(), 2)
        self.config.refresh_from_db()
        self.assertTrue(self.config.last_sync_ok)

    def test_webhook_then_sync_confirms_without_double_posting(self):
        self.post_webhook(signed(school_fees_payment()))
        p1, p2 = self.sync_returns(("SCHOOL_FEES", school_fees_payment()))
        with p1, p2:
            summary = service.sync_school(self.config)
        self.assertEqual(summary["new"], 0)
        self.assertEqual(StudentFeePayment.objects.count(), 1)
        self.assertTrue(Txn.objects.get().verified_by_sync)

    def test_amount_mismatch_detected_and_corrected(self):
        # A tampered/incorrect webhook claims 50,000; SchoolPay's records say 5,000.
        self.post_webhook(signed(school_fees_payment(amount="50000")))
        p1, p2 = self.sync_returns(("SCHOOL_FEES", school_fees_payment(amount="5000")))
        with p1, p2:
            service.sync_school(self.config)
        txn = Txn.objects.get()
        self.assertEqual(txn.status, Txn.STATUS_MISMATCH)
        self.assertEqual(txn.synced_amount, D("5000"))

        bursar = make_user("bursar", school=self.school)
        service.fix_amount_mismatch(txn, bursar)
        txn.refresh_from_db(); self.tuition.refresh_from_db()
        self.assertEqual(txn.status, Txn.STATUS_POSTED)
        self.assertEqual(txn.fee_payment.amount, D("5000"))
        self.assertEqual(self.tuition.amount_paid, D("5000"))
        self.assertEqual(StudentFeePayment.objects.filter(is_reversed=True).count(), 1)

    def test_strict_mode_waits_for_sync(self):
        self.config.strict_mode = True
        self.config.save()
        self.post_webhook(signed(school_fees_payment()))
        self.assertEqual(Txn.objects.get().status, Txn.STATUS_PENDING)
        self.assertFalse(StudentFeePayment.objects.exists())
        p1, p2 = self.sync_returns(("SCHOOL_FEES", school_fees_payment()))
        with p1, p2:
            service.sync_school(self.config)
        self.assertEqual(Txn.objects.get().status, Txn.STATUS_POSTED)

    def test_failed_sync_is_recorded(self):
        with mock.patch.object(client, "fetch_range", side_effect=client.SchoolPayError("Invalid hash")):
            with self.assertRaises(client.SchoolPayError):
                service.sync_school(self.config)
        self.config.refresh_from_db()
        self.assertFalse(self.config.last_sync_ok)
        self.assertIn("Invalid hash", self.config.last_sync_message)

    def test_hourly_task_skips_disabled_schools(self):
        other = make_school()
        off = SchoolPayConfig(school=other["school"], school_code="1", is_enabled=False)
        off.set_api_password("x"); off.save()
        with mock.patch("mainApp.schoolpay.service.sync_school", return_value={"fetched": 0}) as sync:
            sync_all_schools()
        self.assertEqual([c.args[0].pk for c in sync.call_args_list], [self.config.pk])

    def test_reconciliation(self):
        self.post_webhook(signed(school_fees_payment(receipt="1", amount="50000")))
        self.post_webhook(signed(school_fees_payment(receipt="2", amount="30000", code="unknown")))
        r = service.reconciliation(self.school, date(2026, 3, 10))
        self.assertEqual((r["schoolpay_total"], r["posted_total"], r["difference"]), (D("80000"), D("50000"), D("30000")))
        self.assertEqual(len(r["attention"]), 1)


# ---------------------------------------------------------- bursar screens --

class BursarScreenTests(SchoolPayTestBase):
    def setUp(self):
        super().setUp()
        self.bursar = make_user("bursar", school=self.school)
        self.client.login(username="bursar", password="Str0ng-Pass!")

    def test_assign_unmatched_payment_by_admission_number(self):
        self.post_webhook(signed(school_fees_payment(code="1234567", name="Unknown")))
        txn = Txn.objects.get()
        target = make_student(self.ctx, first="Amina")
        self.client.post(reverse("schoolpay_assign", args=[txn.pk]),
                         {"admission_number": target.admission_number, "remember_code": "1"})
        txn.refresh_from_db(); target.refresh_from_db()
        self.assertEqual(txn.status, Txn.STATUS_POSTED)
        self.assertEqual(txn.fee_payment.student, target)
        self.assertEqual(target.schoolpay_code, "1234567")
        self.assertEqual(txn.resolved_by, self.bursar)

    def test_dismiss_needs_reason(self):
        self.post_webhook(signed(school_fees_payment(code="nobody")))
        txn = Txn.objects.get()
        self.client.post(reverse("schoolpay_dismiss", args=[txn.pk]), {"reason": ""})
        txn.refresh_from_db()
        self.assertEqual(txn.status, Txn.STATUS_UNMATCHED)
        self.client.post(reverse("schoolpay_dismiss", args=[txn.pk]), {"reason": "Refunded by SchoolPay"})
        txn.refresh_from_db()
        self.assertEqual(txn.status, Txn.STATUS_IGNORED)

    def test_other_school_cannot_touch_payment(self):
        self.post_webhook(signed(school_fees_payment(code="nobody")))
        txn = Txn.objects.get()
        other = make_school()
        make_user("intruder", school=other["school"])
        self.client.login(username="intruder", password="Str0ng-Pass!")
        resp = self.client.post(reverse("schoolpay_assign", args=[txn.pk]), {"admission_number": "x"})
        self.assertEqual(resp.status_code, 404)

    def test_teacher_has_no_access(self):
        make_user("teacher", role="TEACHER", school=self.school)
        self.client.login(username="teacher", password="Str0ng-Pass!")
        self.assertEqual(self.client.get(reverse("schoolpay_settings")).status_code, 403)
        self.assertEqual(self.client.get(reverse("schoolpay_payments")).status_code, 403)

    def test_redirect_after_action_stays_on_site(self):
        self.post_webhook(signed(school_fees_payment(code="nobody")))
        txn = Txn.objects.get()
        resp = self.client.post(reverse("schoolpay_dismiss", args=[txn.pk]),
                                {"reason": "x", "next": "https://evil.example.com/"})
        self.assertEqual(resp["Location"], reverse("schoolpay_payments"))

    def test_settings_password_is_encrypted_and_kept(self):
        url = reverse("schoolpay_settings")
        self.client.post(url, {"action": "save", "school_code": "809", "api_password": "new-pass",
                               "is_enabled": "on", "sync_days_back": 3, "allowed_ips": ""})
        self.config.refresh_from_db()
        self.assertNotIn("new-pass", self.config.api_password_encrypted)
        self.assertEqual(self.config.get_api_password(), "new-pass")
        self.client.post(url, {"action": "save", "school_code": "809", "api_password": "",
                               "is_enabled": "on", "sync_days_back": 3, "allowed_ips": ""})
        self.config.refresh_from_db()
        self.assertEqual(self.config.get_api_password(), "new-pass")

    def test_settings_validation(self):
        resp = self.client.post(reverse("schoolpay_settings"), {
            "action": "save", "school_code": "80A", "sync_days_back": 3, "allowed_ips": "not-an-ip"})
        self.assertContains(resp, "numbers only")
        self.assertContains(resp, "not a valid IP address")

    def test_pages_render(self):
        self.post_webhook(signed(school_fees_payment(code="nobody", name="Jeremy Soran")))
        for name in ("schoolpay_settings", "schoolpay_payments", "schoolpay_import_codes"):
            resp = self.client.get(reverse(name))
            self.assertEqual(resp.status_code, 200, name)
        resp = self.client.get(reverse("schoolpay_payments") + "?tab=attention")
        self.assertContains(resp, "Choose the student")          # name-based suggestion offered
        self.assertContains(resp, self.student.admission_number)

    def test_import_codes_csv(self):
        a = make_student(self.ctx, first="Amina")
        self.post_webhook(signed(school_fees_payment(receipt="77", code="2000001", name="Amina")))  # waiting
        csv_file = io.BytesIO(
            f"Admission number,SchoolPay code\n{a.admission_number},2000001\nADM-NOPE,3\n"
            f"{self.student.admission_number},2000001\n".encode())
        csv_file.name = "codes.csv"
        resp = self.client.post(reverse("schoolpay_import_codes"), {"file": csv_file})
        report = resp.context["report"]
        self.assertEqual(report["updated"], 1)
        self.assertEqual(report["not_found"], ["ADM-NOPE"])
        self.assertEqual(len(report["conflicts"]), 1)
        self.assertEqual(report["auto_posted"], 1)
        self.assertEqual(Txn.objects.get(receipt_number="77").status, Txn.STATUS_POSTED)


class CryptoTests(TestCase):
    def test_round_trip_and_key_change(self):
        from mainApp.schoolpay import crypto
        token = crypto.encrypt("pw")
        self.assertNotEqual(token, "pw")
        self.assertEqual(crypto.decrypt(token), "pw")
        with override_settings(SECRET_KEY="different", FIELD_ENCRYPTION_KEY=""):
            self.assertEqual(crypto.decrypt(token), "")

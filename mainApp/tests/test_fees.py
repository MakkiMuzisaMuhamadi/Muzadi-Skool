"""
Fee tests: allocation, credits, receipt numbers, duplicates, reversal.

Run:  python manage.py test mainApp.tests.test_fees
"""
from datetime import date
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from mainApp.fee_services import (
    DuplicatePaymentError,
    FeePaymentError,
    apply_existing_credits,
    record_payment,
    reverse_payment,
)
from mainApp.models.auditModels import AuditLog
from mainApp.models.fees_models import (
    ReceiptSequence,
    StudentFeeAdjustment,
    StudentFeeCredit,
    StudentFeePayment,
)
from mainApp.tests.factories import make_charge, make_school, make_student, make_user

D = Decimal


def new_payment(ctx, student, amount, method="CASH", reference=None, on=date(2026, 3, 10)):
    return StudentFeePayment(
        school=ctx["school"], student=student,
        academic_year=ctx["year"], term=ctx["term"],
        amount=D(str(amount)), payment_method=method,
        reference=reference, payment_date=on,
    )


class PaymentAllocationTests(TestCase):
    def setUp(self):
        self.ctx = make_school()
        self.student = make_student(self.ctx)
        self.bursar = make_user(school=self.ctx["school"])
        self.tuition = make_charge(self.ctx, self.student, 300_000, "Tuition", priority=1)
        self.uniform = make_charge(self.ctx, self.student, 50_000, "Uniform", priority=2)

    def pay(self, amount, **kw):
        return record_payment(new_payment(self.ctx, self.student, amount, **kw), user=self.bursar)

    def test_partial_payment_goes_to_highest_priority_first(self):
        self.pay(200_000)
        self.tuition.refresh_from_db(); self.uniform.refresh_from_db()
        self.assertEqual(self.tuition.amount_paid, D("200000"))
        self.assertEqual(self.tuition.status, "PARTIAL")
        self.assertEqual(self.uniform.amount_paid, D("0"))
        self.assertEqual(self.uniform.status, "PENDING")

    def test_exact_payment_clears_everything(self):
        payment = self.pay(350_000)
        self.tuition.refresh_from_db(); self.uniform.refresh_from_db()
        self.assertEqual((self.tuition.status, self.uniform.status), ("PAID", "PAID"))
        self.assertEqual(payment.unapplied_amount, D("0"))
        self.assertFalse(StudentFeeCredit.objects.exists())

    def test_overpayment_becomes_credit(self):
        payment = self.pay(400_000)
        self.assertEqual(payment.unapplied_amount, D("50000"))
        credit = StudentFeeCredit.objects.get(source_payment=payment)
        self.assertEqual(credit.balance, D("50000"))
        self.assertTrue(credit.is_active)

    def test_credit_pays_a_later_charge(self):
        self.pay(400_000)                                   # 50,000 credit
        lunch = make_charge(self.ctx, self.student, 30_000, "Lunch", priority=3)
        applied = apply_existing_credits(self.student, user=self.bursar)
        lunch.refresh_from_db()
        self.assertEqual(applied, D("30000"))
        self.assertEqual(lunch.status, "PAID")
        self.assertEqual(StudentFeeCredit.objects.get().balance, D("20000"))

    def test_credit_marked_used_up(self):
        self.pay(400_000)
        make_charge(self.ctx, self.student, 80_000, "Lunch", priority=3)
        apply_existing_credits(self.student)
        credit = StudentFeeCredit.objects.get()
        self.assertEqual(credit.balance, D("0"))
        self.assertFalse(credit.is_active)

    def test_zero_or_negative_amount_refused(self):
        for bad in (0, -5000):
            with self.assertRaises(FeePaymentError):
                self.pay(bad)
        self.assertFalse(StudentFeePayment.objects.exists())

    def test_student_from_another_school_refused(self):
        other = make_school()
        stranger = make_student(other)
        payment = new_payment(self.ctx, stranger, 10_000)   # this school, other school's student
        with self.assertRaises(FeePaymentError):
            record_payment(payment, user=self.bursar)

    def test_payment_is_audited(self):
        payment = self.pay(10_000)
        log = AuditLog.objects.get(model_name="StudentFeePayment", object_id=str(payment.pk))
        self.assertEqual(log.action, "CREATE")
        self.assertEqual(log.user, self.bursar)
        self.assertEqual(log.new_values["amount"], "10000")

    def test_fee_adjustment_waiver_updates_status(self):
        adj = StudentFeeAdjustment.objects.create(
            charge=self.uniform, adjustment_type="WAIVER", amount=D("50000"), reason="Bursary"
        )
        adj.apply()
        self.uniform.refresh_from_db()
        self.assertEqual(self.uniform.status, "WAIVED")


class ReceiptNumberTests(TestCase):
    def setUp(self):
        self.ctx = make_school()
        self.student = make_student(self.ctx)
        make_charge(self.ctx, self.student, 1_000_000)

    def test_numbers_are_sequential_per_school_and_year(self):
        numbers = [
            record_payment(new_payment(self.ctx, self.student, 1000 + i)).receipt_no
            for i in range(3)
        ]
        pk = self.ctx["school"].pk
        self.assertEqual(numbers, [f"RCPT-{pk:03d}-2026-{n:06d}" for n in (1, 2, 3)])

    def test_each_school_has_its_own_counter(self):
        other = make_school()
        other_student = make_student(other)
        a = record_payment(new_payment(self.ctx, self.student, 1000)).receipt_no
        b = record_payment(new_payment(other, other_student, 1000)).receipt_no
        self.assertTrue(a.endswith("-000001"))
        self.assertTrue(b.endswith("-000001"))
        self.assertNotEqual(a, b)

    def test_new_year_restarts_at_one(self):
        record_payment(new_payment(self.ctx, self.student, 1000, on=date(2026, 12, 30)))
        nxt = record_payment(new_payment(self.ctx, self.student, 1001, on=date(2027, 1, 5)))
        self.assertIn("-2027-000001", nxt.receipt_no)

    def test_skips_numbers_that_already_exist(self):
        pk = self.ctx["school"].pk
        # A receipt typed in before the counter existed:
        StudentFeePayment.objects.create(
            school=self.ctx["school"], student=self.student,
            academic_year=self.ctx["year"], term=self.ctx["term"],
            amount=D("5"), receipt_no=ReceiptSequence.format_number(pk, 2026, 1),
            payment_date=date(2026, 3, 1),
        )
        p = record_payment(new_payment(self.ctx, self.student, 1000))
        self.assertTrue(p.receipt_no.endswith("-000002"))

    def test_saving_without_service_still_gets_unique_number(self):
        p = new_payment(self.ctx, self.student, 1000)
        p.save()  # old code paths that call .save() directly
        self.assertTrue(p.receipt_no.startswith("RCPT-"))


class DuplicatePaymentTests(TestCase):
    def setUp(self):
        self.ctx = make_school()
        self.student = make_student(self.ctx)
        make_charge(self.ctx, self.student, 1_000_000)

    def test_double_click_is_caught(self):
        record_payment(new_payment(self.ctx, self.student, 200_000))
        with self.assertRaises(DuplicatePaymentError):
            record_payment(new_payment(self.ctx, self.student, 200_000))
        self.assertEqual(StudentFeePayment.objects.count(), 1)

    def test_cashier_can_confirm_a_real_second_payment(self):
        record_payment(new_payment(self.ctx, self.student, 200_000))
        record_payment(new_payment(self.ctx, self.student, 200_000), allow_duplicate=True)
        self.assertEqual(StudentFeePayment.objects.count(), 2)

    def test_different_amount_is_not_a_duplicate(self):
        record_payment(new_payment(self.ctx, self.student, 200_000))
        record_payment(new_payment(self.ctx, self.student, 150_000))
        self.assertEqual(StudentFeePayment.objects.count(), 2)

    def test_mobile_money_reference_cannot_be_reused(self):
        record_payment(new_payment(self.ctx, self.student, 50_000, method="MOBILE", reference="MP2610ABC"))
        other = make_student(self.ctx)
        with self.assertRaises(FeePaymentError) as err:
            record_payment(
                new_payment(self.ctx, other, 70_000, method="MOBILE", reference=" mp2610abc "),
                allow_duplicate=True,   # even "confirm" cannot reuse a transaction ID
            )
        self.assertNotIsInstance(err.exception, DuplicatePaymentError)
        self.assertIn("MP2610ABC", str(err.exception))

    def test_reference_can_be_reused_after_reversal(self):
        p = record_payment(new_payment(self.ctx, self.student, 50_000, method="BANK", reference="SLIP-9"))
        reverse_payment(p, reason="Posted to wrong student")
        record_payment(new_payment(self.ctx, make_student(self.ctx), 50_000, method="BANK", reference="SLIP-9"))


class ReversalTests(TestCase):
    def setUp(self):
        self.ctx = make_school()
        self.student = make_student(self.ctx)
        self.user = make_user(school=self.ctx["school"])
        self.tuition = make_charge(self.ctx, self.student, 300_000)

    def test_reversal_restores_balance(self):
        p = record_payment(new_payment(self.ctx, self.student, 300_000), user=self.user)
        reverse_payment(p, user=self.user, reason="Wrong student")
        self.tuition.refresh_from_db(); p.refresh_from_db()
        self.assertEqual(self.tuition.amount_paid, D("0"))
        self.assertEqual(self.tuition.status, "PENDING")
        self.assertTrue(p.is_reversed)
        self.assertEqual(p.reversed_by, self.user)
        self.assertEqual(p.reversal_reason, "Wrong student")

    def test_reversal_withdraws_credit_even_if_already_used(self):
        p = record_payment(new_payment(self.ctx, self.student, 350_000))  # 50k credit
        lunch = make_charge(self.ctx, self.student, 40_000, "Lunch", priority=2)
        apply_existing_credits(self.student)
        lunch.refresh_from_db()
        self.assertEqual(lunch.status, "PAID")

        reverse_payment(p, reason="Cheque bounced")
        lunch.refresh_from_db(); self.tuition.refresh_from_db()
        credit = StudentFeeCredit.objects.get(source_payment=p)
        self.assertEqual(lunch.amount_paid, D("0"))
        self.assertEqual(self.tuition.amount_paid, D("0"))
        self.assertEqual((credit.balance, credit.is_active), (D("0"), False))

    def test_reason_required_and_no_double_reversal(self):
        p = record_payment(new_payment(self.ctx, self.student, 1000))
        with self.assertRaises(FeePaymentError):
            reverse_payment(p, reason="  ")
        reverse_payment(p, reason="Typo")
        with self.assertRaises(FeePaymentError):
            reverse_payment(p, reason="Again")

    def test_reversal_is_audited(self):
        p = record_payment(new_payment(self.ctx, self.student, 1000))
        reverse_payment(p, user=self.user, reason="Typo")
        log = AuditLog.objects.filter(model_name="StudentFeePayment", action="UPDATE").get()
        self.assertEqual(log.new_values["reason"], "Typo")


class ReverseViewTests(TestCase):
    def setUp(self):
        self.ctx = make_school()
        self.student = make_student(self.ctx)
        make_charge(self.ctx, self.student, 100_000)
        self.payment = record_payment(new_payment(self.ctx, self.student, 100_000))

    def test_admin_of_same_school_can_reverse(self):
        make_user("admin1", school=self.ctx["school"])
        self.client.login(username="admin1", password="Str0ng-Pass!")
        self.client.post(reverse("fee_payment_reverse", args=[self.payment.pk]), {"reason": "Mistake"})
        self.payment.refresh_from_db()
        self.assertTrue(self.payment.is_reversed)

    def test_admin_of_other_school_gets_404(self):
        other = make_school()
        make_user("intruder", school=other["school"])
        self.client.login(username="intruder", password="Str0ng-Pass!")
        resp = self.client.post(reverse("fee_payment_reverse", args=[self.payment.pk]), {"reason": "x"})
        self.assertEqual(resp.status_code, 404)
        self.payment.refresh_from_db()
        self.assertFalse(self.payment.is_reversed)

    def test_teacher_cannot_reverse(self):
        make_user("teacher1", role="TEACHER", school=self.ctx["school"])
        self.client.login(username="teacher1", password="Str0ng-Pass!")
        resp = self.client.post(reverse("fee_payment_reverse", args=[self.payment.pk]), {"reason": "x"})
        self.assertEqual(resp.status_code, 403)


class CollectPageTests(TestCase):
    """The cashier screen (AJAX path used by fees_collect.html)."""

    def setUp(self):
        self.url = reverse("fees_collect")
        self.ctx = make_school()
        self.student = make_student(self.ctx)
        make_charge(self.ctx, self.student, 500_000)
        make_user("cashier", school=self.ctx["school"])
        self.client.login(username="cashier", password="Str0ng-Pass!")

    def post(self, **extra):
        data = {
            "student": self.student.pk, "academic_year": self.ctx["year"].pk,
            "term": self.ctx["term"].pk, "payment_date": "2026-03-10",
            "amount": "100000", "payment_method": "CASH", "reference": "", "notes": "",
        }
        data.update(extra)
        return self.client.post(self.url, data, HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_payment_saved_and_receipt_returned(self):
        resp = self.post()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "success")
        self.assertIn("RCPT-", resp.json()["message"])

    def test_double_click_gets_409_with_override_flag(self):
        self.post()
        resp = self.post()
        self.assertEqual(resp.status_code, 409)
        self.assertTrue(resp.json()["duplicate"])
        self.assertEqual(StudentFeePayment.objects.count(), 1)

    def test_confirmed_second_payment_is_saved(self):
        self.post()
        resp = self.post(confirm_duplicate="1")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(StudentFeePayment.objects.count(), 2)

    def test_reused_reference_gets_400_without_override(self):
        self.post(payment_method="MOBILE", reference="TX-1")
        resp = self.post(payment_method="MOBILE", reference="TX-1", amount="5000", confirm_duplicate="1")
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(resp.json()["duplicate"])
        self.assertIn("TX-1", resp.json()["errors"]["__all__"][0])

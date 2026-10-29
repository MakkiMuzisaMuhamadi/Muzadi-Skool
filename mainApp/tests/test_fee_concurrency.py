"""
Two (or more) cashiers saving at the same moment.

These tests need PostgreSQL (SQLite has no row locking), so they are
skipped automatically on SQLite. Run them against your real database engine:

    python manage.py test mainApp.tests.test_fee_concurrency
"""
import threading
import time
import unittest
from unittest import mock
from datetime import date
from decimal import Decimal

from django.db import connection, connections
from django.test import TransactionTestCase

from mainApp import fee_services
from mainApp.fee_services import record_payment
from mainApp.models.fees_models import StudentFeeAllocation, StudentFeePayment
from mainApp.tests.factories import make_charge, make_school, make_student

D = Decimal


def run_in_parallel(fn, n):
    """Start n threads that all call fn() at the same instant."""
    barrier = threading.Barrier(n)
    errors, results = [], []

    def worker(i):
        try:
            barrier.wait()
            results.append(fn(i))
        except Exception as exc:  # collected and checked by the test
            errors.append(exc)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results, errors


@unittest.skipUnless(connection.vendor == "postgresql", "Row locking needs PostgreSQL")
class ConcurrentPaymentTests(TransactionTestCase):
    def setUp(self):
        self.ctx = make_school()

    def _payment(self, student, amount, i):
        return StudentFeePayment(
            school=self.ctx["school"], student=student,
            academic_year=self.ctx["year"], term=self.ctx["term"],
            amount=D(amount) + i,  # different amounts: not a double-click
            payment_method="CASH", payment_date=date(2026, 3, 10),
        )

    def test_receipt_numbers_never_collide(self):
        students = [make_student(self.ctx) for _ in range(10)]
        for s in students:
            make_charge(self.ctx, s, 500_000)

        results, errors = run_in_parallel(
            lambda i: record_payment(self._payment(students[i], 1000, i)).receipt_no, 10
        )
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 10)
        expected = {f"RCPT-{self.ctx['school'].pk:03d}-2026-{n:06d}" for n in range(1, 11)}
        self.assertEqual(set(results), expected)

    def test_two_cashiers_same_student_never_overpay_a_charge(self):
        student = make_student(self.ctx)
        charge = make_charge(self.ctx, student, 100_000)

        # Slow the database down a little, like a busy server on report day,
        # so that without locks the payments really would overlap.
        original_create = fee_services.StudentFeeAllocation.objects.create

        def slow_create(**kwargs):
            time.sleep(0.05)
            return original_create(**kwargs)

        # 5 cashiers each take ~60,000 for the same 100,000 charge at once.
        with mock.patch.object(fee_services.StudentFeeAllocation.objects, "create", slow_create):
            results, errors = run_in_parallel(
                lambda i: record_payment(self._payment(student, 60_000, i)), 5
            )
        self.assertEqual(errors, [])

        charge.refresh_from_db()
        allocated = sum(a.allocated_amount for a in StudentFeeAllocation.objects.filter(charge=charge))
        # Without locking, several payments each "see" 100,000 outstanding and
        # amount_paid ends up wrong. With locking the books always balance:
        self.assertEqual(charge.amount_paid, D("100000"))
        self.assertEqual(allocated, D("100000"))
        self.assertEqual(charge.status, "PAID")

        total_paid = sum(p.amount for p in StudentFeePayment.objects.filter(student=student))
        total_unapplied = sum(p.unapplied_amount for p in StudentFeePayment.objects.filter(student=student))
        self.assertEqual(total_paid - total_unapplied, D("100000"))


@unittest.skipUnless(connection.vendor == "postgresql", "Row locking needs PostgreSQL")
class ConcurrentSchoolPayTests(TransactionTestCase):
    def test_webhook_and_sync_arriving_together_post_once(self):
        from mainApp.models.schoolpay_models import SchoolPayConfig, SchoolPayTransaction
        from mainApp.schoolpay.service import ingest
        from mainApp.tests.test_schoolpay import school_fees_payment

        ctx = make_school()
        school = ctx["school"]
        school.current_term = ctx["term"]
        school.save()
        student = make_student(ctx)
        student.schoolpay_code = "1006480152"
        student.save()
        make_charge(ctx, student, 300_000)
        config = SchoolPayConfig(school=school, school_code="809", is_enabled=True)
        config.set_api_password("pw")
        config.save()

        sources = ["WEBHOOK", "SYNC"] * 3  # six deliveries of the same receipt at once
        _results, errors = run_in_parallel(
            lambda i: ingest(SchoolPayConfig.objects.get(pk=config.pk), "SCHOOL_FEES",
                             school_fees_payment(), sources[i]),
            len(sources),
        )
        self.assertEqual(errors, [])
        self.assertEqual(SchoolPayTransaction.objects.count(), 1)
        self.assertEqual(StudentFeePayment.objects.count(), 1)

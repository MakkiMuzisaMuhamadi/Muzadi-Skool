from django.db import transaction
from django.utils import timezone
from mainApp.models.fees_models import *
from django.db import transaction

def generate_invoice_no(school_id: int) -> str:
    return f"INV-{school_id}-{timezone.now().strftime('%Y%m%d%H%M%S')}"

def generate_receipt_no(school_id: int) -> str:
    return f"RCT-{school_id}-{timezone.now().strftime('%Y%m%d%H%M%S')}"

@transaction.atomic
def generate_student_invoice(student, academic_year, term):
    qs = FeeStructure.objects.filter(
        school=student.school,
        academic_year=academic_year,
        term=term,
        is_active=True,
    )

    # best match -> fallback matching
    structure = (
        qs.filter(student_class=student.current_class, stream=student.stream, residence=student.residence).first()
        or qs.filter(student_class=student.current_class, residence=student.residence).first()
        or qs.filter(level=student.level, residence=student.residence).first()
        or qs.filter(level=student.level).first()
        or qs.first()
    )
    if not structure:
        raise ValueError("No Fee Structure configured for this scope.")

    invoice = FeeInvoice.objects.create(
        school=student.school,
        student=student,
        academic_year=academic_year,
        term=term,
        invoice_no=generate_invoice_no(student.school_id),
        issued_on=timezone.now().date(),
        status="ISSUED",
    )

    total = 0
    for line in structure.lines.select_related("item"):
        FeeInvoiceLine.objects.create(invoice=invoice, item=line.item, amount=line.amount)
        total += line.amount

    invoice.total_amount = total
    invoice.save(update_fields=["total_amount"])
    return invoice

@transaction.atomic
def record_payment(student, amount, method="CASH", invoice=None, reference="", received_by=None):
    payment = FeePayment.objects.create(
        school=student.school,
        student=student,
        invoice=invoice,
        amount=amount,
        method=method,
        reference=reference,
        received_by=received_by,
        receipt_no=generate_receipt_no(student.school_id),
    )

    if invoice:
        bal = invoice.balance()
        if bal <= 0:
            invoice.status = "PAID"
        elif bal < (invoice.total_amount - invoice.total_discount):
            invoice.status = "PARTIAL"
        invoice.save(update_fields=["status"])

    return payment

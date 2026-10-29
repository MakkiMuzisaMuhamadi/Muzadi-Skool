from datetime import timedelta
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, DecimalField, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from weasyprint import HTML

from mainApp.decorators import role_required
from mainApp.fee_services import (
    FeePaymentError,
    approve_adjustment,
    clearance_queryset,
    create_bulk_manual_charges,
    create_manual_student_charge,
    defaulter_rows,
    generate_charges_from_structure,
    post_opening_balance,
    queue_defaulter_reminders,
    record_payment,
    reject_adjustment,
    request_adjustment,
    resolve_term_for_date,
    reverse_adjustment,
    reverse_payment,
    student_fee_summary,
    waive_outstanding_for_term,
)
from mainApp.forms.fees_forms import (
    AdjustmentDecisionForm,
    AdjustmentReversalForm,
    BulkManualChargeForm,
    DefaulterReminderForm,
    FeeCategoryForm,
    FeeCollectionSettingForm,
    FeeStructureForm,
    FeeStructureItemFormSet,
    PaymentReversalForm,
    StudentFeeAdjustmentForm,
    StudentFeeOpeningBalanceForm,
    StudentFeePaymentForm,
    StudentManualChargeForm,
    WaiveTermForm,
)
from mainApp.models.calendar import AcademicYear, Term
from mainApp.models.fees_models import (
    FeeCategory,
    FeeCollectionSetting,
    FeeStructure,
    StudentFeeAdjustment,
    StudentFeeCharge,
    StudentFeeCredit,
    StudentFeeOpeningBalance,
    StudentFeePayment,
)
from mainApp.models.models import Class, Stream
from mainApp.models.student import Student

# Bursar: collects money, adds charges, requests discounts, runs reports.
# Admin: also sets up fees, approves discounts and reverses receipts.
FEE_STAFF = ["SUPER_ADMIN", "ADMIN", "BURSAR"]
FEE_ADMIN = ["SUPER_ADMIN", "ADMIN"]


def _school_from_user(request):
    return getattr(request.user.profile, "school", None)


def _is_admin(request):
    return getattr(request.user.profile, "role", "") in FEE_ADMIN


def _errors(exc):
    return " ".join(getattr(exc, "messages", None) or [str(exc)])


def _is_ajax(request):
    return request.headers.get("x-requested-with") == "XMLHttpRequest"


def _category_qs(school):
    return FeeCategory.objects.filter(school=school, is_active=True).order_by("priority", "name")


def _validate_unique_fee_categories(formset):
    seen = set()
    ok = True
    for form in formset.forms:
        if not hasattr(form, "cleaned_data") or form.cleaned_data.get("DELETE"):
            continue
        cat = form.cleaned_data.get("fee_category")
        if not cat:
            continue
        if cat.id in seen:
            form.add_error("fee_category", "This fee category has already been selected.")
            ok = False
        seen.add(cat.id)
    return ok


def _filter_lists(school, academic_year=None):
    terms = (Term.objects.filter(academic_year_id=academic_year).order_by("order", "name")
             if academic_year else Term.objects.none())
    return {
        "academic_years": AcademicYear.objects.all().order_by("-start_date"),
        "terms": terms,
        "classes": Class.objects.filter(school=school).order_by("level__order", "name"),
    }


# ============================================================ categories ==

@login_required
@role_required(FEE_ADMIN)
def fee_category_list(request):
    school = _school_from_user(request)
    categories = FeeCategory.objects.filter(school=school).order_by("priority", "name")
    return render(request, "dashboard/fees/fee_category_list.html", {"categories": categories})


@login_required
@role_required(FEE_ADMIN)
def fee_category_create(request):
    school = _school_from_user(request)
    form = FeeCategoryForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        obj = form.save(commit=False)
        obj.school = school
        obj.created_by = request.user
        obj.save()
        messages.success(request, "Fee category created.")
        return redirect("fee_category_list")
    return render(request, "dashboard/fees/fee_category_form.html", {"form": form, "title": "Add Fee Category"})


@login_required
@role_required(FEE_ADMIN)
def fee_category_edit(request, pk):
    school = _school_from_user(request)
    obj = get_object_or_404(FeeCategory, pk=pk, school=school)
    form = FeeCategoryForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Fee category updated.")
        return redirect("fee_category_list")
    return render(request, "dashboard/fees/fee_category_form.html", {"form": form, "title": "Edit Fee Category"})


# ============================================================ structures ==

@login_required
@role_required(FEE_ADMIN)
def fee_structure_list(request):
    school = _school_from_user(request)
    structures = FeeStructure.objects.filter(school=school).select_related(
        "academic_year", "term", "class_ref", "stream"
    ).order_by("-academic_year__start_date", "term__order", "class_ref__name")

    academic_year = request.GET.get("academic_year")
    term = request.GET.get("term")
    class_ref = request.GET.get("class_ref")
    if academic_year:
        structures = structures.filter(academic_year_id=academic_year)
    if term:
        structures = structures.filter(term_id=term)
    if class_ref:
        structures = structures.filter(class_ref_id=class_ref)

    return render(request, "dashboard/fees/fee_structure_list.html", {
        "structures": structures, **_filter_lists(school, academic_year)})


def _limit_formset_categories(formset, school):
    qs = _category_qs(school)
    for row in formset.forms:
        row.fields["fee_category"].queryset = qs
    formset.empty_form.fields["fee_category"].queryset = qs


@login_required
@role_required(FEE_ADMIN)
def fee_structure_create(request):
    school = _school_from_user(request)
    if request.method == "POST":
        form = FeeStructureForm(request.POST, school=school)
        formset = FeeStructureItemFormSet(request.POST)
        _limit_formset_categories(formset, school)
        if form.is_valid():
            structure = form.save(commit=False)
            structure.school = school
            structure.created_by = request.user
            formset = FeeStructureItemFormSet(request.POST, instance=structure)
            _limit_formset_categories(formset, school)
            if formset.is_valid() and _validate_unique_fee_categories(formset):
                structure.save()
                formset.instance = structure
                formset.save()
                messages.success(request, "Fee structure created. Click 'Generate charges' to bill the students.")
                return redirect("fee_structure_detail", pk=structure.pk)
    else:
        form = FeeStructureForm(school=school)
        formset = FeeStructureItemFormSet()
        _limit_formset_categories(formset, school)

    return render(request, "dashboard/fees/fee_structure_form.html", {
        "form": form, "formset": formset, "title": "Create Fee Structure"})


@login_required
@role_required(FEE_ADMIN)
def fee_structure_edit(request, pk):
    school = _school_from_user(request)
    structure = get_object_or_404(FeeStructure, pk=pk, school=school)

    if request.method == "POST":
        form = FeeStructureForm(request.POST, instance=structure, school=school)
        formset = FeeStructureItemFormSet(request.POST, instance=structure)
        _limit_formset_categories(formset, school)
        if form.is_valid() and formset.is_valid() and _validate_unique_fee_categories(formset):
            form.save()
            formset.save()
            if structure.charges_generated:
                messages.warning(request, "Saved. Students already billed keep the old amounts until you click "
                                          "'Apply changes to billed students'.")
            else:
                messages.success(request, "Fee structure updated.")
            return redirect("fee_structure_detail", pk=structure.pk)
    else:
        form = FeeStructureForm(instance=structure, school=school)
        formset = FeeStructureItemFormSet(instance=structure)
        _limit_formset_categories(formset, school)

    return render(request, "dashboard/fees/fee_structure_form.html", {
        "form": form, "formset": formset, "title": "Edit Fee Structure", "structure": structure})


@login_required
@role_required(FEE_ADMIN)
def fee_structure_detail(request, pk):
    school = _school_from_user(request)
    structure = get_object_or_404(
        FeeStructure.objects.select_related("academic_year", "term", "class_ref", "stream"), pk=pk, school=school)
    stats = structure.generated_charges.aggregate(
        students=Count("student", distinct=True),
        billed=Coalesce(Sum("amount"), Value(0), output_field=DecimalField()),
        paid=Coalesce(Sum("amount_paid"), Value(0), output_field=DecimalField()),
    )
    return render(request, "dashboard/fees/fee_structure_detail.html", {
        "structure": structure,
        "items": structure.items.select_related("fee_category").all(),
        "stats": stats,
    })


@login_required
@role_required(FEE_ADMIN)
@require_POST
def fee_structure_generate_charges(request, pk):
    school = _school_from_user(request)
    structure = get_object_or_404(FeeStructure, pk=pk, school=school)
    apply_changes = request.POST.get("mode") == "apply_changes"
    try:
        r = generate_charges_from_structure(structure=structure, user=request.user, replace_existing=apply_changes)
    except FeePaymentError as exc:
        messages.error(request, _errors(exc))
        return redirect("fee_structure_detail", pk=structure.pk)

    msg = f"{r['students']} student(s) matched. New charges: {r['created']}."
    if apply_changes:
        msg += f" Updated: {r['updated']}. Removed items waived: {r['removed']}."
    messages.success(request, msg)
    if r["skipped_overlap"]:
        messages.warning(request, f"{r['skipped_overlap']} charge(s) were skipped because those students are "
                                  f"already billed for the same fee this term by another structure.")
    return redirect("fee_structure_detail", pk=structure.pk)


# ======================================================= opening balances ==

@login_required
@role_required(FEE_STAFF)
def fee_opening_balance_list(request):
    school = _school_from_user(request)
    rows = StudentFeeOpeningBalance.objects.filter(school=school).select_related(
        "student", "academic_year", "term", "fee_category").order_by("-created_at")
    return render(request, "dashboard/fees/fee_opening_balance_list.html", {"rows": rows})


@login_required
@role_required(FEE_STAFF)
def fee_opening_balance_create(request):
    school = _school_from_user(request)
    initial = {}
    term = resolve_term_for_date(school) if school else None
    if term:
        initial = {"academic_year": term.academic_year_id, "term": term.pk}
    form = StudentFeeOpeningBalanceForm(request.POST or None, school=school, initial=initial)
    if not form.is_bound and term:
        form.fields["term"].queryset = Term.objects.filter(academic_year_id=term.academic_year_id)

    if request.method == "POST" and form.is_valid():
        ob = form.save(commit=False)
        ob.school = school
        ob.created_by = request.user
        ob.save()
        try:
            post_opening_balance(ob, user=request.user)
        except FeePaymentError as exc:
            messages.error(request, _errors(exc))
            return redirect("fee_opening_balance_list")
        messages.success(request, "Old fee balance added to the student's account.")
        return redirect("fee_opening_balance_list")

    return render(request, "dashboard/fees/fee_opening_balance_form.html", {"form": form, "title": "Add Old Fee Balance"})


# ============================================================ collection ==

def _summary_context(student):
    s = student_fee_summary(student)
    return {
        "total_billed": s["billed"], "total_paid": s["paid"],
        "owing": s["owing"], "credit_total": s["credit"], "balance": s["balance"],
    }


@login_required
@role_required(FEE_STAFF)
def fees_collect(request):
    school = _school_from_user(request)
    student_id = request.GET.get("student_id") or request.POST.get("student")
    selected_student = Student.objects.filter(school=school, pk=student_id).first() if student_id else None

    if request.method == "POST":
        post_data = request.POST.copy()
        if selected_student and not post_data.get("student"):
            post_data["student"] = str(selected_student.id)
        form = StudentFeePaymentForm(post_data, school=school)

        if form.is_valid():
            payment = form.save(commit=False)
            payment.school = school
            try:
                record_payment(payment, user=request.user,
                               allow_duplicate=request.POST.get("confirm_duplicate") == "1")
            except FeePaymentError as exc:
                if _is_ajax(request):
                    return JsonResponse({"status": "error", "errors": {"__all__": exc.messages}}, status=400)
                form.add_error(None, _errors(exc))
            else:
                receipt_url = reverse("fee_payment_receipt", kwargs={"pk": payment.pk})
                msg = f"Payment received. Receipt No: {payment.receipt_no}"
                if payment.unapplied_amount > 0:
                    msg += f" (UGX {payment.unapplied_amount:,.0f} kept as credit for future fees)"
                if _is_ajax(request):
                    return JsonResponse({"status": "success", "message": msg, "receipt_url": receipt_url,
                                         "reset_url": reverse("fees_collect")})
                messages.success(request, msg)
                return redirect("fee_payment_receipt", pk=payment.pk)

        if _is_ajax(request):
            return JsonResponse({"status": "error",
                                 "errors": {f: [str(e) for e in errs] for f, errs in form.errors.items()}},
                                status=400)
    else:
        initial = {"student": selected_student.id} if selected_student else {}
        form = StudentFeePaymentForm(initial=initial, school=school)

    student_summary = None
    if selected_student:
        charges = StudentFeeCharge.objects.filter(student=selected_student).select_related(
            "academic_year", "term", "fee_category").order_by("-term__start_date", "fee_category__priority", "id")
        student_summary = {
            "charges": charges,
            "payments": StudentFeePayment.objects.filter(student=selected_student).order_by("-payment_date", "-id")[:10],
            "credits": StudentFeeCredit.objects.filter(student=selected_student, balance__gt=0, is_active=True),
            **_summary_context(selected_student),
        }

    return render(request, "dashboard/fees/fees_collect.html", {
        "form": form, "selected_student": selected_student, "student_summary": student_summary})


@login_required
@role_required(FEE_STAFF)
def student_fee_statement(request, student_id):
    school = _school_from_user(request)
    student = get_object_or_404(Student, pk=student_id, school=school)

    charges = StudentFeeCharge.objects.filter(student=student).select_related(
        "academic_year", "term", "fee_category").order_by("term__start_date", "fee_category__priority", "id")
    payments = StudentFeePayment.objects.filter(student=student).select_related(
        "academic_year", "term", "received_by", "reversed_by").order_by("payment_date", "id")
    credits = StudentFeeCredit.objects.filter(student=student).order_by("created_at")

    return render(request, "dashboard/fees/student_fee_statement.html", {
        "student": student, "charges": charges, "payments": payments, "credits": credits,
        "can_reverse": _is_admin(request),
        "waive_form": WaiveTermForm(student=student) if _is_admin(request) else None,
        "pending_adjustments": StudentFeeAdjustment.objects.filter(charge__student=student, status="PENDING").count(),
        **_summary_context(student),
    })


@login_required
@role_required(FEE_ADMIN)
@require_POST
def fee_student_waive_term(request, student_id):
    school = _school_from_user(request)
    student = get_object_or_404(Student, pk=student_id, school=school)
    form = WaiveTermForm(request.POST, student=student)
    if form.is_valid():
        try:
            r = waive_outstanding_for_term(student, form.cleaned_data["term"], request.user,
                                           form.cleaned_data["reason"])
            messages.success(request, f"Waived UGX {r['total']:,.0f} on {r['count']} unpaid charge(s).")
        except FeePaymentError as exc:
            messages.error(request, _errors(exc))
    else:
        messages.error(request, "Choose a term and give a reason.")
    return redirect("student_fee_statement", student_id=student.pk)


# ============================================================ adjustments ==

@login_required
@role_required(FEE_STAFF)
def student_fee_charge_detail(request, pk):
    school = _school_from_user(request)
    charge = get_object_or_404(
        StudentFeeCharge.objects.select_related("student", "fee_category", "academic_year", "term"),
        pk=pk, school=school)

    form = StudentFeeAdjustmentForm(request.POST or None, charge=charge)
    if request.method == "POST" and form.is_valid():
        try:
            adj = request_adjustment(charge, form.cleaned_data["adjustment_type"], form.cleaned_data["amount"],
                                     form.cleaned_data["reason"], request.user)
        except FeePaymentError as exc:
            form.add_error(None, _errors(exc))
        else:
            if adj.status == "PENDING":
                messages.info(request, "Sent to an admin for approval. It will apply once approved.")
            else:
                messages.success(request, "Adjustment applied.")
            return redirect("student_fee_charge_detail", pk=charge.pk)

    return render(request, "dashboard/fees/student_fee_charge_detail.html", {
        "charge": charge,
        "allocations": charge.allocations.select_related("payment").all(),
        "credit_applications": charge.credit_applications.select_related("credit").all(),
        "adjustments": charge.adjustments.select_related("approved_by", "created_by").order_by("-created_at"),
        "adjustment_form": form,
        "reverse_form": AdjustmentReversalForm(),
        "can_admin": _is_admin(request),
    })


@login_required
@role_required(FEE_ADMIN)
def fee_adjustment_approvals(request):
    school = _school_from_user(request)
    rows = StudentFeeAdjustment.objects.filter(charge__school=school, status="PENDING").select_related(
        "charge__student", "charge__fee_category", "charge__term", "created_by").order_by("created_at")
    return render(request, "dashboard/fees/fee_adjustment_approvals.html", {
        "rows": rows, "decision_form": AdjustmentDecisionForm()})


@login_required
@role_required(FEE_ADMIN)
@require_POST
def fee_adjustment_decide(request, pk):
    school = _school_from_user(request)
    adj = get_object_or_404(StudentFeeAdjustment, pk=pk, charge__school=school)
    note = request.POST.get("note", "")
    try:
        if request.POST.get("decision") == "approve":
            approve_adjustment(adj, request.user, note)
            messages.success(request, "Approved and applied.")
        else:
            reject_adjustment(adj, request.user, note)
            messages.info(request, "Rejected. Nothing was changed on the student's account.")
    except FeePaymentError as exc:
        messages.error(request, _errors(exc))
    return redirect(request.POST.get("next") or "fee_adjustment_approvals")


@login_required
@role_required(FEE_ADMIN)
@require_POST
def fee_adjustment_reverse(request, pk):
    school = _school_from_user(request)
    adj = get_object_or_404(StudentFeeAdjustment, pk=pk, charge__school=school)
    form = AdjustmentReversalForm(request.POST)
    if form.is_valid():
        try:
            reverse_adjustment(adj, request.user, form.cleaned_data["reason"])
            messages.success(request, "Adjustment reversed.")
        except FeePaymentError as exc:
            messages.error(request, _errors(exc))
    else:
        messages.error(request, "Give a reason for reversing the adjustment.")
    return redirect("student_fee_charge_detail", pk=adj.charge_id)


# ========================================================= manual charges ==

@login_required
@role_required(FEE_STAFF)
def student_manual_charge_create(request):
    school = _school_from_user(request)
    form = StudentManualChargeForm(request.POST or None, school=school)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        charge = create_manual_student_charge(
            school=school, student=cd["student"], academic_year=cd["academic_year"], term=cd["term"],
            fee_category=cd["fee_category"], amount=cd["amount"], description=cd["description"],
            notes=cd.get("notes", ""), user=request.user)
        messages.success(request, "Charge added to the student's account.")
        return redirect("student_fee_statement", student_id=charge.student_id)
    return render(request, "dashboard/fees/student_manual_charge_form.html", {"form": form, "title": "Add Student Charge"})


@login_required
@role_required(FEE_STAFF)
def bulk_manual_charge_create(request):
    school = _school_from_user(request)
    form = BulkManualChargeForm(request.POST or None, school=school)
    if request.method == "POST" and form.is_valid():
        cd = form.cleaned_data
        r = create_bulk_manual_charges(
            school=school, academic_year=cd["academic_year"], term=cd["term"], class_ref=cd["class_ref"],
            stream=cd.get("stream"), fee_category=cd["fee_category"], amount=cd["amount"],
            description=cd["description"], notes=cd.get("notes", ""),
            skip_if_same_description_exists=cd.get("skip_if_same_description_exists", True), user=request.user)
        messages.success(request, f"Charged {r['created_count']} student(s). Skipped (already charged): "
                                  f"{r['skipped_count']}.")
        return redirect("manual_charge_list")
    return render(request, "dashboard/fees/bulk_manual_charge_form.html", {"form": form, "title": "Bulk Class/Stream Charge"})


@login_required
@role_required(FEE_STAFF)
def manual_charge_list(request):
    school = _school_from_user(request)
    rows = StudentFeeCharge.objects.filter(school=school, source_type="MANUAL").select_related(
        "student", "academic_year", "term", "fee_category", "class_ref", "stream").order_by("-created_at", "-id")

    academic_year = request.GET.get("academic_year")
    q = request.GET.get("q", "").strip()
    for param, field in (("academic_year", "academic_year_id"), ("term", "term_id"), ("class_ref", "class_ref_id"),
                         ("stream", "stream_id"), ("fee_category", "fee_category_id")):
        val = request.GET.get(param)
        if val:
            rows = rows.filter(**{field: val})
    if q:
        rows = rows.filter(Q(student__admission_number__icontains=q) | Q(student__name__icontains=q)
                           | Q(student__first_name__icontains=q) | Q(student__last_name__icontains=q)
                           | Q(description__icontains=q))

    return render(request, "dashboard/fees/manual_charge_list.html", {
        "rows": rows, "fee_categories": _category_qs(school), "q": q, **_filter_lists(school, academic_year)})


# =============================================================== receipts ==

def _render_pdf_from_template(request, template_name, context, filename):
    html_string = render_to_string(template_name, context, request=request)
    pdf_file = HTML(string=html_string, base_url=request.build_absolute_uri("/")).write_pdf()
    response = HttpResponse(pdf_file, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    return response


@login_required
@role_required(FEE_STAFF)
def fee_payment_receipt(request, pk):
    school = _school_from_user(request)
    payment = get_object_or_404(
        StudentFeePayment.objects.select_related("student", "academic_year", "term", "received_by", "school"),
        pk=pk, school=school)
    context = {
        "school": school, "payment": payment, "student": payment.student,
        "allocations": payment.allocations.select_related("charge__fee_category", "charge__term").all(),
        "printed_at": timezone.now(), "title": "Fees Payment Receipt",
        **_summary_context(payment.student),
    }
    return _render_pdf_from_template(request, "dashboard/fees/pdf/payment_receipt_pdf.html", context,
                                     f"receipt-{payment.receipt_no}.pdf")


@login_required
@role_required(FEE_ADMIN)
def fee_payment_reverse(request, pk):
    school = _school_from_user(request)
    payment = get_object_or_404(StudentFeePayment.objects.select_related("student", "received_by"),
                                pk=pk, school=school)
    form = PaymentReversalForm(request.POST or None)

    if request.method == "POST" and form.is_valid():
        reason = form.cleaned_data["reason"]
        try:
            reverse_payment(payment, user=request.user, reason=reason)
        except FeePaymentError as exc:
            messages.error(request, _errors(exc))
            return redirect("student_fee_statement", student_id=payment.student_id)

        # A reversed SchoolPay receipt usually means "posted to the wrong student":
        # put it back in the SchoolPay inbox so it can be assigned correctly.
        try:
            from mainApp.models.schoolpay_models import SchoolPayTransaction as Txn
            for txn in Txn.objects.filter(fee_payment=payment):
                txn.fee_payment = None
                txn.student = None
                txn.status = Txn.STATUS_UNMATCHED
                txn.status_message = (f"Receipt {payment.receipt_no} was reversed by "
                                      f"{request.user.get_username()}: {reason}. Assign it to the right student, "
                                      f"or dismiss it.")[:255]
                txn.save()
        except ImportError:
            pass

        messages.success(request, f"Receipt {payment.receipt_no} reversed. UGX {payment.amount:,.0f} is back on "
                                  f"{payment.student}'s balance.")
        return redirect("student_fee_statement", student_id=payment.student_id)

    return render(request, "dashboard/fees/fee_payment_reverse.html", {
        "payment": payment, "form": form,
        "allocations": payment.allocations.select_related("charge__fee_category", "charge__term"),
        "credits": payment.created_credits.all(),
    })


# ================================================================ reports ==

def _defaulter_filters(request):
    return {
        "academic_year": request.GET.get("academic_year") or None,
        "term": request.GET.get("term") or None,
        "class_id": request.GET.get("class_ref") or None,
        "stream_id": request.GET.get("stream") or None,
    }


@login_required
@role_required(FEE_STAFF)
def fee_defaulters_report(request):
    school = _school_from_user(request)
    f = _defaulter_filters(request)
    rows = defaulter_rows(school, **f)
    totals = {
        "count": len(rows),
        "balance": sum((r["balance"] for r in rows), Decimal("0")),
        "no_phone": sum(1 for r in rows if not r["parent_phone"]),
    }
    return render(request, "dashboard/fees/fee_defaulters_report.html", {
        "rows": rows, "totals": totals, "reminder_form": DefaulterReminderForm(),
        "streams": Stream.objects.filter(class_ref_id=f["class_id"]) if f["class_id"] else Stream.objects.none(),
        **_filter_lists(school, f["academic_year"]),
    })


@login_required
@role_required(FEE_STAFF)
def fee_defaulters_report_pdf(request):
    school = _school_from_user(request)
    rows = defaulter_rows(school, **_defaulter_filters(request))
    return _render_pdf_from_template(request, "dashboard/fees/pdf/defaulters_report_pdf.html", {
        "school": school, "rows": rows, "printed_at": timezone.now(), "title": "Defaulters Report",
    }, f"defaulters-report-{timezone.now():%Y%m%d%H%M%S}.pdf")


@login_required
@role_required(FEE_STAFF)
@require_POST
def fee_defaulters_sms(request):
    school = _school_from_user(request)
    form = DefaulterReminderForm(request.POST)
    if not form.is_valid():
        messages.error(request, "Write the reminder message first.")
        return redirect("fee_defaulters_report")
    f = {
        "academic_year": request.POST.get("academic_year") or None,
        "term": request.POST.get("term") or None,
        "class_id": request.POST.get("class_ref") or None,
        "stream_id": request.POST.get("stream") or None,
    }
    rows = defaulter_rows(school, **f)
    selected = set(request.POST.getlist("student_ids"))
    if selected:
        rows = [r for r in rows if str(r["student_id"]) in selected]
    if not rows:
        messages.warning(request, "No students to remind.")
        return redirect("fee_defaulters_report")
    campaign, queued = queue_defaulter_reminders(school, rows, request.user, form.cleaned_data["message"])
    if not queued:
        messages.warning(request, "None of these parents has a valid phone number.")
        return redirect("fee_defaulters_report")
    messages.success(request, f"{queued} reminder SMS queued. They are being sent now.")
    return redirect("sms_campaign_detail", pk=campaign.pk)


@login_required
@role_required(FEE_STAFF)
def fee_clearance_report(request):
    """
    Fast clearance list: totals are calculated in one database query, the page shows
    100 students at a time, and counts come from the database (not by loading every student).
    """
    school = _school_from_user(request)
    term = None
    if request.GET.get("term"):
        term = Term.objects.filter(pk=request.GET["term"]).select_related("academic_year").first()
    term = term or resolve_term_for_date(school)
    class_id = request.GET.get("class_ref") or None
    stream_id = request.GET.get("stream") or None
    show = request.GET.get("show", "all")
    printing = request.GET.get("print") == "1"

    context = {
        "term": term, "show": show, "class_id": class_id, "stream_id": stream_id, "printing": printing,
        "all_terms": Term.objects.select_related("academic_year").order_by("-start_date")[:30],
        "classes": Class.objects.filter(school=school).order_by("level__order", "name"),
        "streams": Stream.objects.filter(class_ref_id=class_id).order_by("name") if class_id else Stream.objects.none(),
        "percent_needed": FeeCollectionSetting.for_school(school).clearance_percentage,
        "page_obj": None, "total": 0, "cleared_count": 0,
    }
    if term is None:
        return render(request, "dashboard/fees/fee_clearance_report.html", context)

    qs, pct = clearance_queryset(school, term, class_id=class_id, stream_id=stream_id)
    context["total"] = qs.count()
    context["cleared_count"] = qs.filter(cleared=True).count()
    if show == "cleared":
        qs = qs.filter(cleared=True)
    elif show == "not_cleared":
        qs = qs.filter(cleared=False)
    qs = qs.order_by("current_class__name", "stream__name", "admission_number")

    # Printing a whole class at once is fine; the whole school is split into pages.
    per_page = 1000 if (printing and class_id) else 100
    context["page_obj"] = Paginator(qs, per_page).get_page(request.GET.get("page"))
    params = request.GET.copy()
    params.pop("page", None)
    context["querystring"] = params.urlencode()
    return render(request, "dashboard/fees/fee_clearance_report.html", context)


@login_required
@role_required(FEE_ADMIN)
def fee_settings(request):
    school = _school_from_user(request)
    setting = FeeCollectionSetting.for_school(school)
    form = FeeCollectionSettingForm(request.POST or None, instance=setting)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Fee settings saved.")
        return redirect("fee_settings")
    return render(request, "dashboard/fees/fee_settings.html", {"form": form, "title": "Fee Settings"})


def _reversed_count(school, start, end):
    qs = StudentFeePayment.objects.filter(school=school, is_reversed=True)
    if start:
        qs = qs.filter(reversed_at__date__gte=start)
    if end:
        qs = qs.filter(reversed_at__date__lte=end)
    return qs.count()


def _financial_queryset(request, school):
    today = timezone.localdate()
    period = request.GET.get("period", "daily")
    date_from = request.GET.get("date_from")
    date_to = request.GET.get("date_to")

    payments = StudentFeePayment.objects.filter(school=school, is_reversed=False).select_related(
        "student", "academic_year", "term", "received_by").order_by("-payment_date", "-id")

    if period == "daily":
        start_date = end_date = today
        payments = payments.filter(payment_date=today)
    elif period == "weekly":
        start_date = today - timedelta(days=today.weekday())
        end_date = start_date + timedelta(days=6)
        payments = payments.filter(payment_date__range=[start_date, end_date])
    elif period == "monthly":
        start_date = today.replace(day=1)
        next_month = (start_date.replace(year=start_date.year + 1, month=1) if start_date.month == 12
                      else start_date.replace(month=start_date.month + 1))
        end_date = next_month - timedelta(days=1)
        payments = payments.filter(payment_date__range=[start_date, end_date])
    else:
        start_date, end_date = date_from or "", date_to or ""
        if date_from:
            payments = payments.filter(payment_date__gte=date_from)
        if date_to:
            payments = payments.filter(payment_date__lte=date_to)

    if request.GET.get("academic_year"):
        payments = payments.filter(academic_year_id=request.GET["academic_year"])
    if request.GET.get("term"):
        payments = payments.filter(term_id=request.GET["term"])
    if request.GET.get("payment_method"):
        payments = payments.filter(payment_method=request.GET["payment_method"])

    money = Coalesce(Sum("amount"), Value(0), output_field=DecimalField())
    summary = payments.aggregate(
        total_collected=money,
        total_unapplied=Coalesce(Sum("unapplied_amount"), Value(0), output_field=DecimalField()))
    return {
        "school": school,
        "payments": payments,
        "summary": summary,
        "total_allocated": summary["total_collected"] - summary["total_unapplied"],
        "by_method": payments.values("payment_method").annotate(total=money, count=Count("id")).order_by("payment_method"),
        # Daily cash-up: what each cashier must hand over, by method.
        "by_cashier": payments.values("received_by__username", "received_by__first_name",
                                      "received_by__last_name", "payment_method")
                              .annotate(total=money, count=Count("id"))
                              .order_by("received_by__username", "payment_method"),
        "reversed_in_period": _reversed_count(school, start_date, end_date),
        "period": period, "date_from": start_date, "date_to": end_date,
        "printed_at": timezone.now(), "title": "Financial Report",
    }


@login_required
@role_required(FEE_STAFF)
def fee_financial_report(request):
    school = _school_from_user(request)
    context = _financial_queryset(request, school)
    academic_year = request.GET.get("academic_year", "")
    context.update({
        "academic_years": AcademicYear.objects.all().order_by("-start_date"),
        "terms": (Term.objects.filter(academic_year_id=academic_year).order_by("order", "name")
                  if academic_year else Term.objects.none()),
    })
    return render(request, "dashboard/fees/fee_financial_report.html", context)


@login_required
@role_required(FEE_STAFF)
def fee_financial_report_pdf(request):
    school = _school_from_user(request)
    return _render_pdf_from_template(request, "dashboard/fees/pdf/financial_report_pdf.html",
                                     _financial_queryset(request, school),
                                     f"financial-report-{timezone.now():%Y%m%d%H%M%S}.pdf")


@login_required
@role_required(FEE_STAFF)
def fee_collection_report(request):
    """Old URL: the financial report now covers this (custom date range)."""
    params = request.GET.copy()
    params["period"] = "custom"
    return redirect(f"{reverse('fee_financial_report')}?{params.urlencode()}")
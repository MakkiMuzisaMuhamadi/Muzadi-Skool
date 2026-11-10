"""
Platform screens for Muzadi (SUPER_ADMIN) - admin.<your domain>/platform/
and the small school-side pages that go with them (setup checklist, SMS
credit, subscription status, What's new).
"""
from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST

from mainApp import messaging_wallet, platform_services, subscriptions, tenancy
from mainApp.decorators import role_required
from mainApp.forms.platform_forms import (
    MessagingAccountForm, NewSchoolForm, ReleaseNoteForm, SchoolAddressForm, SmsTopUpForm,
    SubscriptionForm, SubscriptionPaymentForm,
)
from mainApp.models.models import School
from mainApp.models.platform_models import (
    ReleaseNote, SchoolMessagingAccount, SchoolSubscription, SmsCreditEntry, SubscriptionPayment,
)


def platform_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not tenancy.is_platform_user(request.user):
            raise PermissionDenied("Muzadi staff only.")
        with tenancy.guard_disabled():
            return view(request, *args, **kwargs)
    return wrapper


def _school_or_404(pk):
    return get_object_or_404(School._base_manager, pk=pk)


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------
@platform_required
def platform_dashboard(request):
    from mainApp.dashboard_stats import platform_summary
    rows, alerts, totals = platform_summary()
    return render(request, "platform/dashboard.html", {
        "title": "Muzadi platform", "rows": rows, "alerts": alerts, "totals": totals,
        "base_domain": tenancy.base_domain(),
    })


# ---------------------------------------------------------------------------
# New school
# ---------------------------------------------------------------------------
@platform_required
def platform_school_new(request):
    form = NewSchoolForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        school, user, password = platform_services.create_school(
            name=d["name"], short_name=d["short_name"], subdomain=d["subdomain"], levels=d["levels"],
            admin_username=d["admin_username"], admin_first_name=d["admin_first_name"],
            admin_last_name=d["admin_last_name"], admin_email=d["admin_email"], admin_phone=d["admin_phone"],
            plan=d["plan"], trial_days=d["trial_days"], free_sms_units=d["free_sms_units"],
            contact_phone=d["contact_phone"], contact_email=d["contact_email"], address=d["address"],
            created_by=request.user,
        )
        return render(request, "platform/school_created.html", {
            "title": "School created", "school": school, "admin_user": user, "password": password,
            "url": tenancy.school_url(school, "/"),
        })
    return render(request, "platform/school_new.html", {
        "title": "New school", "form": form, "base_domain": tenancy.base_domain(),
    })


# ---------------------------------------------------------------------------
# One school
# ---------------------------------------------------------------------------
@platform_required
def platform_school_detail(request, pk):
    school = _school_or_404(pk)
    sub, _ = SchoolSubscription.objects.get_or_create(school=school)
    acc = messaging_wallet.account_for(school)
    action = request.POST.get("action") if request.method == "POST" else None

    address_form = SchoolAddressForm(request.POST if action == "address" else None,
                                     request.FILES if action == "address" else None, instance=school)
    sub_form = SubscriptionForm(request.POST if action == "subscription" else None, instance=sub)
    msg_form = MessagingAccountForm(request.POST if action == "messaging" else None, instance=acc)
    topup_form = SmsTopUpForm(request.POST if action == "topup" else None)
    pay_form = SubscriptionPaymentForm(request.POST if action == "payment" else None)

    if action == "address" and address_form.is_valid():
        address_form.save()
        messages.success(request, "School details saved.")
        return redirect("platform_school_detail", pk=pk)
    if action == "subscription" and sub_form.is_valid():
        obj = sub_form.save(commit=False)
        if obj.plan in ("BASIC", "STANDARD", "PREMIUM") and "plan" in sub_form.changed_data \
                and "enabled_modules" not in sub_form.changed_data:
            obj.apply_plan(obj.plan)
        else:
            obj.enabled_modules = sub_form.cleaned_data["enabled_modules"]
        obj.save()
        messages.success(request, f"Subscription saved: {obj.get_plan_display()}, now {obj.state()}.")
        return redirect("platform_school_detail", pk=pk)
    if action == "messaging" and msg_form.is_valid():
        msg_form.save()
        messages.success(request, "Messaging settings saved.")
        return redirect("platform_school_detail", pk=pk)
    if action == "topup" and topup_form.is_valid():
        d = topup_form.cleaned_data
        entry = messaging_wallet.top_up(school, d["units"], amount_ugx=d["amount_ugx"] or 0,
                                        note=d["note"], user=request.user)
        messages.success(request, f"Added {d['units']} SMS units. New balance: {entry.balance_after}.")
        return redirect("platform_school_detail", pk=pk)
    if action == "payment" and pay_form.is_valid():
        d = pay_form.cleaned_data
        platform_services.record_subscription_payment(
            school, amount_ugx=d["amount_ugx"], covers_until=d["covers_until"],
            reference=d["reference"], note=d["note"], user=request.user)
        messages.success(request, f"Payment recorded. Paid up to {d['covers_until']:%d %b %Y}.")
        return redirect("platform_school_detail", pk=pk)

    from mainApp.models.student import Student
    return render(request, "platform/school_detail.html", {
        "title": school.name, "school": school, "subscription": sub, "account": acc,
        "address_form": address_form, "sub_form": sub_form, "msg_form": msg_form,
        "topup_form": topup_form, "pay_form": pay_form,
        "ledger": SmsCreditEntry.objects.filter(school=school)[:25],
        "usage": messaging_wallet.monthly_usage(school),
        "payments": SubscriptionPayment.objects.filter(school=school)[:20],
        "students": Student._base_manager.filter(school=school).count(),
        "url": tenancy.school_url(school, "/") if school.subdomain or school.custom_domain else "",
        "checklist": platform_services.setup_checklist(school),
    })


@platform_required
@require_POST
def platform_school_support(request, pk):
    """Open a school as support: your account is moved to that school, then you sign in there."""
    school = _school_or_404(pk)
    profile = request.user.profile
    profile.school = school
    profile.save(update_fields=["school"])
    messages.info(request, f"Your account is now attached to {school.name}.")
    if tenancy.tenancy_enabled() and (school.subdomain or school.custom_domain):
        return redirect(tenancy.school_url(school, "/login/"))
    return redirect("dashboard")


@platform_required
def platform_sms_usage_csv(request):
    """SMS units used per school per month - for invoicing postpaid schools."""
    import csv

    from django.db.models import Sum
    from django.db.models.functions import TruncMonth

    rows = (SmsCreditEntry.objects
            .filter(kind__in=[SmsCreditEntry.KIND_USAGE, SmsCreditEntry.KIND_REFUND])
            .annotate(month=TruncMonth("created_at"))
            .values("school__name", "school_id", "month").annotate(total=Sum("units"))
            .order_by("-month", "school__name"))
    prices = dict(SchoolMessagingAccount.objects.values_list("school_id", "price_per_unit_ugx"))
    modes = dict(SchoolMessagingAccount.objects.values_list("school_id", "billing_mode"))
    resp = HttpResponse(content_type="text/csv")
    resp["Content-Disposition"] = 'attachment; filename="sms_usage_by_school.csv"'
    w = csv.writer(resp)
    w.writerow(["Month", "School", "Billing", "SMS units", "Price per unit (UGX)", "Amount (UGX)"])
    for r in rows:
        units = -(r["total"] or 0)
        price = prices.get(r["school_id"], 0) or 0
        w.writerow([r["month"].strftime("%Y-%m") if r["month"] else "", r["school__name"],
                    modes.get(r["school_id"], ""), units, price, units * price])
    return resp


# ---------------------------------------------------------------------------
# Release notes
# ---------------------------------------------------------------------------
@platform_required
def platform_release_notes(request):
    return render(request, "platform/release_notes.html", {
        "title": "Release notes", "notes": ReleaseNote.objects.all().prefetch_related("schools"),
    })


@platform_required
def platform_release_note_form(request, pk=None):
    note = get_object_or_404(ReleaseNote, pk=pk) if pk else None
    form = ReleaseNoteForm(request.POST or None, instance=note)
    if request.method == "POST" and form.is_valid():
        with transaction.atomic():
            obj = form.save(commit=False)
            if obj.is_published:
                obj.publish()
            if not obj.created_by_id:
                obj.created_by = request.user
            obj.save()
            form.save_m2m()
        messages.success(request, "Release note saved." + (" It is now visible." if obj.is_published else ""))
        return redirect("platform_release_notes")
    return render(request, "platform/release_note_form.html", {
        "title": "Edit release note" if note else "New release note", "form": form, "note": note,
    })


# ---------------------------------------------------------------------------
# School side
# ---------------------------------------------------------------------------
def _request_school(request):
    return getattr(request, "school", None) or getattr(getattr(request.user, "profile", None), "school", None)


@login_required
def whats_new(request):
    school = _request_school(request)
    notes = list(platform_services.release_notes_for(request.user, school)[:30])
    read_ids = set(request.user.release_note_reads.values_list("note_id", flat=True))
    for n in notes:
        n.is_new = n.pk not in read_ids
        n.paragraphs = [p.strip() for p in n.body.split("\n\n") if p.strip()]
    platform_services.mark_release_notes_read(request.user, notes)
    return render(request, "platform/whats_new.html", {"title": "What's new", "notes": notes})


@login_required
@role_required(["SUPER_ADMIN", "ADMIN", "TEACHER", "BURSAR", "NURSE", "LIBRARIAN", "STOREKEEPER", "TRANSPORT_OFFICER"])
def school_setup_checklist(request):
    """Setup guide: what to set up, in which order, and what each step needs first."""
    from mainApp import setup_guide

    school = _request_school(request)
    if school is None:
        messages.info(request, "Your account is not linked to a school.")
        return redirect("dashboard")
    role = tenancy.user_role(request.user)
    guide = setup_guide.guide_for(school, role)
    return render(request, "platform/setup_checklist.html", {
        "title": "Setup guide", "school": school, "guide": guide,
        "done": guide["done"], "percent": guide["percent"],
        "routine": setup_guide.TERM_ROUTINE, "is_admin": role in setup_guide.ADMIN_ROLES,
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN", "BURSAR"])
def school_sms_credit(request):
    school = _request_school(request)
    if school is None:
        return redirect("dashboard")
    acc = messaging_wallet.account_for(school)
    return render(request, "platform/school_sms_credit.html", {
        "title": "SMS credit", "school": school, "account": acc,
        "used_this_month": messaging_wallet.month_usage_units(school),
        "usage": messaging_wallet.monthly_usage(school),
        "ledger": SmsCreditEntry.objects.filter(school=school)[:50],
    })


@login_required
@role_required(["SUPER_ADMIN", "ADMIN", "BURSAR"])
def subscription_status(request):
    school = _request_school(request)
    if school is None:
        return redirect("dashboard")
    sub = subscriptions.subscription_for(school)
    from mainApp.models.platform_models import MODULE_CHOICES
    enabled = subscriptions.modules_for(school)
    return render(request, "platform/subscription_status.html", {
        "title": "Subscription", "school": school, "subscription": sub,
        "state": sub.state() if sub else "ACTIVE",
        "modules": [(label, code in enabled) for code, label in MODULE_CHOICES],
        "payments": SubscriptionPayment.objects.filter(school=school)[:12],
    })


# ---------------------------------------------------------------------------
# System logs (errors from the app's log file)
# ---------------------------------------------------------------------------
@platform_required
def platform_logs(request):
    from mainApp import log_viewer

    level = request.GET.get("level", "WARNING")
    if level not in log_viewer.LEVELS:
        level = "WARNING"
    query = (request.GET.get("q") or "").strip()[:100]
    cross = request.GET.get("cross") == "1"

    if request.GET.get("download") == "1":
        resp = HttpResponse(log_viewer.read_raw(), content_type="text/plain; charset=utf-8")
        resp["Content-Disposition"] = 'attachment; filename="school360-log.txt"'
        return resp

    return render(request, "platform/logs.html", {
        "title": "System logs",
        "entries": log_viewer.read_entries(level=level, query=query, cross_school_only=cross),
        "summary": log_viewer.summary(),
        "level": level, "q": query, "cross": cross,
        "levels": [("ERROR", "Errors only"), ("WARNING", "Errors + warnings"), ("INFO", "Everything")],
        "log_path": log_viewer.log_path(),
    })

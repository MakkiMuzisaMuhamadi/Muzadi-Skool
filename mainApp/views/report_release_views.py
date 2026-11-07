"""
Releasing report cards to learners and parents.

* Publish / unpublish report cards (drafts are only seen by staff)
* Fees hold settings (Report Setup -> Fees Hold Settings)
* "Allowed despite balance" overrides per learner per term
* Send report-card links to parents by SMS (link works without a login and
  honours the fees hold)
* The public parent link itself
"""
from __future__ import annotations

from mainApp.academic_period import school_current_year_term

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from mainApp.models.calendar import Term
from mainApp.models.fees_models import FeeCollectionSetting
from mainApp.models.models import Class, Stream
from mainApp.models.reporting_task_celery_models import (
    ReportCardSnapshot,
    ReportReleaseOverride,
    ReportRun,
)
from mainApp.models.student import Student
from mainApp.report_configs.report_access import (
    bulk_fee_holds,
    make_parent_link_token,
    read_parent_link_token,
    report_access_for,
)
from mainApp.views.reports_views import _user_school, report_admin_required


def _school_or_404(request):
    school = _user_school(request)
    if not school:
        raise Http404("No school linked to your account.")
    return school


def _run_for(request, run_id):
    run = get_object_or_404(ReportRun.objects.select_related("school", "term", "academic_year"), id=run_id)
    school = _user_school(request)
    if school and not request.user.is_superuser and run.school_id != school.id:
        raise Http404
    return run


def _back_to(request, fallback):
    nxt = request.POST.get("next") or ""
    if nxt.startswith("/") and not nxt.startswith("//"):
        return redirect(nxt)
    return redirect(fallback)


# --------------------------------------------------------------- publish --

@require_POST
@login_required
@report_admin_required
def report_run_publish(request, run_id):
    """Release (or take back) every current report card produced by this run."""
    run = _run_for(request, run_id)
    action = request.POST.get("action")

    qs = ReportCardSnapshot.objects.filter(run=run, is_latest=True).exclude(pdf="").exclude(pdf__isnull=True)

    if action == "publish":
        n = qs.filter(is_published=False).update(
            is_published=True, published_at=timezone.now(), published_by=request.user,
        )
        messages.success(request, f"{n} report card(s) released. Learners can now see them "
                                  f"(unless they are on fees hold).")
    elif action == "unpublish":
        n = qs.filter(is_published=True).update(is_published=False, published_at=None, published_by=None)
        messages.warning(request, f"{n} report card(s) taken back to draft. Learners can no longer see them.")
    else:
        messages.error(request, "Unknown action.")

    return redirect("report_run_detail", run_id=run.id)


@require_POST
@login_required
@report_admin_required
def report_snapshot_publish_toggle(request, snapshot_id):
    snap = get_object_or_404(ReportCardSnapshot.objects.select_related("run"), id=snapshot_id)
    school = _user_school(request)
    if school and not request.user.is_superuser and snap.run.school_id != school.id:
        raise Http404

    if snap.is_published:
        snap.is_published, snap.published_at, snap.published_by = False, None, None
        messages.warning(request, f"{snap.student}'s report card is back to draft.")
    else:
        snap.is_published, snap.published_at, snap.published_by = True, timezone.now(), request.user
        messages.success(request, f"{snap.student}'s report card is released.")
    snap.save(update_fields=["is_published", "published_at", "published_by"])

    return _back_to(request, reverse("report_run_detail", args=[snap.run_id]))


# ------------------------------------------------------- fees hold setup --

class ReportReleaseSettingsForm(forms.ModelForm):
    class Meta:
        model = FeeCollectionSetting
        fields = ["withhold_reports_for_defaulters", "report_hold_basis", "report_hold_allowance"]
        widgets = {
            "withhold_reports_for_defaulters": forms.CheckboxInput(attrs={"class": "custom-control-input"}),
            "report_hold_basis": forms.RadioSelect(),
            "report_hold_allowance": forms.NumberInput(attrs={"class": "form-control", "min": 0, "step": "1000"}),
        }

    def clean_report_hold_allowance(self):
        v = self.cleaned_data.get("report_hold_allowance") or 0
        if v < 0:
            raise forms.ValidationError("Use 0 or more.")
        return v


@login_required
@report_admin_required
def report_release_settings(request):
    school = _school_or_404(request)
    setting = FeeCollectionSetting.for_school(school)

    if request.method == "POST":
        form = ReportReleaseSettingsForm(request.POST, instance=setting)
        if form.is_valid():
            form.save()
            if setting.withhold_reports_for_defaulters:
                messages.success(request, "Saved. Learners with unpaid fees will not see their report cards.")
            else:
                messages.success(request, "Saved. Report cards are not withheld for fees.")
            return redirect("report_release_settings")
    else:
        form = ReportReleaseSettingsForm(instance=setting)

    return render(request, "dashboard/reports/release_settings.html", {
        "title": "Fees Hold Settings",
        "form": form,
        "setting": setting,
    })


# -------------------------------------------- allowed despite balance --

@login_required
@report_admin_required
def report_release_overrides(request):
    """
    Pick a term and class -> see learners whose report cards are held for fees,
    and allow individual learners (payment plan, bursary, promise to pay).
    """
    school = _school_or_404(request)

    terms = Term.objects.select_related("academic_year").order_by("-academic_year__start_date", "-order")
    classes = Class.objects.filter(school=school).select_related("level").order_by("level__order", "name")

    term = Term.objects.filter(id=request.GET.get("term_id") or 0).first()
    if not term:
        term = school_current_year_term(school)[1] or Term.objects.filter(is_current=True).first() or terms.first()
    class_ref = classes.filter(id=request.GET.get("class_ref_id") or 0).first()
    stream = Stream.objects.filter(id=request.GET.get("stream_id") or 0, class_ref=class_ref).first() if class_ref else None

    if request.method == "POST":
        action = request.POST.get("action")
        student = get_object_or_404(Student, id=request.POST.get("student_id"), school=school)
        post_term = get_object_or_404(Term, id=request.POST.get("term_id"))

        if action == "allow":
            reason = (request.POST.get("reason") or "").strip()[:255]
            if not reason:
                messages.error(request, "Please write a reason (e.g. 'Payment plan agreed with bursar').")
            else:
                ReportReleaseOverride.objects.update_or_create(
                    student=student, term=post_term,
                    defaults={"school": school, "reason": reason, "allowed_by": request.user, "is_active": True},
                )
                messages.success(request, f"{student} can now see the {post_term} report card.")
        elif action == "revoke":
            ReportReleaseOverride.objects.filter(student=student, term=post_term).update(is_active=False)
            messages.warning(request, f"{student}'s report card is held again until fees are cleared.")
        return redirect(f"{reverse('report_release_overrides')}?{request.GET.urlencode()}")

    setting = FeeCollectionSetting.for_school(school)
    rows = []
    if term and class_ref:
        students = Student.objects.filter(school=school, is_active=True, current_class=class_ref)
        if stream:
            students = students.filter(stream=stream)
        students = list(students.select_related("stream").order_by("first_name", "last_name"))
        holds = bulk_fee_holds(school, term, [st.id for st in students])
        overrides = {
            o.student_id: o
            for o in ReportReleaseOverride.objects.filter(term=term, student__in=students, is_active=True)
            .select_related("allowed_by")
        }
        for st in students:
            h = holds.get(st.id)
            if h and (h.held or h.overridden):
                rows.append({"student": st, "hold": h, "override": overrides.get(st.id)})

    all_overrides = (
        ReportReleaseOverride.objects.filter(school=school, term=term, is_active=True)
        .select_related("student", "allowed_by", "student__current_class")
        .order_by("student__first_name") if term else []
    )

    return render(request, "dashboard/reports/release_overrides.html", {
        "title": "Allowed Despite Balance",
        "terms": terms,
        "classes": classes,
        "streams": Stream.objects.filter(class_ref=class_ref).order_by("name") if class_ref else [],
        "term": term,
        "class_ref": class_ref,
        "stream": stream,
        "rows": rows,
        "setting": setting,
        "all_overrides": all_overrides,
    })


# ------------------------------------------------- send links to parents --

DEFAULT_LINK_SMS = ("Dear {parent}, {student}'s {term} report card is ready. "
                    "View it here: {link} - {school}")
DEFAULT_HOLD_SMS = ("Dear {parent}, {student}'s {term} report card is ready but is withheld because of "
                    "a fees balance of UGX {balance}. Please clear it to view the report. {school}")


class _Safe(dict):
    def __missing__(self, key):
        return "{" + key + "}"


@require_POST
@login_required
@report_admin_required
def report_run_send_to_parents(request, run_id):
    """
    SMS every parent (who agreed to SMS) of the learners in this run:
      * released + cleared  -> link to the report card
      * on fees hold        -> optional fees reminder instead (no link)
    Draft (unreleased) report cards are skipped.
    """
    from mainApp.models.communication_models import SmsCampaign, SmsMessage
    from mainApp.models.student import Parent
    from mainApp.sms_service import pick_parent_number

    run = _run_for(request, run_id)
    school = run.school
    remind_held = request.POST.get("remind_held") == "1"

    snaps = list(
        ReportCardSnapshot.objects
        .filter(run=run, is_latest=True, is_published=True)
        .exclude(pdf="").exclude(pdf__isnull=True)
        .select_related("student", "term")
    )
    if not snaps:
        messages.error(request, "Release the report cards first - only released report cards are sent.")
        return redirect("report_run_detail", run_id=run.id)

    holds = bulk_fee_holds(school, run.term, [s.student_id for s in snaps])
    school_name = getattr(school, "short_name", "") or school.name
    term_name = getattr(run.term, "name", "")

    links = held = skipped_no_parent = 0
    with transaction.atomic():
        campaign = SmsCampaign.objects.create(
            school=school,
            title=f"Report cards {term_name} {timezone.localdate():%d %b %Y}",
            audience="STUDENTS",
            message_body=DEFAULT_LINK_SMS,
            filters={"source": "report_cards", "run_id": str(run.id)},
            created_by=request.user,
            queued_at=timezone.now(),
        )
        seen = set()
        for snap in snaps:
            student = snap.student
            hold = holds.get(student.id)
            is_held = bool(hold and hold.held)
            if is_held and not remind_held:
                continue

            parents = Parent.objects.filter(
                is_active=True, sms_consent=True,
                child_links__student=student, child_links__receives_sms=True,
            ).distinct()
            sent_any = False
            for parent in parents:
                number = pick_parent_number(parent)
                if not number or (number, student.pk) in seen:
                    continue
                seen.add((number, student.pk))
                values = _Safe(
                    parent=(getattr(parent, "first_name", "") or "Parent"),
                    student=(student.first_name or str(student)),
                    term=term_name,
                    school=school_name,
                )
                if is_held:
                    values["balance"] = f"{hold.balance:,.0f}"
                    text = DEFAULT_HOLD_SMS.format_map(values)
                else:
                    token = make_parent_link_token(snap)
                    values["link"] = request.build_absolute_uri(reverse("report_parent_link", args=[token]))
                    text = DEFAULT_LINK_SMS.format_map(values)
                SmsMessage.objects.create(
                    school=school, campaign=campaign, student=student, parent=parent,
                    to_number=number, text=text[:459], status="PENDING",
                )
                sent_any = True
                if is_held:
                    held += 1
                else:
                    links += 1
            if not sent_any:
                skipped_no_parent += 1

    total = links + held
    if total:
        from mainApp.tasks.fee_tasks import send_sms_campaign
        transaction.on_commit(lambda: send_sms_campaign.delay(campaign.pk))
        msg = f"Queued {links} report-card link SMS"
        if remind_held:
            msg += f" and {held} fees reminder SMS"
        msg += "."
        if skipped_no_parent:
            msg += f" {skipped_no_parent} learner(s) have no parent with a valid number who accepts SMS."
        messages.success(request, msg)
    else:
        campaign.delete()
        messages.warning(request, "No SMS were queued. Check that parents are linked, have valid numbers "
                                  "and accept SMS, and that the report cards are released.")
    return redirect("report_run_detail", run_id=run.id)


# ------------------------------------------------------ the parent link --

def report_parent_link(request, token):
    """Opened from the SMS. No login. Honours release + fees hold, checked live."""
    snap_id = read_parent_link_token(token)
    if not snap_id:
        return render(request, "dashboard/reports/parent_link.html", {
            "state": "invalid",
        }, status=404)

    snap = (
        ReportCardSnapshot.objects
        .select_related("student", "student__school", "term", "academic_year")
        .filter(id=snap_id)
        .first()
    )
    if not snap:
        return render(request, "dashboard/reports/parent_link.html", {"state": "invalid"}, status=404)

    # If the card was re-generated after the SMS went out, show the newest version.
    if not snap.is_latest:
        newer = (
            ReportCardSnapshot.objects
            .filter(student=snap.student, term=snap.term, is_latest=True)
            .exclude(pdf="").exclude(pdf__isnull=True)
            .first()
        )
        if newer:
            snap = newer

    access = report_access_for(snap, None, as_parent=True)
    if not access.allowed or not snap.pdf:
        return render(request, "dashboard/reports/parent_link.html", {
            "state": "held" if access.code == "FEES_HOLD" else "not_released",
            "snapshot": snap,
            "access": access,
            "school": snap.student.school,
        }, status=403)

    filename = snap.pdf.name.split("/")[-1] or f"report_{snap.id}.pdf"
    response = FileResponse(snap.pdf.open("rb"), content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    return response

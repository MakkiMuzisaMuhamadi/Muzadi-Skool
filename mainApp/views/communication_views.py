# mainApp/views/communication_views.py
import json
import re
from datetime import timedelta

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.db.models import Count, Q, Sum, Prefetch
from django.http import Http404, HttpResponse, JsonResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.shortcuts import render, redirect, get_object_or_404
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET, require_POST

from mainApp.decorators import role_required
from mainApp.forms.communication_forms import SmsComposeForm
from mainApp.models.communication_models import SmsCampaign, SmsMessage, WhatsAppTemplate
from mainApp.models.student import Student, Parent, StudentGuardian
from mainApp.parent_utils import normalize_phone
from mainApp.sms_service import AfricaTalkingSMS, pick_number, sms_units
from mainApp.whatsapp_service import (
    WhatsAppCloud, NOT_ON_WHATSAPP_CODES, pick_whatsapp_number, fill_template_body, clean_param,
)


SMS_ROLES = ["SUPER_ADMIN", "ADMIN"]          # same roles that see the "New SMS" button
PLACEHOLDER_RE = re.compile(r"\{(student_name|first_name|class|stream|school_name|parent_name)\}")
STALE_SENDING_AFTER = timedelta(minutes=10)   # a batch that died mid-way can be retried after this
FAILED_STATUSES = ["FAILED", "UNDELIVERED"]
DELIVERED_STATUSES = ["DELIVERED", "READ"]
SENT_OR_BETTER = ["SENT", "DELIVERED", "READ"]
STATUS_RANK = {"PENDING": 0, "SENDING": 1, "SENT": 2, "DELIVERED": 3, "READ": 4}   # never go backwards


def get_school(request):
    return getattr(getattr(request.user, "profile", None), "school", None)


class AudienceError(Exception):
    """Raised when the chosen filters can't produce a safe recipient list."""


EXCLUDED_STATUSES = ["ALUMNI", "TRANSFERRED_OUT", "INACTIVE"]


def active_students(school):
    """Students who are really in school now (not alumni, transferred out or inactive)."""
    return (
        Student.objects
        .filter(school=school, is_active=True, is_alumni=False)
        .exclude(academic_status__in=EXCLUDED_STATUSES)
    )


# ---------------------------------------------------------------------------
# Recipients
# ---------------------------------------------------------------------------
def student_display_name(s):
    if s is None:
        return ""
    return s.name or " ".join(p for p in [s.first_name, s.middle_name or "", s.last_name] if p)


def resolve_audience(form, school):
    """
    Returns ("students", <Student queryset>) or ("parents", <Parent queryset>).
    Querysets only — nothing is loaded yet, so this is cheap.
    Never falls back to "everyone" when a filter is missing.
    """
    cd = form.cleaned_data
    audience = cd["audience"]
    students = active_students(school)

    if audience == "ALL":
        return "students", students

    if audience == "LEVEL":
        if not cd.get("level"):
            raise AudienceError("Please choose a level.")
        return "students", students.filter(level=cd["level"])

    if audience == "CLASS":
        cls = cd.get("current_class")
        if not cls:
            raise AudienceError("Please choose a class.")
        students = students.filter(current_class=cls)
        st = cd.get("stream")
        if st:
            if st.class_ref_id != cls.id:
                raise AudienceError(f"Stream '{st}' does not belong to class '{cls}'.")
            students = students.filter(stream=st)
        return "students", students

    if audience == "STREAM":
        if not cd.get("stream"):
            raise AudienceError("Please choose a stream.")
        return "students", students.filter(stream=cd["stream"])

    if audience == "STUDENTS":
        selected = cd.get("students")
        if not selected:
            raise AudienceError("Please select at least one student.")
        return "students", students.filter(pk__in=[x.pk for x in selected])

    if audience == "PARENTS":
        selected = cd.get("parents")
        if not selected:
            raise AudienceError("Please select at least one parent.")
        return "parents", Parent.objects.filter(
            pk__in=[x.pk for x in selected], school=school, is_active=True
        )

    raise AudienceError("Unknown audience.")


def contact_numbers(phone, alternate, whatsapp, use_alt, use_phone_wa):
    """(sms_number, whatsapp_number) for one contact. Either can be ""."""
    sms = pick_number(phone, alternate, use_alt)
    wa = pick_whatsapp_number(whatsapp, phone, alternate if use_alt else None, use_phone_wa)
    return sms, wa


def parent_numbers(phone, alternate, whatsapp, sms_ok, wa_ok, use_alt, use_phone_wa):
    """Like contact_numbers, but drops a channel the parent has not agreed to."""
    sms, wa = contact_numbers(phone, alternate, whatsapp, use_alt, use_phone_wa)
    return (sms if sms_ok else ""), (wa if wa_ok else "")


def destination(channel, sms, wa):
    """Which number is used for this channel (WhatsApp first when allowed)."""
    if channel == "SMS":
        return ("SMS", sms) if sms else (None, "")
    if channel == "WHATSAPP":
        return ("WHATSAPP", wa) if wa else (None, "")
    # WHATSAPP_SMS
    if wa:
        return "WHATSAPP", wa
    if sms:
        return "SMS", sms
    return None, ""


def count_recipients(form, school):
    """
    FAST count for the live preview. Reads only phone columns.
    Returns how many students are in the selection, how many can be reached and by which channel.
    """
    cd = form.cleaned_data
    channel = cd.get("channel") or "SMS"
    use_alt = cd.get("use_alternate_if_missing", False)
    use_em = cd.get("use_emergency_contact", False)
    use_phone_wa = cd.get("use_phone_for_whatsapp", False)
    kind, qs = resolve_audience(form, school)

    by_channel = {"SMS": set(), "WHATSAPP": set()}

    def add(sms, wa):
        ch, num = destination(channel, sms, wa)
        if ch:
            by_channel[ch].add(num)
            return True
        return False

    def result(extra):
        return {
            "channel": channel,
            "valid_numbers": len(by_channel["SMS"]) + len(by_channel["WHATSAPP"]),
            "whatsapp_numbers": len(by_channel["WHATSAPP"]),
            "sms_numbers": len(by_channel["SMS"]),
            **extra,
        }

    # ---- "Selected parents" mode ----
    if kind == "parents":
        rows = list(qs.values_list("phone", "alternate_phone", "whatsapp_number",
                                   "sms_consent", "whatsapp_consent"))
        missing = sum(0 if add(*parent_numbers(ph, alt, wa, sms_ok, wa_ok, use_alt, use_phone_wa)) else 1
                      for ph, alt, wa, sms_ok, wa_ok in rows)
        return result({"kind": "parents", "parents": len(rows), "unreachable": missing})

    # ---- student-based modes ----
    # Only guardians who are active and set to receive messages about that child
    links = (
        StudentGuardian.objects
        .filter(student_id__in=qs.values("id"), parent__is_active=True, receives_sms=True)
        .values_list("student_id", "parent_id", "parent__phone",
                     "parent__alternate_phone", "parent__whatsapp_number",
                     "parent__sms_consent", "parent__whatsapp_consent")
    )
    via_parent, parent_ids = set(), set()
    for sid, pid, ph, alt, wa, sms_ok, wa_ok in links:
        if add(*parent_numbers(ph, alt, wa, sms_ok, wa_ok, use_alt, use_phone_wa)):
            via_parent.add(sid)
            parent_ids.add(pid)

    total = via_em = 0
    for sid, ec in qs.values_list("id", "emergency_contact_phone").iterator(chunk_size=2000):
        total += 1
        if sid in via_parent or not use_em:
            continue
        # emergency contact: same number tried on WhatsApp only if allowed
        if add(*contact_numbers(ec, None, None, False, use_phone_wa)):
            via_em += 1

    reached = len(via_parent) + via_em
    return result({
        "kind": "students",
        "students": total,
        "reached_students": reached,
        "via_parent": len(via_parent),
        "parents": len(parent_ids),
        "via_emergency": via_em,
        "unreachable": total - reached,
    })


def build_targets(form, school):
    """
    Full recipient list used when actually sending.
    Returns dicts: {parent, student, name, sms, wa}
      - sms / wa are the numbers available for that contact ("" when none).
      - a target with neither usable for the channel is logged as FAILED.
    """
    cd = form.cleaned_data
    channel = cd.get("channel") or "SMS"
    use_alt = cd.get("use_alternate_if_missing", False)
    use_em = cd.get("use_emergency_contact", False)
    use_phone_wa = cd.get("use_phone_for_whatsapp", False)
    kind, qs = resolve_audience(form, school)

    def numbers_for_parent(p):
        return parent_numbers(p.phone, p.alternate_phone, p.whatsapp_number,
                              p.sms_consent, p.whatsapp_consent, use_alt, use_phone_wa)

    targets = []
    if kind == "parents":
        for p in qs.prefetch_related("children__current_class", "children__stream"):
            sms, wa = numbers_for_parent(p)
            targets.append({"parent": p, "student": None, "name": p.full_name, "sms": sms, "wa": wa})
        return targets

    # Active guardians who receive messages about this child; primary contact first
    guardians = Prefetch(
        "guardian_links",
        queryset=(StudentGuardian.objects
                  .filter(parent__is_active=True, receives_sms=True)
                  .select_related("parent")
                  .order_by("-is_primary", "id")),
    )
    for s in qs.select_related("current_class", "stream").prefetch_related(guardians):
        got_one = False
        for link in s.guardian_links.all():
            p = link.parent
            sms, wa = numbers_for_parent(p)
            if destination(channel, sms, wa)[0]:
                got_one = True
                targets.append({"parent": p, "student": s, "name": p.full_name, "sms": sms, "wa": wa})
        if not got_one and use_em:
            sms, wa = contact_numbers(s.emergency_contact_phone, None, None, False, use_phone_wa)
            if destination(channel, sms, wa)[0]:
                got_one = True
                targets.append({"parent": None, "student": s, "name": s.emergency_contact_name,
                                "sms": sms, "wa": wa})
        if not got_one:
            targets.append({"parent": None, "student": s, "name": "", "sms": "", "wa": ""})
    return targets


def render_body(body, school, parent, student, contact_name=""):
    """Fills {student_name}, {first_name}, {class}, {stream}, {school_name}, {parent_name}."""
    if not PLACEHOLDER_RE.search(body):
        return body

    if student is None and parent is not None:
        # "Selected parents": mention all their active children in this school
        kids = [c for c in parent.children.all() if c.is_active and c.school_id == school.id]
        student_name = " & ".join(student_display_name(k) for k in kids)
        first_name = " & ".join(k.first_name for k in kids)
        cls = " & ".join(sorted({k.current_class.name for k in kids if k.current_class}))
        stream = " & ".join(sorted({k.stream.name for k in kids if k.stream}))
    else:
        student_name = student_display_name(student)
        first_name = student.first_name if student else ""
        cls = student.current_class.name if student and student.current_class else ""
        stream = student.stream.name if student and student.stream else ""

    values = {
        "student_name": student_name,
        "first_name": first_name,
        "class": cls,
        "stream": stream,
        "school_name": getattr(school, "name", ""),
        "parent_name": contact_name or (getattr(parent, "full_name", "") if parent else ""),
    }
    text = PLACEHOLDER_RE.sub(lambda m: values.get(m.group(1), ""), body)
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def build_filters_payload(cd):
    """Store only the filters that belong to the chosen audience."""
    aud = cd["audience"]
    payload = {"audience": aud, "channel": cd.get("channel", "SMS")}
    if aud == "LEVEL":
        payload["level_id"] = getattr(cd.get("level"), "id", None)
    elif aud == "CLASS":
        payload["class_id"] = getattr(cd.get("current_class"), "id", None)
        payload["stream_id"] = getattr(cd.get("stream"), "id", None)
    elif aud == "STREAM":
        payload["stream_id"] = getattr(cd.get("stream"), "id", None)
    elif aud == "STUDENTS":
        payload["students_ids"] = [s.id for s in (cd.get("students") or [])]
    elif aud == "PARENTS":
        payload["parents_ids"] = [p.id for p in (cd.get("parents") or [])]
    return payload


def create_campaign(request, school, form):
    """
    Creates the campaign with PENDING rows (one per destination) and FAILED rows for people
    nobody can be reached for. No network calls in here. Rolls back if nobody can be reached.
    """
    cd = form.cleaned_data
    channel = cd.get("channel") or "SMS"
    body = cd.get("message_body") or ""
    wa_tpl = cd.get("wa_template") if channel != "SMS" else None
    wa_raw_params = cd.get("wa_params") or []
    targets = build_targets(form, school)

    if not targets:
        raise AudienceError("No students or parents match this selection.")

    sender_id = getattr(settings, "AFRICASTALKING_SENDER_ID", "") or ""
    seen = set()          # (channel, number, text): one number can still get a different text per child
    unreachable = 0

    with transaction.atomic():
        campaign = SmsCampaign.objects.create(
            school=school,
            title=cd["title"],
            audience=cd["audience"],
            channel=channel,
            template=cd.get("template"),
            wa_template=wa_tpl,
            wa_params=wa_raw_params,
            message_body=body,
            filters=build_filters_payload(cd),
            sender_id_used=sender_id if channel != "WHATSAPP" else "",
            created_by=request.user,
            queued_at=timezone.now(),
        )

        rows = []
        common = dict(created_by=request.user, updated_by=request.user, school=school, campaign=campaign)

        for t in targets:
            parent, student, name = t["parent"], t["student"], t["name"]
            ch, number = destination(channel, t["sms"], t["wa"])
            sms_text = render_body(body, school, parent, student, name) if body else ""

            if not ch:
                unreachable += 1
                rows.append(SmsMessage(
                    **common, parent=parent, student=student, channel="SMS" if channel == "SMS" else "WHATSAPP",
                    to_number="", text=sms_text or "(no number)", units=0, status="FAILED",
                    status_detail="No valid number for this channel",
                ))
                continue

            if ch == "WHATSAPP":
                params = [clean_param(render_body(v, school, parent, student, name)) for v in wa_raw_params]
                text = fill_template_body(wa_tpl.body, wa_tpl.param_names, params)
                key = ("WHATSAPP", number, text)
                if key in seen:
                    continue
                seen.add(key)
                fallback_ok = channel == "WHATSAPP_SMS" and t["sms"] and sms_text
                rows.append(SmsMessage(
                    **common, parent=parent, student=student, channel="WHATSAPP",
                    provider="WHATSAPP_CLOUD", to_number=number, text=text, wa_params=params,
                    units=0, status="PENDING",
                    fallback_text=sms_text if fallback_ok else "",
                    fallback_number=t["sms"] if fallback_ok else "",
                ))
            else:
                key = ("SMS", number, sms_text)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(SmsMessage(
                    **common, parent=parent, student=student, channel="SMS",
                    to_number=number, text=sms_text, units=sms_units(sms_text), status="PENDING",
                ))

        if not seen:
            transaction.set_rollback(True)
            raise AudienceError(
                f"Nobody in this selection can be reached on this channel ({unreachable} without a number). "
                "Link parents to the students, add WhatsApp numbers, or fix the emergency contact numbers."
            )

        SmsMessage.objects.bulk_create(rows, batch_size=500)
        pending_ids = list(campaign.messages.filter(status="PENDING").values_list("id", flat=True))

    return campaign, pending_ids


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------
class Senders:
    """Creates the SMS / WhatsApp clients only when they are actually needed."""
    def __init__(self):
        self._sms = None
        self._wa = None

    @property
    def sms(self):
        if self._sms is None:
            self._sms = AfricaTalkingSMS()
        return self._sms

    @property
    def wa(self):
        if self._wa is None:
            self._wa = WhatsAppCloud()
        return self._wa


SAVE_FIELDS = [
    "raw_response", "status", "sent_at", "provider_message_id", "status_detail", "cost",
    "channel", "provider", "to_number", "text", "units", "updated_at",
]


def _fail(msg, detail):
    msg.status = "FAILED"
    msg.status_detail = detail[:255]
    msg.save(update_fields=SAVE_FIELDS)
    return False


def send_sms(senders, msg, prefix=""):
    try:
        resp = senders.sms.send_one(message=msg.text, to_number=msg.to_number, enqueue=True) or {}
    except Exception as e:
        return _fail(msg, f"{prefix}{e}")

    result = senders.sms.parse_response(resp)
    msg.raw_response = resp
    msg.provider = "AFRICASTALKING"
    msg.provider_message_id = result["message_id"]
    msg.cost = result["cost"]
    msg.status_detail = f"{prefix}{result['status']}"[:255]
    if result["ok"]:
        msg.status = "SENT"
        msg.sent_at = timezone.now()
    else:
        msg.status = "FAILED"     # e.g. InvalidPhoneNumber, InsufficientBalance
    msg.save(update_fields=SAVE_FIELDS)
    return result["ok"]


def fall_back_to_sms(senders, msg, reason):
    """WhatsApp failed: send the SMS version instead (same row, so the campaign counts stay right)."""
    if not (msg.fallback_number and msg.fallback_text):
        msg.status = "UNDELIVERED" if msg.sent_at else "FAILED"
        msg.status_detail = reason[:255]
        msg.save(update_fields=SAVE_FIELDS)
        return False

    msg.channel = "SMS"
    msg.to_number = msg.fallback_number
    msg.text = msg.fallback_text
    msg.units = sms_units(msg.fallback_text)
    msg.sent_at = None
    return send_sms(senders, msg, prefix=f"Not on WhatsApp ({reason[:90]}), sent by SMS: ")


def send_whatsapp(senders, msg, prefix=""):
    tpl = msg.campaign.wa_template
    if tpl is None:
        return _fail(msg, f"{prefix}WhatsApp template was deleted")

    try:
        resp = senders.wa.send_template(
            to=msg.to_number, template_name=tpl.name, language=tpl.language,
            params=msg.wa_params, param_format=tpl.param_format, param_names=tpl.param_names,
        )
    except Exception as e:
        return fall_back_to_sms(senders, msg, f"{prefix}{e}")

    result = senders.wa.parse_response(resp)
    msg.raw_response = resp
    msg.provider = "WHATSAPP_CLOUD"
    if result["ok"]:
        msg.provider_message_id = result["message_id"]
        msg.status = "SENT"
        msg.sent_at = timezone.now()
        msg.status_detail = f"{prefix}{result['status']}"[:255]
        msg.save(update_fields=SAVE_FIELDS)
        return True
    return fall_back_to_sms(senders, msg, f"{prefix}{result['status']}")


def send_message(senders, msg, resend=False):
    """Sends one message on its channel. Returns True if the provider accepted it."""
    prefix = "Resent: " if resend else ""
    if msg.channel == "WHATSAPP":
        return send_whatsapp(senders, msg, prefix)
    return send_sms(senders, msg, prefix)


def claim_batch(campaign, batch_size):
    """
    Marks up to batch_size PENDING rows as SENDING and returns their ids.
    The conditional update means two requests can't claim the same row (works on SQLite too).
    """
    candidate_ids = list(
        campaign.messages.filter(status="PENDING").exclude(to_number="")
        .order_by("id").values_list("id", flat=True)[:batch_size]
    )
    claimed = []
    for mid in candidate_ids:
        if SmsMessage.objects.filter(id=mid, status="PENDING").update(
            status="SENDING", status_detail="Sending…", updated_at=timezone.now()
        ):
            claimed.append(mid)
    return claimed


def campaign_totals(campaign):
    return campaign.messages.aggregate(
        total=Count("id"),
        sent=Count("id", filter=Q(status__in=SENT_OR_BETTER)),
        failed=Count("id", filter=Q(status__in=FAILED_STATUSES)),
        delivered=Count("id", filter=Q(status__in=DELIVERED_STATUSES)),
        pending=Count("id", filter=Q(status__in=["PENDING", "SENDING"])),
    )


def annotate_counts(qs):
    return qs.annotate(
        total_msgs=Count("messages", distinct=True),
        delivered_msgs=Count("messages", filter=Q(messages__status__in=DELIVERED_STATUSES), distinct=True),
        read_msgs=Count("messages", filter=Q(messages__status="READ"), distinct=True),
        whatsapp_msgs=Count("messages", filter=Q(messages__channel="WHATSAPP"), distinct=True),
        sent_msgs=Count("messages", filter=Q(messages__status="SENT"), distinct=True),
        failed_msgs=Count("messages", filter=Q(messages__status__in=FAILED_STATUSES), distinct=True),
        pending_msgs=Count("messages", filter=Q(messages__status__in=["PENDING", "SENDING"]), distinct=True),
    )


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------
@login_required
def sms_campaign_list(request):
    school = get_school(request)
    campaigns = annotate_counts(SmsCampaign.objects.filter(school=school)).order_by("-queued_at")
    return render(request, "dashboard/communication/sms_campaign_list.html", {"campaigns": campaigns})


@login_required
@role_required(SMS_ROLES)
def sms_compose(request):
    """
    Normal (non-JavaScript) path. The page normally uses sms_compose_ajax + sms_send_batch;
    this view is the fallback when JavaScript is off.
    """
    school = get_school(request)
    if school is None:
        messages.error(request, "Your account is not linked to a school.")
        return redirect("sms_campaign_list")

    form = SmsComposeForm(request.POST or None, school=school)

    if request.method == "POST" and form.is_valid():
        try:
            campaign, pending_ids = create_campaign(request, school, form)
        except AudienceError as e:
            form.add_error(None, str(e))
        else:
            senders = Senders()
            sent = failed = 0
            for mid in claim_batch(campaign, len(pending_ids)):
                if send_message(senders, SmsMessage.objects.select_related("campaign__wa_template").get(id=mid)):
                    sent += 1
                else:
                    failed += 1
            messages.success(request, f"Campaign created: {sent}/{sent + failed} sent, {failed} failed.")
            return redirect("sms_campaign_detail", pk=campaign.pk)

    return render(request, "dashboard/communication/sms_compose.html", {
        "form": form,
        "whatsapp_ready": WhatsAppCloud().is_configured,
    })


@require_POST
@login_required
@role_required(SMS_ROLES)
def sms_compose_ajax(request):
    school = get_school(request)
    if school is None:
        return JsonResponse({"ok": False, "errors": {"__all__": ["Your account is not linked to a school."]}}, status=400)

    form = SmsComposeForm(request.POST, school=school)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors}, status=400)

    try:
        campaign, pending_ids = create_campaign(request, school, form)
    except AudienceError as e:
        return JsonResponse({"ok": False, "errors": {"__all__": [str(e)]}}, status=400)

    return JsonResponse({"ok": True, "campaign_id": campaign.id, "queued": len(pending_ids)})


@require_POST
@login_required
@role_required(SMS_ROLES)
def sms_send_batch(request, pk):
    school = get_school(request)
    campaign = get_object_or_404(SmsCampaign, pk=pk, school=school)

    try:
        batch_size = int(request.POST.get("batch_size") or 10)
    except ValueError:
        batch_size = 10
    batch_size = max(1, min(batch_size, 50))

    ids = claim_batch(campaign, batch_size)

    senders = Senders()
    sent_now = failed_now = 0
    for msg in SmsMessage.objects.filter(id__in=ids).select_related("campaign__wa_template"):
        if send_message(senders, msg):
            sent_now += 1
        else:
            failed_now += 1

    totals = campaign_totals(campaign)
    # Done when nothing is left to claim (rows another tab is still sending don't keep us looping)
    done = not campaign.messages.filter(status="PENDING").exclude(to_number="").exists()

    return JsonResponse({
        "ok": True,
        "sent_now": sent_now,
        "failed_now": failed_now,
        "totals": totals,
        "done": done,
        "redirect_url": reverse("sms_campaign_detail", kwargs={"pk": campaign.pk}) if done else "",
    })


@require_GET
@login_required
@role_required(SMS_ROLES)
def sms_preview_recipients(request):
    """GET ?audience=CLASS&current_class=5...  ->  counts for the live preview box (fast)."""
    school = get_school(request)
    data = request.GET.copy()
    data["title"] = "preview"
    form = SmsComposeForm(data, school=school, for_preview=True)
    if not form.is_valid():
        return JsonResponse({"ok": False, "errors": form.errors}, status=400)
    try:
        counts = count_recipients(form, school)
    except AudienceError as e:
        return JsonResponse({"ok": False, "errors": {"__all__": [str(e)]}}, status=400)
    return JsonResponse({"ok": True, **counts})


@require_GET
@login_required
@role_required(SMS_ROLES)
def sms_recipient_search(request):
    """
    Select2 search for the Students / Parents pickers.
    GET ?kind=students|parents&q=text  ->  {"results": [{"id": 1, "text": "..."}]}
    Returns at most 30 matches, so the page never loads the whole school.
    """
    school = get_school(request)
    kind = request.GET.get("kind")
    q = (request.GET.get("q") or "").strip()
    results = []

    if kind == "students":
        qs = active_students(school).select_related("current_class")
        if q:
            qs = qs.filter(
                Q(name__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q)
                | Q(admission_number__icontains=q)
            )
        for st in qs.order_by("first_name", "last_name")[:30]:
            cls = st.current_class.name if st.current_class else "No class"
            results.append({"id": st.pk, "text": f"{student_display_name(st)} — {cls} ({st.admission_number})"})

    elif kind == "parents":
        qs = Parent.objects.filter(school=school, is_active=True)
        if q:
            cond = Q(full_name__icontains=q) | Q(phone__icontains=q) | Q(alternate_phone__icontains=q)
            digits = "".join(ch for ch in q if ch.isdigit())
            if len(digits) >= 6:
                # phones are stored as +256...; let "0772 123..." still match
                cond |= (Q(phone__icontains=normalize_phone(q)) | Q(alternate_phone__icontains=normalize_phone(q))
                         | Q(phone__icontains=digits.lstrip("0")) | Q(alternate_phone__icontains=digits.lstrip("0")))
            qs = qs.filter(cond)
        for p in qs.order_by("first_name", "last_name")[:30]:
            results.append({"id": p.pk, "text": f"{p.full_name} ({p.phone})"})

    return JsonResponse({"results": results})


@login_required
def sms_campaign_detail(request, pk):
    school = get_school(request)
    campaign = annotate_counts(SmsCampaign.objects.filter(pk=pk, school=school)).first()
    if not campaign:
        raise Http404

    sms_messages_qs = campaign.messages.select_related("parent", "student", "student__current_class").order_by("id")
    total_units = campaign.messages.filter(channel="SMS").exclude(to_number="").aggregate(u=Sum("units"))["u"] or 0
    stale_cutoff = timezone.now() - STALE_SENDING_AFTER
    can_resume = campaign.messages.filter(
        Q(status="PENDING") | Q(status="SENDING", updated_at__lt=stale_cutoff)
    ).exclude(to_number="").exists()

    return render(request, "dashboard/communication/sms_campaign_detail.html", {
        "campaign": campaign,
        "sms_messages": sms_messages_qs,
        "total_units": total_units,
        "can_resume": can_resume,
    })


@require_POST            # a GET link or browser prefetch must never send SMS
@login_required
@role_required(SMS_ROLES)
def sms_resend_failed(request, pk):
    """
    Retries: FAILED/UNDELIVERED messages, PENDING ones that were never sent,
    and SENDING ones stuck for more than 10 minutes (the batch was interrupted).
    Skips numbers Africa's Talking said are invalid — they would fail again and cost money.
    """
    school = get_school(request)
    campaign = get_object_or_404(SmsCampaign, pk=pk, school=school)
    stale_cutoff = timezone.now() - STALE_SENDING_AFTER

    retry_qs = (
        campaign.messages
        .filter(
            Q(status__in=FAILED_STATUSES + ["PENDING"])
            | Q(status="SENDING", updated_at__lt=stale_cutoff)
        )
        .exclude(to_number="")
        .exclude(status_detail__icontains="InvalidPhoneNumber")
        .select_related("campaign__wa_template")
        .order_by("id")
    )

    senders = Senders()
    resent = still_failed = 0
    for msg in retry_qs:
        # claim the row so two clicks can't send it twice
        if not SmsMessage.objects.filter(id=msg.id, status=msg.status).update(status="SENDING"):
            continue
        if send_message(senders, msg, resend=True):
            resent += 1
        else:
            still_failed += 1

    if resent or still_failed:
        messages.success(request, f"Retry complete: {resent} sent, {still_failed} failed.")
    else:
        messages.info(request, "Nothing to retry.")
    return redirect("sms_campaign_detail", pk=campaign.pk)


@csrf_exempt
def africastalking_dlr(request):
    """
    Delivery report callback from Africa's Talking.
    Set the callback URL in the AT dashboard (SMS > Callback URLs > Delivery Reports), e.g.
        https://yourdomain/communication/sms/dlr/?token=<AFRICASTALKING_CALLBACK_TOKEN>
    AT statuses: Sent, Submitted, Buffered, Success (= delivered), Rejected, Failed,
                 AbsentSubscriber, Expired.
    """
    if request.method != "POST":
        return HttpResponseBadRequest("POST only")

    expected = getattr(settings, "AFRICASTALKING_CALLBACK_TOKEN", "")
    if expected and request.GET.get("token") != expected:
        return HttpResponseForbidden("bad token")

    data = request.POST.dict()
    message_id = data.get("id") or data.get("messageId") or ""
    raw_status = data.get("status") or ""
    status = raw_status.upper()

    if not message_id:
        return JsonResponse({"ok": True, "note": "no message id"})

    msg = SmsMessage.objects.filter(provider_message_id=message_id, channel="SMS").first()
    if msg:
        msg.raw_dlr = data
        if status in ("SUCCESS", "DELIVERED"):
            msg.status = "DELIVERED"
            msg.delivered_at = timezone.now()
        elif status in ("FAILED", "REJECTED", "ABSENTSUBSCRIBER", "EXPIRED"):
            msg.status = "UNDELIVERED"
        elif status in ("SENT", "SUBMITTED", "BUFFERED"):
            msg.status = "SENT"
        else:
            msg.status = "UNKNOWN"

        reason = data.get("failureReason") or ""
        msg.status_detail = (f"{raw_status} {reason}".strip() or msg.status_detail)[:255]
        msg.save(update_fields=["raw_dlr", "status", "delivered_at", "status_detail", "updated_at"])

    return JsonResponse({"ok": True})



# ---------------------------------------------------------------------------
# WhatsApp: webhook (delivery / read receipts) and templates
# ---------------------------------------------------------------------------
@csrf_exempt
def whatsapp_webhook(request):
    """
    Meta calls this URL.
      GET  = one-time check when you save the webhook in the Meta app (must echo hub.challenge)
      POST = status updates: sent, delivered, read, failed
    Set in Meta: App > WhatsApp > Configuration > Webhook
        Callback URL: https://<your-domain>/communication/whatsapp/webhook/
        Verify token: the same text as WHATSAPP_VERIFY_TOKEN
        Subscribe to the "messages" field.
    """
    if request.method == "GET":
        mode = request.GET.get("hub.mode")
        token = request.GET.get("hub.verify_token")
        challenge = request.GET.get("hub.challenge", "")
        expected = getattr(settings, "WHATSAPP_VERIFY_TOKEN", "")
        if mode == "subscribe" and expected and token == expected:
            return HttpResponse(challenge, content_type="text/plain")
        return HttpResponseForbidden("verification failed")

    if request.method != "POST":
        return HttpResponseBadRequest("GET or POST only")

    if not WhatsAppCloud.verify_signature(request.body, request.headers.get("X-Hub-Signature-256", "")):
        return HttpResponseForbidden("bad signature")

    try:
        payload = json.loads(request.body.decode("utf-8") or "{}")
    except ValueError:
        return HttpResponseBadRequest("invalid json")

    senders = None
    for entry in payload.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {}) or {}
            for st in value.get("statuses", []) or []:
                senders = _apply_whatsapp_status(st, senders)
            # value.get("messages") = replies from parents. Not stored yet.

    return JsonResponse({"ok": True})


def _apply_whatsapp_status(st, senders):
    wamid = st.get("id") or ""
    status = (st.get("status") or "").lower()
    if not wamid:
        return senders

    msg = (SmsMessage.objects
           .filter(provider_message_id=wamid, channel="WHATSAPP")
           .select_related("campaign")
           .first())
    if not msg:
        return senders

    msg.raw_dlr = st
    now = timezone.now()

    if status == "failed":
        errors = st.get("errors") or [{}]
        code = errors[0].get("code")
        title = errors[0].get("title") or errors[0].get("message") or "failed"
        reason = f"WhatsApp {code}: {title}"
        if code in NOT_ON_WHATSAPP_CODES:
            reason += " (number can't receive WhatsApp)"
        msg.save(update_fields=["raw_dlr", "updated_at"])
        senders = senders or Senders()
        # Sends the SMS version when the campaign is "WhatsApp, SMS if not on WhatsApp";
        # otherwise just marks the message as undelivered.
        fall_back_to_sms(senders, msg, reason)
        return senders

    new_status = {"sent": "SENT", "delivered": "DELIVERED", "read": "READ"}.get(status)
    if not new_status or STATUS_RANK.get(new_status, 0) < STATUS_RANK.get(msg.status, 0):
        msg.save(update_fields=["raw_dlr", "updated_at"])    # never go backwards (e.g. read -> delivered)
        return senders

    msg.status = new_status
    if new_status in ("DELIVERED", "READ") and not msg.delivered_at:
        msg.delivered_at = now
    if new_status == "READ":
        msg.read_at = now
    msg.status_detail = {"SENT": "Sent on WhatsApp", "DELIVERED": "Delivered on WhatsApp",
                         "READ": "Read on WhatsApp"}[new_status]
    msg.save(update_fields=["raw_dlr", "status", "delivered_at", "read_at", "status_detail", "updated_at"])
    return senders


@login_required
@role_required(SMS_ROLES)
def whatsapp_template_list(request):
    school = get_school(request)
    templates = WhatsAppTemplate.objects.filter(school=school).order_by("-is_active", "name")
    wa = WhatsAppCloud()
    return render(request, "dashboard/communication/whatsapp_template_list.html", {
        "templates": templates,
        "whatsapp_ready": wa.is_configured,
        "can_sync": bool(wa.token and wa.waba_id),
    })


@require_POST
@login_required
@role_required(SMS_ROLES)
def whatsapp_template_sync(request):
    """Copies the templates from WhatsApp Manager into this school's list."""
    school = get_school(request)
    try:
        remote = WhatsAppCloud().fetch_templates()
    except Exception as e:
        messages.error(request, f"Could not load templates from WhatsApp: {e}")
        return redirect("whatsapp_template_list")

    now = timezone.now()
    seen_ids = []
    created = updated = 0
    for t in remote:
        info = WhatsAppCloud.describe_template(t)
        if not info["name"]:
            continue
        obj, was_created = WhatsAppTemplate.objects.update_or_create(
            school=school, name=info["name"], language=info["language"],
            defaults={
                "category": info["category"],
                "status": info["status"],
                "body": info["body"],
                "param_format": info["param_format"],
                "param_names": info["param_names"],
                "is_supported": info["is_supported"],
                "is_active": True,
                "last_synced_at": now,
                "updated_by": request.user,
            },
        )
        if was_created:
            obj.created_by = request.user
            obj.save(update_fields=["created_by"])
            created += 1
        else:
            updated += 1
        seen_ids.append(obj.id)

    # Templates deleted in WhatsApp Manager can no longer be sent
    removed = (WhatsAppTemplate.objects.filter(school=school, is_active=True)
               .exclude(id__in=seen_ids).update(is_active=False))

    messages.success(request, f"Templates synced: {created} new, {updated} updated, {removed} no longer available.")
    return redirect("whatsapp_template_list")
